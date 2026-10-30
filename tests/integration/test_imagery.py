"""hero.imagery against the REAL Google + Overpass endpoints, counted on the test DB."""

from __future__ import annotations

import httpx
from datetime import UTC, datetime

import pytest
from PIL import Image

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.imagery.osm import fetch_osm
from hero.llm.errors import UpstreamError
from hero.scene.geo import LocalFrame
from hero.scene.schema import FootpathSide, LatLon, RunStatus

SEED_START = LatLon(lat=12.97400, lon=77.64050)
SEED_END = LatLon(lat=12.97467, lon=77.64058)
OCEAN = LatLon(lat=-30.0, lon=-140.0)  # South Pacific: no Street View coverage


@pytest.fixture()
def run_id(test_db):
    seg = repo.create_segment(SegmentCreate(name="Imagery test", city="Bengaluru", start=SEED_START, end=SEED_END,
                                            footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"test": True})
    yield run.id
    repo.update_run(run.id, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                           error={"code": "test", "message": "test run retired", "step": "test"}))


@pytest.fixture()
def google(run_id):
    settings = get_settings()
    return GoogleImagery(settings, ImageryCache(settings.data_dir_abs / "imagery"), run_id)


def test_satellite_tile_is_a_1280_png_and_second_fetch_is_a_counted_cache_hit(google, run_id):
    first = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)
    second = google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)

    with Image.open(first.path) as img:
        assert img.size == (1280, 1280)
    assert first.url.startswith("/media/imagery/") and first.url.endswith(".png")
    assert second.cache_hit is True and second.path == first.path
    rows = repo.list_google_requests(run_id)
    assert [r.kind for r in rows] == ["satellite", "satellite"]
    assert rows[1].cache_hit is True and rows[1].est_cost_usd == 0.0
    counts = repo.google_counts(run_id)
    assert counts.cache_hits >= 1 and counts.requests + counts.cache_hits == 2  # disk cache may already be warm


def test_streetview_metadata_ok_at_seed_and_image_fetch_is_a_jpeg(google, run_id):
    meta = google.streetview_metadata(SEED_START.lat, SEED_START.lon)
    assert meta.status in ("OK", "ZERO_RESULTS")
    if not meta.ok:
        pytest.skip(f"Street View has no coverage at the seed start right now (status {meta.status})")

    pano = google.streetview_image(SEED_START.lat, SEED_START.lon, 6.7)

    with Image.open(pano.path) as img:
        assert img.size == (640, 640)
    kinds = [r.kind for r in repo.list_google_requests(run_id)]
    assert kinds == ["streetview_metadata", "streetview"]


def test_streetview_metadata_non_ok_in_the_ocean_is_a_status_not_an_exception(google, run_id):
    meta = google.streetview_metadata(OCEAN.lat, OCEAN.lon)

    assert meta.ok is False and meta.status == "ZERO_RESULTS"
    rows = repo.list_google_requests(run_id)
    assert rows[-1].kind == "streetview_metadata" and rows[-1].est_cost_usd == 0.0


def test_bad_google_key_raises_upstream_error_with_env_hint_and_caches_nothing(run_id, tmp_path):
    settings = get_settings().model_copy(update={"google_maps_api_key": type(get_settings().google_maps_api_key)("bad-key")})
    google = GoogleImagery(settings, ImageryCache(tmp_path / "imagery"), run_id, sleep=lambda _s: None)

    with pytest.raises(UpstreamError) as exc:
        google.satellite_tile(SEED_START.lat, SEED_START.lon, 19)

    assert "HERO_GOOGLE_MAPS_API_KEY" in exc.value.message
    assert exc.value.detail["kind"] == "satellite"
    assert list((tmp_path / "imagery").glob("*.png")) == []


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
