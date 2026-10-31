"""SQLAlchemy models for the entities in spec/data.md (Phase 1 + the Phase 2 street-model tables).

These never leave ``hero.db``: ``hero.db.repo`` converts rows to Pydantic records.
UUID4 strings as primary keys; datetimes stored as naive UTC (SQLite has no tz);
large documents stored as JSON text columns, read whole.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (JSON, Boolean, CHAR, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hero.db.base import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


Money = Numeric(10, 6, asdecimal=False)


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    start_lat: Mapped[float] = mapped_column(Float, nullable=False)
    start_lon: Mapped[float] = mapped_column(Float, nullable=False)
    end_lat: Mapped[float] = mapped_column(Float, nullable=False)
    end_lon: Mapped[float] = mapped_column(Float, nullable=False)
    footpath_side: Mapped[str] = mapped_column(String(8), nullable=False)
    length_m: Mapped[float] = mapped_column(Float, nullable=False)
    heading_deg: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    quality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scene_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Logical FK to pipeline_runs.id, deliberately unenforced in Phase 1: segments <-> pipeline_runs
    # would be circular and enforcing it complicates insert ordering (segment first, run second,
    # then update latest_run_id). spec/data.md records it as a logical FK.
    latest_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    best_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Derived cache (0003): ``schema_version`` of ``scene_json`` (null = no scene).
    scene_schema: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    runs: Mapped[list[PipelineRun]] = relationship(back_populates="segment", cascade="all, delete-orphan")
    game_runs: Mapped[list[GameRun]] = relationship(back_populates="segment", cascade="all, delete-orphan")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    inputs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    steps_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    imagery_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    layout_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    obstacles_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    iterations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scene_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Money, nullable=False, default=0.0)
    google_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    google_cached_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    segment: Mapped[Segment] = relationship(back_populates="runs")
    llm_calls: Mapped[list[LlmCall]] = relationship(back_populates="run", cascade="all, delete-orphan")
    google_requests_rows: Mapped[list[GoogleRequest]] = relationship(back_populates="run")


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Money, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    response_summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    run: Mapped[PipelineRun] = relationship(back_populates="llm_calls")


class GoogleRequest(Base):
    __tablename__ = "google_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    est_cost_usd: Mapped[float] = mapped_column(Money, nullable=False, default=0.0)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    run: Mapped[PipelineRun | None] = relationship(back_populates="google_requests_rows")


class GameRun(Base):
    __tablename__ = "game_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    tokens_collected: Mapped[int] = mapped_column(Integer, nullable=False)
    loops: Mapped[int] = mapped_column(Integer, nullable=False)
    crashed_into_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crashed_into_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    client: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    segment: Mapped[Segment] = relationship(back_populates="game_runs")


class ObservationRow(Base):
    """Phase 2 ``observations``: first-class georeferenced evidence; never updated after insert."""

    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("segment_id", "obs_key", name="uq_observations_segment_obs_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    obs_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    pitch_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fov_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    zoom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scale: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meters_per_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    pano_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_date: Mapped[str | None] = mapped_column(String(7), nullable=True)
    station_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    offset_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    view: Mapped[str | None] = mapped_column(String(16), nullable=True)
    georef_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cost_usd: Mapped[float] = mapped_column(Money, nullable=False, default=0.0)
    acquired_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ModelVersionRow(Base):
    """Phase 2 ``model_versions``: immutable canonical-model snapshots, dense versions from 0."""

    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("segment_id", "version", name="uq_model_versions_segment_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    scene_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fidelity_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fidelity_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Derived cache (0003): everything below is recomputable from the knowledge columns above.
    scene_schema: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    compiled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gaps_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    export_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ObservationExtractionRow(Base):
    """Phase 2 ``observation_extractions``: raw schema-validated perception output of each model call."""

    __tablename__ = "observation_extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class RunReportRow(Base):
    """Phase 2 ``run_reports``: one deterministic report per run (primary key = run id)."""

    __tablename__ = "run_reports"

    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), primary_key=True)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


Index("ix_game_runs_segment_score", GameRun.segment_id, GameRun.score.desc())
Index("ix_pipeline_runs_segment_created", PipelineRun.segment_id, PipelineRun.created_at.desc())
