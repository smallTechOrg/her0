"""run_pipeline(run_id): the ordered step graph, the error handler, finalize and persistence.

Every step runs inside ``run_step``: it records ``started``, times the step, catches
``UpstreamError | SchemaValidationError | BudgetExceeded | Exception``, writes the StepRecord via
``repo.append_run_step``, logs ``pipeline.step`` and either continues (recoverable) or routes to
``fail`` which persists every partial artefact so the admin can inspect what happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from pydantic import ValidationError

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import RunUpdate, SegmentUpdate, StepRecord
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.llm.anthropic_client import AnthropicClient
from hero.llm.budget import BudgetGuard
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UpstreamError
from hero.llm.pricing import load_price_table
from hero.logging import get_logger
from hero.pipeline import steps as S
from hero.pipeline.state import PipelineError, PipelineState
from hero.pipeline.steps import PipelineContext
from hero.scene.geo import LocalFrame
from hero.scene.schema import Quality, RunStatus, SegmentStatus

log = get_logger("hero.pipeline")


@dataclass(frozen=True)
class Step:
    name: str
    fn: S.StepFn
    recoverable: bool
    when: Callable[[PipelineState], bool] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def settings_snapshot(settings, segment: repo.SegmentRecord) -> dict[str, Any]:
    return {
        "start": segment.start.model_dump(), "end": segment.end.model_dump(), "footpath_side": segment.footpath_side.value,
        "models": {"vision": settings.vision_model, "review": settings.review_model},
        "zoom": settings.satellite_zoom, "samples": settings.streetview_samples,
        "max_iterations": settings.max_review_iterations,
        "budgets": {"total_usd": settings.claude_budget_usd},
    }


# ------------------------------------------------------------------ persistence


def persist(state: PipelineState, **extra: Any) -> None:
    totals = repo.run_llm_totals(state.run_id)
    google = repo.google_counts(state.run_id)
    changes = RunUpdate(
        imagery=state.imagery_json(),
        layout=state.layout.model_dump(mode="json") if state.layout else None,
        obstacles=state.obstacles.model_dump(mode="json") if state.obstacles else None,
        iterations=[it.to_json() for it in state.iterations],
        scene=state.scene,
        input_tokens=totals.input_tokens, output_tokens=totals.output_tokens, cost_usd=totals.cost_usd,
        google_requests=google.requests, google_cached_hits=google.cache_hits,
        **extra,
    )
    repo.update_run(state.run_id, changes)


def _record_step(state: PipelineState, record: StepRecord) -> None:
    for i, existing in enumerate(state.steps):
        if existing.name == record.name:
            state.steps[i] = record
            break
    else:
        state.steps.append(record)
    repo.append_run_step(state.run_id, record)


def _log_step(state: PipelineState, name: str, status: str, latency_ms: int | None, detail: str | None) -> None:
    log.info("pipeline.step", run_id=state.run_id, segment_slug=state.segment.slug, step=name, status=status,
             latency_ms=latency_ms, detail=(detail or "")[:200])


# ------------------------------------------------------------------ the error handler


def run_step(state: PipelineState, ctx: PipelineContext, step: Step) -> bool:
    """Run one step; returns True when the pipeline may continue."""
    if step.when is not None and not step.when(state):
        record = StepRecord(name=step.name, status="skipped", started_at=_now(), latency_ms=0, detail="skipped")
        _record_step(state, record)
        _log_step(state, step.name, "skipped", 0, "skipped")
        return True
    started_at = _now()
    _record_step(state, StepRecord(name=step.name, status="started", started_at=started_at))
    _log_step(state, step.name, "started", None, None)
    clock = time.perf_counter()
    try:
        detail = step.fn(state, ctx)
    except BudgetExceeded as exc:
        latency_ms = max(1, int((time.perf_counter() - clock) * 1000))
        _record_step(state, StepRecord(name=step.name, status="failed", started_at=started_at, latency_ms=latency_ms,
                                       detail=exc.message[:500]))
        _log_step(state, step.name, "failed", latency_ms, exc.message)
        state.error = PipelineError(code="budget_exhausted", message=exc.message, step=step.name, detail=exc.as_detail())
        return False
    except (UpstreamError, SchemaValidationError, ValidationError, Exception) as exc:  # noqa: BLE001 - the handler
        latency_ms = max(1, int((time.perf_counter() - clock) * 1000))
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        _record_step(state, StepRecord(name=step.name, status="failed", started_at=started_at, latency_ms=latency_ms,
                                       detail=message[:500]))
        _log_step(state, step.name, "failed", latency_ms, message)
        if step.recoverable:
            state.warn(f"Step {step.name} failed and was skipped: {message[:160]}")
            persist(state)
            return True
        code = "upstream_error" if isinstance(exc, (UpstreamError, SchemaValidationError)) else "internal_error"
        detail = getattr(exc, "detail", None) or {}
        state.error = PipelineError(code=code, message=message, step=step.name, detail=dict(detail))
        log.error("pipeline.error", run_id=state.run_id, step=step.name, error=repr(exc)[:300])
        return False
    latency_ms = max(1, int((time.perf_counter() - clock) * 1000))
    _record_step(state, StepRecord(name=step.name, status="ok", started_at=started_at, latency_ms=latency_ms,
                                   detail=detail[:500]))
    _log_step(state, step.name, "ok", latency_ms, detail)
    persist(state)
    return True


def fail(state: PipelineState) -> repo.PipelineRunRecord:
    error = state.error or PipelineError(code="internal_error", message="Pipeline stopped without an error record.",
                                         step="unknown")
    status = RunStatus.failed_budget if error.code == "budget_exhausted" else RunStatus.failed
    persist(state, status=status, error=error.to_json(), finished_at=_now())
    if state.previous_segment_status != SegmentStatus.ready.value:
        # no successful scene yet: the failed run is the one to inspect
        repo.update_segment(state.segment.id, SegmentUpdate(status=SegmentStatus.failed, latest_run_id=state.run_id))
    else:
        # the previous scene stays live and latest_run_id keeps pointing at the run that produced it
        repo.update_segment(state.segment.id, SegmentUpdate(status=SegmentStatus.ready))
    log.warning("pipeline.finished", run_id=state.run_id, status=status.value, step=error.step, code=error.code,
                cost_usd=state.cost.cost_usd)
    return repo.require_run(state.run_id)


def succeed(state: PipelineState, ctx: PipelineContext) -> repo.PipelineRunRecord:
    assert state.scene is not None
    quality = Quality(state.scene.generation.quality)
    repo.finalize_segment_scene(state.segment.id, run_id=state.run_id, scene=state.scene, quality=quality,
                                thumbnail_path=S.thumbnail_rel_path(ctx))
    persist(state, status=RunStatus.succeeded, error=None, finished_at=_now())
    run = repo.require_run(state.run_id)
    log.info("pipeline.finished", run_id=state.run_id, status="succeeded", cost_usd=run.cost_usd,
             google_requests=run.google_requests, google_cached_hits=run.google_cached_hits,
             objects=len(state.scene.objects), object_types=S.object_type_summary(state.scene),
             tokens=len(state.scene.tokens), walkability=state.scene.walkability.score,
             warnings=state.scene.generation.warnings, quality=quality.value)
    return run


# ------------------------------------------------------------------ entry point


def build_context(run_id: str, settings=None) -> PipelineContext:
    settings = settings or get_settings()
    prices = load_price_table(settings)
    prices.require(settings.vision_model)
    prices.require(settings.review_model)
    guard = BudgetGuard(settings)
    data_dir = settings.data_dir_abs
    return PipelineContext(
        settings=settings, run_id=run_id, llm=AnthropicClient(settings, guard, prices),
        google=GoogleImagery(settings, ImageryCache(data_dir / "imagery"), run_id),
        osm_cache=ImageryCache(data_dir / "osm"), data_dir=data_dir,
    )


def run_pipeline(run_id: str, *, llm=None) -> repo.PipelineRunRecord:
    """Execute the whole graph for ``run_id`` (a ``queued`` run). Never raises: every outcome is
    persisted on the run row (``succeeded`` | ``failed`` | ``failed_budget``)."""
    settings = get_settings()
    run = repo.require_run(run_id)
    segment = repo.get_segment_by_id(run.segment_id)
    assert segment is not None
    frame = LocalFrame.from_endpoints(segment.start.lat, segment.start.lon, segment.end.lat, segment.end.lon,
                                      segment.footpath_side)
    state = PipelineState(run=run, segment=segment, settings_snapshot=settings_snapshot(settings, segment), frame=frame,
                          started_at=_now(), previous_segment_status=segment.status.value)
    repo.update_run(run_id, RunUpdate(status=RunStatus.running, started_at=state.started_at))
    repo.update_segment(segment.id, SegmentUpdate(status=SegmentStatus.generating))
    try:
        ctx = build_context(run_id, settings)
        if llm is not None:
            ctx.llm = llm
    except Exception as exc:  # noqa: BLE001 - config errors are reported on the run, never raised
        state.error = PipelineError(code="upstream_error", message=str(exc), step="build_context")
        return fail(state)

    graph = [
        Step("resolve_geometry", S.resolve_geometry, recoverable=False),
        Step("fetch_satellite", S.fetch_satellite, recoverable=False),
        Step("verify_streetview", S.verify_streetview, recoverable=True),
        Step("fetch_streetview", S.fetch_streetview, recoverable=True, when=lambda s: bool(s.streetview_available)),
        Step("detect_layout", S.detect_layout, recoverable=False),
        Step("detect_obstacles", S.detect_obstacles, recoverable=True, when=lambda s: bool(s.streetview)),
        Step("assemble_scene", S.assemble, recoverable=False),
    ]
    for step in graph:
        if not run_step(state, ctx, step):
            return fail(state)
    for n in range(1, max(0, settings.max_review_iterations) + 1):
        before = len(state.iterations)
        ok = run_step(state, ctx, Step(f"review_iteration_{n}", S.review_once(n), recoverable=True))
        if not ok:
            return fail(state)
        if len(state.iterations) == before:  # the review call failed (recoverable): keep the current scene
            state.warn(f"Review stopped at iteration {n}; the current scene is kept.")
            break
        if state.iterations[-1].accepted:
            break
    if not run_step(state, ctx, Step("finalize", S.finalize, recoverable=False)):
        return fail(state)
    return succeed(state, ctx)
