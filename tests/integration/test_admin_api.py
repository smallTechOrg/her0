"""/api/admin/* over the real app + test DB (spec/api.md -> ## Admin). Uses the session's one
real generation for the run-detail shape; never starts a second LLM run."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import RunUpdate
from hero.scene.schema import RunStatus

SEED = {"name": "100 Feet Road, Indiranagar (west footpath)", "city": "Bengaluru",
        "start": {"lat": 12.97400, "lon": 77.64050}, "end": {"lat": 12.97467, "lon": 77.64058}, "footpath_side": "left"}


class _AdminAuth(dict):
    """Admin header dict whose repr never leaks the password (pytest prints fixture values on failure)."""

    def __repr__(self) -> str:  # pragma: no cover - only shown in tracebacks
        return "<admin auth headers (redacted)>"

    __str__ = __repr__


@pytest.fixture()
def auth() -> dict[str, str]:
    return _AdminAuth({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})


# ---------------------------------------------------------------- auth / status


def test_status_without_password_is_401_unauthorized(client):
    resp = client.get("/api/admin/status")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers["X-Request-Id"]


def test_status_with_wrong_password_is_401_unauthorized(client):
    resp = client.get("/api/admin/status", headers={"X-Hero-Admin-Password": "definitely-wrong"})
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


def test_status_reports_spend_runs_google_models_and_seed(client, auth, generated_segment):
    resp = client.get("/api/admin/status", headers=auth)

    assert resp.status_code == 200
    data = resp.json()["data"]
    settings = get_settings()
    assert data["budget_usd"] == settings.claude_budget_usd
    assert data["spent_usd"] > 0 and data["remaining_usd"] == pytest.approx(max(0.0, data["budget_usd"] - data["spent_usd"]))
    assert data["run_budget_usd"] == settings.run_budget_usd
    assert data["runs"]["total"] >= 1 and data["runs"]["succeeded"] >= 1
    assert set(data["runs"]) == {"total", "succeeded", "failed", "failed_budget"}
    assert data["google"]["requests"] + data["google"]["cache_hits"] >= 1
    assert data["models"] == {"vision": settings.vision_model, "review": settings.review_model}
    assert data["streetview_available_last"] in (True, False)
    assert data["seed_segment"]["name"] == SEED["name"]


# ---------------------------------------------------------------- segments


def test_list_segments_includes_the_generated_segment_with_its_latest_run(client, auth, generated_segment):
    resp = client.get("/api/admin/segments", headers=auth)

    assert resp.status_code == 200
    rows = resp.json()["data"]
    seg = next(r for r in rows if r["slug"] == generated_segment)
    assert seg["status"] == "ready" and seg["quality"] in ("full", "satellite_only")
    assert seg["latest_run"]["status"] in ("succeeded", "failed", "failed_budget")  # newest run, any outcome
    assert seg["latest_run_id"] and repo.require_run(seg["latest_run_id"]).status is RunStatus.succeeded
    assert seg["thumbnail_url"].startswith("/media/previews/")
    assert seg["walkability"]["score"] >= 0
    assert client.get(seg["thumbnail_url"]).status_code == 200


def test_create_segment_returns_201_with_derived_fields_and_unique_slug(client, auth):
    body = {**SEED, "name": f"Create Test {uuid.uuid4().hex[:6]} (west footpath)"}

    first = client.post("/api/admin/segments", json=body, headers=auth)
    second = client.post("/api/admin/segments", json=body, headers=auth)

    assert first.status_code == 201
    data = first.json()["data"]
    assert data["status"] == "draft" and 70 < data["length_m"] < 80 and data["latest_run"] is None
    assert data["footpath_side"] == "left" and data["start"] == SEED["start"]
    assert second.status_code == 201 and second.json()["data"]["slug"] == data["slug"] + "-2"


def test_create_segment_too_short_is_422_with_the_reason(client, auth):
    body = {**SEED, "name": "Too short", "end": {"lat": 12.97405, "lon": 77.64050}}

    resp = client.post("/api/admin/segments", json=body, headers=auth)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_error" and "30" in err["message"] and "150" in err["message"]


def test_create_segment_with_missing_field_is_422_validation_error(client, auth):
    resp = client.post("/api/admin/segments", json={"name": "x", "city": "y"}, headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------- generate


def test_generate_unknown_segment_is_404(client, auth):
    resp = client.post("/api/admin/segments/no-such-street/generate", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_generate_while_a_run_is_active_is_409_run_in_progress(client, auth, generated_segment, monkeypatch):
    """A run with a LIVE worker thread in this process blocks /generate with 409."""
    import threading

    from hero.pipeline import registry

    seg = repo.require_segment(generated_segment)
    blocker = repo.create_run(seg.id, {"test": "blocker"})
    repo.update_run(blocker.id, RunUpdate(status=RunStatus.running, started_at=datetime.now(UTC)))
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    monkeypatch.setitem(registry._threads, blocker.id, thread)
    try:
        resp = client.post(f"/api/admin/segments/{generated_segment}/generate", headers=auth)
        assert client.get("/api/admin/status", headers=auth).json()["data"]["active_run_id"] == blocker.id
    finally:
        release.set()
        thread.join(5)
        repo.update_run(blocker.id, RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                              error={"code": "test", "message": "blocker", "step": "test"}))

    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "run_in_progress" and err["detail"]["run_id"] == blocker.id
    assert client.get("/api/admin/status", headers=auth).json()["data"]["active_run_id"] is None


def test_orphan_run_without_a_worker_thread_is_retired_not_409(client, auth, generated_segment, monkeypatch):
    """A queued/running row with no live thread (server restarted mid-run) is marked failed at once and
    the next /generate proceeds to the budget check (402 here) instead of a 15-minute 409 lockout."""
    from hero.config import reset_settings

    seg = repo.require_segment(generated_segment)
    orphan = repo.create_run(seg.id, {"test": "orphan"})
    repo.update_run(orphan.id, RunUpdate(status=RunStatus.running, started_at=datetime.now(UTC)))
    runs_before = repo.run_stats().total
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0.000001")
    reset_settings()
    try:
        resp = client.post(f"/api/admin/segments/{generated_segment}/generate", headers=auth)
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()

    assert resp.status_code == 402 and resp.json()["error"]["code"] == "budget_exhausted"
    retired = repo.require_run(orphan.id)
    assert retired.status is RunStatus.failed and retired.finished_at is not None
    assert "restart" in retired.error["message"]
    assert any(s.status == "failed" and "restart" in (s.detail or "") for s in retired.steps)
    assert repo.run_stats().total == runs_before
    assert client.get("/api/admin/status", headers=auth).json()["data"]["active_run_id"] is None


def test_status_retires_a_queued_orphan_run(client, auth, generated_segment):
    seg = repo.require_segment(generated_segment)
    orphan = repo.create_run(seg.id, {"test": "queued-orphan"})
    assert repo.get_active_run().id == orphan.id

    data = client.get("/api/admin/status", headers=auth).json()["data"]

    assert data["active_run_id"] is None
    assert repo.require_run(orphan.id).status is RunStatus.failed


def test_generate_with_exhausted_budget_is_402_before_any_run_is_created(client, auth, generated_segment, monkeypatch):
    from hero.config import reset_settings

    runs_before = repo.run_stats().total
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0.000001")
    reset_settings()
    try:
        resp = client.post(f"/api/admin/segments/{generated_segment}/generate", headers=auth)
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()

    assert resp.status_code == 402
    err = resp.json()["error"]
    assert err["code"] == "budget_exhausted" and "HERO_CLAUDE_BUDGET_USD" in err["message"]
    assert err["detail"]["budget_usd"] == pytest.approx(0.000001)
    assert repo.run_stats().total == runs_before


# ---------------------------------------------------------------- runs


def test_run_detail_exposes_every_artefact_of_the_real_run(client, auth, generated_segment):
    seg = repo.require_segment(generated_segment)

    resp = client.get(f"/api/admin/runs/{seg.latest_run_id}", headers=auth)

    assert resp.status_code == 200
    run = resp.json()["data"]
    assert run["id"] == seg.latest_run_id and run["status"] == "succeeded"
    assert run["segment"] == {"slug": seg.slug, "name": seg.name}
    assert run["inputs"]["models"] and run["inputs"]["budgets"]["total_usd"] > 0
    names = [s["name"] for s in run["steps"]]
    assert names[:3] == ["resolve_geometry", "fetch_satellite", "verify_streetview"] and names[-1] == "finalize"
    assert all(s["latency_ms"] >= 0 and s["status"] in ("ok", "skipped", "failed") for s in run["steps"])
    sat = run["imagery"]["satellite"]
    assert sat["url"].startswith("/media/imagery/") and sat["annotated_url"].startswith("/media/previews/")
    assert sat["zoom"] == get_settings().satellite_zoom and sat["meters_per_px"] > 0
    assert client.get(sat["url"]).status_code == 200 and client.get(sat["annotated_url"]).status_code == 200
    for pano in run["imagery"]["streetview"]:
        assert set(pano) >= {"url", "x_m", "heading"} and client.get(pano["url"]).status_code == 200
    # verify_streetview ran in this succeeded run, so the flag is a real bool (null only before the check)
    assert isinstance(run["imagery"]["streetview_available"], bool) and isinstance(run["imagery"]["osm_available"], bool)
    assert run["layout"]["footpath_coverage"] and "road_width_m" in run["layout"]
    assert run["obstacles"] is None or "objects" in run["obstacles"]
    assert 1 <= len(run["iterations"]) <= get_settings().max_review_iterations
    it = run["iterations"][0]
    assert set(it) >= {"n", "preview_url", "accepted", "critique", "patch_ops", "applied"}
    assert client.get(it["preview_url"]).status_code == 200
    assert run["scene"]["schema_version"] == 1 and run["scene"]["generation"]["run_id"] == run["id"]
    assert client.get(run["preview_url"]).status_code == 200
    cost = run["cost"]
    assert cost["cost_usd"] > 0 and cost["input_tokens"] > 0 and 2 <= len(cost["calls"]) <= 4
    assert set(cost["calls"][0]) >= {"step", "model", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "ok"}
    assert cost["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in cost["calls"]), abs=1e-6)
    assert run["google"]["requests"] + run["google"]["cache_hits"] <= 6
    assert run["error"] is None
    assert run["budget"]["spent_total_usd"] >= cost["cost_usd"] and run["budget"]["budget_usd"] > 0


def test_run_detail_unknown_id_is_404(client, auth):
    resp = client.get("/api/admin/runs/not-a-run", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_recent_runs_list_is_newest_first_with_segment_and_duration(client, auth, generated_segment):
    resp = client.get("/api/admin/runs?limit=5", headers=auth)

    assert resp.status_code == 200
    runs = resp.json()["data"]
    assert 1 <= len(runs) <= 5
    created = [r["created_at"] for r in runs]
    assert created == sorted(created, reverse=True)
    done = repo.require_segment(generated_segment).latest_run_id
    rows = client.get("/api/admin/runs?limit=100", headers=auth).json()["data"]
    done_row = next(r for r in rows if r["id"] == done)
    assert done_row["status"] == "succeeded" and done_row["segment"]["slug"] == generated_segment
    assert done_row["duration_s"] > 0 and done_row["cost_usd"] > 0


def test_recent_runs_rejects_out_of_range_limit(client, auth):
    resp = client.get("/api/admin/runs?limit=0", headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
