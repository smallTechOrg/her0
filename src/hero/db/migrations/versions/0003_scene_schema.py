"""scene_schema - derived-cache columns for the compiled scene and its export (spec/data.md, Phase 2.1)

``model_versions``: ``scene_schema`` (int, not null, default 2), ``compiled_by`` (varchar 64), ``gaps_json``,
``export_json``; ``segments``: ``scene_schema`` (int, null). Nothing in the knowledge tables changes.

Idempotent: every column is checked against the live table before ``ADD COLUMN`` (re-runnable on a partially
migrated copy) and the backfill only reads ``scene_json``, so running it twice gives the same rows.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _scene(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _schema_of(scene: dict | None) -> int | None:
    v = scene.get("schema_version") if scene else None
    return v if isinstance(v, int) else None


def upgrade() -> None:
    _add("model_versions", sa.Column("scene_schema", sa.Integer, nullable=False, server_default="2"))
    _add("model_versions", sa.Column("compiled_by", sa.String(64), nullable=True))
    _add("model_versions", sa.Column("gaps_json", sa.JSON, nullable=True))
    _add("model_versions", sa.Column("export_json", sa.JSON, nullable=True))
    _add("segments", sa.Column("scene_schema", sa.Integer, nullable=True))

    bind = op.get_bind()
    mv = sa.table("model_versions", sa.column("id", sa.String), sa.column("scene_json", sa.JSON),
                  sa.column("scene_schema", sa.Integer), sa.column("compiled_by", sa.String))
    for row in bind.execute(sa.select(mv.c.id, mv.c.scene_json)).all():
        scene = _scene(row[1])
        schema = _schema_of(scene)
        model_ref = scene.get("model") if scene else None
        compiled_by = model_ref.get("compiled_by") if isinstance(model_ref, dict) else None
        values: dict = {"scene_schema": schema if schema is not None else 2}
        if compiled_by:
            values["compiled_by"] = str(compiled_by)[:64]
        bind.execute(mv.update().where(mv.c.id == row[0]).values(**values))

    seg = sa.table("segments", sa.column("id", sa.String), sa.column("scene_json", sa.JSON),
                   sa.column("scene_schema", sa.Integer))
    for row in bind.execute(sa.select(seg.c.id, seg.c.scene_json)).all():
        bind.execute(seg.update().where(seg.c.id == row[0]).values(scene_schema=_schema_of(_scene(row[1]))))


def downgrade() -> None:
    with op.batch_alter_table("segments") as batch:
        if "scene_schema" in _columns("segments"):
            batch.drop_column("scene_schema")
    with op.batch_alter_table("model_versions") as batch:
        cols = _columns("model_versions")
        for name in ("export_json", "gaps_json", "compiled_by", "scene_schema"):
            if name in cols:
                batch.drop_column(name)
