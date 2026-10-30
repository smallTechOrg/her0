"""/api/admin/segments/{slug}/{observations,observations/upload,imagery,placement-preview} on the real app + test DB.

Google / Nominatim are replaced by the fixture transport; nothing leaves the machine and nothing is spent.
"""

from __future__ import annotations

import hashlib
import io
from functools import partial

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate
from hero.db.repo import SegmentCreate
from hero.imagery.google import GoogleImagery
from hero.model.types import ModelKind
from hero.scene.schema import FootpathSide, LatLon
from tests.fixtures.google.fake_google import FakeGoogle
from tests.fixtures.street_fixtures import make_fidelity_map, make_model, straight_reference_line

ORIGIN = (12.97400, 77.64050)
END = (12.97490, 77.64050)


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


def jpeg_bytes(size=(64, 48), color=(120, 140, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 200, 30)).save(buf, format="PNG")
    return buf.getvalue()


def upload(client, slug, auth, *, data=None, name="p.jpg", ctype="image/jpeg", **fields):
    form = {"lat": "12.97420", "lon": "77.64050", "heading_deg": "0", **{k: str(v) for k, v in fields.items()}}
    form = {k: v for k, v in form.items() if v != "None"}
    return client.post(f"/api/admin/segments/{slug}/observations/upload", headers=auth,
                       files={"file": (name, data if data is not None else jpeg_bytes(), ctype)}, data=form)


# ---------------------------------------------------------------- auth / not found


@pytest.mark.parametrize("method,path", [
    ("get", "observations"), ("post", "observations/upload"), ("post", "imagery"), ("post", "placement-preview")])
def test_every_imagery_endpoint_needs_the_admin_password(app_client, method, path):
    seg = new_segment("Auth check")
    resp = getattr(app_client, method)(f"/api/admin/segments/{seg.slug}/{path}")
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("method,path", [("get", "observations"), ("post", "imagery"), ("post", "placement-preview")])
def test_unknown_segment_is_404(app_client, auth, method, path):
    kw = {"json": {"max_images": 10}} if path == "imagery" else {}
    resp = getattr(app_client, method)(f"/api/admin/segments/nope-nope/{path}", headers=auth, **kw)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- upload


def test_valid_photo_upload_returns_201_with_view_station_and_is_served_from_media(app_client, auth, tmp_path):
    seg = new_segment("Upload ok")
    data = jpeg_bytes()
    resp = upload(app_client, seg.slug, auth, data=data, heading_deg=0, lat="12.97420", lon="77.64051", note="gate")
    assert resp.status_code == 201, resp.text
    obs = resp.json()["data"]
    sha = hashlib.sha256(data).hexdigest()
    assert obs["kind"] == "user_photo" and obs["source"] == "upload" and obs["sha256"] == sha
    assert obs["view"] == "forward" and obs["note"] == "gate" and obs["fused_in_version"] is None
    assert obs["station_s"] == pytest.approx(22.2, abs=0.5) and obs["offset_t"] == pytest.approx(-1.08, abs=0.1)
    assert obs["fov_deg"] == 70.0 and obs["pitch_deg"] == 0.0 and obs["cost_usd"] == 0.0
    assert obs["url"] == f"/media/uploads/{sha}.jpg"
    assert (tmp_path / "uploads" / f"{sha}.jpg").read_bytes() == data

    media = app_client.get(obs["url"])
    assert media.status_code == 200 and media.content == data
    assert "max-age=86400" in media.headers["cache-control"]


def test_view_classes_follow_the_heading_relative_to_the_road(app_client, auth):
    seg = new_segment("Upload views")
    got = {}
    for heading in (0, 90, 180, 270):
        got[heading] = upload(app_client, seg.slug, auth, data=jpeg_bytes(color=(heading % 250, 5, 5)),
                              heading_deg=heading).json()["data"]["view"]
    assert got == {0: "forward", 90: "right", 180: "back", 270: "left"}


