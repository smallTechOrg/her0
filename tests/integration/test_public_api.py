"""Public API (spec/api.md ## Public) against the real SQLite test DB.

Deterministic cases insert a ``ready`` segment carrying the canonical example scene via
``hero.db.repo``; the recorded fixture street (``seeded_fixture_segment``, no LLM/Google spend) exercises the same
routes with a v2 scene; real generation is covered by the live tier.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.db.session import reset_engine
from hero.scene.schema import FootpathSide, LatLon, Quality, Scene, load_example_scene

START = LatLon(lat=12.97400, lon=77.64050)
END = LatLon(lat=12.97467, lon=77.64058)
EXAMPLE_TOKENS = 2
EXAMPLE_HAZARD_IDS = {"o1", "o2", "o3", "o4"}
LIST_KEYS = {"slug", "name", "city", "quality", "length_m", "thumbnail_url", "best_score", "best_distance_m",
             "run_count", "walkability", "model"}
HAZARD_KEYS = {"id", "type", "label", "confidence", "tile_px", "latlon", "action"}


def _create(slug_prefix: str) -> repo.SegmentRecord:
    slug = f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    return repo.create_segment(SegmentCreate(name=f"Test Street {slug}", city="Bengaluru", start=START, end=END,
                                             footpath_side=FootpathSide.left, slug=slug))


@pytest.fixture()
def ready_segment(test_db) -> repo.SegmentRecord:
    seg = _create("ready")
    run = repo.create_run(seg.id, inputs={})
    return repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                       thumbnail_path=f"previews/{run.id}/thumb.png")


@pytest.fixture()
def draft_segment(test_db) -> repo.SegmentRecord:
    return _create("draft")


def _valid_run(score: int = 120, distance_m: float = 70.0, tokens: int = 1) -> dict:
    return {"score": score, "distance_m": distance_m, "tokens_collected": tokens, "loops": 0,
            "crashed_into_type": "parked_vehicle", "crashed_into_id": "o1", "duration_s": 12.5}


def _gallery_entry(client, slug: str) -> dict | None:
    body = client.get("/api/segments").json()
    assert body["ok"] is True
    return next((s for s in body["data"] if s["slug"] == slug), None)


# ---------------------------------------------------------------- GET /api/segments


def test_gallery_lists_ready_segment_with_thumbnail_walkability_and_zero_runs(client, ready_segment):
    entry = _gallery_entry(client, ready_segment.slug)

    assert entry is not None
    assert set(entry) == LIST_KEYS
    assert entry["name"] == ready_segment.name and entry["city"] == "Bengaluru"
    assert entry["quality"] == "full"
    assert entry["length_m"] == pytest.approx(ready_segment.length_m)
    assert entry["thumbnail_url"] == f"/media/{ready_segment.thumbnail_path}"
    assert entry["best_score"] is None and entry["best_distance_m"] is None
    assert entry["run_count"] == 0
    assert entry["walkability"] == {"footpath_coverage_pct": 29.5, "hazard_count": 4, "crossing_present": True,
                                    "score": 50}


def test_gallery_excludes_segments_that_are_not_ready(client, draft_segment):
    assert _gallery_entry(client, draft_segment.slug) is None


def test_gallery_is_newest_first(client, test_db):
    older = _create("older")
    newer = _create("newer")
    for seg in (older, newer):
        run = repo.create_run(seg.id, inputs={})
        repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                    thumbnail_path=None)

    slugs = [s["slug"] for s in client.get("/api/segments").json()["data"]]

    assert slugs.index(newer.slug) < slugs.index(older.slug)


def test_gallery_entry_without_thumbnail_has_null_url(client, test_db):
    seg = _create("nothumb")
    run = repo.create_run(seg.id, inputs={})
    repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.satellite_only,
                                thumbnail_path=None)

    entry = _gallery_entry(client, seg.slug)

    assert entry["thumbnail_url"] is None and entry["quality"] == "satellite_only"


# ---------------------------------------------------------------- GET /api/segments/{slug}


def test_segment_detail_adds_endpoints_and_status(client, ready_segment):
    body = client.get(f"/api/segments/{ready_segment.slug}").json()

    assert body["ok"] is True
    data = body["data"]
    assert set(data) == LIST_KEYS | {"start", "end", "status"}
    assert data["status"] == "ready"
    assert data["start"] == {"lat": START.lat, "lon": START.lon}
    assert data["end"] == {"lat": END.lat, "lon": END.lon}
    assert data["walkability"]["score"] == 50


def test_segment_detail_for_draft_reports_status_and_no_walkability(client, draft_segment):
    data = client.get(f"/api/segments/{draft_segment.slug}").json()["data"]

    assert data["status"] == "draft"
    assert data["walkability"] is None and data["thumbnail_url"] is None and data["quality"] is None


def test_segment_detail_unknown_slug_is_404_envelope_with_request_id(client):
    resp = client.get("/api/segments/no-such-street", headers={"X-Request-Id": "req-404"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "not_found"
    assert body["error"]["message"]
    assert resp.headers["X-Request-Id"] == "req-404"


# ---------------------------------------------------------------- GET /api/segments/{slug}/scene


def test_scene_endpoint_returns_the_stored_scene_verbatim(client, ready_segment):
    body = client.get(f"/api/segments/{ready_segment.slug}/scene").json()

    assert body["ok"] is True
    scene = Scene.model_validate(body["data"])
    assert scene == load_example_scene()
    assert body["data"]["ground"]["tile_url"].startswith("/media/imagery/")
    assert body["data"]["objects"][0]["billboard_url"].startswith("/media/crops/")


def test_scene_endpoint_for_draft_segment_is_404_with_not_generated_message(client, draft_segment):
    resp = client.get(f"/api/segments/{draft_segment.slug}/scene")

    assert resp.status_code == 404
    assert resp.json()["error"] == {"code": "not_found", "message": "This street has not been generated yet.",
                                    "detail": None}


def test_scene_endpoint_unknown_slug_is_404(client):
    resp = client.get("/api/segments/no-such-street/scene")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- POST /api/segments/{slug}/runs


def test_first_run_is_new_best_and_returns_walkability_and_hazards(client, ready_segment):
    resp = client.post(f"/api/segments/{ready_segment.slug}/runs", json=_valid_run(score=120),
                       headers={"User-Agent": "pytest/phone " * 30})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"run_id", "score", "best_score", "is_new_best", "walkability", "hazards"}
    assert uuid.UUID(data["run_id"]).version == 4
    assert data["score"] == 120 and data["best_score"] == 120 and data["is_new_best"] is True
    assert data["walkability"] == {"footpath_coverage_pct": 29.5, "hazard_count": 4, "crossing_present": True,
                                   "score": 50}
    assert {h["id"] for h in data["hazards"]} == EXAMPLE_HAZARD_IDS
    for hazard in data["hazards"]:
        assert set(hazard) == HAZARD_KEYS
        assert hazard["action"] != "none"
        assert len(hazard["tile_px"]) == 2 and set(hazard["latlon"]) == {"lat", "lon"}
    stored = repo.list_top_game_runs(ready_segment.id)
    assert len(stored) == 1 and stored[0].id == data["run_id"]
    assert stored[0].crashed_into_type == "parked_vehicle" and len(stored[0].client) <= 120


def test_lower_then_higher_runs_track_best_and_run_count(client, ready_segment):
    url = f"/api/segments/{ready_segment.slug}/runs"
    client.post(url, json=_valid_run(score=120, distance_m=70.0))

    lower = client.post(url, json=_valid_run(score=80, distance_m=30.0)).json()["data"]
    higher = client.post(url, json=_valid_run(score=170, distance_m=120.0)).json()["data"]
    entry = _gallery_entry(client, ready_segment.slug)

    assert lower["is_new_best"] is False and lower["best_score"] == 120
    assert higher["is_new_best"] is True and higher["best_score"] == 170
    assert entry["best_score"] == 170 and entry["best_distance_m"] == pytest.approx(120.0)
    assert entry["run_count"] == 3


def test_equal_score_is_not_a_new_best(client, ready_segment):
    url = f"/api/segments/{ready_segment.slug}/runs"
    client.post(url, json=_valid_run(score=120))

    again = client.post(url, json=_valid_run(score=120)).json()["data"]

    assert again["is_new_best"] is False and again["best_score"] == 120


def test_best_score_survives_engine_reset_and_a_separate_server_process(client, ready_segment, live_server):
    url = f"/api/segments/{ready_segment.slug}/runs"
    client.post(url, json=_valid_run(score=150, distance_m=100.0))

    reset_engine()
    after_reset = client.get(f"/api/segments/{ready_segment.slug}").json()["data"]
    other_process = httpx.get(f"{live_server}/api/segments/{ready_segment.slug}", timeout=5.0).json()["data"]

    assert after_reset["best_score"] == 150 and after_reset["run_count"] == 1
    assert other_process["best_score"] == 150 and other_process["best_distance_m"] == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"score": 70 + 50 * 1 + 2}, "score above distance + 50*tokens + 1"),
        ({"score": -1}, "negative score"),
        ({"distance_m": -5.0}, "negative distance"),
        ({"distance_m": 5001.0, "score": 10}, "distance over 5000 m"),
        ({"tokens_collected": EXAMPLE_TOKENS * 2 + 1, "loops": 1, "score": 0}, "more tokens than the scene offers"),
        ({"loops": -1}, "negative loops"),
        ({"duration_s": -0.1}, "negative duration"),
        ({"score": "lots"}, "non-numeric score"),
        ({"crashed_into_type": "x" * 33}, "crashed_into_type too long"),
    ],
)
def test_impossible_run_is_422_and_not_recorded(client, ready_segment, patch, reason):
    body = {**_valid_run(score=100, distance_m=70.0, tokens=1), **patch}

    resp = client.post(f"/api/segments/{ready_segment.slug}/runs", json=body)

    assert resp.status_code == 422, reason
    err = resp.json()["error"]
    assert err["code"] == "validation_error" and err["message"]
    assert repo.count_game_runs(ready_segment.id) == 0
    assert _gallery_entry(client, ready_segment.slug)["best_score"] is None


def test_run_with_missing_field_is_422(client, ready_segment):
    body = _valid_run()
    del body["duration_s"]

    resp = client.post(f"/api/segments/{ready_segment.slug}/runs", json=body)

    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_run_with_non_json_body_is_422(client, ready_segment):
    resp = client.post(f"/api/segments/{ready_segment.slug}/runs", content=b"not json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 422 and resp.json()["ok"] is False


def test_run_on_unknown_or_ungenerated_street_is_404(client, draft_segment):
    unknown = client.post("/api/segments/no-such-street/runs", json=_valid_run())
    draft = client.post(f"/api/segments/{draft_segment.slug}/runs", json=_valid_run())

    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    assert draft.status_code == 404
    assert draft.json()["error"]["message"] == "This street has not been generated yet."


def test_phase_2_endpoints_are_not_served_in_phase_1(client, ready_segment):
    assert client.get("/api/segments/nearest?lat=12.974&lon=77.6405").status_code == 404
    assert client.get(f"/api/segments/{ready_segment.slug}/runs?limit=5").status_code in (404, 405)


# ---------------------------------------------------------------- the recorded v2 street (replaces the live-generated fixture)
# Real generation is exercised by the live tier (tests/live, slice S7); here the same routes run on a recorded v2 street.


def test_v2_street_is_in_the_gallery_with_ready_quality(client, v2_street):
    entry = _gallery_entry(client, v2_street.slug)

    assert entry is not None, "published v2 street must appear in the gallery"
    assert set(entry) == LIST_KEYS
    assert entry["length_m"] == pytest.approx(100.0)
    assert 0 <= entry["walkability"]["score"] <= 100 and entry["run_count"] == 0


def test_v2_scene_validates_as_scene_v2(client, v2_street):
    from hero.model.scene_v2 import SceneV2

    body = client.get(f"/api/segments/{v2_street.slug}/scene").json()

    assert body["ok"] is True
    scene = SceneV2.model_validate(body["data"])
    assert scene.segment.slug == v2_street.slug and scene.about.model_version == 2


# ---------------------------------------------------------------- Phase 2: model badge, scene v2


@pytest.fixture()
def v2_street(test_db, tmp_path):
    from hero.tools.load_fixture_street import seed_fixture_segment
    from tests.fixtures.street_fixtures import write_mini_fixture_dir

    slug = f"pub-v2-{uuid.uuid4().hex[:8]}"
    return seed_fixture_segment(slug, fixture_dir=write_mini_fixture_dir(tmp_path / "fx", slug=slug))


def test_gallery_phase1_segment_has_null_model_badge(client, ready_segment):
    assert _gallery_entry(client, ready_segment.slug)["model"] is None


def test_gallery_v2_segment_has_fidelity_badge(client, v2_street):
    entry = _gallery_entry(client, v2_street.slug)

    assert set(entry["model"]) == {"version", "fidelity_level", "fidelity_label", "photos", "updated_at", "legacy"}
    assert entry["model"]["version"] == 2 and entry["model"]["fidelity_level"] == "L2"
    assert entry["model"]["photos"] == 4 and entry["model"]["legacy"] is False
    assert "Multi-view" in entry["model"]["fidelity_label"]
    assert client.get(f"/api/segments/{v2_street.slug}").json()["data"]["model"] == entry["model"]


def test_scene_v2_is_served_with_about(client, v2_street):
    scene = client.get(f"/api/segments/{v2_street.slug}/scene").json()["data"]
    assert scene["schema_version"] == 2 and scene["about"]["model_version"] == 2
    assert "path" in scene and "corridor" in scene


def test_scene_v1_still_served_for_phase1_segment(client, ready_segment):
    assert client.get(f"/api/segments/{ready_segment.slug}/scene").json()["data"]["schema_version"] == 1


def test_game_run_on_v2_segment_returns_hazards_with_string_types(client, v2_street):
    resp = client.post(f"/api/segments/{v2_street.slug}/runs", json=_valid_run(score=5, distance_m=5.0, tokens=0))

    assert resp.status_code == 200, resp.text
    hazards = resp.json()["data"]["hazards"]
    assert hazards and hazards[0]["type"] == "parked_vehicle" and hazards[0]["action"] == "dodge"


# ---------------------------------------------------------------- the recorded fixture street (seeded_fixture_segment)


def test_fixture_street_gallery_badge_and_about_panel_data_agree(client, seeded_fixture_segment):
    """The About panel's data (fidelity, photo count, summary, least-certain parts) is in the served scene and matches the badge."""
    entry = _gallery_entry(client, seeded_fixture_segment)
    assert entry is not None and entry["model"]["fidelity_level"] == "L2" and entry["model"]["legacy"] is False

    about = client.get(f"/api/segments/{seeded_fixture_segment}/scene").json()["data"]["about"]

    assert about["fidelity"]["level"] == entry["model"]["fidelity_level"]
    assert about["fidelity"]["label"] == entry["model"]["fidelity_label"]
    assert about["photos"]["total"] == entry["model"]["photos"] == 48
    assert about["photos"]["street_view"] + about["photos"]["satellite"] + about["photos"]["user"] == 48
    assert about["model_version"] == entry["model"]["version"]
    assert "fidelity L2" in about["summary"] and "48 photos" in about["summary"]
    assert about["low_confidence_parts"] and about["improve_hint"]
    assert about["legacy"] is False and about["placement_verified"] is True


def test_fixture_street_detail_and_game_run_hazards(client, seeded_fixture_segment):
    detail = client.get(f"/api/segments/{seeded_fixture_segment}").json()["data"]
    assert detail["status"] == "ready" and detail["model"]["photos"] == 48 and {"start", "end"} <= set(detail)

    resp = client.post(f"/api/segments/{seeded_fixture_segment}/runs", json=_valid_run(score=5, distance_m=5.0, tokens=0))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["hazards"]


def test_legacy_import_badge_is_flagged_legacy(client, ready_segment):
    """A Phase-1 segment lazily imported as v0 'legacy' by the admin model page shows legacy=true on its card."""
    from hero.config import get_settings

    assert _gallery_entry(client, ready_segment.slug)["model"] is None
    auth = {"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()}
    assert client.get(f"/api/admin/segments/{ready_segment.slug}/model", headers=auth).status_code == 200

    model = _gallery_entry(client, ready_segment.slug)["model"]
    assert model["legacy"] is True and model["version"] == 0
