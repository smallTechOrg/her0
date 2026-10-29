"""Shared fixtures.

- ``test_db`` (session): points HERO_DATABASE_URL at HERO_TEST_DATABASE_URL, runs
  ``alembic upgrade head``, yields, then drops the file. Settings + engine are reset.
- ``client``: FastAPI TestClient on a fresh app bound to the test DB.
- ``live_server`` (session): uvicorn on port 8712 (subprocess) against the test DB; yields
  the base URL. Used by tests/e2e (Playwright) and any HTTP-level live test.
- ``generated_segment`` (session) - PLACEHOLDER SEAM: S2-pipeline implements it here
  (runs ``run_pipeline`` once against the test DB and returns the ready segment's slug).
  Until then the name is defined below and fails loudly if used.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config

from hero.config import PROJECT_ROOT, get_settings, reset_settings
from hero.db.session import reset_engine

LIVE_PORT = 8712


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src" / "hero" / "db" / "migrations"))
    return cfg


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    return Path(url[len(prefix):]) if url.startswith(prefix) else None


@pytest.fixture(scope="session")
def test_db() -> str:
    reset_settings()
    base = get_settings()
    test_url = base.resolve_database_url(base.test_database_url)
    assert test_url != base.resolve_database_url(), "HERO_TEST_DATABASE_URL must differ from HERO_DATABASE_URL"
    path = _sqlite_path(test_url)
    if path is not None:
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            p.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["HERO_DATABASE_URL"] = test_url
    reset_settings()
    reset_engine()
    command.upgrade(_alembic_config(), "head")
    yield test_url
    reset_engine()
    reset_settings()
    os.environ.pop("HERO_DATABASE_URL", None)
    if path is not None:
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            p.unlink(missing_ok=True)


@pytest.fixture()
def client(test_db):
    from fastapi.testclient import TestClient

    from hero.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def live_server(test_db: str):
    """uvicorn on 127.0.0.1:8712 against the test DB; yields the base URL."""
    env = {**os.environ, "HERO_DATABASE_URL": test_db, "HERO_HOST": "127.0.0.1", "HERO_PORT": str(LIVE_PORT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hero.main:app", "--host", "127.0.0.1", "--port", str(LIVE_PORT)],
        cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{LIVE_PORT}"
    deadline = time.time() + 30
    try:
        while True:
            try:
                if httpx.get(base_url + "/api/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(f"live server exited early with code {proc.returncode}")
            if time.time() > deadline:
                raise RuntimeError("live server did not become healthy within 30 s")
            time.sleep(0.25)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def generated_segment(test_db: str) -> str:
    """SEAM for S2-pipeline: replace this body with one real ``run_pipeline`` against the
    seed segment on the test DB and return the ready segment slug. Reused by S3/S4/S5 tests."""
    raise NotImplementedError("generated_segment is implemented by slice S2-pipeline in tests/conftest.py")
