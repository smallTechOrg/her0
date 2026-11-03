"""Public API for a recompiled (v3-lite) street: scene, hazards from the stored manifest, token validation, textures
route, admin gaps/model summary. v1/v2 stored scenes keep being served verbatim."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.fusion.recompile import recompile_segment
from hero.tools.load_fixture_street import seed_fixture_segment

HAZARD_KEYS = {"id", "type", "label", "confidence", "tile_px", "latlon", "action"}


@pytest.fixture()
def data_dir(test_db, tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("HERO_DATA_DIR", str(d))
    reset_settings()
    yield d
    reset_settings()


@pytest.fixture()
def v2_street(data_dir):
    return seed_fixture_segment(f"pub-v2-{uuid.uuid4().hex[:8]}")


@pytest.fixture()
def v3_street(data_dir):
    seg = seed_fixture_segment(f"pub-v3-{uuid.uuid4().hex[:8]}")
    recompile_segment(seg.slug)
    return seg


@pytest.fixture()
def auth():
    return {"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()}


def _manifest(slug: str, data_dir: Path) -> dict:
    sha8 = model_repo.get_export(repo.get_segment(slug).id)["sha8"]
    return json.loads((data_dir / "exports" / slug / f"street_{sha8}.manifest.json").read_text())


def _run(**kw) -> dict:
    return {"score": 60, "distance_m": 10.0, "tokens_collected": 1, "loops": 0, "crashed_into_type": "pole",
            "crashed_into_id": "x", "duration_s": 5.0, **kw}


# ------------------------------------------------------------------ scene


def test_recompiled_street_serves_the_v3_lite_scene_with_assets(client, v3_street, data_dir):
    body = client.get(f"/api/segments/{v3_street.slug}/scene").json()["data"]
    assert body["schema_version"] == 3
    assert {"geometry", "lanes", "objects", "tokens", "path", "corridor", "buildings"}.isdisjoint(body)
    assets = body["assets"]
    sha8 = assets["sha8"]
    assert assets["glb_url"] == f"/api/segments/{v3_street.slug}/model.glb?v={sha8}"
    assert assets["manifest_url"].endswith(f"manifest.json?v={sha8}") and assets["triangles"] > 0
    assert len(json.dumps(body)) < 40_000
    assert body["model"]["compiled_by"] == "hero.model.compile/2"
    assert body["dynamic_context"]["excluded_count"] == len(body["dynamic_context"]["entity_ids"])


def test_a_v2_street_is_served_verbatim_as_stored(client, v2_street):
    stored = repo.get_segment(v2_street.slug).scene.model_dump(mode="json")
    body = client.get(f"/api/segments/{v2_street.slug}/scene").json()["data"]
    assert body["schema_version"] == 2 and body == stored


def test_a_compiled_but_never_exported_v3_scene_is_not_served(client, v3_street):
    seg = repo.get_segment(v3_street.slug)
    scene = seg.scene.model_dump(mode="json")
    scene["assets"] = None
    from hero.db import models
    from hero.db.session import get_session

    with get_session() as s:
        s.get(models.Segment, seg.id).scene_json = scene
    resp = client.get(f"/api/segments/{v3_street.slug}/scene")
    assert resp.status_code == 404 and "not built yet" in resp.json()["error"]["message"]


def test_gallery_card_and_detail_work_for_a_v3_street(client, v3_street):
    cards = {c["slug"]: c for c in client.get("/api/segments").json()["data"]}
    assert cards[v3_street.slug]["walkability"] is not None
    assert client.get(f"/api/segments/{v3_street.slug}").status_code == 200


# ------------------------------------------------------------------ runs / hazards


def test_run_hazards_come_from_the_stored_manifest_colliders(client, v3_street, data_dir):
    manifest = _manifest(v3_street.slug, data_dir)
    expected = [c for c in manifest["colliders"] if c["action"] != "none"]
    data = client.post(f"/api/segments/{v3_street.slug}/runs", json=_run(tokens_collected=0, score=10)).json()["data"]
    assert [h["id"] for h in data["hazards"]] == [c["id"] for c in expected]
    assert all(set(h) == HAZARD_KEYS for h in data["hazards"])
    first = data["hazards"][0] if data["hazards"] else None
    if first:
        c = expected[0]
        assert first["type"] == c["type"] and first["label"] == c["label"] and first["action"] == c["action"]
        assert first["latlon"] == c["latlon"] and first["confidence"] == c["confidence"]
    assert data["walkability"]["score"] >= 0 and data["is_new_best"] is True


def test_token_count_is_validated_against_the_manifest_and_gameplay_value(client, v3_street, data_dir):
    manifest = _manifest(v3_street.slug, data_dir)
    n = len(manifest["tokens"])
    value = manifest["gameplay"]["tokens"]["value"]
    ok = client.post(f"/api/segments/{v3_street.slug}/runs",
                     json=_run(tokens_collected=n, score=n * value, distance_m=n * value + 1.0))
    assert ok.status_code == 200, ok.text
    too_many = client.post(f"/api/segments/{v3_street.slug}/runs",
                           json=_run(tokens_collected=n + 1, score=(n + 1) * value, distance_m=(n + 1) * value + 1.0))
    assert too_many.status_code == 422 and "tokens_collected" in too_many.json()["error"]["message"]


def test_impossible_score_is_422_for_v3_too(client, v3_street):
    resp = client.post(f"/api/segments/{v3_street.slug}/runs", json=_run(score=100000, distance_m=5.0))
    assert resp.status_code == 422


def test_v2_street_hazards_still_come_from_scene_objects(client, v2_street):
    scene = repo.get_segment(v2_street.slug).scene
    data = client.post(f"/api/segments/{v2_street.slug}/runs", json=_run(tokens_collected=0, score=5)).json()["data"]
    assert [h["id"] for h in data["hazards"]] == [o.id for o in scene.hazards()]


# ------------------------------------------------------------------ media / admin


def test_media_textures_route_serves_baked_files_with_cache_header(data_dir, test_db):
    from fastapi.testclient import TestClient

    from hero.app import create_app

    tex = data_dir / "textures" / "some-street"
    tex.mkdir(parents=True)
    (tex / "ground_v1_abcd1234.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/media/textures/some-street/ground_v1_abcd1234.jpg")
        assert r.status_code == 200 and r.content == b"\xff\xd8\xff\xd9"
        assert r.headers["cache-control"] == "public, max-age=86400"
        assert c.get("/media/textures/some-street/missing.jpg").status_code == 404
        assert c.get("/media/textures/../hero.db").status_code in (404, 400)


def test_recompiled_street_has_baked_texture_files_served_under_media(data_dir, v3_street):
    files = sorted((data_dir / "textures" / v3_street.slug).glob("*.jpg"))
    assert len(files) == 3  # ground + two frontage ribbons
    from fastapi.testclient import TestClient

    from hero.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get(f"/media/textures/{v3_street.slug}/{files[0].name}")
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"


def test_admin_gaps_returns_the_stored_report_and_model_head_has_schema_and_summary(client, v3_street, auth):
    gaps = client.get(f"/api/admin/segments/{v3_street.slug}/gaps", headers=auth).json()["data"]
    stored = model_repo.get_version(v3_street.id, model_repo.head_version(v3_street.id)).gaps
    assert gaps == stored and gaps["gap_report"] == 1 and gaps["bins"]
    assert {"ground", "frontage_modelled", "frontage_opposite", "footpath"} <= set(gaps["uncovered"])
    head = client.get(f"/api/admin/segments/{v3_street.slug}/model", headers=auth).json()["data"]
    assert head["scene_schema"] == 3
    assert head["gaps"]["uncovered"] == gaps["uncovered"] and head["gaps"]["interpolated_m"] >= 0


def test_admin_gaps_is_computed_on_the_fly_for_a_not_yet_recompiled_version(client, v2_street, auth):
    gaps = client.get(f"/api/admin/segments/{v2_street.slug}/gaps", headers=auth).json()["data"]
    assert gaps["gap_report"] == 1 and gaps["model_version"] == model_repo.head_version(v2_street.id)
    assert model_repo.get_version(v2_street.id, gaps["model_version"]).gaps is None  # nothing was written
    head = client.get(f"/api/admin/segments/{v2_street.slug}/model", headers=auth).json()["data"]
    assert head["scene_schema"] == 2 and head["gaps"] is None


def test_admin_gaps_errors(client, v3_street, auth):
    assert client.get(f"/api/admin/segments/{v3_street.slug}/gaps").status_code == 401
    r = client.get(f"/api/admin/segments/{v3_street.slug}/gaps?version=99", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/api/admin/segments/nope/gaps", headers=auth).status_code == 404
