"""Public API (spec/api.md ## Public) against the real SQLite test DB.

Deterministic cases insert a ``ready`` segment carrying the canonical example scene via
``hero.db.repo``; the ``generated_segment`` tests (S2's session fixture) exercise the same
routes against a really generated street and its media files.
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
             "run_count", "walkability"}
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


# ---------------------------------------------------------------- the really generated street (S2 fixture)


def test_generated_street_is_in_the_gallery_with_a_servable_thumbnail(client, generated_segment):
    entry = _gallery_entry(client, generated_segment)

    assert entry is not None, "generated street must appear in the gallery"
    assert set(entry) == LIST_KEYS
    assert entry["quality"] in ("full", "satellite_only")
    assert 30.0 <= entry["length_m"] <= 150.0
    assert 0 <= entry["walkability"]["score"] <= 100
    assert entry["thumbnail_url"].startswith("/media/previews/")
    thumb = client.get(entry["thumbnail_url"])
    assert thumb.status_code == 200 and thumb.headers["Cache-Control"] == "public, max-age=86400"
    assert thumb.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_generated_scene_validates_and_its_media_urls_resolve(client, generated_segment):
    body = client.get(f"/api/segments/{generated_segment}/scene").json()

    assert body["ok"] is True
    scene = Scene.model_validate(body["data"])
    assert scene.segment.slug == generated_segment
    tile = client.get(scene.ground.tile_url)
    assert tile.status_code == 200 and tile.headers["Content-Type"].startswith("image/")
    for obj in scene.objects:
        if obj.billboard_url:
            assert client.get(obj.billboard_url).status_code == 200, obj.id
    for building in scene.buildings:
        if building.facade_url:
            assert client.get(building.facade_url).status_code == 200, building.id


def test_run_on_generated_street_reports_its_real_hazards(client, generated_segment):
    scene = Scene.model_validate(client.get(f"/api/segments/{generated_segment}/scene").json()["data"])
    distance = scene.segment.length_m * 1.5
    tokens = min(len(scene.tokens), 1)
    body = _valid_run(score=round(distance) + 50 * tokens, distance_m=distance, tokens=tokens)
    body["loops"] = 1
    body["crashed_into_type"] = scene.hazards()[0].type.value if scene.hazards() else None
    body["crashed_into_id"] = scene.hazards()[0].id if scene.hazards() else None

    resp = client.post(f"/api/segments/{generated_segment}/runs", json=body)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["walkability"] == scene.walkability.model_dump()
    assert [h["id"] for h in data["hazards"]] == [h.id for h in scene.hazards()]
    assert data["best_score"] >= data["score"]
    assert _gallery_entry(client, generated_segment)["run_count"] >= 1
