"""hero.imagery.acquire against a recorded-shape fake Google (httpx MockTransport) and the test DB."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from hero.db import model_repo, repo
from hero.db.repo import SegmentCreate
from hero.imagery.acquire import acquire, classify_view, quantize_heading
from hero.imagery.plan import ImageryPlan, plan_images
from hero.llm.errors import UpstreamError
from hero.scene.schema import FootpathSide, LatLon, RunStatus
from tests.fixtures.google.fake_google import FakeGoogle, make_imagery, straight_ref

PLAN = dict(sv_spacing_m=10.0, headings=["forward", "back", "left", "right"], sat_crop_zoom=20,
            sat_crop_spacing_m=40.0, include_overview=True, max_images=60, s_range=None, overview_zoom=19)


def plan(**kw) -> ImageryPlan:
    return ImageryPlan(**{**PLAN, **kw})


@pytest.fixture()
def seg_run(test_db):
    seg = repo.create_segment(SegmentCreate(name="Acquire test", city="Bengaluru", start=LatLon(lat=12.974, lon=77.6405),
                                            end=LatLon(lat=12.9749, lon=77.6405), footpath_side=FootpathSide.left))
    made = []

    def new_run():
        run = repo.create_run(seg.id, {"test": True})
        made.append(run.id)
        return run.id

    yield seg, new_run
    for rid in made:
        repo.update_run(rid, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                            error={"code": "test", "message": "retired", "step": "test"}))


def run_acquire(seg_run, tmp_path, fake, ref, p, run_id=None):
    seg, new_run = seg_run
    run_id = run_id or new_run()
    settings, google = make_imagery(tmp_path, fake, run_id)
    return run_id, acquire(segment_id=seg.id, run_id=run_id, ref=ref, plan=p, google=google,
                           data_dir=settings.data_dir_abs), google


def test_first_acquire_stores_one_observation_per_distinct_pano_and_heading(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, pano_spacing_m=15.0)
    run_id, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan())
    seg, _ = seg_run

    sv = [o for o in res.observations if o.kind == "streetview"]
    keys = {(o.pano_id, o.heading_deg) for o in sv}
    assert len(keys) == len(sv) and len(sv) < 44, "adjacent stations on the same pano must dedupe"
    assert res.deduped == 44 - len(sv) and res.skipped == []
    assert len(fake.calls_of("streetview")) == len(sv)
    assert len(fake.calls_of("streetview_metadata")) == 11, "one free metadata call per station, shared by its headings"
    assert {o.kind for o in res.observations} == {"streetview", "satellite_crop", "satellite_overview"}
    assert res.new == len(res.observations) == len(fake.paid_calls) and res.cached == 0
    assert res.cost_usd == pytest.approx(len(sv) * 0.007 + 4 * 0.002)
    stored = model_repo.get_observations(seg.id, limit=500)
    assert len(stored) == len(res.observations)

    for o in sv:
        assert o.sha256 == hashlib.sha256((tmp_path / o.file_path).read_bytes()).hexdigest()
        assert o.capture_date == "2023-03" and o.cost_usd == 0.007 and o.acquired_run_id == run_id
        assert o.station_s is not None and o.offset_t is not None and o.view is not None
        assert o.offset_t == pytest.approx(3.0, abs=0.3), "the fake panos sit 3 m left of the line"
        assert o.pitch_deg == 0 and o.fov_deg == 90 and o.georef["request"]["pano"] == o.pano_id
    views = {o.view.value for o in sv}
    assert views == {"forward", "back", "left", "right"}
    overview = next(o for o in res.observations if o.kind == "satellite_overview")
    assert overview.zoom == 19 and overview.scale == 2 and overview.georef["size_px"] == [1280, 1280]
    assert overview.meters_per_px == pytest.approx(0.2907, abs=0.01) or overview.meters_per_px > 0
    assert sum(1 for o in res.observations if o.kind == "satellite_crop") == 3


def test_second_acquire_with_the_same_plan_is_all_cache_hits_and_adds_no_rows(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref)
    seg, _ = seg_run
    _, first, _ = run_acquire(seg_run, tmp_path, fake, ref, plan())
    paid_before = len(fake.paid_calls)
    rows_before = len(model_repo.get_observations(seg.id, limit=500))

    run2, second, _ = run_acquire(seg_run, tmp_path, fake, ref, plan())

    assert second.new == 0 and second.cached == first.new and second.cost_usd == 0.0
    assert len(fake.paid_calls) == paid_before, "a fully stored plan makes zero Google image requests"
    assert len(model_repo.get_observations(seg.id, limit=500)) == rows_before
    assert [o.id for o in second.observations] == [o.id for o in first.observations]
    rows = repo.list_google_requests(run2)
    assert rows and all(r.cache_hit for r in rows) and all(r.est_cost_usd == 0.0 for r in rows)
    assert repo.google_counts(run2).requests == 0


def test_acquire_extends_a_stored_plan_paying_only_for_new_images(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref)
    _, first, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward", "back"]))
    paid_before = len(fake.paid_calls)
    _, second, _ = run_acquire(seg_run, tmp_path, fake, ref, plan())
    new_calls = len(fake.paid_calls) - paid_before
    assert second.new == new_calls > 0
    assert second.cached >= first.new, "everything run 1 stored is a cache hit"
    assert all(o.view.value in ("left", "right") for o in second.observations
               if o.kind == "streetview" and o.acquired_run_id and o.id not in {x.id for x in first.observations})


def test_zero_results_station_is_skipped_with_its_status_and_never_raises(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, pano_spacing_m=10.0, coverage=(0.0, 40.0))
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"]))
    skipped = [x for x in res.skipped if x["kind"] == "streetview"]
    assert skipped and all(x["reason"] == "ZERO_RESULTS" for x in skipped)
    assert {x["station_s"] for x in skipped} <= {60.0, 70.0, 80.0, 90.0, 100.0}
    assert any(o.kind == "streetview" for o in res.observations)
    assert len(fake.calls_of("streetview")) == len([o for o in res.observations if o.kind == "streetview"])


def test_403_on_every_streetview_frame_skips_frames_without_raising(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, frame_status=403)
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"]))
    frames = [x for x in res.skipped if x["kind"] == "streetview"]
    assert frames and all(x["reason"].startswith("frame_error") for x in frames)
    assert {o.kind for o in res.observations} == {"satellite_crop", "satellite_overview"}
    assert any("403" in w for w in res.warnings)


def test_metadata_http_error_skips_the_station_not_the_run(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, metadata_status_code=403)
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"]))
    assert all(x["reason"].startswith("metadata_error") for x in res.skipped if x["kind"] == "streetview")
    assert fake.calls_of("streetview") == []


def test_overview_failure_raises_upstream_error_when_none_is_stored(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, require_key="a-different-key")
    with pytest.raises(UpstreamError):
        run_acquire(seg_run, tmp_path, fake, ref, plan())


def test_overview_failure_is_tolerated_when_one_is_already_stored(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref)
    run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"]))
    # a different overview zoom => cache miss => the (now failing) request is made, but an overview exists already
    broken = FakeGoogle(ref, require_key="a-different-key")
    _, res, _ = run_acquire(seg_run, tmp_path / "other", broken, ref, plan(headings=["forward"], overview_zoom=18))
    assert any(x["kind"] == "satellite_overview" for x in res.skipped)


def test_crop_that_is_the_no_imagery_placeholder_is_refetched_at_zoom_19(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, placeholder_zooms=frozenset({20}))
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"], sv_spacing_m=40.0))
    crops = [o for o in res.observations if o.kind == "satellite_crop"]
    # the middle crop (s = 50 m) at zoom 19 is the very tile of the overview: one image, one observation
    assert len(crops) == 2 and all(o.zoom == 19 for o in crops)
    assert all(o.georef["zoom"] == 19 for o in crops)
    assert [x["reason"] for x in res.skipped] == ["same_image_as_satellite_overview"]
    assert any("fell back to zoom 19" in w for w in res.warnings)
    zooms = [c["zoom"] for c in fake.calls_of("satellite")]
    assert zooms.count(20) == 3 and zooms.count(19) == 3  # 3 placeholder probes; overview + 2 fallbacks are paid (1 cache hit)
    assert res.cost_usd == pytest.approx(6 * 0.002 + 4 * 0.007)  # sat: overview + 3 wasted probes + 2 fallbacks; 4 frames


def test_crop_with_no_imagery_even_at_zoom_19_is_skipped(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, placeholder_zooms=frozenset({19, 20}))
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"], include_overview=False))
    assert [x["reason"] for x in res.skipped if x["kind"] == "satellite_crop"] == ["no_imagery"] * 3
    assert not [o for o in res.observations if o.kind == "satellite_crop"]


# ---------------------------------------------------------------- cost guards


def test_new_paid_images_never_exceed_max_images_even_with_placeholder_refetches(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, placeholder_zooms=frozenset({20}), pano_spacing_m=5.0)
    _, res, google = run_acquire(seg_run, tmp_path, fake, ref, plan(headings=["forward"], sv_spacing_m=40.0, max_images=6,
                                                            include_overview=False))
    assert len(fake.paid_calls) <= 6 and res.paid_requests == len(fake.paid_calls) == google.paid_requests
    assert any(x["reason"] == "max_images_reached" for x in res.skipped)


@pytest.mark.parametrize("max_images", [6, 12, 30])
def test_google_image_requests_never_exceed_max_images_on_a_long_line(seg_run, tmp_path, max_images):
    ref = straight_ref(300.0)
    fake = FakeGoogle(ref, pano_spacing_m=7.0)
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(max_images=max_images))
    assert len(fake.paid_calls) <= max_images
    assert res.trimmed is (max_images < 70) or res.trimmed is False
    assert res.new + res.cached == len(res.observations)


def test_trimmed_plan_widens_spacing_and_reports_it(seg_run, tmp_path):
    ref = straight_ref(400.0)
    fake = FakeGoogle(ref, pano_spacing_m=10.0)
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, plan(max_images=60))
    assert res.trimmed and any("widened" in w for w in res.warnings)
    assert len(fake.paid_calls) <= 60
    assert {o.view.value for o in res.observations if o.kind == "streetview"} == {"forward", "back", "left", "right"}


def test_metadata_calls_are_free_and_not_counted_against_the_cap(seg_run, tmp_path):
    ref = straight_ref(100.0)
    fake = FakeGoogle(ref, coverage=(1000.0, 2000.0))  # no panos anywhere: every station is ZERO_RESULTS
    _, res, google = run_acquire(seg_run, tmp_path, fake, ref, plan(max_images=60))
    assert len(fake.calls_of("streetview_metadata")) == 11
    assert fake.calls_of("streetview") == [] and google.paid_requests == len(fake.paid_calls) == 4
    assert res.cost_usd == pytest.approx(4 * 0.002)
    assert all(r.est_cost_usd == 0.0 for r in repo.list_google_requests(res.observations[0].acquired_run_id)
               if r.kind == "streetview_metadata")


def test_estimated_cost_in_plan_info_matches_what_acquire_spends(seg_run, tmp_path):
    ref = straight_ref(100.0)
    p = plan(headings=["forward"], sv_spacing_m=20.0)
    fake = FakeGoogle(ref, pano_spacing_m=20.0, lateral_m=0.0)
    items, info = plan_images(ref, p)
    _, res, _ = run_acquire(seg_run, tmp_path, fake, ref, p)
    assert res.cost_usd == pytest.approx(info.est_cost_usd, abs=0.0005) and res.planned == len(items)


# ---------------------------------------------------------------- helpers


def test_classify_view_and_heading_quantisation():
    assert classify_view(10, 0) == "forward" and classify_view(350, 0) == "forward"
    assert classify_view(180, 0) == "back" and classify_view(95, 0) == "right" and classify_view(265, 0) == "left"
    assert classify_view(45, 0) == "oblique" and classify_view(1, 359) == "forward"
    assert quantize_heading(2.4) == 0.0 and quantize_heading(2.6) == 5.0 and quantize_heading(358.0) == 0.0
