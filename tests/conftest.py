"""Shared fixtures (Phase 2).

- ``free_port`` (session): a free TCP port from ``socket.bind(("127.0.0.1", 0))``; never 8711 (dev server).
- ``test_db`` (session): a PER-PROCESS SQLite file ``data/hero_test_<pid>.db`` derived from
  ``HERO_TEST_DATABASE_URL`` (so concurrent slices / sessions never share a file), ``alembic upgrade head``
  applied, ``HERO_DATABASE_URL`` pointed at it; the file (+ WAL/SHM) is removed at session end.
- ``client``: FastAPI TestClient on a fresh app bound to the test DB.
- ``live_server`` (session): uvicorn subprocess on ``free_port`` against the test DB; yields the base URL.
  Used by tests/e2e (Playwright) and any HTTP-level live test.
- ``seeded_fixture_segment`` (session): the recorded fixture street (``fixture-curved-street``) seeded into the
  test DB by ``hero.tools.load_fixture_street.seed_fixture_segment`` - no spend. Yields the slug. Skips (with a
  reason) until the S1d fixture directory exists.
- ``seed_fixture`` (session): the seeding callable ``(slug=..., up_to_version=...) -> SegmentRecord`` for tests
  that need a street at a specific version or under their own slug.

``generated_segment`` (the real-generation fixture) lives in ``tests/live_gate.py``, registered below as a plugin so it
resolves from tests/live AND from the live-marked tests under tests/e2e (it is only ever instantiated by a live test).
The integration and e2e tiers otherwise use ``seeded_fixture_segment``.

Live gating: ``live``-marked tests are SKIPPED unless ``-m`` selects live explicitly or an argument path is in tests/live
(the gate runs ``uv run pytest tests/live -q -s``). So a bare ``uv run pytest`` never spends money.

``pytest -m live`` selects the tests that need the network and real keys (marker registered in pyproject.toml).
The repo root is put on ``sys.path`` so ``from tests.fixtures.street_fixtures import ...`` works.
"""

from __future__ import annotations

import os
import socket
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

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEV_PORT = 8711

pytest_plugins = ["tests.live_gate"]


def live_requested(config: pytest.Config) -> bool:
    """True when the invocation explicitly asks for live tests: ``-m`` mentions ``live`` (and not only ``not live``)
    or a command-line path points into ``tests/live``."""
    expr = (config.getoption("markexpr", default="") or "").replace("not live", "")
    if "live" in expr:
        return True
    live_dir = (PROJECT_ROOT / "tests" / "live").resolve()
    for arg in config.invocation_params.args:
        path = Path(str(arg).split("::", 1)[0])
        path = (config.invocation_params.dir / path).resolve() if not path.is_absolute() else path.resolve()
        if path == live_dir or live_dir in path.parents:
            return True
    return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if live_requested(config):
        return
    skip = pytest.mark.skip(reason="live test (real network/keys): run `uv run pytest tests/live` or `-m live`")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src" / "hero" / "db" / "migrations"))
    return cfg


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    return Path(url[len(prefix):]) if url.startswith(prefix) else None


def per_process_url(base_url: str, pid: int | None = None) -> str:
    """``sqlite:///data/hero_test.db`` -> ``sqlite:///data/hero_test_<pid>.db`` (non-SQLite URLs unchanged)."""
    pid = os.getpid() if pid is None else pid
    prefix = "sqlite:///"
    if not base_url.startswith(prefix):
        return base_url
    p = Path(base_url[len(prefix):])
    return prefix + str(p.with_name(f"{p.stem}_{pid}{p.suffix}"))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port != DEV_PORT
    return port


def _unlink_db(path: Path | None) -> None:
    if path is None:
        return
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        p.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def free_port() -> int:
    return find_free_port()


@pytest.fixture(scope="session")
def test_db() -> str:
    reset_settings()
    base = get_settings()
    test_url = base.resolve_database_url(per_process_url(base.test_database_url))
    assert test_url != base.resolve_database_url(), "HERO_TEST_DATABASE_URL must differ from HERO_DATABASE_URL"
    path = _sqlite_path(test_url)
    _unlink_db(path)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("HERO_DATABASE_URL")
    os.environ["HERO_DATABASE_URL"] = test_url
    reset_settings()
    reset_engine()
    command.upgrade(_alembic_config(), "head")
    try:
        yield test_url
    finally:
        reset_engine()
        if previous is None:
            os.environ.pop("HERO_DATABASE_URL", None)
        else:
            os.environ["HERO_DATABASE_URL"] = previous
        reset_settings()
        _unlink_db(path)


@pytest.fixture()
def client(test_db):
    from fastapi.testclient import TestClient

    from hero.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def live_server(test_db: str, free_port: int):
    """uvicorn on 127.0.0.1:<free port> against the test DB; yields the base URL."""
    env = {**os.environ, "HERO_DATABASE_URL": test_db, "HERO_HOST": "127.0.0.1", "HERO_PORT": str(free_port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hero.main:app", "--host", "127.0.0.1", "--port", str(free_port)],
        cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{free_port}"
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
def seed_fixture(test_db: str):
    """``seed(slug="fixture-curved-street", up_to_version=None) -> SegmentRecord``; skips until the fixture exists."""
    from hero.tools.load_fixture_street import FixtureMissingError, seed_fixture_segment

    def seed(slug: str = "fixture-curved-street", *, up_to_version: int | None = None):
        try:
            return seed_fixture_segment(slug, up_to_version=up_to_version)
        except FixtureMissingError as exc:
            pytest.skip(f"model fixture not generated yet (slice S1d): {exc}")

    return seed


@pytest.fixture(scope="session")
def seeded_fixture_segment(seed_fixture) -> str:
    """Slug of the fully seeded fixture street (head version = newest fixture version)."""
    return seed_fixture().slug
