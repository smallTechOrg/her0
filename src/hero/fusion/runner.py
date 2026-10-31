"""run_fusion(run_id): the ordered step graph, the error handler, finalize and persistence (spec/agent.md).

Every step runs inside ``run_step``: it records ``started``, times the step, catches
``BudgetExceeded | UpstreamError | SchemaValidationError | Exception``, writes the ``StepRecord`` via
``repo.append_run_step``, logs ``pipeline.step`` and either continues (recoverable) or routes to ``fail`` which
persists every partial artefact. ``run_fusion`` never raises: every outcome is on the run row
(``succeeded | failed | failed_budget``).

* A failed run never replaces a published scene; if the segment has no scene yet, ``fail`` publishes the compiled
  v0 prior as a clearly-labelled low-fidelity scene (``about.degraded``, quality ``generic``).
* ``acquire`` runs and no-op runs end after ``select_unfused`` / ``acquire_imagery`` with a report; no model call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import RunUpdate, SegmentUpdate, StepRecord
from hero.fusion import publish as publish_mod
from hero.fusion.maxquality import Quality
from hero.fusion import steps as S
from hero.fusion.state import FusionState, PipelineError
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.llm.anthropic_client import AnthropicClient
from hero.llm.budget import BudgetGuard
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UpstreamError
from hero.llm.pricing import load_price_table
from hero.logging import get_logger
from hero.scene.schema import RunStatus, SegmentStatus

log = get_logger("hero.pipeline")


@dataclass(frozen=True)
class Step:
    name: str
    fn: S.StepFn
    recoverable: bool
    when: Callable[[FusionState], bool] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ run inputs (written when the run is queued)


def run_inputs(settings, segment: repo.SegmentRecord, *, mode: str, plan_overrides: dict[str, Any] | None,
               base_version: int | None, model: str | None = None,
               quality: dict[str, Any] | Quality | None = None) -> dict[str, Any]:
    """``model`` (already validated by ``hero.llm.models.resolve_model``) sets BOTH the vision and the review model of
    the run; None keeps the configured defaults. ``models.source`` records which."""
    from hero.imagery.plan import default_plan, merge_plan

    plan = merge_plan(default_plan(settings), dict(plan_overrides or {}))
    q = quality if isinstance(quality, Quality) else Quality.model_validate(quality or {})
    return {
        "quality": q.model_dump(),
        "mode": mode, "imagery_plan": plan.model_dump(mode="json"), "plan_overrides": dict(plan_overrides or {}),
        "base_version": base_version, "structured_mode": settings.structured_mode,
        "start": segment.start.model_dump(), "end": segment.end.model_dump(), "footpath_side": segment.footpath_side.value,
        "models": ({"vision": model, "review": model, "source": "request"} if model else
                   {"vision": settings.vision_model, "review": settings.review_model, "source": "default"}),
        "max_iterations": settings.max_review_iterations, "batch_images": settings.perception_batch_images,
        "budgets": {"total_usd": settings.claude_budget_usd},
    }


# ------------------------------------------------------------------ persistence


def persist(state: FusionState, **extra: Any) -> None:
    totals = repo.run_llm_totals(state.run_id)
    google = repo.google_counts(state.run_id)
    changes = RunUpdate(
        imagery=state.imagery_json(), iterations=[it.to_json() for it in state.critique_iterations],
        scene=state.scene, input_tokens=totals.input_tokens, output_tokens=totals.output_tokens,
        cost_usd=totals.cost_usd, google_requests=google.requests, google_cached_hits=google.cache_hits, **extra)
    repo.update_run(state.run_id, changes)


def _record_step(state: FusionState, record: StepRecord) -> None:
    for i, existing in enumerate(state.steps):
        if existing.name == record.name:
            state.steps[i] = record
            break
    else:
        state.steps.append(record)
    repo.append_run_step(state.run_id, record)


def _log_step(state: FusionState, name: str, status: str, latency_ms: int | None, detail: str | None) -> None:
    log.info("pipeline.step", run_id=state.run_id, segment_slug=state.segment.slug, step=name, status=status,
             latency_ms=latency_ms, detail=(detail or "")[:200])


# ------------------------------------------------------------------ the error handler


def run_step(state: FusionState, ctx: S.FusionContext, step: Step) -> bool:
    """Run one step; returns True when the pipeline may continue."""
    if step.when is not None and not step.when(state):
        _record_step(state, StepRecord(name=step.name, status="skipped", started_at=_now(), latency_ms=0,
                                       detail="skipped"))
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
            state.degraded = True
            persist(state)
            return True
        code = "upstream_error" if isinstance(exc, (UpstreamError, SchemaValidationError)) else "internal_error"
        detail = getattr(exc, "detail", None) or {}
        state.error = PipelineError(code=code, message=message, step=step.name, detail=dict(detail))
        log.error("pipeline.error", run_id=state.run_id, step=step.name, error=repr(exc)[:300])
        return False
    latency_ms = max(1, int((time.perf_counter() - clock) * 1000))
    _record_step(state, StepRecord(name=step.name, status="ok", started_at=started_at, latency_ms=latency_ms,
                                   detail=(detail or "")[:500]))
    _log_step(state, step.name, "ok", latency_ms, detail)
    persist(state)
    return True


# ------------------------------------------------------------------ outcomes


def _restore_segment_status(state: FusionState) -> None:
    previous = SegmentStatus(state.previous_segment_status)
    if previous is SegmentStatus.generating:
        previous = SegmentStatus.draft
    repo.update_segment(state.segment.id, SegmentUpdate(status=previous))


def _publish_prior_if_unpublished(state: FusionState, ctx: S.FusionContext) -> None:
    """A generation that produced nothing still leaves a descriptive, labelled low-fidelity street, never a blank."""
    seg = repo.get_segment_by_id(state.segment.id)
    if seg is None or seg.scene is not None or state.base is None or state.base.fused_observation_ids:
        return
    scene = publish_mod.prior_fallback_scene(state.base, state.observations, segment_name=seg.name, city=seg.city,
                                             warnings=["degraded: the generation failed; this is the map-only prior"])
    from hero.scene.schema import Quality

    repo.finalize_segment_scene(seg.id, run_id=state.run_id, scene=scene, quality=Quality(scene.generation.quality.value),
                                thumbnail_path=None)
    log.info("fusion.prior_published", run_id=state.run_id, segment_slug=seg.slug)


def fail(state: FusionState, ctx: S.FusionContext | None = None) -> repo.PipelineRunRecord:
    error = state.error or PipelineError(code="internal_error", message="Pipeline stopped without an error record.",
                                         step="unknown")
    status = RunStatus.failed_budget if error.code == "budget_exhausted" else RunStatus.failed
    state.degraded = True
    if ctx is not None:
        try:
            state.warn(f"Run stopped at {error.step}: {error.message[:200]}")
            rep = S.build_report(state, ctx, failed=True)
            if rep is not None:
                from hero.db import model_repo

                model_repo.save_run_report(state.run_id, state.segment.id, rep)
        except Exception as exc:  # noqa: BLE001 - a missing report must not hide the real failure
            log.error("fusion.report_failed", run_id=state.run_id, error=repr(exc)[:200])
        try:
            _publish_prior_if_unpublished(state, ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("fusion.prior_failed", run_id=state.run_id, error=repr(exc)[:200])
    persist(state, status=status, error=error.to_json(), finished_at=_now())
    seg = repo.get_segment_by_id(state.segment.id)
    if seg is not None and seg.scene is not None:
        repo.update_segment(seg.id, SegmentUpdate(status=SegmentStatus.ready))
    else:
        repo.update_segment(state.segment.id, SegmentUpdate(status=SegmentStatus.failed, latest_run_id=state.run_id))
    log.warning("pipeline.finished", run_id=state.run_id, status=status.value, step=error.step, code=error.code,
                cost_usd=repo.run_spend_usd(state.run_id))
    return repo.require_run(state.run_id)


def succeed(state: FusionState) -> repo.PipelineRunRecord:
    persist(state, status=RunStatus.succeeded, error=None, finished_at=_now())
    if not state.version_created:
        _restore_segment_status(state)
    run = repo.require_run(state.run_id)
    log.info("pipeline.finished", run_id=state.run_id, status="succeeded", mode=state.mode,
             version_created=state.version_created, version=state.version, cost_usd=run.cost_usd,
             google_requests=run.google_requests, google_cached_hits=run.google_cached_hits,
             warnings=len(state.warnings))
    return run


# ------------------------------------------------------------------ entry point


def build_context(run_id: str, settings=None, *, llm=None, google=None, http: httpx.Client | None = None,
                  models: dict[str, str] | None = None) -> S.FusionContext:
    settings = settings or get_settings()
    prices = load_price_table(settings)
    prices.require((models or {}).get("vision") or settings.vision_model)
    prices.require((models or {}).get("review") or settings.review_model)
    guard = BudgetGuard(settings)
    data_dir = settings.data_dir_abs
    return S.FusionContext(
        settings=settings, run_id=run_id, llm=llm or AnthropicClient(settings, guard, prices),
        google=google or GoogleImagery(settings, ImageryCache(data_dir / "imagery"), run_id), guard=guard,
        prices=prices, osm_cache=ImageryCache(data_dir / "osm"), data_dir=data_dir, http=http)


def run_fusion(run_id: str, *, llm=None, google=None, http: httpx.Client | None = None) -> repo.PipelineRunRecord:
    """Execute the whole graph for ``run_id`` (a ``queued`` run). Never raises."""
    settings = get_settings()
    run = repo.require_run(run_id)
    segment = repo.get_segment_by_id(run.segment_id)
    assert segment is not None
    inputs = run.inputs or {}
    state = FusionState(
        run=run, segment=segment, mode=str(inputs.get("mode", "generate")), settings_snapshot=dict(inputs),
        plan_overrides=dict(inputs.get("plan_overrides") or {}), started_at=_now(),
        models={k: str(v) for k, v in dict(inputs.get("models") or {}).items() if k in ("vision", "review") and v},
        previous_segment_status=segment.status.value, quality=Quality.from_inputs(inputs.get("quality")))
    repo.update_run(run_id, RunUpdate(status=RunStatus.running, started_at=state.started_at))
    if segment.status is not SegmentStatus.ready:  # a ready street stays playable while it is regenerated
        repo.update_segment(segment.id, SegmentUpdate(status=SegmentStatus.generating))
    try:
        ctx = build_context(run_id, settings, llm=llm, google=google, http=http, models=state.models)
    except Exception as exc:  # noqa: BLE001 - config errors are reported on the run, never raised
        state.error = PipelineError(code="upstream_error", message=str(exc), step="build_context")
        return fail(state)
    try:
        return _execute(state, ctx)
    except Exception as exc:  # noqa: BLE001 - last line of defence: the run row must always end up final
        log.error("pipeline.crashed", run_id=run_id, error=repr(exc)[:300])
        state.error = PipelineError(code="internal_error", message=f"Pipeline crashed: {str(exc)[:300]}", step="runner")
        return fail(state, ctx)


def _execute(state: FusionState, ctx: S.FusionContext) -> repo.PipelineRunRecord:
    settings = ctx.settings
    head = [
        Step("load_base_model", S.load_base_model, recoverable=False),
        Step("resolve_reference_line", S.resolve_reference_line, recoverable=True),
        Step("fetch_osm_context", S.fetch_osm_context, recoverable=True),
        Step("plan_imagery", S.plan_imagery, recoverable=False),
        Step("acquire_imagery", S.acquire_imagery, recoverable=False),
    ]
    for step in head:
        if not run_step(state, ctx, step):
            return fail(state, ctx)
        if state.base is None and step.name == "fetch_osm_context":
            state.error = PipelineError(code="internal_error", step=step.name,
                                        message="No base model could be established for this street.")
            return fail(state, ctx)
    if state.mode == "acquire":
        return _finish(state, ctx)
    if not run_step(state, ctx, Step("select_unfused", S.select_unfused, recoverable=False)):
        return fail(state, ctx)
    if state.noop:
        return _finish(state, ctx)
    for step in [
        Step("prepare_views", S.prepare_views, recoverable=False),
        Step("perceive_overhead", S.perceive_overhead, recoverable=True, when=lambda s: s.has_overhead),
        Step("perceive_streetlevel", S.perceive_streetlevel, recoverable=True, when=lambda s: s.has_street),
        Step("project_detections", S.project_detections, recoverable=False),
        Step("merge", S.merge, recoverable=False),
        Step("consistency_check", S.consistency_check, recoverable=True),
    ]:
        if not run_step(state, ctx, step):
            return fail(state, ctx)
    rounds = state.quality.rounds(settings.max_review_iterations)
    for n in range(1, max(0, rounds) + 1):
        before = len(state.critique_iterations)
        if not run_step(state, ctx, Step(f"critique_{n}", S.critique_once(n), recoverable=True)):
            return fail(state, ctx)
        if len(state.critique_iterations) == before:
            break
        last = state.critique_iterations[-1]
        if last.accepted:
            break
        if (state.quality.preset or state.quality.review_rounds is not None) and last.applied <= state.quality.review_stop_changes and n < rounds:
            state.review_stop = (f"early stop after round {n}: the review applied {last.applied} change(s) "
                                 f"(<= {state.quality.review_stop_changes}), so the model has converged")
            break
    if not run_step(state, ctx, Step("publish", S.publish, recoverable=False)):
        return fail(state, ctx)
    return _finish(state, ctx)


def _finish(state: FusionState, ctx: S.FusionContext) -> repo.PipelineRunRecord:
    """The ``report`` step, then the run row goes ``succeeded`` (a failing report is logged, never fatal)."""
    run_step(state, ctx, Step("report", S.report, recoverable=True))
    return succeed(state)
