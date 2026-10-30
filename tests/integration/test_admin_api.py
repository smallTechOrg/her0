"""/api/admin/* over the real app + test DB (spec/api.md -> ## Admin).

No Anthropic spend: the street comes from ``seeded_fixture_segment`` (the recorded fixture), and the generate flow
runs ``hero.fusion`` over the recorded Google transport with ``ReplayLLM`` answers (the worker's ``run_fusion`` is
swapped for a replay-backed one, everything else - endpoint, registry, single-flight, DB, run detail - is real).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.repo import RunUpdate
from hero.scene.schema import RunStatus
from tests.fixtures.google.fake_google import make_imagery
from tests.integration.test_fusion_run import FORWARD_BACK, PricedReplay, new_world

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


def test_status_reports_spend_runs_google_models_limits_seed_and_demo_street(client, auth, seeded_fixture_segment):
    resp = client.get("/api/admin/status", headers=auth)

    assert resp.status_code == 200
    data = resp.json()["data"]
    settings = get_settings()
    assert data["budget_usd"] == settings.claude_budget_usd
    assert data["spent_usd"] >= 0 and data["remaining_usd"] == pytest.approx(max(0.0, data["budget_usd"] - data["spent_usd"]))
    assert "run_budget_usd" not in data
    assert data["runs"]["total"] >= 3 and data["runs"]["succeeded"] >= 3
    assert set(data["runs"]) == {"total", "succeeded", "failed", "failed_budget"}
    assert set(data["google"]) == {"requests", "cache_hits", "est_cost_usd"}
    assert data["models"] == {"vision": settings.vision_model, "review": settings.review_model}
    assert data["streetview_available_last"] in (True, False, None)
    assert data["seed_segment"]["name"] == SEED["name"]
    assert data["limits"] == {
        "max_images_per_run": settings.max_images_per_run,
        "sv_spacing_m": settings.sv_spacing_m, "headings": settings.sv_headings_list,
        "sat_crop_zoom": settings.sat_crop_zoom, "sat_crop_spacing_m": settings.sat_crop_spacing_m,
        "batch_images": settings.perception_batch_images, "structured_mode": settings.structured_mode}
    demo = data["demo_street"]
    assert demo["query"] == "100 Feet Road, Indiranagar" and demo["city"] == "Bengaluru"
    assert demo["name"] == "100 Feet Road, Indiranagar, canonical demo (west footpath)"
    assert demo["hint"] == {"lat": 12.9711, "lon": 77.6413} and demo["half_length_m"] == 50
    assert demo["footpath_side"] == "left"


# ---------------------------------------------------------------- segments


def test_list_segments_includes_the_seeded_street_with_its_model_block_and_latest_run(client, auth, seeded_fixture_segment):
    resp = client.get("/api/admin/segments", headers=auth)

    assert resp.status_code == 200
    rows = resp.json()["data"]
    seg = next(r for r in rows if r["slug"] == seeded_fixture_segment)
    assert seg["status"] == "ready" and seg["quality"] in ("full", "satellite_only", "generic")
    assert seg["latest_run"]["status"] == "succeeded" and seg["latest_run"]["mode"] == "generate"
    assert seg["latest_run"]["version_created"] in (True, False)
    assert seg["walkability"]["score"] >= 0
    head = model_repo.head_version(repo.require_segment(seeded_fixture_segment).id)
    assert seg["model"] == {"head_version": head, "fidelity_level": "L2", "legacy": False,
                            "photos": seg["model"]["photos"], "placement_verified": True}
    assert seg["model"]["photos"] > 10


def test_segment_without_a_model_has_a_null_model_block_and_listing_does_not_import_legacy(client, auth):
    body = {**SEED, "name": f"No Model {uuid.uuid4().hex[:6]} (west footpath)"}
    created = client.post("/api/admin/segments", json=body, headers=auth).json()["data"]
    assert created["model"] is None

    rows = client.get("/api/admin/segments", headers=auth).json()["data"]
    row = next(r for r in rows if r["slug"] == created["slug"])
    assert row["model"] is None and row["status"] == "draft"
    assert model_repo.head_version(created["id"]) is None


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
    assert err["code"] == "validation_error" and "30" in err["message"] and "200" in err["message"]


def test_create_segment_with_missing_field_is_422_validation_error(client, auth):
    resp = client.post("/api/admin/segments", json={"name": "x", "city": "y"}, headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------- generate


def test_generate_unknown_segment_is_404(client, auth):
    resp = client.post("/api/admin/segments/no-such-street/generate", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_generate_while_a_run_is_active_is_409_run_in_progress(client, auth, seeded_fixture_segment, monkeypatch):
    """A run with a LIVE worker thread in this process blocks /generate with 409."""
    import threading

    from hero.pipeline import registry

    seg = repo.require_segment(seeded_fixture_segment)
    blocker = repo.create_run(seg.id, {"test": "blocker"})
    repo.update_run(blocker.id, RunUpdate(status=RunStatus.running, started_at=datetime.now(UTC)))
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    monkeypatch.setitem(registry._threads, blocker.id, thread)
    try:
        resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", headers=auth)
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


def test_orphan_run_without_a_worker_thread_is_retired_not_409(client, auth, seeded_fixture_segment, monkeypatch):
    """A queued/running row with no live thread (server restarted mid-run) is marked failed at once and
    the next /generate proceeds to the budget check (402 here) instead of a 15-minute 409 lockout."""
    from hero.config import reset_settings

    seg = repo.require_segment(seeded_fixture_segment)
    orphan = repo.create_run(seg.id, {"test": "orphan"})
    repo.update_run(orphan.id, RunUpdate(status=RunStatus.running, started_at=datetime.now(UTC)))
    runs_before = repo.run_stats().total
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0")
    reset_settings()
    try:
        resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", headers=auth)
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


def test_status_retires_a_queued_orphan_run(client, auth, seeded_fixture_segment):
    seg = repo.require_segment(seeded_fixture_segment)
    orphan = repo.create_run(seg.id, {"test": "queued-orphan"})
    assert repo.get_active_run().id == orphan.id

    data = client.get("/api/admin/status", headers=auth).json()["data"]

    assert data["active_run_id"] is None
    assert repo.require_run(orphan.id).status is RunStatus.failed


def test_generate_with_exhausted_budget_is_402_before_any_run_is_created(client, auth, seeded_fixture_segment, monkeypatch):
    from hero.config import reset_settings

    runs_before = repo.run_stats().total
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0")
    reset_settings()
    try:
        resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", headers=auth)
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()

    assert resp.status_code == 402
    err = resp.json()["error"]
    assert err["code"] == "budget_exhausted" and "HERO_CLAUDE_BUDGET_USD" in err["message"]
    assert err["detail"]["budget_usd"] == 0.0
    assert repo.run_stats().total == runs_before


# ---------------------------------------------------------------- generate: body, validation, refusals


class _Run:
    id, status = "run-123", type("S", (), {"value": "queued"})()


def _capture_start(monkeypatch):
    from hero.pipeline import registry

    seen: dict = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None):
        seen.update(slug=slug, mode=mode, overrides=plan_overrides)
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    return seen


def test_generate_without_a_body_starts_a_generate_run_with_all_defaults(client, auth, seeded_fixture_segment, monkeypatch):
    seen = _capture_start(monkeypatch)

    resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", headers=auth)

    assert resp.status_code == 202
    assert resp.json()["data"] == {"run_id": "run-123", "status": "queued", "mode": "generate"}
    assert seen == {"slug": seeded_fixture_segment, "mode": "generate", "overrides": {}}


def test_generate_forwards_the_imagery_overrides(client, auth, seeded_fixture_segment, monkeypatch):
    seen = _capture_start(monkeypatch)
    body = {"imagery": {"sv_spacing_m": 20, "headings": ["left", "right"], "sat_crop_zoom": 19, "max_images": 30,
                        "s_range": {"s_from": 10, "s_to": 60}}}

    resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", json=body, headers=auth)

    assert resp.status_code == 202
    assert seen["overrides"] == {"sv_spacing_m": 20.0, "headings": ["left", "right"], "sat_crop_zoom": 19,
                                 "max_images": 30, "s_range": {"s_from": 10.0, "s_to": 60.0}}


@pytest.mark.parametrize("body", [
    {"imagery": {"sv_spacing_m": 3}}, {"imagery": {"sv_spacing_m": 41}}, {"imagery": {"headings": []}},
    {"imagery": {"headings": ["up"]}}, {"imagery": {"sat_crop_zoom": 17}}, {"imagery": {"sat_crop_zoom": 22}},
    {"imagery": {"max_images": 0}}, {"imagery": {"max_images": 201}}, {"imagery": {"sat_crop_spacing_m": 0}},
    {"imagery": {"s_range": {"s_from": 50, "s_to": 10}}}, {"imagery": {"surprise": 1}},
    {"run_budget_usd": 0.5}, {"mystery": True}])
def test_generate_rejects_out_of_range_bodies_with_422_and_starts_nothing(client, auth, seeded_fixture_segment,
                                                                          monkeypatch, body):
    from hero.pipeline import registry

    monkeypatch.setattr(registry, "start_run", lambda *a, **k: pytest.fail("must not start a run"))
    runs_before = repo.run_stats().total

    resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/generate", json=body, headers=auth)

    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
    assert repo.run_stats().total == runs_before


def test_generate_unknown_segment_with_a_bad_body_is_404_not_422(client, auth):
    resp = client.post("/api/admin/segments/no-such-street/generate", json={"imagery": {"sv_spacing_m": 3}}, headers=auth)
    assert resp.status_code == 404


def test_imagery_acquire_run_is_not_refused_on_the_google_dollar_estimate(client, auth, seeded_fixture_segment,
                                                                         monkeypatch):
    seen = _capture_start(monkeypatch)

    resp = client.post(f"/api/admin/segments/{seeded_fixture_segment}/imagery", headers=auth,
                       json={"max_images": 200, "sv_spacing_m": 4})

    assert resp.status_code == 202 and seen["mode"] == "acquire"


# ---------------------------------------------------------------- generate: the whole flow over HTTP (replayed model)


@pytest.fixture()
def world(test_db, tmp_path, monkeypatch):
    w = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    yield w
    reset_settings()


@pytest.fixture()
def replay_worker(world, monkeypatch):
    """The registry's worker runs hero.fusion over the recorded Google transport with ReplayLLM answers."""
    from hero.fusion.runner import run_fusion
    from hero.pipeline import registry

    made: list[PricedReplay] = []

    def worker(run_id):
        _settings, google = make_imagery(world.tmp, world.fake, run_id)
        llm = world.llm()
        made.append(llm)
        return run_fusion(run_id, llm=llm, google=google, http=world.fake.client())

    monkeypatch.setattr(registry, "run_fusion", worker)
    return made


