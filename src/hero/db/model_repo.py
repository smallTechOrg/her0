"""Phase-2 persistence: observations, model versions, extractions, run reports (spec/data.md, Contracts).

Every function returns Pydantic records and runs in ONE transaction (``get_session``), unless an
existing ``session`` is passed to compose calls. SQLAlchemy rows never leave this module.

Function index
--------------
observations   upsert_observation, get_observations, get_observation, observation_counts
versions       head_version, get_version, list_versions, save_version_only, publish_version
derived cache  update_derived (recompile: scene/scene_schema/compiled_by/gaps/export of one version), get_export
extractions    save_extraction, list_extractions
run reports    save_run_report, get_run_report

Interpretation notes
--------------------
* ``entity_count`` stored on a version = entities whose state is not ``removed`` (``stats.removed_count``
  counts the others); ``observation_count`` = ``len(model.fused_observation_ids)``;
  ``fidelity_level`` = ``fidelity.overall.level``.
* ``model_hash`` is ``hero.model.diff.model_hash`` when that module exists, otherwise an identical
  local implementation (sha256 of canonical JSON excluding ``version, parent_version, run_id, created_at, kind``).
* ``publish_version`` only overwrites ``segments.thumbnail_path`` when a path is given.
* A bad version number raises ``VersionConflictError`` (a ``ValueError`` and a repo ``InvalidInputError``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hero.db import models
from hero.db.repo import InvalidInputError, _aware, _get_run_row, _get_segment_row, _naive, _session
from hero.model.types import (
    CanonicalModel,
    EntityState,
    FidelityMap,
    ModelDiff,
    Observation,
    ObservationKind,
    ObservationSource,
    RunReport,
    ViewKind,
)

QUALITIES = ("full", "satellite_only", "generic")
EXTRACTION_KINDS = ("overhead", "streetlevel", "critique")
_HASH_EXCLUDE = {"version", "parent_version", "run_id", "created_at", "kind"}


class VersionConflictError(InvalidInputError, ValueError):
    """A model version number is not ``head + 1`` (or the version payload is inconsistent)."""


# ------------------------------------------------------------------ inputs / records


class ObservationCreate(BaseModel):
    """The ``observations`` columns minus ``id`` / ``created_at`` (``georef_json`` -> ``georef``)."""

    model_config = ConfigDict(extra="forbid")

    segment_id: str
    obs_key: str = Field(min_length=1, max_length=128)
    kind: ObservationKind
    source: ObservationSource
    sha256: str = Field(min_length=64, max_length=64)
    file_path: str
    media_type: str
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    bytes: int = Field(ge=0)
    lat: float
    lon: float
    heading_deg: float | None = None
    pitch_deg: float | None = None
    fov_deg: float | None = None
    zoom: int | None = None
    scale: int | None = None
    meters_per_px: float | None = None
    pano_id: str | None = None
    capture_date: str | None = Field(default=None, max_length=7)
    station_s: float | None = None
    offset_t: float | None = None
    view: ViewKind | None = None
    georef: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0)
    acquired_run_id: str | None = None
    note: str | None = None
    # Optional, for fixture seeding only (re-creating a recorded street with stable ids); normal ingest omits both.
    id: str | None = None
    created_at: datetime | None = None


class ModelVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, protected_namespaces=())

    version: int = Field(ge=0)
    kind: Literal["legacy", "prior", "fused", "edit"]
    parent_version: int | None = None
    run_id: str | None = None
    model: CanonicalModel
    scene: dict[str, Any] | None = None
    fidelity: FidelityMap
    diff: ModelDiff | None = None
    summary: str = ""
    # Derived cache (Alembic 0003). ``scene_schema`` defaults to the scene's own ``schema_version``.
    scene_schema: int | None = None
    compiled_by: str | None = None
    gaps: dict[str, Any] | None = None
    export: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _consistent_with_model(self) -> "ModelVersionCreate":
        if self.model.version != self.version:
            raise ValueError(f"version {self.version} != model.version {self.model.version}")
        if self.model.kind.value != self.kind:
            raise ValueError(f"kind {self.kind!r} != model.kind {self.model.kind.value!r}")
        return self


class ModelVersionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    kind: str
    parent_version: int | None
    run_id: str | None
    created_at: datetime
    entity_count: int
    observation_count: int
    fidelity_level: int
    summary: str
    scene_schema: int = 2


class ModelVersionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, protected_namespaces=())

    segment_id: str
    version: int
    kind: str
    parent_version: int | None
    run_id: str | None
    model: CanonicalModel
    scene: dict[str, Any] | None
    fidelity: FidelityMap
    diff: ModelDiff | None
    summary: str
    model_hash: str
    entity_count: int
    observation_count: int
    fidelity_level: int
    created_at: datetime
    scene_schema: int = 2
    compiled_by: str | None = None
    gaps: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class ExtractionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    segment_id: str
    run_id: str
    batch_index: int = Field(ge=0)
    kind: Literal["overhead", "streetlevel", "critique"]
    observation_ids: list[str]
    model: str
    prompt_name: str
    extraction: dict[str, Any]


class ExtractionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, protected_namespaces=())

    id: str
    segment_id: str
    run_id: str
    batch_index: int
    kind: str
    observation_ids: list[str]
    model: str
    prompt_name: str
    extraction: dict[str, Any]
    created_at: datetime


# ------------------------------------------------------------------ row -> record


def _observation_record(row: models.ObservationRow) -> Observation:
    return Observation(
        id=row.id, segment_id=row.segment_id, obs_key=row.obs_key, kind=ObservationKind(row.kind),
        source=ObservationSource(row.source), sha256=row.sha256.strip(), file_path=row.file_path,
        media_type=row.media_type, width_px=row.width_px, height_px=row.height_px, bytes=row.bytes,
        lat=row.lat, lon=row.lon, heading_deg=row.heading_deg, pitch_deg=row.pitch_deg, fov_deg=row.fov_deg,
        zoom=row.zoom, scale=row.scale, meters_per_px=row.meters_per_px, pano_id=row.pano_id,
        capture_date=row.capture_date, station_s=row.station_s, offset_t=row.offset_t,
        view=ViewKind(row.view) if row.view else None, georef=dict(row.georef_json or {}),
        cost_usd=float(row.cost_usd), acquired_run_id=row.acquired_run_id, note=row.note,
        created_at=_aware(row.created_at),
    )


def _version_record(row: models.ModelVersionRow) -> ModelVersionRecord:
    return ModelVersionRecord(
        segment_id=row.segment_id, version=row.version, kind=row.kind, parent_version=row.parent_version,
        run_id=row.run_id, model=CanonicalModel.model_validate(row.model_json), scene=row.scene_json,
        fidelity=FidelityMap.model_validate(row.fidelity_json),
        diff=ModelDiff.model_validate(row.diff_json) if row.diff_json else None, summary=row.summary,
        model_hash=row.model_hash.strip(), entity_count=row.entity_count, observation_count=row.observation_count,
        fidelity_level=row.fidelity_level, created_at=_aware(row.created_at),
        scene_schema=row.scene_schema, compiled_by=row.compiled_by, gaps=row.gaps_json, export=row.export_json,
    )


def _version_summary(row: models.ModelVersionRow) -> ModelVersionSummary:
    return ModelVersionSummary(
        version=row.version, kind=row.kind, parent_version=row.parent_version, run_id=row.run_id,
        created_at=_aware(row.created_at), entity_count=row.entity_count,
        observation_count=row.observation_count, fidelity_level=row.fidelity_level, summary=row.summary,
        scene_schema=row.scene_schema,
    )


def _extraction_record(row: models.ObservationExtractionRow) -> ExtractionRecord:
    return ExtractionRecord(
        id=row.id, segment_id=row.segment_id, run_id=row.run_id, batch_index=row.batch_index, kind=row.kind,
        observation_ids=list(row.observation_ids or []), model=row.model, prompt_name=row.prompt_name,
        extraction=row.extraction_json, created_at=_aware(row.created_at),
    )


# ------------------------------------------------------------------ observations


def upsert_observation(data: ObservationCreate, *, session: Session | None = None) -> tuple[Observation, bool]:
    """Insert, or return the existing row for ``(segment_id, obs_key)``. Returns ``(row, created)``."""
    with _session(session) as s:
        _get_segment_row(s, segment_id=data.segment_id)
        stmt = select(models.ObservationRow).where(
            models.ObservationRow.segment_id == data.segment_id, models.ObservationRow.obs_key == data.obs_key)
        existing = s.scalars(stmt).first()
        if existing is not None:
            return _observation_record(existing), False
        values = data.model_dump(exclude={"georef", "id", "created_at"}, mode="python")
        if data.id is not None:
            values["id"] = data.id
        if data.created_at is not None:
            values["created_at"] = _naive(data.created_at)
        values["kind"] = data.kind.value
        values["source"] = data.source.value
        values["view"] = data.view.value if data.view else None
        row = models.ObservationRow(**values, georef_json=data.georef)
        try:
            with s.begin_nested():
                s.add(row)
                s.flush()
        except IntegrityError:  # lost a race with a concurrent insert of the same key
            existing = s.scalars(stmt).first()
            if existing is None:
                raise
            return _observation_record(existing), False
        return _observation_record(row), True


def get_observation(observation_id: str, *, session: Session | None = None) -> Observation | None:
    with _session(session) as s:
        row = s.get(models.ObservationRow, observation_id)
        return _observation_record(row) if row else None


def get_observations(segment_id: str, *, ids: Sequence[str] | None = None, kinds: Sequence[str] | None = None,
                     limit: int = 1000, offset: int = 0, newest_first: bool = False,
                     session: Session | None = None) -> list[Observation]:
    """Observations of a segment, oldest first (``created_at, id``) unless ``newest_first``."""
    with _session(session) as s:
        stmt = select(models.ObservationRow).where(models.ObservationRow.segment_id == segment_id)
        if ids is not None:
            stmt = stmt.where(models.ObservationRow.id.in_(list(ids)))
        if kinds is not None:
            stmt = stmt.where(models.ObservationRow.kind.in_([str(k) for k in kinds]))
        if newest_first:
            stmt = stmt.order_by(models.ObservationRow.created_at.desc(), models.ObservationRow.id.desc())
        else:
            stmt = stmt.order_by(models.ObservationRow.created_at.asc(), models.ObservationRow.id.asc())
        stmt = stmt.limit(limit).offset(offset)
        return [_observation_record(r) for r in s.scalars(stmt)]


def observation_counts(segment_id: str, *, session: Session | None = None) -> dict[str, int]:
    """Observation count by ``kind`` (kinds with no rows are absent)."""
    with _session(session) as s:
        rows = s.execute(select(models.ObservationRow.kind, func.count())
                         .where(models.ObservationRow.segment_id == segment_id)
                         .group_by(models.ObservationRow.kind)).all()
        return {kind: int(n) for kind, n in rows}


# ------------------------------------------------------------------ model versions


def _hash(model: CanonicalModel) -> str:
    try:
        from hero.model.diff import model_hash  # S1c owns the canonical definition
    except ImportError:
        data = model.model_dump(mode="json", exclude=_HASH_EXCLUDE)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return model_hash(model)


def _schema_of(scene: dict[str, Any] | None, explicit: int | None = None) -> int:
    if explicit is not None:
        return explicit
    v = scene.get("schema_version") if scene else None
    return v if isinstance(v, int) else 2


def _compiled_by_of(scene: dict[str, Any] | None) -> str | None:
    ref = scene.get("model") if scene else None
    value = ref.get("compiled_by") if isinstance(ref, dict) else None
    return str(value)[:64] if value else None


def _head(s: Session, segment_id: str) -> int | None:
    return s.scalar(select(func.max(models.ModelVersionRow.version))
                    .where(models.ModelVersionRow.segment_id == segment_id))


def head_version(segment_id: str, *, session: Session | None = None) -> int | None:
    with _session(session) as s:
        return _head(s, segment_id)


def get_version(segment_id: str, version: int, *, session: Session | None = None) -> ModelVersionRecord | None:
    with _session(session) as s:
        row = s.scalars(select(models.ModelVersionRow).where(
            models.ModelVersionRow.segment_id == segment_id, models.ModelVersionRow.version == version)).first()
        return _version_record(row) if row else None


def list_versions(segment_id: str, *, session: Session | None = None) -> list[ModelVersionSummary]:
    with _session(session) as s:
        stmt = (select(models.ModelVersionRow).where(models.ModelVersionRow.segment_id == segment_id)
                .order_by(models.ModelVersionRow.version.asc()))
        return [_version_summary(r) for r in s.scalars(stmt)]


def _insert_version(s: Session, segment_id: str, rec: ModelVersionCreate) -> models.ModelVersionRow:
    _get_segment_row(s, segment_id=segment_id)
    head = _head(s, segment_id)
    expected = 0 if head is None else head + 1
    if rec.version != expected:
        raise VersionConflictError(
            f"model version must be head + 1: segment has head {head}, got version {rec.version} (expected {expected}).")
    entity_count = sum(1 for e in rec.model.entities if e.state is not EntityState.removed)
    row = models.ModelVersionRow(
        segment_id=segment_id, version=rec.version, kind=rec.kind, parent_version=rec.parent_version,
        run_id=rec.run_id, model_json=rec.model.model_dump(mode="json"), scene_json=rec.scene,
        fidelity_json=rec.fidelity.model_dump(mode="json"),
        diff_json=rec.diff.model_dump(mode="json") if rec.diff else None, summary=rec.summary,
        model_hash=_hash(rec.model), entity_count=entity_count,
        observation_count=len(rec.model.fused_observation_ids), fidelity_level=rec.fidelity.overall.level,
        scene_schema=_schema_of(rec.scene, rec.scene_schema), compiled_by=rec.compiled_by or _compiled_by_of(rec.scene),
        gaps_json=rec.gaps, export_json=rec.export,
    )
    s.add(row)
    s.flush()
    return row


def save_version_only(segment_id: str, rec: ModelVersionCreate, *,
                      session: Session | None = None) -> ModelVersionRecord:
    """Store a version (v0 ``prior`` / ``legacy``) WITHOUT touching ``segments``."""
    with _session(session) as s:
        return _version_record(_insert_version(s, segment_id, rec))


def _apply_segment_pointer(seg: models.Segment, rec: ModelVersionCreate, run_id: str | None,
                           thumbnail_path: str | None, quality: str) -> None:
    seg.scene_json = rec.scene
    seg.scene_schema = _schema_of(rec.scene, rec.scene_schema)
    seg.status = "ready"
    seg.quality = quality
    seg.latest_run_id = run_id
    if thumbnail_path is not None:
        seg.thumbnail_path = thumbnail_path
    seg.updated_at = models.utcnow()


def publish_version(segment_id: str, rec: ModelVersionCreate, *, thumbnail_path: str | None, quality: str,
                    session: Session | None = None) -> ModelVersionRecord:
    """Atomically insert version ``head + 1`` and point the segment at its scene.

    One transaction: ``model_versions`` insert, then ``segments.scene_json / status="ready" / quality /
    latest_run_id / thumbnail_path``. Any failure leaves both unchanged.
    """
    if quality not in QUALITIES:
        raise InvalidInputError(f"quality must be one of {QUALITIES} (got {quality!r}).")
    if rec.scene is None:
        raise InvalidInputError("publish_version needs the compiled scene (rec.scene is None).")
    with _session(session) as s:
        seg = _get_segment_row(s, segment_id=segment_id)
        row = _insert_version(s, segment_id, rec)
        _apply_segment_pointer(seg, rec, rec.run_id, thumbnail_path, quality)
        s.flush()
        return _version_record(row)


def update_derived(segment_id: str, version: int, *, scene: dict[str, Any], compiled_by: str | None,
                   gaps: dict[str, Any] | None, export: dict[str, Any] | None, scene_schema: int | None = None,
                   session: Session | None = None) -> ModelVersionRecord:
    """Overwrite ONLY the derived columns of ``version`` (``scene_json / scene_schema / compiled_by / gaps_json /
    export_json``) - the knowledge columns (``model_json``, ``fidelity_json``, ``diff_json``, ``model_hash``) are never
    touched. When ``version`` is the head and the segment already serves a scene, the segment pointer
    (``segments.scene_json / scene_schema``) follows, in the same transaction.
    """
    with _session(session) as s:
        seg = _get_segment_row(s, segment_id=segment_id)
        row = s.scalars(select(models.ModelVersionRow).where(
            models.ModelVersionRow.segment_id == segment_id, models.ModelVersionRow.version == version)).first()
        if row is None:
            raise InvalidInputError(f"segment has no model version {version}.")
        schema = _schema_of(scene, scene_schema)
        row.scene_json = scene
        row.scene_schema = schema
        row.compiled_by = compiled_by[:64] if compiled_by else None
        row.gaps_json = gaps
        row.export_json = export
        if version == _head(s, segment_id) and seg.scene_json is not None:
            seg.scene_json = scene
            seg.scene_schema = schema
            seg.updated_at = models.utcnow()
        s.flush()
        return _version_record(row)


def get_export(segment_id: str, version: int | None = None, *, session: Session | None = None) -> dict[str, Any] | None:
    """``export_json`` of ``version`` (default: head); ``None`` when never exported."""
    with _session(session) as s:
        n = _head(s, segment_id) if version is None else version
        if n is None:
            return None
        row = s.scalars(select(models.ModelVersionRow).where(
            models.ModelVersionRow.segment_id == segment_id, models.ModelVersionRow.version == n)).first()
        return dict(row.export_json) if row is not None and row.export_json else None


# ------------------------------------------------------------------ extractions


def save_extraction(rec: ExtractionCreate, *, session: Session | None = None) -> str:
    with _session(session) as s:
        _get_segment_row(s, segment_id=rec.segment_id)
        _get_run_row(s, rec.run_id)
        row = models.ObservationExtractionRow(
            segment_id=rec.segment_id, run_id=rec.run_id, batch_index=rec.batch_index, kind=rec.kind,
            observation_ids=list(rec.observation_ids), model=rec.model, prompt_name=rec.prompt_name,
            extraction_json=rec.extraction)
        s.add(row)
        s.flush()
        return row.id


def list_extractions(segment_id: str, run_id: str | None = None, *,
                     session: Session | None = None) -> list[ExtractionRecord]:
    with _session(session) as s:
        stmt = select(models.ObservationExtractionRow).where(models.ObservationExtractionRow.segment_id == segment_id)
        if run_id is not None:
            stmt = stmt.where(models.ObservationExtractionRow.run_id == run_id)
        stmt = stmt.order_by(models.ObservationExtractionRow.created_at.asc(),
                             models.ObservationExtractionRow.batch_index.asc(), models.ObservationExtractionRow.id.asc())
        return [_extraction_record(r) for r in s.scalars(stmt)]


# ------------------------------------------------------------------ run reports


def save_run_report(run_id: str, segment_id: str, report: RunReport, *, session: Session | None = None) -> None:
    """Insert or replace the report of ``run_id`` (one per run)."""
    with _session(session) as s:
        _get_run_row(s, run_id)
        _get_segment_row(s, segment_id=segment_id)
        row = s.get(models.RunReportRow, run_id)
        payload = report.model_dump(mode="json")
        if row is None:
            row = models.RunReportRow(run_id=run_id, segment_id=segment_id)
            s.add(row)
        row.segment_id = segment_id
        row.mode = report.mode
        row.base_version = report.base_version
        row.result_version = report.result_version
        row.version_created = report.version_created
        row.report_json = payload
        s.flush()


def get_run_report(run_id: str, *, session: Session | None = None) -> RunReport | None:
    with _session(session) as s:
        row = s.get(models.RunReportRow, run_id)
        return RunReport.model_validate(row.report_json) if row else None
