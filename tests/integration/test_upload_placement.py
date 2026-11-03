"""POST /api/admin/segments/{slug}/observations/upload with s+side placement, facing, captured_at, provenance and the
``affects`` advice (an uploaded close-up can lift a stretch to L3, recorded Bellandur model)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate, ObservationCreate
from hero.db.repo import SegmentCreate
from hero.model.fidelity import compute_fidelity
from hero.model.types import CanonicalModel, ModelKind, Observation
from hero.scene.schema import FootpathSide, LatLon
from tests.fixtures.street_fixtures import make_fidelity_map, make_model, straight_reference_line

ORIGIN = (12.97400, 77.64050)
END = (12.97490, 77.64050)
BELL = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "bellandur"


@pytest.fixture()
def app_client(test_db, tmp_path, monkeypatch):
    from hero.app import create_app

    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
    reset_settings()


@pytest.fixture()
def auth(app_client):
    class Redacted(dict):
        __repr__ = __str__ = lambda self: "<admin auth (redacted)>"  # noqa: E731

    return Redacted({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})


def new_segment(name: str, *, with_line: bool = True):
    seg = repo.create_segment(SegmentCreate(name=name, city="Bengaluru", start=LatLon(lat=ORIGIN[0], lon=ORIGIN[1]),
                                            end=LatLon(lat=END[0], lon=END[1]), footpath_side=FootpathSide.left))
    if with_line:
        ref = straight_reference_line(100.0, origin=ORIGIN)
        model_repo.save_version_only(seg.id, ModelVersionCreate(
            version=0, kind="prior", model=make_model(ref, slug=seg.slug), fidelity=make_fidelity_map(ref.length_s)))
    return seg


def jpeg(color=(120, 140, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return buf.getvalue()


def post(client, slug, auth, *, data=None, **fields):
    form = {k: str(v) for k, v in fields.items() if v is not None}
    return client.post(f"/api/admin/segments/{slug}/observations/upload", headers=auth,
                       files={"file": ("p.jpg", data if data is not None else jpeg(), "image/jpeg")}, data=form)


def line():
    return straight_reference_line(100.0, origin=ORIGIN)   # due north, left = west


# ---------------------------------------------------------------- s + side placement


@pytest.mark.parametrize("side,t", [("left", 5.5), ("right", -5.5), ("centre", 0.0)])
def test_s_side_places_the_camera_5_5_m_off_the_line(app_client, auth, side, t):
    seg = new_segment(f"Place {side}")
    resp = post(app_client, seg.slug, auth, s=40, side=side, facing="forward")
    assert resp.status_code == 201, resp.text
    d = resp.json()["data"]
    lat, lon = line().point_at(40.0, t)
    assert (d["lat"], d["lon"]) == pytest.approx((lat, lon), abs=2e-7)
    assert d["station_s"] == pytest.approx(40.0, abs=0.01) and d["offset_t"] == pytest.approx(t, abs=0.01)
    assert d["kind"] == "user_photo" and d["source"] == "upload" and d["cost_usd"] == 0.0
    assert d["placement"] == {"s": pytest.approx(40.0, abs=0.01), "t": pytest.approx(t, abs=0.01), "side": side,
                              "facing": "forward", "captured_at": None, "how": "s_side", "lat": pytest.approx(lat, abs=2e-7),
                              "lon": pytest.approx(lon, abs=2e-7), "heading_deg": pytest.approx(0.0, abs=0.01)}


@pytest.mark.parametrize("facing,heading,view", [("forward", 0, "forward"), ("back", 180, "back"), ("left", 270, "left"),
                                                 ("right", 90, "right"), ("toward_left", 270, "left"),
                                                 ("toward_right", 90, "right")])
def test_facing_becomes_a_heading_from_the_tangent_and_a_view_class(app_client, auth, facing, heading, view):
    seg = new_segment(f"Facing {facing}")
    d = post(app_client, seg.slug, auth, data=jpeg((heading % 250, 1, 1)), s=30, side="right", facing=facing).json()["data"]
    assert d["heading_deg"] == pytest.approx(heading) and d["view"] == view
    assert d["placement"]["facing"] == facing


def test_facing_follows_a_curved_tangent(app_client, auth, test_db):
    from tests.fixtures.street_fixtures import curved_reference_line

    seg = repo.create_segment(SegmentCreate(name="Facing curved", city="Bengaluru", start=LatLon(lat=ORIGIN[0], lon=ORIGIN[1]),
                                            end=LatLon(lat=END[0], lon=END[1]), footpath_side=FootpathSide.left))
    ref = curved_reference_line()
    model_repo.save_version_only(seg.id, ModelVersionCreate(
        version=0, kind="prior", model=make_model(ref, slug=seg.slug), fidelity=make_fidelity_map(ref.length_s)))
    d = post(app_client, seg.slug, auth, s=80, side="left", facing="forward").json()["data"]
    assert d["heading_deg"] == pytest.approx(ref.bearing_deg(80.0), abs=0.3) and abs(d["heading_deg"]) > 1.0


def test_explicit_heading_wins_over_facing(app_client, auth):
    seg = new_segment("Heading wins")
    d = post(app_client, seg.slug, auth, s=30, side="left", facing="back", heading_deg=95).json()["data"]
    assert d["heading_deg"] == 95.0 and d["view"] == "right"


def test_lat_lon_placement_still_works_and_is_recorded(app_client, auth):
    seg = new_segment("Latlon how")
    d = post(app_client, seg.slug, auth, lat="12.97420", lon="77.64051", heading_deg=0).json()["data"]
    assert d["placement"]["how"] == "latlon" and d["placement"]["side"] in {"left", "right", "centre"}
    assert d["placement"]["s"] == pytest.approx(22.2, abs=0.5)


# ---------------------------------------------------------------- captured_at

@pytest.mark.parametrize("given,stored", [("2024-05", "2024-05"), ("2023-11-30", "2023-11"), (None, None)])
def test_captured_at_is_stored_as_year_month(app_client, auth, given, stored):
    seg = new_segment("Captured ok")
    d = post(app_client, seg.slug, auth, s=10, side="left", facing="forward", captured_at=given).json()["data"]
    assert d["capture_date"] == stored and d["placement"]["captured_at"] == stored


def test_a_future_captured_at_is_422(app_client, auth):
    seg = new_segment("Captured future")
    nxt = datetime.now(UTC)
    future = f"{nxt.year + 1}-{nxt.month:02d}"
    resp = post(app_client, seg.slug, auth, s=10, side="left", facing="forward", captured_at=future)
    assert resp.status_code == 422 and "future" in resp.json()["error"]["message"]
    assert model_repo.get_observations(seg.id) == []


def test_current_month_is_not_the_future(app_client, auth):
    seg = new_segment("Captured now")
    now = datetime.now(UTC)
    assert post(app_client, seg.slug, auth, s=10, side="left", facing="forward",
                captured_at=f"{now.year}-{now.month:02d}").status_code == 201


@pytest.mark.parametrize("bad", ["2024", "2024-13", "2024-02-30", "May 2024", "2024/05"])
def test_malformed_captured_at_is_422(app_client, auth, bad):
    seg = new_segment("Captured bad")
    assert post(app_client, seg.slug, auth, s=10, side="left", facing="forward", captured_at=bad).status_code == 422


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("fields", [
    {},                                                              # neither placement
    {"side": "left", "facing": "forward"},                           # side without s
    {"s": 10, "facing": "forward"},                                  # s without side
    {"s": 10, "side": "up", "facing": "forward"},                    # bad side
    {"s": -1, "side": "left", "facing": "forward"},                  # s below range
    {"s": 100.5, "side": "left", "facing": "forward"},               # s above range
    {"s": "abc", "side": "left", "facing": "forward"},
    {"s": 10, "side": "left"},                                       # no facing / heading
    {"s": 10, "side": "left", "facing": "sideways"},
    {"lat": "12.9742", "lon": "77.6405"},                            # no heading / facing
    {"lat": "12.9742", "facing": "forward"},                         # lon missing
    {"lat": "12.9742", "lon": "77.64150", "heading_deg": 0},         # > 60 m from the line
])
def test_invalid_placement_is_422_and_stores_nothing(app_client, auth, fields):
    seg = new_segment("Place invalid")
    resp = post(app_client, seg.slug, auth, **fields)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error", resp.text
    assert model_repo.get_observations(seg.id) == []


def test_unreadable_image_is_422(app_client, auth):
    seg = new_segment("Place unreadable")
    resp = post(app_client, seg.slug, auth, data=b"not an image", s=10, side="left", facing="forward")
    assert resp.status_code == 422


def test_no_reference_line_is_422_for_s_placement(app_client, auth):
    seg = new_segment("Place no line", with_line=False)
    resp = post(app_client, seg.slug, auth, s=10, side="left", facing="forward")
    assert resp.status_code == 422 and "reference line" in resp.json()["error"]["message"]


# ---------------------------------------------------------------- idempotence + persistence


def test_reupload_is_200_with_the_original_placement(app_client, auth):
    seg = new_segment("Place twice")
    data = jpeg((9, 9, 9))
    a = post(app_client, seg.slug, auth, data=data, s=20, side="left", facing="forward", captured_at="2024-01")
    b = post(app_client, seg.slug, auth, data=data, s=70, side="right", facing="back", captured_at="2022-02")
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["data"]["id"] == b.json()["data"]["id"]
    assert b.json()["data"]["placement"] == a.json()["data"]["placement"] and b.json()["data"]["capture_date"] == "2024-01"
    assert len(model_repo.get_observations(seg.id)) == 1


def test_placement_survives_a_new_app_instance(app_client, auth):
    from hero.app import create_app

    seg = new_segment("Place persist")
    sent = post(app_client, seg.slug, auth, s=33, side="left", facing="right", captured_at="2024-03").json()["data"]
    with TestClient(create_app()) as fresh:
        listed = fresh.get(f"/api/admin/segments/{seg.slug}/observations", headers=auth).json()["data"]["items"][0]
    assert listed["id"] == sent["id"] and listed["capture_date"] == "2024-03" and listed["station_s"] == pytest.approx(33.0, abs=0.01)
    stored = model_repo.get_observation(sent["id"])
    assert stored.georef["placement"]["how"] == "s_side" and stored.georef["placement"]["side"] == "left"


def test_batch_of_photos_is_sent_one_per_request_and_each_is_stored(app_client, auth):
    seg = new_segment("Place batch")
    codes = [post(app_client, seg.slug, auth, data=jpeg((i * 30, 5, 5)), s=10 + 20 * i, side="left", facing="left").status_code
             for i in range(4)]
    assert codes == [201] * 4 and len(model_repo.get_observations(seg.id)) == 4


# ---------------------------------------------------------------- affects (fidelity advice)


def test_affects_on_a_stretch_without_a_model_of_both_sides_stays_at_l2(app_client, auth):
    seg = new_segment("Affects prior")
    d = post(app_client, seg.slug, auth, s=50, side="left", facing="left").json()["data"]
    a = d["affects"]
    assert a["can_reach_level"] == 2 and a["s_from"] == pytest.approx(44.0, abs=0.5) and a["s_to"] == pytest.approx(56.0, abs=0.5)
    assert isinstance(a["message"], str) and a["message"]


def seed_bellandur(name: str):
    """A segment carrying the recorded Bellandur model (all L2) with its 48 recorded observations."""
    seg = repo.create_segment(SegmentCreate(name=name, city="Bengaluru", start=LatLon(lat=ORIGIN[0], lon=ORIGIN[1]),
                                            end=LatLon(lat=END[0], lon=END[1]), footpath_side=FootpathSide.left))
    model = CanonicalModel.model_validate(json.loads((BELL / "model_v1.json").read_text()))
    recs = [Observation.model_validate(o) for o in json.loads((BELL / "observations.json").read_text())]
    for o in recs:
        data = o.model_dump(mode="python", exclude={"segment_id"})
        data.update(segment_id=seg.id)
        model_repo.upsert_observation(ObservationCreate.model_validate(
            {k: v for k, v in data.items() if k in ObservationCreate.model_fields}))
    by_id = {o.id: o for o in recs}
    fused = model.model_copy(update={"segment_slug": seg.slug, "version": 0, "parent_version": None, "kind": ModelKind.fused,
                                     "run_id": None})
    model_repo.save_version_only(seg.id, ModelVersionCreate(version=0, kind="fused", model=fused,
                                                            fidelity=compute_fidelity(fused, by_id)))
    return seg, fused, by_id


def test_an_uploaded_closeup_lifts_a_recorded_l2_stretch_to_l3(app_client, auth):
    seg, model, by_id = seed_bellandur("Affects bellandur")
    before = compute_fidelity(model, by_id)
    assert before.overall.histogram_m["L3"] == 0.0 and before.overall.histogram_m["L2"] == pytest.approx(100.0)
    d = post(app_client, seg.slug, auth, s=50, side="left", facing="left").json()["data"]
    assert d["affects"]["can_reach_level"] == 3 and "L3" in d["affects"]["message"]
    assert d["affects"]["s_from"] == pytest.approx(44.0, abs=0.5) and d["affects"]["s_to"] == pytest.approx(56.0, abs=0.5)
    # the advice matches what fusing the stored photo would really produce
    stored = model_repo.get_observation(d["id"])
    assert stored.kind.value == "user_photo"
    fused = model.model_copy(update={"fused_observation_ids": sorted({*model.fused_observation_ids, stored.id})})
    after = compute_fidelity(fused, {**by_id, stored.id: stored})
    assert any(b.level == 3 for b in after.bins) and after.overall.histogram_m["L3"] >= 5.0
