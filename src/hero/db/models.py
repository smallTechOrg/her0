"""SQLAlchemy models for the Phase 1 entities in spec/data.md.

These never leave ``hero.db``: ``hero.db.repo`` converts rows to Pydantic records.
UUID4 strings as primary keys; datetimes stored as naive UTC (SQLite has no tz);
large documents stored as JSON text columns, read whole.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text
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


Index("ix_game_runs_segment_score", GameRun.segment_id, GameRun.score.desc())
Index("ix_pipeline_runs_segment_created", PipelineRun.segment_id, PipelineRun.created_at.desc())
