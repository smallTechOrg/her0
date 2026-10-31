"""Admin model API (spec/api.md ## Admin: model, versions, diff, coverage, entities, run report).

DB state is built with ``tests/fixtures/street_fixtures.py`` (mini fixture dir, 3 versions, 2 runs) loaded through
``hero.tools.load_fixture_street``; no network, no spend.
"""

from __future__ import annotations

import uuid

import pytest

from hero.config import get_settings
from hero.db import model_repo, repo
from hero.db.repo import SegmentCreate
from hero.scene.schema import FootpathSide, LatLon, Quality, load_example_scene
from hero.tools.load_fixture_street import seed_fixture_segment
from tests.fixtures.street_fixtures import write_mini_fixture_dir


class _Auth(dict):
    def __repr__(self) -> str:  # pragma: no cover
        return "<admin auth headers (redacted)>"

    __str__ = __repr__


@pytest.fixture()
def auth():
    return _Auth({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})


@pytest.fixture()
def street(test_db, tmp_path):
    slug = f"model-api-{uuid.uuid4().hex[:8]}"
    seg = seed_fixture_segment(slug, fixture_dir=write_mini_fixture_dir(tmp_path / "fx", slug=slug))
    return seg


def _url(seg, tail: str = "") -> str:
    return f"/api/admin/segments/{seg.slug}/model{tail}"


# ------------------------------------------------------------------ auth


@pytest.mark.parametrize("tail", ["", "/versions/0", "/diff?from=0&to=1", "/coverage", "/entities", "/entities/x"])
def test_model_endpoints_require_password(client, street, tail):
    resp = client.get(_url(street, tail))
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


def test_report_requires_password(client):
    assert client.get("/api/admin/runs/whatever/report").status_code == 401


# ------------------------------------------------------------------ head summary


def test_model_head_summary(client, street, auth):
    data = client.get(_url(street), headers=auth).json()["data"]

    assert data["slug"] == street.slug and data["head_version"] == 2
    assert data["legacy"] is False and data["placement_verified"] is True
    ref = data["reference_line"]
    assert ref["source"] == "osm_nominatim" and 2 <= len(ref["points"]) <= 60
    assert ref["points"][0]["s"] == 0 and ref["points"][-1]["s"] == pytest.approx(ref["length_s"])
    assert data["fidelity"]["overall"]["level"] == 2
    assert data["counts"]["entities"] == 2 and data["counts"]["removed"] == 0
    assert data["counts"]["observations"] == 4 and data["counts"]["fused_observations"] == 4
    assert [v["version"] for v in data["versions"]] == [0, 1, 2]
    assert data["versions"][2]["entity_count"] == 2 and data["versions"][2]["parent_version"] == 1


def test_model_head_for_draft_segment_is_empty(client, test_db, auth):
    seg = repo.create_segment(SegmentCreate(
        name="Draft", city="Bengaluru", start=LatLon(lat=12.97, lon=77.64), end=LatLon(lat=12.9707, lon=77.64),
        footpath_side=FootpathSide.left, slug=f"draft-{uuid.uuid4().hex[:6]}"))
    data = client.get(_url(seg), headers=auth).json()["data"]
    assert data["head_version"] is None and data["versions"] == []
    assert data["counts"]["entities"] == 0


def test_model_head_unknown_segment_404(client, auth):
    resp = client.get("/api/admin/segments/nope-nope/model", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_reference_line_points_are_capped_at_60(client, street, auth, monkeypatch):
    from hero.api import admin_model
    from hero.model.types import RefPoint

    pts = [RefPoint(lat=12.0 + i * 1e-5, lon=77.0, s=float(i)) for i in range(500)]
    out = admin_model._simplify(pts)
    assert len(out) == 60 and out[0]["s"] == 0.0 and out[-1]["s"] == 499.0
    assert [p["s"] for p in out] == sorted(p["s"] for p in out)


def test_legacy_scene_is_imported_lazily_on_first_call(client, test_db, auth):
    seg = repo.create_segment(SegmentCreate(
        name="Legacy", city="Bengaluru", start=LatLon(lat=12.97400, lon=77.64050), end=LatLon(lat=12.97467, lon=77.64058),
        footpath_side=FootpathSide.left, slug=f"legacy-{uuid.uuid4().hex[:6]}"))
    run = repo.create_run(seg.id, inputs={})
    repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full, thumbnail_path=None)
    assert model_repo.head_version(seg.id) is None
    first = client.get(_url(seg), headers=auth).json()["data"]
    second = client.get(_url(seg), headers=auth).json()["data"]

    assert first["head_version"] == 0 and first["legacy"] is True and first["placement_verified"] is False
    assert [v["kind"] for v in second["versions"]] == ["legacy"]  # idempotent: imported once
    assert model_repo.get_version(seg.id, 0).scene["schema_version"] == 1


