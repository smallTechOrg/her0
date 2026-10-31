"""hero.model.coverage: observation cells and visible regions."""

from __future__ import annotations

import pytest

from hero.model.calibrate import range_weight
from hero.model.coverage import FWD_RANGE_M, SIDE_RANGE_M, observation_cells, visible_regions
from hero.model.refline import ReferenceLine
from hero.model.types import ObservationKind, ViewKind
from tests.fixtures.street_fixtures import curved_reference_line, make_observation, straight_reference_line


@pytest.fixture()
def ref() -> ReferenceLine:
    return straight_reference_line(100.0)


def obs_at(ref, s, view, *, kind=ObservationKind.streetview, oid="o1", heading=None, **kw):
    lat, lon = ref.point_at(s, 0.0)
    return make_observation(obs_id=oid, kind=kind, station_s=s, view=view, lat=lat, lon=lon,
                            heading_deg=heading if heading is not None else 0.0, **kw)


def one(cells):
    assert len(cells) == 1
    return cells[0]


def test_forward_cell_looks_ahead_both_sides(ref):
    c = one(observation_cells(obs_at(ref, 20, ViewKind.forward), ref))
    assert (c.kind, c.side, c.station_s) == ("forward", "both", 20.0)
    assert (c.s_from, c.s_to) == (20.0, 20.0 + FWD_RANGE_M)


def test_back_cell_looks_behind(ref):
    c = one(observation_cells(obs_at(ref, 50, ViewKind.back), ref))
    assert (c.kind, c.side, c.s_from, c.s_to) == ("back", "both", 50 - FWD_RANGE_M, 50.0)


@pytest.mark.parametrize("view", [ViewKind.left, ViewKind.right])
def test_side_cells_are_direct_and_one_sided(ref, view):
    c = one(observation_cells(obs_at(ref, 40, view), ref))
    assert c.kind == view.value and c.side == view.value
    assert (c.s_from, c.s_to) == (40 - SIDE_RANGE_M, 40 + SIDE_RANGE_M)


def test_oblique_covers_both_sides_around_the_station(ref):
    c = one(observation_cells(obs_at(ref, 40, ViewKind.oblique), ref))
    assert (c.kind, c.side, c.s_from, c.s_to) == ("oblique", "both", 34.0, 46.0)


def test_cells_are_clipped_to_the_line(ref):
    c = one(observation_cells(obs_at(ref, 95, ViewKind.forward), ref))
    assert c.s_to == pytest.approx(100.0)
    c = one(observation_cells(obs_at(ref, 3, ViewKind.back), ref))
    assert c.s_from == 0.0
    # a forward cell that starts past the end clips to nothing
    assert observation_cells(obs_at(ref, 100.0, ViewKind.forward), ref) == []


def test_view_is_derived_from_heading_and_station_from_latlon_when_unrecorded(ref):
    lat, lon = ref.point_at(30.0, 1.0)
    o = make_observation(station_s=None, view=None, offset_t=None, lat=lat, lon=lon, heading_deg=270.0)
    c = one(observation_cells(o, ref))
    assert c.kind == "left" and c.station_s == pytest.approx(30.0, abs=0.05)  # west of a northbound line = left
    o = make_observation(station_s=10.0, view=None, heading_deg=180.0)
    assert one(observation_cells(o, ref)).kind == "back"
    o = make_observation(station_s=10.0, view=None, heading_deg=None)
    assert one(observation_cells(o, ref)).kind == "oblique"


def test_overview_and_low_zoom_crops_have_no_cells(ref):
    ov = make_observation(kind=ObservationKind.satellite_overview, zoom=19, station_s=None, view=None)
    assert observation_cells(ov, ref) == []
    crop = make_observation(kind=ObservationKind.satellite_crop, zoom=19, station_s=None, view=None)
    assert observation_cells(crop, ref) == []


