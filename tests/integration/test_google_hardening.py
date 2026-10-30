"""QA-audit hardening: only stable Google statuses are cached, failed paid attempts and geocode calls are ledgered,
the cache temp name is unique per thread, and the pre-run Google estimate counts only NEW (unstored) frames.
All over ``httpx.MockTransport`` - no network, no real key."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from hero.config import get_settings
from hero.db import model_repo, repo
from hero.db.repo import SegmentCreate
from hero.imagery import geocode as geo
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.llm.errors import UpstreamError
from hero.pipeline import registry
from hero.scene.schema import FootpathSide, LatLon, RunStatus
from tests.fixtures.google.fake_google import make_imagery, FakeGoogle, straight_ref
from tests.integration.test_fusion_run import ALL_FOUR, FORWARD_BACK, new_world

START = LatLon(lat=12.97400, lon=77.64050)
END = LatLon(lat=12.97467, lon=77.64058)


@pytest.fixture()
def run_id(test_db):
    seg = repo.create_segment(SegmentCreate(name="Hardening", city="Bengaluru", start=START, end=END,
                                            footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"test": True})
    yield run.id
    repo.update_run(run.id, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                           error={"code": "test", "message": "retired", "step": "test"}))


def _google(tmp_path, handler, run_id):
    settings = get_settings().model_copy(update={"google_maps_api_key": SecretStr("k"), "data_dir": tmp_path})
    return GoogleImagery(settings, ImageryCache(tmp_path / "imagery"), run_id,
                         http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _s: None)


# ------------------------------------------------------------------ status caching


@pytest.mark.parametrize("status", ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "UNKNOWN_ERROR", "INVALID_REQUEST"])
def test_streetview_metadata_error_status_raises_and_is_not_cached(tmp_path, run_id, status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"status": status})

    google = _google(tmp_path, handler, run_id)
    for _ in range(2):  # the second attempt must go back to Google, not hit a poisoned cache
        with pytest.raises(UpstreamError):
            google.streetview_metadata(12.97, 77.64)
    assert len(calls) == 2
    assert list((tmp_path / "imagery").glob("*")) == []


@pytest.mark.parametrize("status", ["OK", "ZERO_RESULTS", "NOT_FOUND"])
def test_streetview_metadata_stable_statuses_are_cached(tmp_path, run_id, status):
    google = _google(tmp_path, lambda req: httpx.Response(200, json={"status": status}), run_id)
    assert google.streetview_metadata(12.97, 77.64).status == status
    assert google.streetview_metadata(12.97, 77.64).cache_hit is True


@pytest.mark.parametrize("status", ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "UNKNOWN_ERROR", "INVALID_REQUEST"])
def test_geocode_google_error_status_is_not_cached_and_nominatim_still_answers(tmp_path, status):
    recorded = []

    def handler(req):
        if req.url.host == "maps.googleapis.com":
            return httpx.Response(200, json={"status": status, "results": []})
        return httpx.Response(200, json=[{"display_name": "X", "lat": "1.0", "lon": "2.0"}])

    cache = ImageryCache(tmp_path)
    cands = geo.geocode("somewhere", "City", google_key="k", cache=cache,
                        http=httpx.Client(transport=httpx.MockTransport(handler)),
                        record_google=lambda *a: recorded.append(a))
    assert [c.provider for c in cands] == ["openstreetmap"]
    assert not [p for p in tmp_path.glob("*.json") if not p.name.endswith(".params.json")
                and '"geocode_google"' in cache.sidecar_for(p).read_text()]
    assert recorded and recorded[0][1] is False  # the sent request is ledgered as a miss


def test_geocode_google_zero_results_is_cached_and_second_call_is_a_recorded_hit(tmp_path):
    recorded = []
    sent = []

    def handler(req):
        if req.url.host == "maps.googleapis.com":
            sent.append(1)
            return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(2):
        geo.geocode("nowhere", "City", google_key="k", cache=ImageryCache(tmp_path), http=client,
                    record_google=lambda *a: recorded.append(a))
    assert len(sent) == 1
    assert [r[1] for r in recorded] == [False, True]


def test_resolve_records_geocode_rows_in_google_requests(test_db, tmp_path):
    def handler(req):
        if req.url.host == "maps.googleapis.com":
            return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        return httpx.Response(200, json=[])

    before = repo.google_counts(None)
    geo.geocode("q", "c", google_key="k", cache=ImageryCache(tmp_path), http=httpx.Client(transport=httpx.MockTransport(handler)),
                record_google=geo._record_geocode)
    after = repo.google_counts(None)
    assert after.requests + after.cache_hits == before.requests + before.cache_hits + 1


# ------------------------------------------------------------------ failed paid attempts are ledgered


def test_failed_paid_attempt_is_recorded_in_google_requests(tmp_path, run_id):
    google = _google(tmp_path, lambda req: httpx.Response(403, json={"error": "denied"}), run_id)
    with pytest.raises(UpstreamError):
        google.satellite_tile(12.97, 77.64, 19)
    with pytest.raises(UpstreamError):
        google.streetview_image_pano("PANO_X", 10.0)
    rows = repo.list_google_requests(run_id)
    assert [(r.kind, r.cache_hit) for r in rows] == [("satellite", False), ("streetview", False)]
    assert rows[0].est_cost_usd > 0 and rows[0].path is None
    assert list((tmp_path / "imagery").glob("*.png")) == []


# ------------------------------------------------------------------ cache temp names


def test_atomic_write_temp_name_is_unique_per_thread(tmp_path, monkeypatch):
    import hero.imagery.cache as cache_mod

    names: list[str] = []
    real_replace = cache_mod.os.replace
    barrier = threading.Barrier(2, timeout=5)

    def spy(src, dst):
        names.append(str(src))
        try:
            barrier.wait()  # both threads hold their temp file at once
        except threading.BrokenBarrierError:
            pass
        real_replace(src, dst)

    monkeypatch.setattr(cache_mod.os, "replace", spy)
    target = tmp_path / "a.png"
    ts = [threading.Thread(target=ImageryCache._atomic_write, args=(target, b"x" * (i + 1))) for i in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert len(names) == 2 and len(set(names)) == 2
    assert target.exists()


# ------------------------------------------------------------------ google estimate counts NEW frames only


def test_estimate_after_forward_back_prices_only_the_left_right_frames(test_db, tmp_path, monkeypatch):
    w = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    try:
        settings = get_settings()
        assert w.run(overrides=FORWARD_BACK).status is RunStatus.succeeded
        assert registry._google_estimate_usd(w.seg, settings, FORWARD_BACK) == 0.0

        estimate = registry._google_estimate_usd(w.seg, settings, ALL_FOUR)
        assert estimate > 0

        run2 = w.run(overrides=ALL_FOUR)
        assert run2.status is RunStatus.succeeded
        actual = sum(r.est_cost_usd for r in repo.list_google_requests(run2.id) if not r.cache_hit)
        assert estimate == pytest.approx(actual, abs=1e-6)
        assert registry._google_estimate_usd(w.seg, settings, ALL_FOUR) == 0.0
    finally:
        from hero.config import reset_settings
        reset_settings()


def test_stored_frames_outside_the_plan_do_not_discount_the_estimate(test_db, tmp_path, monkeypatch):
    from hero.config import reset_settings

    w = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    try:
        settings = get_settings()
        assert w.run(overrides={**ALL_FOUR, "s_range": {"s_from": 0, "s_to": 40}}).status is RunStatus.succeeded
        stored = sum(model_repo.observation_counts(w.seg.id).values())
        assert stored > 10
        upper = {**ALL_FOUR, "s_range": {"s_from": 60, "s_to": 100}}
        # none of the stored frames are in the upper window: the old count-based formula would have discounted them
        assert registry._google_estimate_usd(w.seg, settings, upper) > 0.007 * 8
    finally:
        reset_settings()
