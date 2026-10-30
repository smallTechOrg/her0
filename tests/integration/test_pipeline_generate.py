"""Generation plumbing without a real model: the registry (single-flight, modes, guards), run inputs, and the two
tools the orchestrator owns (``import_legacy``, ``create_demo_street``). The fusion run itself is covered in
``test_fusion_run.py`` over ``ReplayLLM``; the real-model generations live in ``tests/live``.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.repo import RunUpdate, SegmentCreate
from hero.fusion.runner import run_inputs
from hero.pipeline import registry
from hero.scene.schema import FootpathSide, LatLon, Quality, RunStatus, Scene
from tests.fixtures.google.fake_google import FakeGoogle, straight_ref
from tests.integration.test_fusion_run import FORWARD_BACK, new_world

SCENE_EXAMPLE = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "scene_example.json"


def new_segment(label="Pipeline test") -> repo.SegmentRecord:
    return repo.create_segment(SegmentCreate(
        name=f"{label} {uuid.uuid4().hex[:6]} (west footpath)", city="Bengaluru", start=LatLon(lat=12.974, lon=77.6405),
        end=LatLon(lat=12.9749, lon=77.6405), footpath_side=FootpathSide.left))


def retire(run_id: str) -> None:
    repo.update_run(run_id, RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                      error={"code": "test", "message": "retired", "step": "test"}))


@pytest.fixture()
def idle_worker(monkeypatch):
    """Swap the fusion entry point for a recorder that finishes the run at once (no network, no model)."""
    ran: list[str] = []

    def worker(run_id):
        ran.append(run_id)
        repo.update_run(run_id, RunUpdate(status=RunStatus.succeeded, finished_at=datetime.now(UTC)))

    monkeypatch.setattr(registry, "run_fusion", worker)
    return ran


# ---------------------------------------------------------------- run inputs


def test_run_inputs_snapshot_the_mode_plan_models_and_budgets(test_db):
    seg = new_segment()
    inputs = run_inputs(get_settings(), seg, mode="acquire", plan_overrides={"headings": ["left"], "sv_spacing_m": 20},
                        base_version=3)

    assert inputs["mode"] == "acquire" and inputs["base_version"] == 3
    assert inputs["imagery_plan"]["headings"] == ["left"] and inputs["imagery_plan"]["sv_spacing_m"] == 20
    assert inputs["imagery_plan"]["max_images"] == get_settings().max_images_per_run
    assert inputs["plan_overrides"] == {"headings": ["left"], "sv_spacing_m": 20}
    assert inputs["budgets"] == {"total_usd": get_settings().claude_budget_usd}
    assert inputs["models"]["vision"] == get_settings().vision_model and inputs["structured_mode"]
    json.dumps(inputs)  # JSON-serialisable (it is stored in pipeline_runs.inputs_json)


def test_run_inputs_reject_an_invalid_plan(test_db):
    with pytest.raises(ValueError, match="sv_spacing_m"):
        run_inputs(get_settings(), new_segment(), mode="generate", plan_overrides={"sv_spacing_m": 2},
                   base_version=None)


# ---------------------------------------------------------------- registry


def test_start_run_queues_a_fusion_run_with_inputs_and_runs_the_worker_thread(test_db, idle_worker):
    seg = new_segment()

    run = registry.start_run(seg.slug, mode="generate", plan_overrides={"headings": ["forward"]})
    registry.wait_for(run.id, 10)

    assert run.status is RunStatus.queued and run.inputs["mode"] == "generate"
    assert run.inputs["plan_overrides"] == {"headings": ["forward"]} and "run_usd" not in run.inputs["budgets"]
    assert run.inputs["base_version"] is None
    assert idle_worker == [run.id] and repo.require_run(run.id).status is RunStatus.succeeded
    assert registry.active_run() is None


def test_start_run_is_single_flight_across_modes_and_names_the_blocker(test_db, idle_worker, monkeypatch):
    seg = new_segment()
    release = threading.Event()
    blocker = repo.create_run(seg.id, {"mode": "generate"})
    repo.update_run(blocker.id, RunUpdate(status=RunStatus.running, started_at=datetime.now(UTC)))
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    monkeypatch.setitem(registry._threads, blocker.id, thread)
    try:
        for mode in ("generate", "acquire"):
            with pytest.raises(registry.RunInProgress) as err:
                registry.start_run(seg.slug, mode=mode)
            assert err.value.run.id == blocker.id
    finally:
        release.set()
        thread.join(5)
        retire(blocker.id)
    assert idle_worker == []


def test_start_run_refuses_unknown_mode_unknown_segment_and_a_spent_budget(test_db, idle_worker, monkeypatch):
    seg = new_segment()
    with pytest.raises(ValueError, match="mode"):
        registry.start_run(seg.slug, mode="rebuild")
    with pytest.raises(repo.NotFoundError):
        registry.start_run("no-such-street")

    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0")
    reset_settings()
    try:
        with pytest.raises(registry.BudgetExhausted) as err:
            registry.start_run(seg.slug)
        assert err.value.budget_usd == 0.0 and "HERO_CLAUDE_BUDGET_USD" in str(err.value)
        # an acquire-only run makes no Claude call, so the exhausted Claude budget does not stop it
        run = registry.start_run(seg.slug, mode="acquire", plan_overrides={"max_images": 10})
        registry.wait_for(run.id, 10)
        assert run.inputs["mode"] == "acquire"
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()


def test_acquire_run_is_never_refused_on_the_google_dollar_estimate(test_db, tmp_path, monkeypatch, idle_worker):
    w = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    try:
        assert registry._google_estimate_usd(w.seg, get_settings(), None) > 0.05  # informational estimate stays
        run = registry.start_run(w.seg.slug, mode="acquire")
        registry.wait_for(run.id, 10)
        assert run.id in idle_worker, "no per-run dollar cap: the acquire run is queued however dear the plan"
        assert not hasattr(registry, "GoogleBudgetExceeded")
    finally:
        reset_settings()


def test_the_registry_dispatches_to_hero_fusion_and_not_to_the_legacy_pipeline():
    import hero.fusion.runner as fusion_runner

    assert registry.run_fusion is fusion_runner.run_fusion
    assert not hasattr(registry, "run_pipeline") and not hasattr(registry, "create_queued_run")


def test_a_crashing_worker_marks_the_run_failed_and_frees_the_slot(test_db, monkeypatch):
    seg = new_segment()

    def boom(run_id):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(registry, "run_fusion", boom)
    run = registry.start_run(seg.slug)
    registry.wait_for(run.id, 10)

    done = repo.require_run(run.id)
    assert done.status is RunStatus.failed and "worker exploded" in done.error["message"]
    assert registry.active_run() is None


# ---------------------------------------------------------------- import_legacy tool


def phase1_segment() -> repo.SegmentRecord:
    scene = Scene.model_validate(json.loads(SCENE_EXAMPLE.read_text()))
    seg = repo.create_segment(SegmentCreate(name=f"Legacy tool {uuid.uuid4().hex[:6]}", city="Bengaluru",
                                            start=scene.segment.start, end=scene.segment.end,
                                            footpath_side=scene.segment.footpath_side))
    repo.finalize_segment_scene(seg.id, run_id="phase1-run", scene=scene, quality=Quality.full,
                                thumbnail_path="previews/phase1/thumb.png")
    return repo.require_segment(seg.slug)


def test_import_legacy_tool_imports_phase_1_segments_once_and_leaves_everything_else(test_db, seeded_fixture_segment, capsys):
    from hero.tools import import_legacy

    legacy = phase1_segment()
    drafts = new_segment("Draft")
    game_before = repo.count_game_runs(legacy.id)

    done = import_legacy.import_all()

    assert legacy.slug in done and drafts.slug not in done and seeded_fixture_segment not in done
    v0 = model_repo.get_version(legacy.id, 0)
    assert v0.kind == "legacy" and v0.model.legacy and v0.model.placement_verified is False
    assert v0.fidelity.overall.level == 0 and v0.scene["schema_version"] == 1
    assert repo.require_segment(legacy.slug).scene.schema_version == 1, "the Phase-1 scene keeps playing"
    assert repo.count_game_runs(legacy.id) == game_before
    assert model_repo.head_version(drafts.id) is None
    assert import_legacy.import_all() == [], "idempotent"
    assert model_repo.list_versions(legacy.id)[0].summary.startswith("Imported Phase-1 scene")

    assert import_legacy.main([]) == 0
    assert "imported 0 legacy segment(s)" in capsys.readouterr().out


def test_import_legacy_main_prints_the_gate_line(test_db, capsys):
    from hero.tools import import_legacy

    phase1_segment()
    assert import_legacy.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("imported ") and "legacy segment(s)" in out
    assert import_legacy.main([]) == 0
    assert "imported 0 legacy segment(s)" in capsys.readouterr().out


# ---------------------------------------------------------------- create_demo_street tool


HINT = {"lat": 12.9711, "lon": 77.6413}


def candidate(cid, lat, lon, heading, **kw):
    from hero.imagery import geocode as geocode_mod

    return geocode_mod.Candidate(id=cid, label=f"{cid} label", lat=lat, lon=lon, provider="google", heading_deg=heading,
                                 heading_source="nearest OSM road" if heading is not None else None,
                                 road_name="100 Feet Road", **kw)


@pytest.fixture()
def demo_world(test_db, tmp_path, monkeypatch):
    """Geocoder, Google and Nominatim/Overpass replaced by the recorded transport; data dir under tmp."""
    from pydantic import SecretStr

    from hero.api import admin_imagery
    from hero.imagery import geocode as geocode_mod
    from hero.imagery.google import GoogleImagery
    from hero.imagery.roads import fetch_road_polyline

    from hero.api import admin as admin_api

    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    monkeypatch.setitem(admin_api.DEMO_STREET, "name", f"Demo street {uuid.uuid4().hex[:6]} (west footpath)")
    reset_settings()
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref)
    seen: dict = {"resolve_calls": []}

    def resolve(query, city, **kwargs):
        seen["resolve_calls"].append((query, city))
        return seen["candidates"]

    def make_google(settings, cache, run_id):
        s = settings.model_copy(update={"google_maps_api_key": SecretStr("test-key")})
        return GoogleImagery(s, cache, run_id, http=fake.client(), sleep=lambda _s: None)

    monkeypatch.setattr(geocode_mod, "resolve", resolve)
    monkeypatch.setattr(admin_imagery, "GoogleImagery", make_google)
    monkeypatch.setattr(admin_imagery, "fetch_road_polyline",
                        lambda lat, lon, **kw: fetch_road_polyline(lat, lon, **{**kw, "http": fake.client()}))
    seen["candidates"] = [candidate("far", 12.99, 77.60, 10.0),
                          candidate("no-heading", 12.9712, 77.6414, None),
                          candidate("good", 12.9744, 77.6405, 4.0)]
    seen["fake"] = fake
    yield seen
    reset_settings()


def test_pick_candidate_takes_the_nearest_one_within_400_m_that_has_a_road_heading():
    from hero.tools.create_demo_street import DemoStreetError, pick_candidate

    cands = [{"label": "far", "lat": 12.99, "lon": 77.60, "heading_deg": 10.0},
             {"label": "no heading", "lat": 12.9712, "lon": 77.6414, "heading_deg": None},
             {"label": "second", "lat": 12.9740, "lon": 77.6408, "heading_deg": 2.0},
             {"label": "first", "lat": 12.9731, "lon": 77.6411, "heading_deg": 5.0}]

    assert pick_candidate(cands, HINT)["label"] == "first"
    with pytest.raises(DemoStreetError, match="within 400 m"):
        pick_candidate(cands[:2], HINT)
    with pytest.raises(DemoStreetError):
        pick_candidate([], HINT)


def test_create_demo_street_resolves_creates_previews_and_writes_the_evidence_image(demo_world, tmp_path):
    from fastapi.testclient import TestClient

    from hero.app import create_app
    from hero.tools.create_demo_street import create_demo_street

    out = tmp_path / "evidence" / "placement.png"
    messages: list[str] = []
    with TestClient(create_app()) as client:
        client.headers.update({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})

        result = create_demo_street(client, out_path=out, say=messages.append)

        assert demo_world["resolve_calls"] == [("100 Feet Road, Indiranagar", "Bengaluru")]
        assert result.created and result.verified and result.problems == [] and result.image_path == out
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 5000
        seg = repo.require_segment(result.slug)
        assert seg.name.startswith("Demo street ")
        assert seg.footpath_side is FootpathSide.left and seg.length_m == pytest.approx(100.0, abs=1.0)
        assert seg.midpoint.lat == pytest.approx(12.9744, abs=2e-5), "centred on the 'good' candidate, not the far one"
        ref = result.placement["reference_line"]
        assert ref["source"] in ("osm_nominatim", "osm_overpass") and ref["snap_m"] <= 25
        assert abs(ref["length_s"] - 100.0) <= 3
        assert model_repo.get_version(seg.id, 0).kind == "prior"
        assert any("resolved" in m for m in messages) and any("placement:" in m for m in messages)

        again = create_demo_street(client, out_path=out, say=messages.append)

        assert again.created is False and again.slug == result.slug
        assert len(demo_world["resolve_calls"]) == 1, "an existing demo street is reused without geocoding again"
        assert sum(1 for s in repo.list_segments() if s.name == seg.name) == 1


def test_create_demo_street_reports_an_unverified_placement(demo_world, tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from hero.api import admin_imagery
    from hero.app import create_app
    from hero.tools.create_demo_street import create_demo_street

    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    monkeypatch.setattr(admin_imagery, "fetch_road_polyline",
                        lambda lat, lon, **kw: __import__("hero.imagery.roads", fromlist=["x"]).fetch_road_polyline(
                            lat, lon, **{**kw, "http": offline}))
    with TestClient(create_app()) as client:
        client.headers.update({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})

        result = create_demo_street(client, out_path=tmp_path / "p.png", say=lambda m: None)

    assert not result.verified and result.placement["reference_line"]["source"] == "straight_chord"
    assert any("not OSM road geometry" in p for p in result.problems)
    assert (tmp_path / "p.png").exists()


def test_create_demo_street_fails_clearly_when_no_candidate_is_near_the_hint(demo_world, tmp_path):
    from fastapi.testclient import TestClient

    from hero.app import create_app
    from hero.tools.create_demo_street import DemoStreetError, create_demo_street

    demo_world["candidates"] = [candidate("far", 12.99, 77.60, 10.0)]
    with TestClient(create_app()) as client:
        client.headers.update({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})
        with pytest.raises(DemoStreetError, match="far label"):
            create_demo_street(client, out_path=tmp_path / "p.png", say=lambda m: None)
    assert not (tmp_path / "p.png").exists()