def _post_generate(client, auth, slug, body=None):
    from hero.pipeline import registry

    resp = client.post(f"/api/admin/segments/{slug}/generate", headers=auth, json=body) if body is not None else \
        client.post(f"/api/admin/segments/{slug}/generate", headers=auth)
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["data"]["run_id"]
    registry.wait_for(run_id, 120)
    return run_id, client.get(f"/api/admin/runs/{run_id}", headers=auth).json()["data"]


def test_generate_over_http_runs_fusion_and_the_run_detail_carries_the_phase_2_keys(world, client, auth, replay_worker):
    slug = world.seg.slug

    resp = client.post(f"/api/admin/segments/{slug}/generate", headers=auth, json={"imagery": FORWARD_BACK})
    assert resp.status_code == 202 and resp.json()["data"]["mode"] == "generate"
    run_id = resp.json()["data"]["run_id"]
    from hero.pipeline import registry

    registry.wait_for(run_id, 120)
    run = client.get(f"/api/admin/runs/{run_id}", headers=auth).json()["data"]

    assert run["status"] == "succeeded" and run["error"] is None and run["segment"]["slug"] == slug
    assert run["mode"] == "generate" and run["base_version"] == 0 and run["result_version"] == 1
    assert run["inputs"]["base_version"] is None, "queued before the v0 prior existed"
    assert run["version_created"] is True and run["report_available"] is True
    assert run["inputs"]["imagery_plan"]["headings"] == ["forward", "back"] and run["inputs"]["mode"] == "generate"
    names = [st["name"] for st in run["steps"]]
    assert names[0] == "load_base_model" and names[-1] == "report" and "critique_1" in names
    assert run["layout"] is None and run["obstacles"] is None
    img = run["imagery"]
    assert img["observations"] and all(o["new"] for o in img["observations"]) and img["planned"] > 10
    assert {"id", "kind", "view", "url", "station_s", "capture_date", "new"} <= set(img["observations"][0])
    assert img["satellite"]["url"].startswith("/media/imagery/") and img["satellite"]["zoom"] and img["satellite"]["cache_hit"] is False
    assert client.get(img["satellite"]["url"]).status_code == 200
    assert img["streetview"] and {"url", "x_m", "heading", "index"} <= set(img["streetview"][0])
    assert client.get(img["streetview"][0]["url"]).status_code == 200
    assert img["streetview_available"] is True and img["osm_available"] is True and img["plan_trimmed"] is False
    assert len(run["iterations"]) == 1 and client.get(run["iterations"][0]["preview_url"]).status_code == 200
    assert run["scene"]["schema_version"] == 2 and run["scene"]["model"]["version"] == 1
    assert client.get(run["thumbnail_url"]).status_code == 200 and run["preview_url"] == run["thumbnail_url"]
    cost = run["cost"]
    assert cost["cost_usd"] > 0 and len(cost["calls"]) == len(replay_worker[0].calls) >= 3
    assert cost["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in cost["calls"]), abs=1e-6)
    assert run["google"]["requests"] > 0 and run["budget"]["spent_total_usd"] >= cost["cost_usd"]

    status = client.get("/api/admin/status", headers=auth).json()["data"]
    assert status["active_run_id"] is None and status["spent_usd"] >= cost["cost_usd"]
    row = next(r for r in client.get("/api/admin/segments", headers=auth).json()["data"] if r["slug"] == slug)
    assert row["status"] == "ready" and row["model"]["head_version"] == 1 and row["model"]["legacy"] is False
    assert row["model"]["fidelity_level"] in ("L0", "L1") and row["latest_run"]["mode"] == "generate"
    assert row["latest_run"]["version_created"] is True and row["thumbnail_url"].startswith("/media/previews/")


def test_the_run_report_endpoint_serves_the_report_of_the_http_run(world, client, auth, replay_worker):
    run_id, run = _post_generate(client, auth, world.seg.slug, {"imagery": FORWARD_BACK})

    resp = client.get(f"/api/admin/runs/{run_id}/report", headers=auth)

    assert resp.status_code == 200
    rep = resp.json()["data"]
    assert rep["run_id"] == run_id and rep["mode"] == "generate" and rep["version_created"] is True
    assert rep["summary"] and rep["suggestions"] and rep["images"]["new"] == len(run["imagery"]["observations"])
    assert rep["cost"]["anthropic_usd"] == pytest.approx(run["cost"]["cost_usd"], abs=1e-6)


def test_a_second_identical_generate_over_http_is_a_free_noop_and_the_run_list_says_so(world, client, auth, replay_worker):
    slug = world.seg.slug
    _post_generate(client, auth, slug, {"imagery": FORWARD_BACK})
    calls_before = sum(len(w.calls) for w in replay_worker)

    run_id, run = _post_generate(client, auth, slug, {"imagery": FORWARD_BACK})

    assert run["status"] == "succeeded" and run["version_created"] is False and run["result_version"] == 1
    assert run["base_version"] == 1 and run["scene"] is None and run["cost"]["cost_usd"] == 0
    assert sum(len(w.calls) for w in replay_worker) == calls_before, "no model call at all"
    assert run["google"]["requests"] == 0 and run["google"]["cache_hits"] > 0
    assert all(o["new"] is False for o in run["imagery"]["observations"]) and run["imagery"]["observations"]
    rep = client.get(f"/api/admin/runs/{run_id}/report", headers=auth).json()["data"]
    assert "no new images" in rep["summary"] and rep["cost"]["total_usd"] == 0
    rows = client.get("/api/admin/runs?limit=5", headers=auth).json()["data"]
    top = next(r for r in rows if r["id"] == run_id)
    assert top["mode"] == "generate" and top["version_created"] is False and top["segment"]["slug"] == slug


def test_generate_with_a_tiny_global_budget_over_http_ends_failed_budget_without_a_model_call(
        world, client, auth, replay_worker, monkeypatch):
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", str(round(repo.total_llm_cost_usd() + 0.05, 4)))
    reset_settings()
    run_id, run = _post_generate(client, auth, world.seg.slug, {"imagery": FORWARD_BACK})

    assert run["status"] == "failed_budget" and run["error"]["code"] == "budget_exhausted"
    assert run["error"]["step"] == "prepare_views" and "run_usd" not in run["inputs"]["budgets"]
    assert "run_budget_usd" not in run["budget"] and run["cost"]["cost_usd"] == 0
    assert all(len(w.calls) == 0 for w in replay_worker)
    assert run["imagery"]["observations"], "the images fetched before the stop stay stored and visible"
    assert client.get(f"/api/admin/runs/{run_id}/report", headers=auth).json()["data"]["degraded"] is True


def test_imagery_endpoint_runs_an_acquire_only_run_through_the_registry(world, client, auth, replay_worker):
    from hero.pipeline import registry

    resp = client.post(f"/api/admin/segments/{world.seg.slug}/imagery", headers=auth, json={"headings": ["left", "right"]})
    assert resp.status_code == 202 and resp.json()["data"]["mode"] == "acquire"
    run_id = resp.json()["data"]["run_id"]
    registry.wait_for(run_id, 120)

    run = client.get(f"/api/admin/runs/{run_id}", headers=auth).json()["data"]
    assert run["status"] == "succeeded" and run["mode"] == "acquire" and run["version_created"] is False
    assert run["cost"]["cost_usd"] == 0 and all(len(w.calls) == 0 for w in replay_worker)
    assert run["imagery"]["observations"] and run["result_version"] == 0
    assert [st["name"] for st in run["steps"]][-1] == "report"


# ---------------------------------------------------------------- runs


def test_run_detail_of_a_seeded_run_has_the_phase_2_shape_and_no_phase_1_artefacts(client, auth, seeded_fixture_segment):
    seg = repo.require_segment(seeded_fixture_segment)

    resp = client.get(f"/api/admin/runs/{seg.latest_run_id}", headers=auth)

    assert resp.status_code == 200
    run = resp.json()["data"]
    assert run["id"] == seg.latest_run_id and run["status"] == "succeeded"
    assert run["segment"] == {"slug": seg.slug, "name": seg.name}
    assert run["mode"] == "generate" and run["report_available"] is True and run["version_created"] in (True, False)
    assert run["layout"] is None and run["obstacles"] is None and run["iterations"] == []
    assert set(run["imagery"]) >= {"satellite", "streetview", "observations", "planned", "plan_trimmed", "warnings"}
    assert set(run["cost"]) == {"input_tokens", "output_tokens", "cost_usd", "calls"}
    assert set(run["google"]) == {"requests", "cache_hits", "est_cost_usd"} and run["error"] is None
    assert run["budget"]["budget_usd"] > 0


def test_phase_1_run_detail_keys_are_unchanged_for_a_phase_1_run(client, auth):
    from hero.scene.schema import FootpathSide, LatLon

    seg = repo.create_segment(repo.SegmentCreate(name=f"P1 {uuid.uuid4().hex[:6]}", city="Bengaluru",
                                                 start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.97467, lon=77.64058),
                                                 footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"models": {"vision": "x"}, "budgets": {"total_usd": 1.0}})
    repo.update_run(run.id, RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                      error={"code": "test", "message": "p1", "step": "x"}))

    data = client.get(f"/api/admin/runs/{run.id}", headers=auth).json()["data"]

    assert "mode" not in data and "report_available" not in data
    assert data["imagery"]["satellite"] is None and data["imagery"]["streetview"] == []
    assert data["imagery"]["streetview_available"] is None and "observations" not in data["imagery"]


