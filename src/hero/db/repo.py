"""Typed persistence API over the Phase 1 entities (spec/data.md); scenes parse as Scene v1 | SceneV2.

Phase-2 tables (observations, model versions, extractions, run reports) live in ``hero.db.model_repo``.

Every function opens its own session via ``hero.db.session.get_session`` unless a
``session`` is passed (compose several writes in one transaction). Inputs and outputs are
Pydantic models; SQLAlchemy rows never leave this module. Datetimes returned are
timezone-aware UTC. Errors: ``NotFoundError`` (map to 404), ``InvalidInputError`` (422).

Function index
--------------
segments      create_segment, get_segment, get_segment_by_id, require_segment, list_segments,
              list_ready_segments, update_segment, finalize_segment_scene
pipeline runs create_run, get_run, require_run, update_run, append_run_step, get_active_run,
              list_runs, list_runs_for_segment, latest_run_for_segment, run_stats
llm calls     record_llm_call, list_llm_calls, run_llm_totals, run_spend_usd, total_llm_cost_usd,
              spend_summary
google        record_google_request, list_google_requests, google_counts
game runs     validate_game_run, record_game_run, best_game_run, list_top_game_runs, count_game_runs
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from hero.db import models
from hero.db.session import get_session
from hero.model.scene_v2 import SceneV2, load_scene
from hero.scene.geo import LocalFrame
from hero.scene.schema import FootpathSide, LatLon, Quality, RunStatus, Scene, SegmentStatus

MIN_SEGMENT_LENGTH_M = 30.0
MAX_SEGMENT_LENGTH_M = 200.0
MAX_RUN_DISTANCE_M = 5000.0
TOKEN_SCORE = 50
SUMMARY_MAX_CHARS = 500
CLIENT_MAX_CHARS = 120

GoogleKind = Literal["satellite", "streetview", "streetview_metadata"]
ACTIVE_RUN_STATUSES = (RunStatus.queued.value, RunStatus.running.value)


# ------------------------------------------------------------------ errors


class RepoError(Exception):
    """Base class for persistence-layer errors with a human-readable message."""


class NotFoundError(RepoError):
    pass


class InvalidInputError(RepoError):
    pass


# ------------------------------------------------------------------ records (outputs)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class SegmentRecord(_Record):
    id: str
    slug: str
    name: str
    city: str
    start: LatLon
    end: LatLon
    footpath_side: FootpathSide
    length_m: float
    heading_deg: float
    status: SegmentStatus
    quality: Quality | None
    scene: Scene | SceneV2 | None
    thumbnail_path: str | None
    latest_run_id: str | None
    best_score: int | None
    best_distance_m: float | None
    created_at: datetime
    updated_at: datetime

    @property
    def midpoint(self) -> LatLon:
        return LocalFrame.from_endpoints(self.start.lat, self.start.lon, self.end.lat, self.end.lon,
                                         self.footpath_side).midpoint()


class StepRecord(BaseModel):
    """One entry of ``pipeline_runs.steps_json``; ``append_run_step`` upserts by ``name``."""

    name: str
    status: Literal["started", "ok", "skipped", "failed"]
    started_at: datetime
    latency_ms: int | None = None
    detail: str | None = None


class PipelineRunRecord(_Record):
    id: str
    segment_id: str
    status: RunStatus
    inputs: dict[str, Any]
    steps: list[StepRecord]
    imagery: dict[str, Any]
    layout: dict[str, Any] | None
    obstacles: dict[str, Any] | None
    iterations: list[dict[str, Any]]
    scene: Scene | SceneV2 | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    google_requests: int
    google_cached_hits: int
    error: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status in (RunStatus.queued, RunStatus.running)


class LlmCallRecord(_Record):
    id: str
    run_id: str
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    ok: bool
    error: str | None
    request_summary: str
    response_summary: str
    created_at: datetime


class GoogleRequestRecord(_Record):
    id: str
    run_id: str | None
    kind: GoogleKind
    cache_key: str
    cache_hit: bool
    est_cost_usd: float
    path: str | None
    created_at: datetime


class GameRunRecord(_Record):
    id: str
    segment_id: str
    score: int
    distance_m: float
    tokens_collected: int
    loops: int
    crashed_into_type: str | None
    crashed_into_id: str | None
    duration_s: float
    client: str
    created_at: datetime


class RunCostSummary(_Record):
    """Sums over ``llm_calls`` for one run plus the per-call rows (admin run view ``cost``)."""

    input_tokens: int
    output_tokens: int
    cost_usd: float
    calls: list[LlmCallRecord]


class GoogleCounts(_Record):
    """``requests`` = real Google calls (cache misses); ``cache_hits`` = served from disk."""

    requests: int
    cache_hits: int
    est_cost_usd: float

    @property
    def attempts(self) -> int:
        return self.requests + self.cache_hits


class RunStats(_Record):
    total: int
    succeeded: int
    failed: int
    failed_budget: int


class SpendSummary(_Record):
    budget_usd: float
    spent_usd: float
    remaining_usd: float


class GameRunResult(_Record):
    run: GameRunRecord
    best_score: int
    best_distance_m: float
    is_new_best: bool


# ------------------------------------------------------------------ inputs


class SegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    start: LatLon
    end: LatLon
    footpath_side: FootpathSide
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")


class SegmentUpdate(BaseModel):
    """Partial update; ``None`` means "leave unchanged" except where ``clear_*`` flags say otherwise."""

    status: SegmentStatus | None = None
    quality: Quality | None = None
    scene: Scene | SceneV2 | None = None
    thumbnail_path: str | None = None
    latest_run_id: str | None = None
    best_score: int | None = None
    best_distance_m: float | None = None


class RunUpdate(BaseModel):
    status: RunStatus | None = None
    steps: list[StepRecord] | None = None
    imagery: dict[str, Any] | None = None
    layout: dict[str, Any] | None = None
    obstacles: dict[str, Any] | None = None
    iterations: list[dict[str, Any]] | None = None
    scene: Scene | SceneV2 | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    google_requests: int | None = None
    google_cached_hits: int | None = None
    error: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class LlmCallCreate(BaseModel):
    run_id: str
    step: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    ok: bool
    error: str | None = None
    request_summary: str = ""
    response_summary: str = ""
    # Phase 2: logged on ``llm.call`` by the caller; deliberately NOT a column (it belongs in request_summary).
    structured_mode: str | None = None


class GoogleRequestCreate(BaseModel):
    run_id: str | None
    kind: GoogleKind
    cache_key: str
    cache_hit: bool
    est_cost_usd: float = Field(ge=0)
    path: str | None = None


class GameRunCreate(BaseModel):
    """Body of ``POST /api/segments/{slug}/runs`` (spec/api.md)."""

    score: int = Field(ge=0)
    distance_m: float = Field(ge=0)
    tokens_collected: int = Field(ge=0)
    loops: int = Field(ge=0)
    crashed_into_type: str | None = Field(default=None, max_length=32)
    crashed_into_id: str | None = Field(default=None, max_length=64)
    duration_s: float = Field(ge=0)


# ------------------------------------------------------------------ helpers


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
        session.flush()
        return
    with get_session() as new_session:
        yield new_session


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def _scene_or_none(data: dict | None) -> Scene | SceneV2 | None:
    """Parse a stored scene, discriminated by ``schema_version`` (1 -> Scene, 2 -> SceneV2)."""
    return load_scene(data) if data else None


def _segment_record(row: models.Segment) -> SegmentRecord:
    return SegmentRecord(
        id=row.id, slug=row.slug, name=row.name, city=row.city,
        start=LatLon(lat=row.start_lat, lon=row.start_lon), end=LatLon(lat=row.end_lat, lon=row.end_lon),
        footpath_side=FootpathSide(row.footpath_side), length_m=row.length_m, heading_deg=row.heading_deg,
        status=SegmentStatus(row.status), quality=Quality(row.quality) if row.quality else None,
        scene=_scene_or_none(row.scene_json), thumbnail_path=row.thumbnail_path,
        latest_run_id=row.latest_run_id, best_score=row.best_score, best_distance_m=row.best_distance_m,
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
    )


def _run_record(row: models.PipelineRun) -> PipelineRunRecord:
    return PipelineRunRecord(
        id=row.id, segment_id=row.segment_id, status=RunStatus(row.status),
        inputs=row.inputs_json or {}, steps=[StepRecord.model_validate(s) for s in (row.steps_json or [])],
        imagery=row.imagery_json or {}, layout=row.layout_json, obstacles=row.obstacles_json,
        iterations=list(row.iterations_json or []), scene=_scene_or_none(row.scene_json),
        input_tokens=row.input_tokens, output_tokens=row.output_tokens, cost_usd=float(row.cost_usd),
        google_requests=row.google_requests, google_cached_hits=row.google_cached_hits,
        error=row.error_json, started_at=_aware(row.started_at), finished_at=_aware(row.finished_at),
        created_at=_aware(row.created_at),
    )


def _llm_record(row: models.LlmCall) -> LlmCallRecord:
    return LlmCallRecord(
        id=row.id, run_id=row.run_id, step=row.step, model=row.model, input_tokens=row.input_tokens,
        output_tokens=row.output_tokens, cost_usd=float(row.cost_usd), latency_ms=row.latency_ms, ok=row.ok,
        error=row.error, request_summary=row.request_summary, response_summary=row.response_summary,
        created_at=_aware(row.created_at),
    )


def _google_record(row: models.GoogleRequest) -> GoogleRequestRecord:
    return GoogleRequestRecord(
        id=row.id, run_id=row.run_id, kind=row.kind, cache_key=row.cache_key, cache_hit=row.cache_hit,
        est_cost_usd=float(row.est_cost_usd), path=row.path, created_at=_aware(row.created_at),
    )


def _game_record(row: models.GameRun) -> GameRunRecord:
    return GameRunRecord(
        id=row.id, segment_id=row.segment_id, score=row.score, distance_m=row.distance_m,
        tokens_collected=row.tokens_collected, loops=row.loops, crashed_into_type=row.crashed_into_type,
        crashed_into_id=row.crashed_into_id, duration_s=row.duration_s, client=row.client,
        created_at=_aware(row.created_at),
    )


_SLUG_STRIP = re.compile(r"\([^)]*\)")
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    base = _SLUG_STRIP.sub("", name).lower()
    return _SLUG_NON_ALNUM.sub("-", base).strip("-") or "segment"


def _compass_suffix(heading_deg: float, side: FootpathSide) -> str:
    """Letter for the compass direction the footpath lies in (left of a north-bound segment -> ``w``)."""
    side_bearing = (heading_deg - 90.0) if side is FootpathSide.left else (heading_deg + 90.0)
    index = int(((side_bearing % 360.0) + 45.0) // 90.0) % 4
    return "nesw"[index]


def _unique_slug(session: Session, base: str) -> str:
    existing = set(session.scalars(select(models.Segment.slug).where(models.Segment.slug.like(base + "%"))))
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _get_segment_row(session: Session, *, slug: str | None = None, segment_id: str | None = None) -> models.Segment:
    stmt = select(models.Segment)
    stmt = stmt.where(models.Segment.slug == slug) if slug is not None else stmt.where(models.Segment.id == segment_id)
    row = session.scalars(stmt).first()
    if row is None:
        raise NotFoundError(f"Unknown segment {slug or segment_id!r}.")
    return row


def _get_run_row(session: Session, run_id: str) -> models.PipelineRun:
    row = session.get(models.PipelineRun, run_id)
    if row is None:
        raise NotFoundError(f"Unknown run {run_id!r}.")
    return row


# ------------------------------------------------------------------ segments


def create_segment(data: SegmentCreate, *, session: Session | None = None) -> SegmentRecord:
    """Create a ``draft`` segment; derives length/heading, validates 30-200 m, generates a unique slug."""
    frame = LocalFrame.from_endpoints(data.start.lat, data.start.lon, data.end.lat, data.end.lon, data.footpath_side)
    if not MIN_SEGMENT_LENGTH_M <= frame.length_m <= MAX_SEGMENT_LENGTH_M:
        raise InvalidInputError(
            f"Segment length must be between {MIN_SEGMENT_LENGTH_M:.0f} and {MAX_SEGMENT_LENGTH_M:.0f} m "
            f"(these endpoints are {frame.length_m:.1f} m apart)."
        )
    with _session(session) as s:
        base_slug = data.slug or f"{slugify(data.name)}-{_compass_suffix(frame.heading_deg, data.footpath_side)}"
        row = models.Segment(
            slug=_unique_slug(s, base_slug), name=data.name, city=data.city,
            start_lat=data.start.lat, start_lon=data.start.lon, end_lat=data.end.lat, end_lon=data.end.lon,
            footpath_side=data.footpath_side.value, length_m=frame.length_m, heading_deg=frame.heading_deg,
            status=SegmentStatus.draft.value,
        )
        s.add(row)
        s.flush()
        return _segment_record(row)


def get_segment(slug: str, *, session: Session | None = None) -> SegmentRecord | None:
    with _session(session) as s:
        row = s.scalars(select(models.Segment).where(models.Segment.slug == slug)).first()
        return _segment_record(row) if row else None


def get_segment_by_id(segment_id: str, *, session: Session | None = None) -> SegmentRecord | None:
    with _session(session) as s:
        row = s.get(models.Segment, segment_id)
        return _segment_record(row) if row else None


def require_segment(slug: str, *, session: Session | None = None) -> SegmentRecord:
    with _session(session) as s:
        return _segment_record(_get_segment_row(s, slug=slug))


def list_segments(status: SegmentStatus | None = None, *, session: Session | None = None) -> list[SegmentRecord]:
    """All segments (optionally filtered by status), newest first."""
    with _session(session) as s:
        stmt = select(models.Segment).order_by(models.Segment.created_at.desc(), models.Segment.id.desc())
        if status is not None:
            stmt = stmt.where(models.Segment.status == status.value)
        return [_segment_record(r) for r in s.scalars(stmt)]


def list_ready_segments(*, session: Session | None = None) -> list[SegmentRecord]:
    return list_segments(SegmentStatus.ready, session=session)


def update_segment(segment_id: str, changes: SegmentUpdate, *, session: Session | None = None) -> SegmentRecord:
    with _session(session) as s:
        row = _get_segment_row(s, segment_id=segment_id)
        for name, value in changes.model_dump(exclude_unset=True).items():
            if name == "scene":
                row.scene_json = changes.scene.model_dump(mode="json") if changes.scene else None
            elif name in ("status", "quality") and value is not None:
                setattr(row, name, value.value if hasattr(value, "value") else value)
            else:
                setattr(row, name, value)
        row.updated_at = models.utcnow()
        s.flush()
        return _segment_record(row)


def finalize_segment_scene(segment_id: str, *, run_id: str, scene: Scene | SceneV2, quality: Quality,
                           thumbnail_path: str | None, session: Session | None = None) -> SegmentRecord:
    """Successful run: replace the scene, set ``ready``, quality, thumbnail and latest run."""
    return update_segment(
        segment_id,
        SegmentUpdate(status=SegmentStatus.ready, quality=quality, scene=scene, thumbnail_path=thumbnail_path,
                      latest_run_id=run_id),
        session=session,
    )


# ------------------------------------------------------------------ pipeline runs


def create_run(segment_id: str, inputs: dict[str, Any], *, session: Session | None = None) -> PipelineRunRecord:
    with _session(session) as s:
        _get_segment_row(s, segment_id=segment_id)
        row = models.PipelineRun(segment_id=segment_id, status=RunStatus.queued.value, inputs_json=inputs,
                                 steps_json=[], imagery_json={}, iterations_json=[])
        s.add(row)
        s.flush()
        return _run_record(row)


def get_run(run_id: str, *, session: Session | None = None) -> PipelineRunRecord | None:
    with _session(session) as s:
        row = s.get(models.PipelineRun, run_id)
        return _run_record(row) if row else None


def require_run(run_id: str, *, session: Session | None = None) -> PipelineRunRecord:
    with _session(session) as s:
        return _run_record(_get_run_row(s, run_id))


def update_run(run_id: str, changes: RunUpdate, *, session: Session | None = None) -> PipelineRunRecord:
    with _session(session) as s:
        row = _get_run_row(s, run_id)
        for name, value in changes.model_dump(exclude_unset=True, mode="json").items():
            if name == "status":
                row.status = RunStatus(value).value
            elif name == "steps":
                row.steps_json = value
            elif name in ("imagery", "layout", "obstacles", "iterations", "scene", "error"):
                setattr(row, f"{name}_json", value)
            elif name in ("started_at", "finished_at"):
                setattr(row, name, _naive(getattr(changes, name)))
            else:
                setattr(row, name, value)
        s.flush()
        return _run_record(row)


def append_run_step(run_id: str, step: StepRecord, *, session: Session | None = None) -> PipelineRunRecord:
    """Append ``step`` to ``steps_json``; a step with the same name replaces its earlier entry (started -> ok)."""
    with _session(session) as s:
        row = _get_run_row(s, run_id)
        steps = [dict(x) for x in (row.steps_json or [])]
        entry = step.model_dump(mode="json")
        for i, existing in enumerate(steps):
            if existing.get("name") == step.name:
                steps[i] = entry
                break
        else:
            steps.append(entry)
        row.steps_json = steps
        s.flush()
        return _run_record(row)


def get_active_run(*, session: Session | None = None) -> PipelineRunRecord | None:
    """The single ``queued``/``running`` run, if any (single-flight guard)."""
    with _session(session) as s:
        stmt = (select(models.PipelineRun).where(models.PipelineRun.status.in_(ACTIVE_RUN_STATUSES))
                .order_by(models.PipelineRun.created_at.desc()))
        row = s.scalars(stmt).first()
        return _run_record(row) if row else None


def list_runs(limit: int = 20, *, session: Session | None = None) -> list[PipelineRunRecord]:
    with _session(session) as s:
        stmt = (select(models.PipelineRun)
                .order_by(models.PipelineRun.created_at.desc(), models.PipelineRun.id.desc()).limit(limit))
        return [_run_record(r) for r in s.scalars(stmt)]


def list_runs_for_segment(segment_id: str, limit: int = 20, *, session: Session | None = None) -> list[PipelineRunRecord]:
    with _session(session) as s:
        stmt = (select(models.PipelineRun).where(models.PipelineRun.segment_id == segment_id)
                .order_by(models.PipelineRun.created_at.desc(), models.PipelineRun.id.desc()).limit(limit))
        return [_run_record(r) for r in s.scalars(stmt)]


def latest_run_for_segment(segment_id: str, *, session: Session | None = None) -> PipelineRunRecord | None:
    runs = list_runs_for_segment(segment_id, limit=1, session=session)
    return runs[0] if runs else None


def run_stats(*, session: Session | None = None) -> RunStats:
    with _session(session) as s:
        rows = s.execute(select(models.PipelineRun.status, func.count()).group_by(models.PipelineRun.status)).all()
        by_status = {status: count for status, count in rows}
        return RunStats(
            total=sum(by_status.values()),
            succeeded=by_status.get(RunStatus.succeeded.value, 0),
            failed=by_status.get(RunStatus.failed.value, 0),
            failed_budget=by_status.get(RunStatus.failed_budget.value, 0),
        )


# ------------------------------------------------------------------ llm calls / spend


def record_llm_call(data: LlmCallCreate, *, session: Session | None = None) -> LlmCallRecord:
    with _session(session) as s:
        _get_run_row(s, data.run_id)
        row = models.LlmCall(
            run_id=data.run_id, step=data.step, model=data.model, input_tokens=data.input_tokens,
            output_tokens=data.output_tokens, cost_usd=data.cost_usd, latency_ms=data.latency_ms, ok=data.ok,
            error=data.error, request_summary=data.request_summary[:SUMMARY_MAX_CHARS],
            response_summary=data.response_summary[:SUMMARY_MAX_CHARS],
        )
        s.add(row)
        s.flush()
        return _llm_record(row)


def list_llm_calls(run_id: str, *, session: Session | None = None) -> list[LlmCallRecord]:
    with _session(session) as s:
        stmt = (select(models.LlmCall).where(models.LlmCall.run_id == run_id)
                .order_by(models.LlmCall.created_at.asc(), models.LlmCall.id.asc()))
        return [_llm_record(r) for r in s.scalars(stmt)]


def run_llm_totals(run_id: str, *, session: Session | None = None) -> RunCostSummary:
    calls = list_llm_calls(run_id, session=session)
    return RunCostSummary(
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cost_usd=float(sum(c.cost_usd for c in calls)),
        calls=calls,
    )


def run_spend_usd(run_id: str, *, session: Session | None = None) -> float:
    with _session(session) as s:
        total = s.scalar(select(func.coalesce(func.sum(models.LlmCall.cost_usd), 0.0))
                         .where(models.LlmCall.run_id == run_id))
        return float(total or 0.0)


def total_llm_cost_usd(*, session: Session | None = None) -> float:
    """Global spend: SUM(cost_usd) over all llm_calls, including failed calls that returned usage."""
    with _session(session) as s:
        total = s.scalar(select(func.coalesce(func.sum(models.LlmCall.cost_usd), 0.0)))
        return float(total or 0.0)


def spend_summary(budget_usd: float, *, session: Session | None = None) -> SpendSummary:
    spent = total_llm_cost_usd(session=session)
    return SpendSummary(budget_usd=budget_usd, spent_usd=spent, remaining_usd=max(0.0, budget_usd - spent))


# ------------------------------------------------------------------ google requests


def record_google_request(data: GoogleRequestCreate, *, session: Session | None = None) -> GoogleRequestRecord:
    with _session(session) as s:
        if data.run_id is not None:
            _get_run_row(s, data.run_id)
        row = models.GoogleRequest(run_id=data.run_id, kind=data.kind, cache_key=data.cache_key,
                                   cache_hit=data.cache_hit, est_cost_usd=data.est_cost_usd, path=data.path)
        s.add(row)
        s.flush()
        return _google_record(row)


def list_google_requests(run_id: str, *, session: Session | None = None) -> list[GoogleRequestRecord]:
    with _session(session) as s:
        stmt = (select(models.GoogleRequest).where(models.GoogleRequest.run_id == run_id)
                .order_by(models.GoogleRequest.created_at.asc(), models.GoogleRequest.id.asc()))
        return [_google_record(r) for r in s.scalars(stmt)]


def google_counts(run_id: str | None = None, *, session: Session | None = None) -> GoogleCounts:
    """New requests vs cache hits (per run, or global when ``run_id`` is None); cost counts misses only."""
    with _session(session) as s:
        miss = case((models.GoogleRequest.cache_hit.is_(False), 1), else_=0)
        hit = case((models.GoogleRequest.cache_hit.is_(True), 1), else_=0)
        stmt = select(
            func.coalesce(func.sum(miss), 0),
            func.coalesce(func.sum(hit), 0),
            func.coalesce(func.sum(models.GoogleRequest.est_cost_usd * miss), 0.0),
        )
        if run_id is not None:
            stmt = stmt.where(models.GoogleRequest.run_id == run_id)
        requests, hits, cost = s.execute(stmt).one()
        return GoogleCounts(requests=int(requests), cache_hits=int(hits), est_cost_usd=float(cost))


# ------------------------------------------------------------------ game runs


def validate_game_run(data: GameRunCreate, tokens_in_scene: int) -> None:
    """spec/data.md insert rules; raises ``InvalidInputError`` (422) on an impossible run."""
    max_score = data.distance_m + data.tokens_collected * TOKEN_SCORE + 1
    if data.score > max_score:
        raise InvalidInputError(
            f"score {data.score} is impossible for {data.distance_m:.0f} m and {data.tokens_collected} tokens "
            f"(max {max_score:.0f})."
        )
    if data.distance_m > MAX_RUN_DISTANCE_M:
        raise InvalidInputError(f"distance_m {data.distance_m:.0f} exceeds the {MAX_RUN_DISTANCE_M:.0f} m limit.")
    max_tokens = data.loops * tokens_in_scene + tokens_in_scene
    if data.tokens_collected > max_tokens:
        raise InvalidInputError(
            f"tokens_collected {data.tokens_collected} exceeds the {max_tokens} available over {data.loops} loops."
        )


def record_game_run(slug: str, data: GameRunCreate, client: str, *, session: Session | None = None) -> GameRunResult:
    """Validate against the segment's scene, insert, and update the segment's best score/distance."""
    with _session(session) as s:
        seg = _get_segment_row(s, slug=slug)
        if seg.status != SegmentStatus.ready.value or not seg.scene_json:
            raise NotFoundError("This street has not been generated yet.")
        validate_game_run(data, len(seg.scene_json.get("tokens", [])))
        row = models.GameRun(
            segment_id=seg.id, score=data.score, distance_m=data.distance_m, tokens_collected=data.tokens_collected,
            loops=data.loops, crashed_into_type=data.crashed_into_type, crashed_into_id=data.crashed_into_id,
            duration_s=data.duration_s, client=(client or "")[:CLIENT_MAX_CHARS],
        )
        s.add(row)
        is_new_best = seg.best_score is None or data.score > seg.best_score
        if is_new_best:
            seg.best_score = data.score
            seg.best_distance_m = data.distance_m
            seg.updated_at = models.utcnow()
        s.flush()
        return GameRunResult(run=_game_record(row), best_score=int(seg.best_score),
                             best_distance_m=float(seg.best_distance_m), is_new_best=is_new_best)


def best_game_run(segment_id: str, *, session: Session | None = None) -> GameRunRecord | None:
    runs = list_top_game_runs(segment_id, limit=1, session=session)
    return runs[0] if runs else None


def list_top_game_runs(segment_id: str, limit: int = 5, *, session: Session | None = None) -> list[GameRunRecord]:
    with _session(session) as s:
        stmt = (select(models.GameRun).where(models.GameRun.segment_id == segment_id)
                .order_by(models.GameRun.score.desc(), models.GameRun.created_at.asc()).limit(limit))
        return [_game_record(r) for r in s.scalars(stmt)]


def count_game_runs(segment_id: str, *, session: Session | None = None) -> int:
    with _session(session) as s:
        return int(s.scalar(select(func.count()).where(models.GameRun.segment_id == segment_id)) or 0)
