"""phase1 - segments, pipeline_runs, llm_calls, google_requests, game_runs (spec/data.md)

Revision ID: 0001
Revises: 0000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = "0000"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(10, 6)


def upgrade() -> None:
    op.create_table(
        "segments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("start_lat", sa.Float, nullable=False),
        sa.Column("start_lon", sa.Float, nullable=False),
        sa.Column("end_lat", sa.Float, nullable=False),
        sa.Column("end_lon", sa.Float, nullable=False),
        sa.Column("footpath_side", sa.String(8), nullable=False),
        sa.Column("length_m", sa.Float, nullable=False),
        sa.Column("heading_deg", sa.Float, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("quality", sa.String(16), nullable=True),
        sa.Column("scene_json", sa.JSON, nullable=True),
        sa.Column("thumbnail_path", sa.Text, nullable=True),
        sa.Column("latest_run_id", sa.String(36), nullable=True),
        sa.Column("best_score", sa.Integer, nullable=True),
        sa.Column("best_distance_m", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_segments_slug", "segments", ["slug"], unique=True)
    op.create_index("ix_segments_status", "segments", ["status"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("inputs_json", sa.JSON, nullable=False),
        sa.Column("steps_json", sa.JSON, nullable=False),
        sa.Column("imagery_json", sa.JSON, nullable=False),
        sa.Column("layout_json", sa.JSON, nullable=True),
        sa.Column("obstacles_json", sa.JSON, nullable=True),
        sa.Column("iterations_json", sa.JSON, nullable=False),
        sa.Column("scene_json", sa.JSON, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", MONEY, nullable=False, server_default="0"),
        sa.Column("google_requests", sa.Integer, nullable=False, server_default="0"),
        sa.Column("google_cached_hits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_json", sa.JSON, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_pipeline_runs_segment_id", "pipeline_runs", ["segment_id"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_pipeline_runs_segment_created", "pipeline_runs",
                    ["segment_id", sa.text("created_at DESC")])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id"), nullable=False),
        sa.Column("step", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", MONEY, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("request_summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("response_summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])

    op.create_table(
        "google_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id"), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("cache_key", sa.Text, nullable=False),
        sa.Column("cache_hit", sa.Boolean, nullable=False),
        sa.Column("est_cost_usd", MONEY, nullable=False, server_default="0"),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_google_requests_run_id", "google_requests", ["run_id"])

    op.create_table(
        "game_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("distance_m", sa.Float, nullable=False),
        sa.Column("tokens_collected", sa.Integer, nullable=False),
        sa.Column("loops", sa.Integer, nullable=False),
        sa.Column("crashed_into_type", sa.String(32), nullable=True),
        sa.Column("crashed_into_id", sa.String(64), nullable=True),
        sa.Column("duration_s", sa.Float, nullable=False),
        sa.Column("client", sa.String(120), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_game_runs_segment_id", "game_runs", ["segment_id"])
    op.create_index("ix_game_runs_segment_score", "game_runs", ["segment_id", sa.text("score DESC")])


def downgrade() -> None:
    op.drop_table("game_runs")
    op.drop_table("google_requests")
    op.drop_table("llm_calls")
    op.drop_table("pipeline_runs")
    op.drop_table("segments")