def test_run_detail_unknown_id_is_404(client, auth):
    resp = client.get("/api/admin/runs/not-a-run", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_recent_runs_list_is_newest_first_with_segment_duration_and_fusion_fields(client, auth, seeded_fixture_segment):
    resp = client.get("/api/admin/runs?limit=5", headers=auth)

    assert resp.status_code == 200
    runs = resp.json()["data"]
    assert 1 <= len(runs) <= 5
    created = [r["created_at"] for r in runs]
    assert created == sorted(created, reverse=True)
    done = repo.require_segment(seeded_fixture_segment).latest_run_id
    rows = client.get("/api/admin/runs?limit=100", headers=auth).json()["data"]
    done_row = next(r for r in rows if r["id"] == done)
    assert done_row["status"] == "succeeded" and done_row["segment"]["slug"] == seeded_fixture_segment
    assert done_row["mode"] == "generate" and done_row["version_created"] in (True, False)
    assert done_row["duration_s"] is not None


def test_recent_runs_rejects_out_of_range_limit(client, auth):
    resp = client.get("/api/admin/runs?limit=0", headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------- address -> segment


def _fake_resolve(monkeypatch, heading=20.0):
    from hero.imagery import geocode as geocode_mod

    def fake(query, city, **kwargs):
        return [geocode_mod.Candidate(id="google-0", label=f"{query}, {city}", lat=12.975, lon=77.641, provider="google",
                                      heading_deg=heading, heading_source="nearest OSM road", road_name="100 Feet Road")]

    monkeypatch.setattr(geocode_mod, "resolve", fake)


def test_resolve_returns_candidates_with_heading_and_sides(client, auth, monkeypatch):
    _fake_resolve(monkeypatch, heading=0.0)
    resp = client.post("/api/admin/segments/resolve", json={"address": "100 Feet Road", "city": "Bengaluru"}, headers=auth)
    assert resp.status_code == 200
    data = resp.json()["data"]
    c = data["candidates"][0]
    assert c["lat"] == 12.975 and c["travel_compass"] == "north" and c["left_side"] == "west" and c["right_side"] == "east"
    assert data["half_length_m"] == {"min": 50.0, "max": 100.0}


def test_resolve_with_no_match_is_404_with_a_hint(client, auth, monkeypatch):
    from hero.imagery import geocode as geocode_mod

    monkeypatch.setattr(geocode_mod, "resolve", lambda *a, **k: [])
    resp = client.post("/api/admin/segments/resolve", json={"address": "zzzzqqq", "city": "Bengaluru"}, headers=auth)
    assert resp.status_code == 404 and "lat, lon" in resp.json()["error"]["message"]


def test_resolve_requires_the_admin_password(client):
    assert client.post("/api/admin/segments/resolve", json={"address": "x", "city": "y"}).status_code == 401


@pytest.mark.parametrize("half,expected", [(50, 100), (75, 150), (100, 200)])
def test_from_address_builds_a_segment_centred_on_the_point(client, auth, half, expected):
    body = {"name": f"Addr Test {uuid.uuid4().hex[:6]}", "city": "Bengaluru", "lat": 12.975, "lon": 77.641,
            "heading_deg": 20.0, "half_length_m": half, "footpath_side": "right"}
    resp = client.post("/api/admin/segments/from-address", json=body, headers=auth)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["status"] == "draft" and data["length_m"] == pytest.approx(expected, abs=1)
    assert data["heading_deg"] == pytest.approx(20.0, abs=0.5) and data["footpath_side"] == "right"
    mid_lat = (data["start"]["lat"] + data["end"]["lat"]) / 2
    mid_lon = (data["start"]["lon"] + data["end"]["lon"]) / 2
    assert mid_lat == pytest.approx(12.975, abs=1e-5) and mid_lon == pytest.approx(77.641, abs=1e-5)


def test_from_address_normalises_opposite_headings_into_the_same_direction(client, auth):
    body = {"name": f"Addr Dir {uuid.uuid4().hex[:6]}", "city": "Bengaluru", "lat": 12.975, "lon": 77.641,
            "heading_deg": 200.0, "half_length_m": 50}
    data = client.post("/api/admin/segments/from-address", json=body, headers=auth).json()["data"]
    assert data["heading_deg"] == pytest.approx(20.0, abs=0.5)


@pytest.mark.parametrize("half", [10, 49, 101, 500])
def test_from_address_rejects_half_lengths_outside_50_to_100(client, auth, half):
    body = {"name": "x", "city": "y", "lat": 12.975, "lon": 77.641, "heading_deg": 0, "half_length_m": half}
    resp = client.post("/api/admin/segments/from-address", json=body, headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_resolve_live_finds_100_feet_road_with_a_road_direction(client, auth):
    """Real Google + Nominatim: the Indiranagar candidate must carry a road heading from OSM geometry."""
    resp = client.post("/api/admin/segments/resolve", json={"address": "100 Feet Road, Indiranagar", "city": "Bengaluru"}, headers=auth)
    if resp.status_code == 404:
        pytest.skip("geocoders unreachable")
    cands = resp.json()["data"]["candidates"]
    assert cands and all({"lat", "lon", "label", "provider"} <= set(c) for c in cands)
    with_heading = [c for c in cands if c["heading_deg"] is not None]
    if not with_heading:
        pytest.skip("Nominatim/Overpass unreachable: no road geometry")
    indiranagar = [c for c in with_heading if 12.96 < c["lat"] < 12.99]
    assert indiranagar, cands
    assert any(c["heading_deg"] < 25 or c["heading_deg"] > 155 for c in indiranagar)  # runs ~north-south
