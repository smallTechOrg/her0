"""street_model - observations, model_versions, observation_extractions, run_reports (spec/data.md, Phase 2)

Additive only: no Phase-1 table or column is touched, so an existing dev DB upgrades in place and its
rows keep working. Works on a fresh DB (0000 -> 0001 -> 0002) and as an upgrade of a 0001 database.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(10, 6)


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("obs_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("width_px", sa.Integer, nullable=False),
        sa.Column("height_px", sa.Integer, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column("heading_deg", sa.Float, nullable=True),
        sa.Column("pitch_deg", sa.Float, nullable=True),
        sa.Column("fov_deg", sa.Float, nullable=True),
        sa.Column("zoom", sa.Integer, nullable=True),
        sa.Column("scale", sa.Integer, nullable=True),
        sa.Column("meters_per_px", sa.Float, nullable=True),
        sa.Column("pano_id", sa.Text, nullable=True),
        sa.Column("capture_date", sa.String(7), nullable=True),
        sa.Column("station_s", sa.Float, nullable=True),
        sa.Column("offset_t", sa.Float, nullable=True),
        sa.Column("view", sa.String(16), nullable=True),
        sa.Column("georef_json", sa.JSON, nullable=False),
        sa.Column("cost_usd", MONEY, nullable=False, server_default="0"),
        sa.Column("acquired_run_id", sa.String(36), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("segment_id", "obs_key", name="uq_observations_segment_obs_key"),
    )
    op.create_index("ix_observations_segment_id", "observations", ["segment_id"])

    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("parent_version", sa.Integer, nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("model_json", sa.JSON, nullable=False),
        sa.Column("scene_json", sa.JSON, nullable=True),
        sa.Column("fidelity_json", sa.JSON, nullable=False),
        sa.Column("diff_json", sa.JSON, nullable=True),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("model_hash", sa.CHAR(64), nullable=False),
        sa.Column("entity_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("observation_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fidelity_level", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("segment_id", "version", name="uq_model_versions_segment_version"),
    )
    op.create_index("ix_model_versions_segment_id", "model_versions", ["segment_id"])

    op.create_table(
        "observation_extractions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id"), nullable=False),
        sa.Column("batch_index", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("observation_ids", sa.JSON, nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_name", sa.String(64), nullable=False),
        sa.Column("extraction_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_observation_extractions_segment_id", "observation_extractions", ["segment_id"])
    op.create_index("ix_observation_extractions_run_id", "observation_extractions", ["run_id"])

    op.create_table(
        "run_reports",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("pipeline_runs.id"), primary_key=True),
        sa.Column("segment_id", sa.String(36), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("base_version", sa.Integer, nullable=True),
        sa.Column("result_version", sa.Integer, nullable=True),
        sa.Column("version_created", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("report_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_run_reports_segment_id", "run_reports", ["segment_id"])


def downgrade() -> None:
    op.drop_table("run_reports")
    op.drop_table("observation_extractions")
    op.drop_table("model_versions")
    op.drop_table("observations")