def test_png_is_accepted_and_stored_with_a_png_extension(app_client, auth):
    seg = new_segment("Upload png")
    resp = upload(app_client, seg.slug, auth, data=png_bytes(), name="p.png", ctype="image/png", heading_deg=365)
    assert resp.status_code == 201 and resp.json()["data"]["url"].endswith(".png")
    assert resp.json()["data"]["heading_deg"] == 5.0
    assert app_client.get(resp.json()["data"]["url"]).status_code == 200


def test_same_photo_twice_is_idempotent(app_client, auth):
    seg = new_segment("Upload twice")
    a = upload(app_client, seg.slug, auth)
    b = upload(app_client, seg.slug, auth)
    assert (a.status_code, b.status_code) == (201, 200) and a.json()["data"]["id"] == b.json()["data"]["id"]
    assert len(model_repo.get_observations(seg.id)) == 1


@pytest.mark.parametrize("payload,ctype", [(b"this is not an image", "image/jpeg"), (b"", "image/jpeg"),
                                           (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;", "image/gif")])
def test_non_image_or_unsupported_format_is_422(app_client, auth, payload, ctype):
    seg = new_segment("Upload bad file")
    resp = upload(app_client, seg.slug, auth, data=payload, ctype=ctype)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
    assert model_repo.get_observations(seg.id) == []


def test_photo_over_the_size_limit_is_422(app_client, auth, monkeypatch):
    monkeypatch.setenv("HERO_UPLOAD_MAX_MB", "0.001")  # ~1 KB
    reset_settings()
    seg = new_segment("Upload big")
    big = jpeg_bytes(size=(600, 600), color=(1, 2, 3))
    from PIL import ImageDraw
    img = Image.effect_noise((600, 600), 80).convert("RGB")
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95); big = buf.getvalue()
    assert len(big) > 1048
    resp = upload(app_client, seg.slug, auth, data=big)
    assert resp.status_code == 422 and "HERO_UPLOAD_MAX_MB" in resp.json()["error"]["message"]


def test_camera_more_than_60m_from_the_line_is_422(app_client, auth):
    seg = new_segment("Upload far")
    far = upload(app_client, seg.slug, auth, lat="12.97420", lon="77.64150")  # ~108 m east
    near = upload(app_client, seg.slug, auth, data=jpeg_bytes(color=(9, 9, 9)), lat="12.97420", lon="77.64080")  # ~32 m east
    assert far.status_code == 422 and "reference line" in far.json()["error"]["message"]
    assert near.status_code == 201


def test_segment_without_a_reference_line_cannot_take_uploads(app_client, auth):
    seg = new_segment("Upload no line", with_line=False)
    resp = upload(app_client, seg.slug, auth)
    assert resp.status_code == 422 and "reference line" in resp.json()["error"]["message"]


@pytest.mark.parametrize("fields", [{"lat": "abc"}, {"lat": "95"}, {"lon": "200"}, {"heading_deg": "nan"},
                                    {"fov_deg": "5"}, {"pitch_deg": "120"}])
def test_bad_numeric_fields_are_422(app_client, auth, fields):
    seg = new_segment("Upload bad fields")
    resp = upload(app_client, seg.slug, auth, **fields)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_missing_file_or_missing_required_field_is_422(app_client, auth):
    seg = new_segment("Upload missing")
    no_file = app_client.post(f"/api/admin/segments/{seg.slug}/observations/upload", headers=auth,
                              data={"lat": "12.9742", "lon": "77.6405", "heading_deg": "0"})
    no_heading = app_client.post(f"/api/admin/segments/{seg.slug}/observations/upload", headers=auth,
                                 files={"file": ("p.jpg", jpeg_bytes(), "image/jpeg")}, data={"lat": "12.9742", "lon": "77.6405"})
    not_multipart = app_client.post(f"/api/admin/segments/{seg.slug}/observations/upload", headers=auth, json={"lat": 1})
    assert no_file.status_code == no_heading.status_code == not_multipart.status_code == 422


# ---------------------------------------------------------------- observations list


def test_observation_list_is_newest_first_filterable_paged_and_reports_fused_version(app_client, auth):
    seg = new_segment("Obs list")
    ids = [upload(app_client, seg.slug, auth, data=jpeg_bytes(color=(i * 20, 9, 9)), heading_deg=i * 90).json()["data"]["id"]
           for i in range(3)]
    model_repo.save_version_only(seg.id, ModelVersionCreate(
        version=1, kind="fused", parent_version=0,
        model=make_model(straight_reference_line(100.0, origin=ORIGIN), slug=seg.slug, version=1, kind=ModelKind.fused,
                         fused_observation_ids=[ids[0]]),
        fidelity=make_fidelity_map(100.0)))

    resp = app_client.get(f"/api/admin/segments/{seg.slug}/observations", headers=auth)
    data = resp.json()["data"]
    assert resp.status_code == 200 and data["total"] == 3
    assert [o["id"] for o in data["items"]] == ids[::-1]
    by_id = {o["id"]: o for o in data["items"]}
    assert by_id[ids[0]]["fused_in_version"] == 1 and by_id[ids[1]]["fused_in_version"] is None
    assert {"id", "kind", "source", "url", "lat", "lon", "heading_deg", "pitch_deg", "fov_deg", "zoom", "view", "station_s",
            "offset_t", "pano_id", "capture_date", "cost_usd", "created_at", "fused_in_version"} <= set(data["items"][0])

    paged = app_client.get(f"/api/admin/segments/{seg.slug}/observations?limit=1&offset=1", headers=auth).json()["data"]
    assert paged["total"] == 3 and [o["id"] for o in paged["items"]] == [ids[1]]
    empty = app_client.get(f"/api/admin/segments/{seg.slug}/observations?kind=streetview", headers=auth).json()["data"]
    assert empty == {"total": 0, "items": []}
    assert app_client.get(f"/api/admin/segments/{seg.slug}/observations?kind=bogus", headers=auth).status_code == 422
    assert app_client.get(f"/api/admin/segments/{seg.slug}/observations?limit=0", headers=auth).status_code == 422


def test_observations_survive_a_new_app_instance(app_client, auth):
    from hero.app import create_app

    seg = new_segment("Obs persist")
    upload(app_client, seg.slug, auth)
    with TestClient(create_app()) as fresh:
        again = fresh.get(f"/api/admin/segments/{seg.slug}/observations", headers=auth).json()["data"]
    assert again["total"] == 1


# ---------------------------------------------------------------- add more imagery (acquire-only run)


class _Run:
    def __init__(self, rid="run-123"):
        self.id, self.status = rid, "queued"


def test_add_imagery_validates_then_starts_an_acquire_run_with_the_overrides(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None):
        seen.update(slug=slug, mode=mode, overrides=plan_overrides)
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    seg = new_segment("Imagery add")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/imagery", headers=auth,
                           json={"headings": ["left", "right"], "sv_spacing_m": 20, "s_range": {"s_from": 10, "s_to": 60}})
    assert resp.status_code == 202
    assert resp.json()["data"] == {"run_id": "run-123", "status": "queued", "mode": "acquire"}
    assert seen == {"slug": seg.slug, "mode": "acquire",
                    "overrides": {"headings": ["left", "right"], "sv_spacing_m": 20.0, "s_range": {"s_from": 10.0, "s_to": 60.0}}}


@pytest.mark.parametrize("body", [{}, {"sv_spacing_m": 3}, {"sv_spacing_m": 41}, {"headings": []}, {"headings": ["up"]},
                                  {"sat_crop_zoom": 17}, {"sat_crop_zoom": 22}, {"max_images": 0}, {"max_images": 201},
                                  {"s_range": {"s_from": 50, "s_to": 10}}, {"surprise": 1}])
def test_add_imagery_rejects_empty_or_out_of_range_bodies_with_422(app_client, auth, monkeypatch, body):
    from hero.pipeline import registry

    monkeypatch.setattr(registry, "start_run", lambda *a, **k: pytest.fail("must not start a run"))
    seg = new_segment("Imagery bad")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/imagery", headers=auth, json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_add_imagery_maps_registry_refusals_to_409_and_402(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seg = new_segment("Imagery refuse")
    active = repo.create_run(seg.id, {})

    def busy(*a, **k):
        raise registry.RunInProgress(active)

    monkeypatch.setattr(registry, "start_run", busy)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/imagery", headers=auth, json={"max_images": 10})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "run_in_progress"
    assert resp.json()["error"]["detail"]["run_id"] == active.id

    def broke(*a, **k):
        raise registry.BudgetExhausted(spent_usd=50.0, budget_usd=50.0)

    monkeypatch.setattr(registry, "start_run", broke)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/imagery", headers=auth, json={"max_images": 10})
    assert resp.status_code == 402 and resp.json()["error"]["code"] == "budget_exhausted"


def test_add_imagery_reports_a_clear_error_while_the_registry_has_no_acquire_mode(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    monkeypatch.setattr(registry, "start_run", lambda slug: _Run())
    seg = new_segment("Imagery unwired")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/imagery", headers=auth, json={"max_images": 10})
    assert resp.status_code == 500 and "not wired" in resp.json()["error"]["message"]


# ---------------------------------------------------------------- placement preview


@pytest.fixture()
def fake_world(monkeypatch, tmp_path):
    """Route the endpoint's Google + Nominatim/Overpass traffic to the fixture transport."""
    from pydantic import SecretStr

    from hero.api import admin_imagery

    ref = straight_reference_line(100.0, origin=ORIGIN)
    fake = FakeGoogle(ref)

    def make_google(settings, cache, run_id):
        s = settings.model_copy(update={"google_maps_api_key": SecretStr("test-key")})
        return GoogleImagery(s, cache, run_id, http=fake.client(), sleep=lambda _s: None)

    monkeypatch.setattr(admin_imagery, "GoogleImagery", make_google)
    monkeypatch.setattr(admin_imagery, "fetch_road_polyline",
                        lambda lat, lon, **kw: __import__("hero.imagery.roads", fromlist=["x"]).fetch_road_polyline(
                            lat, lon, **{**kw, "http": fake.client()}))
    return fake


def test_placement_preview_uses_the_osm_road_draws_it_and_stores_the_prior(app_client, auth, tmp_path, fake_world):
    seg = new_segment("Placement ok", with_line=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/placement-preview", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    ref = data["reference_line"]
    assert ref["source"] == "osm_nominatim" and ref["osm_way_id"] == 4242 and ref["name"] == "100 Feet Road"
    assert ref["length_s"] == pytest.approx(100.0, abs=3.0) and ref["snap_m"] <= 25.0
    assert data["placement_verified"] is True and data["warnings"] == []
    assert data["url"].startswith(f"/media/previews/placement/{seg.slug}.png")
    img = Image.open(tmp_path / "previews" / "placement" / f"{seg.slug}.png")
    assert img.size == (1280, 1280)
    assert app_client.get(data["url"].split("?")[0]).status_code == 200
    assert model_repo.head_version(seg.id) == 0, "v0 prior stored"
    assert len(fake_world.calls_of("satellite")) == 1

    again = app_client.post(f"/api/admin/segments/{seg.slug}/placement-preview", headers=auth)
    assert again.status_code == 200 and len(fake_world.calls_of("satellite")) == 1, "second preview is a cache hit"
    assert model_repo.head_version(seg.id) == 0


def test_placement_preview_without_a_road_degrades_to_the_chord_with_a_warning(app_client, auth, monkeypatch, fake_world):
    from hero.api import admin_imagery

    monkeypatch.setattr(admin_imagery, "fetch_road_polyline", lambda *a, **k: None)
    seg = new_segment("Placement chord", with_line=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/placement-preview", headers=auth)
    data = resp.json()["data"]
    assert resp.status_code == 200 and data["reference_line"]["source"] == "straight_chord"
    assert data["placement_verified"] is False and any("straight chord" in w for w in data["warnings"])


def test_placement_preview_with_a_rejected_google_key_is_502_upstream_error(app_client, auth, monkeypatch, fake_world):
    fake_world.require_key = "other"
    seg = new_segment("Placement 403", with_line=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/placement-preview", headers=auth)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "upstream_error"
    assert "HERO_GOOGLE_MAPS_API_KEY" in resp.json()["error"]["message"]