def test_hires_crop_marks_the_run_of_the_line_inside_it(ref):
    lat, lon = ref.point_at(50.0, 0.0)
    crop = make_observation(obs_id="c1", kind=ObservationKind.satellite_crop, zoom=20, station_s=None, view=None,
                            lat=lat, lon=lon, size_px=(640, 640))  # 0.15 m/px -> 48 m half extent
    cells = observation_cells(crop, ref)
    assert [c.kind for c in cells] == ["overhead_hires"]
    assert cells[0].s_from == pytest.approx(2.0, abs=0.6) and cells[0].s_to == pytest.approx(98.0, abs=0.6)


def test_hires_crop_requires_the_side_offsets_inside_the_crop(ref):
    lat, lon = ref.point_at(50.0, 0.0)
    tiny = make_observation(kind=ObservationKind.satellite_crop, zoom=20, station_s=None, view=None, lat=lat,
                            lon=lon, size_px=(80, 640))  # 6 m wide: +-8 m offsets fall outside
    assert observation_cells(tiny, ref) == []


def test_user_photo_adds_a_closeup_cell(ref):
    o = obs_at(ref, 40, ViewKind.left, kind=ObservationKind.user_photo)
    kinds = sorted(c.kind for c in observation_cells(o, ref))
    assert kinds == ["closeup", "left"]
    close = [c for c in observation_cells(o, ref) if c.kind == "closeup"][0]
    assert close.side == "left" and (close.s_from, close.s_to) == (34.0, 46.0)
    fwd = obs_at(ref, 40, ViewKind.forward, kind=ObservationKind.user_photo)
    assert [c.side for c in observation_cells(fwd, ref) if c.kind == "closeup"] == ["both"]


def test_cells_follow_a_curved_line():
    ref = curved_reference_line()
    c = one(observation_cells(obs_at(ref, 20, ViewKind.forward), ref))
    assert (c.s_from, c.s_to) == (20.0, 35.0)


def test_visible_regions_weight_by_distance_from_the_camera(ref):
    regs = visible_regions(obs_at(ref, 20, ViewKind.forward), ref)
    assert regs and all(r.observation_id == "o1" and r.side == "both" for r in regs)
    assert regs[0].s_from == pytest.approx(20.0) and regs[-1].s_to == pytest.approx(35.0)
    assert all(a.s_to == pytest.approx(b.s_from) for a, b in zip(regs, regs[1:]))
    weights = [r.weight for r in regs]
    assert weights == sorted(weights, reverse=True)  # farther from the camera -> weaker
    assert regs[0].weight == pytest.approx(range_weight(2.5), abs=1e-3) == 1.0
    assert regs[-1].weight == pytest.approx(range_weight(12.5), abs=1e-3)
    assert regs[-1].weight < 1.0


def test_visible_regions_side_view_is_symmetric_and_one_sided(ref):
    regs = visible_regions(obs_at(ref, 50, ViewKind.right), ref)
    assert {r.side for r in regs} == {"right"}
    assert regs[0].s_from == pytest.approx(44.0) and regs[-1].s_to == pytest.approx(56.0)
    assert regs[0].weight == pytest.approx(regs[-1].weight, abs=1e-3)


def test_visible_regions_empty_for_satellite_and_closeups_not_double_counted(ref):
    ov = make_observation(kind=ObservationKind.satellite_overview, zoom=19, station_s=None, view=None)
    assert visible_regions(ov, ref) == []
    up = obs_at(ref, 40, ViewKind.left, kind=ObservationKind.user_photo)
    assert all(r.side == "left" for r in visible_regions(up, ref))
    assert len(visible_regions(up, ref)) == len(visible_regions(obs_at(ref, 40, ViewKind.left), ref))


def test_crop_scale_is_derived_from_zoom_when_meters_per_px_is_missing(ref):
    o = make_observation(kind=ObservationKind.satellite_crop, zoom=20, station_s=None, view=None)
    o = o.model_copy(update={"meters_per_px": None})
    assert isinstance(observation_cells(o, ref), list)
