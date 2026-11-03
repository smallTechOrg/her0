"""Alembic 0003_scene_schema: fresh DB, in-place upgrade of a 0002 DB (copy of the dev DB shape), idempotency."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from hero.config import PROJECT_ROOT, reset_settings
from hero.db.base import Base
from hero.db.session import reset_engine
from tests.conftest import _alembic_config

MV_COLS = {"scene_schema", "compiled_by", "gaps_json", "export_json"}


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("HERO_DATABASE_URL", url)
    reset_settings()
    reset_engine()
    yield url
    reset_engine()
    monkeypatch.undo()
    reset_settings()


def _cols(url: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(create_engine(url)).get_columns(table)}


def _seed_0002(path: Path) -> None:
    """Rows as an existing dev DB has them: a v2-scene segment with one model version, a v1 legacy segment."""
    v2 = json.dumps({"schema_version": 2, "model": {"version": 1, "model_hash": "h", "compiled_by": "hero.model.compile/1"}})
    with sqlite3.connect(path) as con:
        for sid, slug, scene in (("s1", "demo", v2), ("s2", "legacy", json.dumps({"schema_version": 1})), ("s3", "empty", None)):
            con.execute("insert into segments (id, slug, name, city, start_lat, start_lon, end_lat, end_lon, footpath_side,"
                        " length_m, heading_deg, status, scene_json, created_at, updated_at)"
                        " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, slug, slug, "Bengaluru", 12.97, 77.64, 12.971, 77.641, "left", 75.0, 6.7,
                         "ready" if scene else "draft", scene, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        con.execute("insert into model_versions (id, segment_id, version, kind, model_json, scene_json, fidelity_json, summary,"
                    " model_hash, entity_count, observation_count, fidelity_level, created_at)"
                    " values ('m1','s1',1,'fused','{}',?, '{}', 'x', ?, 0, 0, 1, '2026-01-01 00:00:00')", (v2, "a" * 64))
        con.execute("insert into model_versions (id, segment_id, version, kind, model_json, scene_json, fidelity_json, summary,"
                    " model_hash, entity_count, observation_count, fidelity_level, created_at)"
                    " values ('m0','s2',0,'legacy','{}',?, '{}', 'x', ?, 0, 0, 0, '2026-01-01 00:00:00')",
                    (json.dumps({"schema_version": 1}), "b" * 64))
        con.execute("insert into model_versions (id, segment_id, version, kind, model_json, scene_json, fidelity_json, summary,"
                    " model_hash, entity_count, observation_count, fidelity_level, created_at)"
                    " values ('m3','s3',0,'prior','{}',NULL, '{}', 'x', ?, 0, 0, 0, '2026-01-01 00:00:00')", ("c" * 64,))


def test_head_revision_is_0003():
    assert ScriptDirectory.from_config(_alembic_config()).get_current_head() == "0003"


def test_fresh_database_upgrades_to_0003_with_the_derived_columns(db_url, tmp_path):
    command.upgrade(_alembic_config(), "head")
    assert MV_COLS <= _cols(db_url, "model_versions")
    assert "scene_schema" in _cols(db_url, "segments")
    with sqlite3.connect(tmp_path / "mig.db") as con:
        assert con.execute("select version_num from alembic_version").fetchone()[0] == "0003"


def test_upgrade_of_a_0002_database_backfills_schema_and_compiled_by(db_url, tmp_path):
    path = tmp_path / "mig.db"
    command.upgrade(_alembic_config(), "0002")
    _seed_0002(path)
    command.upgrade(_alembic_config(), "head")
    with sqlite3.connect(path) as con:
        mv = dict((r[0], r[1:]) for r in con.execute("select id, scene_schema, compiled_by, gaps_json, export_json from model_versions"))
        seg = dict(con.execute("select slug, scene_schema from segments").fetchall())
    assert mv["m1"] == (2, "hero.model.compile/1", None, None)
    assert mv["m0"] == (1, None, None, None)
    assert mv["m3"] == (2, None, None, None)  # no scene: the column default
    assert seg == {"demo": 2, "legacy": 1, "empty": None}


def test_upgrade_is_idempotent_on_a_copy_and_on_a_partially_migrated_db(db_url, tmp_path, monkeypatch):
    path = tmp_path / "mig.db"
    command.upgrade(_alembic_config(), "0002")
    _seed_0002(path)
    command.upgrade(_alembic_config(), "head")
    copy = tmp_path / "copy.db"
    shutil.copy(path, copy)
    # simulate a half-applied upgrade: version stamp rolled back although the columns exist
    with sqlite3.connect(copy) as con:
        con.execute("update alembic_version set version_num='0002'")
    monkeypatch.setenv("HERO_DATABASE_URL", f"sqlite:///{copy}")
    reset_settings()
    reset_engine()
    command.upgrade(_alembic_config(), "head")
    command.upgrade(_alembic_config(), "head")  # second run is a no-op
    with sqlite3.connect(copy) as con:
        assert con.execute("select version_num from alembic_version").fetchone()[0] == "0003"
        assert con.execute("select scene_schema, compiled_by from model_versions where id='m1'").fetchone() == (
            2, "hero.model.compile/1")
        assert con.execute("select count(*) from model_versions").fetchone()[0] == 3


def test_a_copy_of_the_real_dev_database_upgrades_twice(db_url, tmp_path, monkeypatch):
    src = PROJECT_ROOT / "data" / "hero.db"
    if not src.exists():
        pytest.skip("no data/hero.db in this checkout")
    copy = tmp_path / "devcopy.db"
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as a, sqlite3.connect(copy) as b:
        a.backup(b)  # consistent snapshot; the source is only read
    monkeypatch.setenv("HERO_DATABASE_URL", f"sqlite:///{copy}")
    reset_settings()
    reset_engine()
    with sqlite3.connect(copy) as con:
        before = con.execute("select count(*) from segments").fetchone()[0]
    command.upgrade(_alembic_config(), "head")
    command.upgrade(_alembic_config(), "head")
    with sqlite3.connect(copy) as con:
        assert con.execute("select version_num from alembic_version").fetchone()[0] == "0003"
        assert con.execute("select count(*) from segments").fetchone()[0] == before
        stale = con.execute("select count(*) from model_versions where scene_schema is null").fetchone()[0]
        assert stale == 0


def test_orm_metadata_matches_the_migrated_schema(db_url):
    import hero.db.models  # noqa: F401

    command.upgrade(_alembic_config(), "head")
    with create_engine(db_url).connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert [d for d in diffs if d[0] not in ("modify_default",)] == []


def test_downgrade_to_0002_drops_only_the_derived_columns(db_url):
    command.upgrade(_alembic_config(), "head")
    command.downgrade(_alembic_config(), "0002")
    assert not (MV_COLS & _cols(db_url, "model_versions"))
    assert "scene_schema" not in _cols(db_url, "segments")
    assert {"model_json", "scene_json", "fidelity_json"} <= _cols(db_url, "model_versions")
