"""``create_demo_street --db``: the in-process API is pointed at a chosen SQLite file (mocked client, no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hero.config import get_settings, reset_settings
from hero.db.session import reset_engine
from hero.tools import create_demo_street as tool


@pytest.fixture()
def restore_env(monkeypatch):
    monkeypatch.setenv("HERO_DATABASE_URL", get_settings().database_url)  # monkeypatch restores it afterwards
    yield
    reset_engine()
    reset_settings()


def test_use_database_creates_and_migrates_the_file(tmp_path: Path, restore_env):
    db = tmp_path / "sub" / "demo.db"
    url = tool.use_database(db)
    assert url == f"sqlite:///{db.resolve()}"
    assert get_settings().resolve_database_url() == url
    with sqlite3.connect(db) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert {"segments", "model_versions", "observations"} <= tables
        assert con.execute("select version_num from alembic_version").fetchone()[0].startswith("0002")


def test_main_with_db_uses_that_file_and_the_client(tmp_path: Path, monkeypatch, restore_env, capsys):
    db = tmp_path / "demo.db"
    seen: dict = {}

    def fake_create(client, *, out_path, say=print):
        seen["db_url"] = get_settings().resolve_database_url()
        seen["out"] = out_path
        return tool.DemoResult(slug="demo", created=True, image_path=out_path, placement={"placement_verified": True})

    monkeypatch.setattr(tool, "_default_client", lambda: object())
    monkeypatch.setattr(tool, "create_demo_street", fake_create)
    assert tool.main(["--db", str(db), "--out", str(tmp_path / "p.png")]) == 0
    assert seen["db_url"] == f"sqlite:///{db.resolve()}" and db.exists()
    assert "demo street 'demo'" in capsys.readouterr().out


def test_db_and_base_url_are_mutually_exclusive(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        tool.main(["--db", str(tmp_path / "x.db"), "--base-url", "http://127.0.0.1:1"])
    assert exc.value.code == 2
    assert not (tmp_path / "x.db").exists()