# ------------------------------------------------------------------ versions / diff


def test_version_detail_includes_model_fidelity_and_diff(client, street, auth):
    data = client.get(_url(street, "/versions/1"), headers=auth).json()["data"]
    assert data["version"] == 1 and data["kind"] == "fused" and data["parent_version"] == 0
    assert data["model"]["version"] == 1 and len(data["model"]["entities"]) == 1
    assert data["fidelity"]["overall"]["level"] == 1
    assert data["diff"]["from_version"] == 0 and data["diff"]["to_version"] == 1
    assert data["summary"].startswith("Summary of version 1")


def test_version_zero_has_null_diff(client, street, auth):
    data = client.get(_url(street, "/versions/0"), headers=auth).json()["data"]
    assert data["diff"] is None and data["kind"] == "prior"


@pytest.mark.parametrize("n", [99])
def test_unknown_version_404(client, street, auth, n):
    resp = client.get(_url(street, f"/versions/{n}"), headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_diff_between_versions(client, street, auth):
    data = client.get(_url(street, "/diff?from=1&to=2"), headers=auth).json()["data"]
    assert data["from_version"] == 1 and data["to_version"] == 2
    # reversed order is allowed (from < to not required)
    back = client.get(_url(street, "/diff?from=2&to=1"), headers=auth)
    assert back.status_code == 200 and back.json()["data"]["to_version"] == 1


def test_diff_unknown_version_404_and_missing_param_422(client, street, auth):
    assert client.get(_url(street, "/diff?from=1&to=9"), headers=auth).status_code == 404
    resp = client.get(_url(street, "/diff?from=1"), headers=auth)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


# ------------------------------------------------------------------ coverage


def test_coverage_default_head_lists_covering_observations(client, street, auth):
    data = client.get(_url(street, "/coverage"), headers=auth).json()["data"]
    assert len(data["observations_by_bin"]) == len(data["bins"])
    assert [b["index"] for b in data["observations_by_bin"]] == [b["index"] for b in data["bins"]]
    assert all(len(b["observation_ids"]) <= 12 for b in data["observations_by_bin"])
    covered = {oid for b in data["observations_by_bin"] for oid in b["observation_ids"]}
    fused = set(model_repo.get_version(street.id, 2).model.fused_observation_ids)
    assert covered and covered <= fused


def test_coverage_for_older_version_uses_that_versions_observations(client, street, auth):
    v1 = client.get(_url(street, "/coverage?version=1"), headers=auth).json()["data"]
    fused1 = set(model_repo.get_version(street.id, 1).model.fused_observation_ids)
    assert {oid for b in v1["observations_by_bin"] for oid in b["observation_ids"]} <= fused1
    assert v1["overall"]["level"] == 1


def test_coverage_v0_has_no_observations_and_bad_version_404(client, street, auth):
    v0 = client.get(_url(street, "/coverage?version=0"), headers=auth).json()["data"]
    assert all(b["observation_ids"] == [] for b in v0["observations_by_bin"])
    assert client.get(_url(street, "/coverage?version=42"), headers=auth).status_code == 404
    assert client.get(_url(street, "/coverage?version=-1"), headers=auth).status_code == 422


# ------------------------------------------------------------------ entities


def test_entities_ordered_by_s_with_expected_keys(client, street, auth):
    items = client.get(_url(street, "/entities"), headers=auth).json()["data"]
    assert [e["s"] for e in items] == [20.0, 40.0]
    assert set(items[0]) == {"id", "type", "subtype", "state", "s", "t", "confidence", "description",
                             "evidence_count", "first_seen_version", "last_changed_version"}
    assert items[0]["evidence_count"] == 1


def test_entities_filters(client, street, auth):
    def ids(q: str) -> list[str]:
        return [e["id"] for e in client.get(_url(street, f"/entities?{q}"), headers=auth).json()["data"]]

    assert ids("type=bollard") == ["ent-00000000a2"]
    assert ids("min_confidence=0.75") == ["ent-00000000a1"]  # v2: a1 confidence 0.8, a2 0.7
    assert ids("state=removed") == []
    assert ids("version=1") == ["ent-00000000a1"]


def test_entities_invalid_filters_422(client, street, auth):
    for q in ("type=spaceship", "state=gone", "min_confidence=1.5"):
        assert client.get(_url(street, f"/entities?{q}"), headers=auth).status_code == 422


def test_entity_detail_has_evidence_with_observation_history(client, street, auth):
    data = client.get(_url(street, "/entities/ent-00000000a1"), headers=auth).json()["data"]
    assert data["entity"]["id"] == "ent-00000000a1" and data["entity"]["confidence"] == pytest.approx(0.8)
    ev = data["evidence"][0]
    assert ev["role"] == "supports" and ev["crop_px"] == [10, 20, 30, 40]
    assert ev["observation"]["url"].startswith("/media/") and ev["observation"]["kind"] == "streetview"
    assert set(ev["observation"]) == {"id", "kind", "view", "url", "station_s", "heading_deg", "capture_date",
                                      "width_px", "height_px"}
    assert [h["version"] for h in data["history"]] == [1, 2]
    assert [h["confidence"] for h in data["history"]] == [pytest.approx(0.6), pytest.approx(0.8)]
    assert data["contradictions"] == []


def test_entity_detail_respects_version_and_404s(client, street, auth):
    assert client.get(_url(street, "/entities/ent-00000000a2?version=1"), headers=auth).status_code == 404
    v1 = client.get(_url(street, "/entities/ent-00000000a1?version=1"), headers=auth).json()["data"]
    assert [h["version"] for h in v1["history"]] == [1]
    resp = client.get(_url(street, "/entities/ent-ffffffffff"), headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------------ run report


def test_run_report_for_fixture_runs(client, street, auth):
    runs = repo.list_runs_for_segment(street.id)
    reports = [client.get(f"/api/admin/runs/{r.id}/report", headers=auth) for r in runs]
    ok_reports = [r.json()["data"] for r in reports if r.status_code == 200]
    assert len(ok_reports) == 2
    assert {r["result_version"] for r in ok_reports} == {1, 2}
    assert all(r["segment_slug"] == street.slug and r["version_created"] is True for r in ok_reports)


def test_run_report_404_for_phase1_run_and_unknown(client, test_db, auth):
    seg = repo.create_segment(SegmentCreate(
        name="P1", city="Bengaluru", start=LatLon(lat=12.97, lon=77.64), end=LatLon(lat=12.9707, lon=77.64),
        footpath_side=FootpathSide.left, slug=f"p1-{uuid.uuid4().hex[:6]}"))
    run = repo.create_run(seg.id, inputs={})
    for rid in (run.id, "no-such-run"):
        resp = client.get(f"/api/admin/runs/{rid}/report", headers=auth)
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------------ the full recorded fixture street (seeded_fixture_segment)


def test_full_fixture_street_head_versions_diff_and_evidence_with_boxes(client, seeded_fixture_segment, auth):
    seg = repo.require_segment(seeded_fixture_segment)
    head = client.get(_url(seg), headers=auth).json()["data"]

    assert head["head_version"] == 2 and head["legacy"] is False and head["placement_verified"] is True
    assert sorted(v["version"] for v in head["versions"]) == [0, 1, 2]
    assert head["counts"]["observations"] == 48 and head["counts"]["entities"] >= 5
    assert len(head["reference_line"]["points"]) <= 60

    diff = client.get(_url(seg, "/diff?from=1&to=2"), headers=auth).json()["data"]
    assert diff["from_version"] == 1 and diff["to_version"] == 2

    entities = client.get(_url(seg, "/entities"), headers=auth).json()["data"]
    assert [e["s"] for e in entities] == sorted(e["s"] for e in entities)
    boxes = 0
    for e in entities:
        detail = client.get(_url(seg, f"/entities/{e['id']}"), headers=auth).json()["data"]
        for ev in detail["evidence"]:
            ob = ev["observation"]
            assert ob["url"].startswith("/media/") and ob["width_px"] and ob["height_px"]
            if ev["crop_px"]:
                x, y, w, h = ev["crop_px"]
                assert 0 <= x and 0 <= y and x + w <= ob["width_px"] + 1 and y + h <= ob["height_px"] + 1
                boxes += 1
    assert boxes >= 3, "fixture evidence should carry detection boxes"

    assert client.get(_url(seg, "/versions/9"), headers=auth).status_code == 404
