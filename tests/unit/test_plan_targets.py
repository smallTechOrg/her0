"""ImageryPlan.targets: plan EXACTLY the requested frames (no grid), overview once, mutually exclusive with the grid fields."""

from __future__ import annotations

import pytest

from hero.imagery.plan import ImageryPlan, PlanTarget, default_plan, merge_plan, plan_images
from hero.config import Settings
from tests.fixtures.street_fixtures import straight_reference_line


@pytest.fixture()
def base():
    return default_plan(Settings())


@pytest.fixture()
def ref():
    return straight_reference_line(100.0)


def with_targets(base, targets, **extra):
    return merge_plan(base, {"targets": targets, **extra})


def test_targets_are_planned_exactly_with_overview_once(base, ref):
    plan = with_targets(base, [{"kind": "streetview", "station_s": 12.5, "view": "left"},
                               {"kind": "streetview", "station_s": 12.5, "view": "right"},
                               {"kind": "satellite_crop", "station_s": 50.0, "zoom": 20}])
    items, info = plan_images(ref, plan)
    kinds = [(i.kind, i.view, round(i.station_s, 3)) for i in items]
    assert kinds == [("streetview", "left", 12.5), ("streetview", "right", 12.5), ("satellite_crop", None, 50.0),
                     ("satellite_overview", None, 50.0)]
    left, right = items[0], items[1]
    bearing = ref.bearing_deg(12.5)
    assert left.heading_deg == pytest.approx((bearing + 270) % 360)
    assert right.heading_deg == pytest.approx((bearing + 90) % 360)
    assert (left.lat, left.lon) == pytest.approx(ref.point_at(12.5, 0.0))
    assert items[2].zoom == 20
    assert info.counts == {"streetview": 2, "satellite_crop": 1, "satellite_overview": 1}
    assert info.est_cost_usd == pytest.approx(2 * 0.007 + 2 * 0.002)
    assert not info.trimmed


def test_targets_ignore_the_max_images_grid_trim(base, ref):
    plan = with_targets(base, [{"kind": "streetview", "station_s": float(i), "view": "forward"} for i in range(30)],
                        max_images=5)
    items, info = plan_images(ref, plan)
    assert info.counts["streetview"] == 30 and len(items) == 31


def test_duplicate_targets_collapse_and_are_noted(base, ref):
    t = {"kind": "streetview", "station_s": 10, "view": "back"}
    items, info = plan_images(ref, with_targets(base, [t, t]))
    assert info.counts["streetview"] == 1 and any("duplicate" in n for n in info.notes)


def test_include_overview_false_drops_it(base, ref):
    plan = with_targets(base, [{"kind": "streetview", "station_s": 1, "view": "forward"}], include_overview=False)
    items, _ = plan_images(ref, plan)
    assert [i.kind for i in items] == ["streetview"]


@pytest.mark.parametrize("clash", [{"sv_spacing_m": 10}, {"headings": ["left"]}, {"s_range": {"s_from": 0, "s_to": 10}}])
def test_targets_with_grid_fields_is_rejected(base, clash):
    with pytest.raises(ValueError, match="targets"):
        merge_plan(base, {"targets": [{"kind": "streetview", "station_s": 1, "view": "left"}], **clash})


@pytest.mark.parametrize("bad", [
    {"kind": "streetview", "station_s": 1},                                    # no view
    {"kind": "satellite_crop", "station_s": 1, "view": "left"},                # crop with view
    {"kind": "satellite_crop", "station_s": 1, "zoom": 22},                    # zoom out of range
    {"kind": "streetview", "station_s": -1, "view": "left"},                   # negative station
    {"kind": "panorama", "station_s": 1, "view": "left"}])
def test_invalid_target_is_rejected(base, bad):
    with pytest.raises(ValueError):
        merge_plan(base, {"targets": [bad]})


def test_more_than_200_targets_rejected(base):
    with pytest.raises(ValueError):
        merge_plan(base, {"targets": [{"kind": "streetview", "station_s": 1, "view": "left"}] * 201})


def test_station_beyond_the_line_is_a_plan_error(base, ref):
    plan = with_targets(base, [{"kind": "streetview", "station_s": 150, "view": "left"}])
    with pytest.raises(ValueError, match="outside the line"):
        plan_images(ref, plan)


def test_grid_plans_are_unchanged_without_targets(base, ref):
    items, info = plan_images(ref, base)
    assert base.targets is None and info.counts["streetview"] > 0
    assert isinstance(base, ImageryPlan) and PlanTarget(kind="streetview", station_s=0, view="forward")
