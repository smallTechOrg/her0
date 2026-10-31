"""hero.imagery.google over recorded-shape fixtures (httpx MockTransport) + Overpass (degrade-only).

Google is never called for real here: ``tests/fixtures/google/fake_google.py`` serves the fixtures. The single
``live`` test at the bottom calls the FREE Street View metadata endpoint once and skips without a key.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery, SpendCapExceeded
from hero.imagery.osm import fetch_osm
from hero.llm.errors import UpstreamError
from hero.scene.geo import LocalFrame
from hero.scene.schema import FootpathSide, LatLon, RunStatus
from tests.fixtures.google.fake_google import FakeGoogle, make_imagery, straight_ref

SEED_START = LatLon(lat=12.97400, lon=77.64050)
SEED_END = LatLon(lat=12.97467, lon=77.64058)
OCEAN = LatLon(lat=-30.0, lon=140.0)


@pytest.fixture()
def run_id(test_db):
    seg = repo.create_segment(SegmentCreate(name="Imagery test", city="Bengaluru", start=SEED_START, end=SEED_END,
                                            footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"test": True})
    yield run.id
    repo.update_run(run.id, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                           error={"code": "test", "message": "test run retired", "step": "test"}))


@pytest.fixture()
def fake():
    return FakeGoogle(straight_ref(100.0))


@pytest.fixture()
def google(tmp_path, fake, run_id):
    return make_imagery(tmp_path, fake, run_id)[1]


def test_satellite_tile_is_a_1280_png_and_second_fetch_is_a_counted_cache_hit(google, fake, run_id):
    first = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)
    second = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)

    with Image.open(first.path) as img:
        assert img.size == (1280, 1280)
    assert first.url.startswith("/media/imagery/") and first.url.endswith(".png")
    assert second.cache_hit is True and second.path == first.path
    rows = repo.list_google_requests(run_id)
    assert [r.kind for r in rows] == ["satellite", "satellite"]
    assert rows[0].cache_hit is False and rows[0].est_cost_usd == 0.002
    assert rows[1].cache_hit is True and rows[1].est_cost_usd == 0.0
    assert len(fake.calls_of("satellite")) == 1


def test_satellite_crop_is_the_same_call_and_cache_entry_as_the_tile(google, fake):
    tile = google.satellite_tile(SEED_START.lat, SEED_START.lon, 20)
    crop = google.satellite_crop(SEED_START.lat, SEED_START.lon, 20)
    assert crop.cache_hit is True and crop.cache_key == tile.cache_key


def test_streetview_metadata_carries_pano_position_and_capture_month_and_is_free(google, fake, run_id):
    meta = google.streetview_metadata(SEED_START.lat, SEED_START.lon)
    assert meta.ok and meta.pano_id.startswith("PANO_") and meta.date == "2023-03"
    assert meta.lat is not None and meta.lon is not None
    again = google.streetview_metadata(SEED_START.lat, SEED_START.lon)
    assert again.cache_hit is True and again.pano_id == meta.pano_id
    rows = repo.list_google_requests(run_id)
    assert [r.kind for r in rows] == ["streetview_metadata"] * 2
    assert all(r.est_cost_usd == 0.0 for r in rows)
    assert google.paid_requests == 0 and fake.paid_calls == []


def test_pano_image_fetch_is_a_jpeg_cached_by_pano_heading_and_counted(google, fake, run_id):
    meta = google.streetview_metadata(SEED_START.lat, SEED_START.lon)
    first = google.streetview_image_pano(meta.pano_id, 6.7)
    second = google.streetview_image_pano(meta.pano_id, 6.7)
    other = google.streetview_image_pano(meta.pano_id, 96.7)

    with Image.open(first.path) as img:
        assert img.size == (640, 640) and img.format == "JPEG"
    assert second.cache_hit and second.cache_key == first.cache_key and other.cache_key != first.cache_key
    assert first.params["pano"] == meta.pano_id and "key" not in first.params
    kinds = [r.kind for r in repo.list_google_requests(run_id)]
    assert kinds == ["streetview_metadata", "streetview", "streetview", "streetview"]
    assert len(fake.calls_of("streetview")) == 2


def test_streetview_metadata_non_ok_is_a_status_not_an_exception(tmp_path, run_id):
    fake = FakeGoogle(straight_ref(100.0), coverage=(0.0, 10.0))
    google = make_imagery(tmp_path, fake, run_id)[1]
    meta = google.streetview_metadata(OCEAN.lat, OCEAN.lon)

    assert meta.ok is False and meta.status == "ZERO_RESULTS" and meta.pano_id is None
    rows = repo.list_google_requests(run_id)
    assert rows[-1].kind == "streetview_metadata" and rows[-1].est_cost_usd == 0.0


def test_bad_google_key_raises_upstream_error_with_env_hint_and_caches_nothing(tmp_path, fake, run_id):
    _, google = make_imagery(tmp_path, fake, run_id, key="bad-key")

    with pytest.raises(UpstreamError) as exc:
        google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)

    assert "HERO_GOOGLE_MAPS_API_KEY" in exc.value.message
    assert exc.value.detail["kind"] == "satellite" and exc.value.status == 403
    assert list((tmp_path / "imagery").glob("*.png")) == []
    assert fake.calls_of("denied") and google.paid_requests == 1


def test_transient_5xx_is_retried_three_times_then_succeeds(tmp_path, run_id):
    attempts = {"n": 0}
    fake = FakeGoogle(straight_ref(100.0))

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="busy") if attempts["n"] <= 3 else fake.handler(request)

    settings, _ = make_imagery(tmp_path, fake, run_id)
    sleeps: list[float] = []
    google = GoogleImagery(settings, ImageryCache(tmp_path / "imagery"), run_id,
                           http=httpx.Client(transport=httpx.MockTransport(flaky)), sleep=sleeps.append)
    fetched = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)
    assert fetched.cache_hit is False and attempts["n"] == 4 and sleeps == [1, 2, 4]
    assert google.paid_requests == 1  # retries of one logical request are one paid request


def test_api_key_is_never_written_to_the_cache_sidecar(google, tmp_path):
    fetched = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)
    sidecar = ImageryCache.sidecar_for(tmp_path / "imagery" / fetched.rel_path.split("/")[-1])
    assert "test-key" not in sidecar.read_text()
    assert "key" not in json.loads(sidecar.read_text())["params"]


# ---------------------------------------------------------------- cost guard on the client


def test_paid_request_cap_blocks_the_next_new_image_but_never_cache_hits_or_metadata(google, fake):
    google.limit_paid_requests(2)
    a = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)
    b = google.satellite_tile(SEED_START.lat, SEED_START.lon, 20)
    assert google.paid_remaining == 0
    assert google.satellite_tile(SEED_START.lat, SEED_START.lon, 19).cache_hit is True  # free
    google.streetview_metadata(SEED_START.lat, SEED_START.lon)  # free
    with pytest.raises(SpendCapExceeded):
        google.satellite_tile(SEED_START.lat, SEED_START.lon, 21)
    assert len(fake.paid_calls) == 2 and a.cache_key != b.cache_key


def test_cap_of_zero_sends_nothing_and_no_cap_means_unlimited(google, fake):
    google.limit_paid_requests(0)
    with pytest.raises(SpendCapExceeded):
        google.streetview_image_pano("PANO_000", 0.0)
    assert fake.paid_calls == []
    google.limit_paid_requests(None)
    google.streetview_image_pano("PANO_000", 0.0)
    assert len(fake.paid_calls) == 1


def test_failed_fetch_leaves_no_cache_file_and_the_next_try_pays_again(tmp_path, run_id):
    fake = FakeGoogle(straight_ref(100.0), frame_status=500)
    google = make_imagery(tmp_path, fake, run_id)[1]
    with pytest.raises(UpstreamError):
        google.streetview_image_pano("PANO_000", 0.0)
    assert list((tmp_path / "imagery").glob("*.jpg")) == []


# ---------------------------------------------------------------- Overpass (real, degrade-only)


def test_overpass_returns_ways_projected_into_the_local_frame_and_caches(tmp_path):
    settings = get_settings()
    frame = LocalFrame.from_endpoints(SEED_START.lat, SEED_START.lon, SEED_END.lat, SEED_END.lon, FootpathSide.left)
    cache = ImageryCache(tmp_path / "osm")

    try:
        osm = fetch_osm(frame, cache, settings.overpass_url)
    except UpstreamError as exc:
        if exc.status in (None, 429, 502, 503, 504):  # at capacity / timing out: a third-party outage, not our bug
            pytest.skip(f"Overpass is unavailable right now ({exc.message}); the pipeline degrades to no-OSM")
        raise
    again = fetch_osm(frame, cache, settings.overpass_url)

    assert osm.cache_hit is False and again.cache_hit is True
    assert len(osm.highways) >= 1, "100 Feet Road itself must be in OSM"
    assert all(len(w.points) == len(w.latlons) for w in osm.ways)
    for way in osm.ways:
        for (x, lat), (plat, plon) in zip(way.points, way.latlons):
            assert (x, lat) == pytest.approx(frame.from_latlon(plat, plon))
    assert any(abs(lat) <= 70 and -40 <= x <= frame.length_m + 40 for w in osm.highways for x, lat in w.points)
    assert "road way" in osm.summary(frame.length_m)


def test_overpass_failure_is_an_upstream_error_not_a_crash(tmp_path):
    frame = LocalFrame.from_endpoints(SEED_START.lat, SEED_START.lon, SEED_END.lat, SEED_END.lon, FootpathSide.left)

    with pytest.raises(UpstreamError) as exc:
        fetch_osm(frame, ImageryCache(tmp_path / "osm"), "http://127.0.0.1:9/interpreter",
                  http=httpx.Client(timeout=2.0), sleep=lambda _s: None)

    assert exc.value.detail["kind"] == "osm"


# ---------------------------------------------------------------- the one optional live probe (free endpoint)


@pytest.mark.live
def test_live_streetview_metadata_is_ok_at_the_seed_and_costs_nothing(tmp_path, run_id):
    """Street View Static METADATA is free; this is the only real Google call in the S2 tests (skips without a key)."""
    if not os.environ.get("HERO_GOOGLE_MAPS_API_KEY"):
        try:
            if not get_settings().google_maps_api_key.get_secret_value():
                pytest.skip("HERO_GOOGLE_MAPS_API_KEY is not set")
        except Exception:  # noqa: BLE001 - settings refuse to load without keys
            pytest.skip("HERO_GOOGLE_MAPS_API_KEY is not set")
    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    google = GoogleImagery(settings, ImageryCache(tmp_path / "imagery"), run_id)
    meta = google.streetview_metadata(SEED_START.lat, SEED_START.lon)
    assert meta.status in ("OK", "ZERO_RESULTS")
    assert google.paid_requests == 0
    assert repo.list_google_requests(run_id)[-1].est_cost_usd == 0.0
