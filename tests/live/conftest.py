"""Phase-2 live gate: the ``live_server`` override (log to docs/evidence). The ``admin``/``demo_street``/``gate``/
``generated_segment`` fixtures and helpers live in ``tests/live_gate.py`` (a pytest plugin, see tests/conftest.py).

Phase-2 live gate fixtures (slice S7). REAL spend: the orchestrator runs ``uv run pytest tests/live -q -s``.

Session fixtures, in dependency order:

- ``live_server`` (overrides tests/conftest.py): the same uvicorn subprocess on a free port against the per-process test
  DB, but its stdout (the JSON log lines) goes to ``docs/evidence/phase2-server.log`` so the observability gate can tail it.
- ``admin``: an ``httpx.Client`` with the admin password header against ``live_server``.
- ``demo_street``: creates the 100 Feet Road demo street through the address flow (``hero.tools.create_demo_street``),
  saves ``docs/evidence/placement.png`` and asserts the placement criteria BEFORE any generation can start.
- ``gate``: exactly two real full generations (run 1 forward+back, run 2 +left/right) and one free no-op run, with a
  module-level counter (``LLM_RUNS``) that aborts the session if a third LLM-calling run is attempted. Collects
  everything the assertions need and writes ``docs/evidence/phase2-gate.json``.
- ``generated_segment``: the slug of the v2 street after the generations (used by tests/e2e/*_live.py when they are
  run together with tests/live, e.g. ``pytest tests/live tests/e2e -m live``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

from hero.config import PROJECT_ROOT
from tests.live_gate import (ADMIN_HEADER, EVIDENCE_DIR, GATE_JSON, MAX_LLM_RUNS, PLACEMENT_PNG, RUN_CAP_USD,  # noqa: F401
                             SERVER_LOG, TOTAL_CAP_USD, angle_from_north, data)


@pytest.fixture(scope="session")
def live_server(test_db: str, free_port: int):
    """uvicorn on 127.0.0.1:<free port> against the test DB; stdout (JSON log lines) -> ``docs/evidence/phase2-server.log``."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HERO_DATABASE_URL": test_db, "HERO_HOST": "127.0.0.1", "HERO_PORT": str(free_port),
           "PYTHONUNBUFFERED": "1"}
    with SERVER_LOG.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "hero.main:app", "--host", "127.0.0.1", "--port", str(free_port)],
            cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
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
