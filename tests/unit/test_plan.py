"""hero.imagery.plan: pure planner + the max-images cost guard."""

from __future__ import annotations

import math

import pytest

from hero.imagery.plan import ImageryPlan, SRange, default_plan, merge_plan, overview_zoom_for, plan_images
from tests.fixtures.street_fixtures import curved_reference_line, straight_reference_line


def plan(**kw) -> ImageryPlan:
    base = dict(sv_spacing_m=10.0, headings=["forward", "back", "left", "right"], sat_crop_zoom=20,
                sat_crop_spacing_m=40.0, include_overview=True, max_images=60, s_range=None, overview_zoom=19)
    base.update(kw)
    return ImageryPlan(**base)


def counts(items):
    out: dict[str, int] = {}
    for i in items:
        out[i.kind] = out.get(i.kind, 0) + 1
    return out


def test_default_plan_on_100m_line_is_44_streetview_3_crops_1_overview():
    items, info = plan_images(straight_reference_line(100.0), plan())
    assert counts(items) == {"streetview": 44, "satellite_crop": 3, "satellite_overview": 1}
    assert info.counts == {"streetview": 44, "satellite_crop": 3, "satellite_overview": 1}
    assert info.trimmed is False and info.effective_spacing_m == 10.0
    assert info.est_cost_usd == pytest.approx(44 * 0.007 + 4 * 0.002)


def test_400m_line_with_max_60_is_trimmed_widened_and_keeps_all_four_headings():
    items, info = plan_images(straight_reference_line(400.0), plan(max_images=60))
    assert len(items) <= 60 and info.paid_total <= 60
    assert info.trimmed is True and info.effective_spacing_m > 10.0
    views = {i.view for i in items if i.kind == "streetview"}
    assert views == {"forward", "back", "left", "right"}
    stations = sorted({i.station_s for i in items if i.kind == "streetview"})
    assert stations[0] == 0.0 and stations[-1] == pytest.approx(400.0)
    assert any("widened" in n for n in info.notes)


@pytest.mark.parametrize("max_images", [8, 12, 25, 60, 200])
@pytest.mark.parametrize("length", [37.0, 100.0, 250.0])
def test_planned_paid_items_never_exceed_max_images_when_two_stations_fit(max_images, length):
    items, info = plan_images(straight_reference_line(length), plan(max_images=max_images))
    assert len(items) <= max_images
    assert {i.view for i in items if i.kind == "streetview"} == {"forward", "back", "left", "right"}


def test_headings_are_relative_to_the_road_tangent():
    items, _ = plan_images(straight_reference_line(20.0, bearing_deg=90.0), plan(sv_spacing_m=20.0))
    first = {i.view: i.heading_deg for i in items if i.kind == "streetview" and i.station_s == 0.0}
    assert first["forward"] == pytest.approx(90.0)
    assert first["right"] == pytest.approx(180.0)
    assert first["back"] == pytest.approx(270.0)
    assert min(first["left"], 360.0 - first["left"]) == pytest.approx(0.0, abs=1e-6)  # 0 / 360


def test_curved_line_headings_follow_the_local_tangent():
    ref = curved_reference_line()
    items, _ = plan_images(ref, plan(headings=["forward"]))
    for i in (x for x in items if x.kind == "streetview"):
        assert i.heading_deg == pytest.approx(ref.bearing_deg(i.station_s))
    sv = [x for x in items if x.kind == "streetview"]
    assert sv[0].heading_deg != pytest.approx(sv[-1].heading_deg, abs=5.0)  # the road really turns


def test_s_range_limits_stations_and_crops_to_the_window():
    items, info = plan_images(straight_reference_line(100.0), plan(s_range=SRange(s_from=20.0, s_to=60.0)))
    sv = [i for i in items if i.kind == "streetview"]
    assert min(i.station_s for i in sv) == pytest.approx(20.0) and max(i.station_s for i in sv) == pytest.approx(60.0)
    crops = [i for i in items if i.kind == "satellite_crop"]
    assert len(crops) == 1 and 20.0 < crops[0].station_s < 60.0
    assert info.counts["streetview"] == 5 * 4


def test_s_range_outside_the_line_is_rejected():
    with pytest.raises(ValueError, match="outside the line"):
        plan_images(straight_reference_line(50.0), plan(s_range=SRange(s_from=60.0, s_to=90.0)))


def test_overview_can_be_switched_off_and_single_heading_plan_is_small():
    items, info = plan_images(straight_reference_line(100.0), plan(include_overview=False, headings=["forward"], sv_spacing_m=20.0))
    assert counts(items) == {"streetview": 6, "satellite_crop": 3}
    assert info.counts["satellite_overview"] == 0


def test_degenerate_max_images_below_one_full_station_is_left_to_the_acquire_hard_cap():
    items, info = plan_images(straight_reference_line(100.0), plan(max_images=4))
    assert info.trimmed and info.counts["streetview"] == 4  # headings are never dropped; acquire enforces the cap


def test_tiny_max_images_reduces_crops_before_dropping_headings():
    items, info = plan_images(straight_reference_line(100.0), plan(max_images=10))
    assert len(items) <= 10
    assert info.counts["streetview"] >= 2 * 4
    assert any("close-up crops reduced" in n for n in info.notes)


def test_planner_is_deterministic():
    ref = curved_reference_line()
    a, ia = plan_images(ref, plan())
    b, ib = plan_images(ref, plan())
    assert a == b and ia == ib


def test_long_line_lowers_the_overview_zoom_so_the_tile_covers_it():
    assert overview_zoom_for(100.0, 12.97, 19) == 19
    assert overview_zoom_for(400.0, 12.97, 19) < 19
    items, info = plan_images(straight_reference_line(400.0), plan())
    overview = next(i for i in items if i.kind == "satellite_overview")
    assert overview.zoom < 19 and any("overview zoom" in n for n in info.notes)


def test_default_plan_reads_the_settings():
    from hero.config import get_settings

    p = default_plan(get_settings())
    s = get_settings()
    assert p.sv_spacing_m == s.sv_spacing_m and p.headings == s.sv_headings_list and p.max_images == s.max_images_per_run


# ---------------------------------------------------------------- merge_plan validation


def test_merge_plan_applies_overrides_and_ignores_none():
    merged = merge_plan(plan(), {"sv_spacing_m": 20, "headings": ["left", "right", "left"], "max_images": None})
    assert merged.sv_spacing_m == 20 and merged.headings == ["left", "right"] and merged.max_images == 60


@pytest.mark.parametrize("overrides,needle", [
    ({"sv_spacing_m": 3.9}, "sv_spacing_m"), ({"sv_spacing_m": 40.1}, "sv_spacing_m"),
    ({"headings": []}, "headings"), ({"headings": ["up"]}, "headings"),
    ({"sat_crop_zoom": 17}, "sat_crop_zoom"), ({"sat_crop_zoom": 22}, "sat_crop_zoom"),
    ({"max_images": 0}, "max_images"), ({"max_images": 201}, "max_images"),
    ({"bogus": 1}, "unknown"), ({"s_range": {"s_from": 10, "s_to": 5}}, "s_range"),
])
def test_merge_plan_rejects_out_of_range_values(overrides, needle):
    with pytest.raises(ValueError, match=needle):
        merge_plan(plan(), overrides)


def test_merge_plan_accepts_the_boundaries():
    merged = merge_plan(plan(), {"sv_spacing_m": 4, "sat_crop_zoom": 21, "max_images": 200})
    assert (merged.sv_spacing_m, merged.sat_crop_zoom, merged.max_images) == (4, 21, 200)
    assert math.isfinite(merged.sat_crop_spacing_m)
