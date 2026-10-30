"""Alembic 0002_street_model: works on a fresh DB and as an in-place upgrade of a Phase-1 (0001) DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from hero.config import reset_settings
from hero.db.base import Base
from hero.db.session import reset_engine
from tests.conftest import _alembic_config

PHASE2_TABLES = {"observations", "model_versions", "observation_extractions", "run_reports"}
PHASE1_TABLES = {"segments", "pipeline_runs", "llm_calls", "google_requests", "game_runs"}


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


def _revision(path: Path) -> str:
    with sqlite3.connect(path) as con:
        return con.execute("select version_num from alembic_version").fetchone()[0]


def test_head_revision_is_0002():
    assert ScriptDirectory.from_config(_alembic_config()).get_current_head() == "0002"


def test_fresh_database_upgrades_to_0002_with_all_tables(db_url, tmp_path):
    command.upgrade(_alembic_config(), "head")
    assert _revision(tmp_path / "mig.db") == "0002"
    tables = set(inspect(create_engine(db_url)).get_table_names())
    assert PHASE1_TABLES | PHASE2_TABLES <= tables


def test_upgrade_of_a_phase1_database_keeps_rows_and_adds_tables(db_url, tmp_path):
    path = tmp_path / "mig.db"
    command.upgrade(_alembic_config(), "0001")
    assert _revision(path) == "0001"
    with sqlite3.connect(path) as con:
        con.execute("insert into segments (id, slug, name, city, start_lat, start_lon, end_lat, end_lon, footpath_side,"
                    " length_m, heading_deg, status, scene_json, best_score, created_at, updated_at)"
                    " values ('s1','legacy-w','Legacy','Bengaluru',12.974,77.6405,12.97467,77.64058,'left',75.0,6.7,"
                    "'ready','{\"schema_version\": 1}',420,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
        con.execute("insert into game_runs (id, segment_id, score, distance_m, tokens_collected, loops, duration_s, client,"
                    " created_at) values ('g1','s1',420,70.0,1,0,12.5,'ua','2026-01-02 00:00:00')")
    command.upgrade(_alembic_config(), "head")
    assert _revision(path) == "0002"
    with sqlite3.connect(path) as con:
        assert con.execute("select slug, best_score, scene_json from segments").fetchall() == [
            ("legacy-w", 420, '{"schema_version": 1}')]
        assert con.execute("select count(*) from game_runs").fetchone()[0] == 1
        assert con.execute("select count(*) from model_versions").fetchone()[0] == 0
    assert PHASE2_TABLES <= set(inspect(create_engine(db_url)).get_table_names())


def test_orm_metadata_matches_the_migrated_schema(db_url):
    import hero.db.models  # noqa: F401

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    significant = [d for d in diffs if d[0] not in ("modify_default",)]
    assert significant == [], significant


def test_unique_constraints_exist_on_observations_and_versions(db_url):
    command.upgrade(_alembic_config(), "head")
    insp = inspect(create_engine(db_url))
    obs_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("observations")]
    ver_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("model_versions")]
    assert ("segment_id", "obs_key") in obs_uniques
    assert ("segment_id", "version") in ver_uniques


def test_downgrade_removes_only_the_phase2_tables(db_url):
    command.upgrade(_alembic_config(), "head")
    command.downgrade(_alembic_config(), "0001")
    tables = set(inspect(create_engine(db_url)).get_table_names())
    assert PHASE1_TABLES <= tables and not (PHASE2_TABLES & tables)
