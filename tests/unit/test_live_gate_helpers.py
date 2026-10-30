"""Mock-only checks of the Phase-2 live-gate helpers (no network, no spend)."""

from __future__ import annotations

import httpx
import pytest
from _pytest.outcomes import Exit

from tests import live_gate as lc


def _admin(calls: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"path": request.url.path, "body": request.content})
        return httpx.Response(202, json={"ok": True, "data": {"run_id": f"run-{len(calls)}", "status": "queued"}})

    return httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))


def test_third_llm_calling_run_aborts_the_session(monkeypatch):
    monkeypatch.setattr(lc, "LLM_RUNS", 0)
    calls: list[dict] = []
    with _admin(calls) as admin:
        assert lc.start_generation(admin, "s", lc.RUN1_IMAGERY, llm_expected=True) == "run-1"
        assert lc.start_generation(admin, "s", lc.RUN2_IMAGERY, llm_expected=True) == "run-2"
        # the free no-op run does not count against the limit
        assert lc.start_generation(admin, "s", lc.RUN2_IMAGERY, llm_expected=False, run_budget_usd=0.05) == "run-3"
        with pytest.raises(Exit):
            lc.start_generation(admin, "s", lc.RUN2_IMAGERY, llm_expected=True)
    assert len(calls) == 3, "the aborted third run must never reach the API"
    assert b'"run_budget_usd":0.05' in calls[2]["body"].replace(b" ", b"")


def test_mean_bearing_and_angle_from_north():
    north = [{"lat": 12.9700, "lon": 77.6400}, {"lat": 12.9710, "lon": 77.6400}]
    assert lc.mean_bearing_deg(north) == pytest.approx(0.0, abs=0.01)
    east = [{"lat": 12.97, "lon": 77.640}, {"lat": 12.97, "lon": 77.641}]
    assert lc.mean_bearing_deg(east) == pytest.approx(90.0, abs=0.01)
    slightly_west = [{"lat": 12.97, "lon": 77.640}, {"lat": 12.971, "lon": 77.6399}]
    assert lc.angle_from_north(lc.mean_bearing_deg(slightly_west)) < 10
    assert lc.angle_from_north(350.0) == 10.0


def test_calibration_table_is_serialisable():
    import json

    table = lc.calibration_table()
    json.dumps(table)
    assert table["confidence"][0]["raw"] == 0.0 and table["range_weight"][0]["weight"] == 1.0
