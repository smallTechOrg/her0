import math
from dataclasses import dataclass

import pytest

from hero.model.projection import (
    CAMERA_HEIGHT_M, footpath_runs_to_states, project_overhead_polyline, project_street_detection,
    st_to_tile_px, strip_samples_from_traces, tile_px_to_st,
)
from hero.model.refline import RectGrid
from hero.model.types import FootpathState
from hero.scene.geo import TileGeoref
from tests.fixtures.street_fixtures import DEMO_ORIGIN, curved_reference_line, make_observation, straight_reference_line


def px_for_range(d, w=640, h=640, fov=90.0):
    f = (w / 2) / math.tan(math.radians(fov) / 2)
    return h / 2 + CAMERA_HEIGHT_M * f / d  # centre column: v - cy = H f / d


def box_at(range_m, u=320, w=40):
    v = px_for_range(range_m)
    return (int(u - w / 2), int(v - 60), w, 60)


def cam_obs(ref, **kw):
    cam = ref.point_at(50.0, 0.0)
    return make_observation(lat=cam[0], lon=cam[1], **kw)


def test_ground_plane_straight_ahead_and_right():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref, heading_deg=0.0)
    p = project_street_detection(obs, ref, box_at(10.0), touches_ground=True, est_distance_m=None)
    assert p.method == "ground_plane"
    assert p.range_m == pytest.approx(10.0, rel=0.02)
    assert p.s == pytest.approx(60.0, abs=0.3) and p.t == pytest.approx(0.0, abs=0.1)
    assert p.weight == pytest.approx(1.0)
    assert p.sigma_m == pytest.approx(0.4 + 0.1 * p.range_m, abs=1e-6)
    p2 = project_street_detection(obs, ref, (600, int(px_for_range(8.0)) - 40, 40, 40), touches_ground=True,
                                  est_distance_m=None)
    assert p2.t < -1.0  # right of north-going travel -> negative t


def test_heading_left_view_gives_positive_t():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref, heading_deg=270.0)  # looking west = left of north travel
    p = project_street_detection(obs, ref, box_at(8.0), touches_ground=True, est_distance_m=None)
    assert p.t == pytest.approx(8.0, rel=0.03)
    assert p.s == pytest.approx(50.0, abs=0.3)


def test_pitch_shifts_range():
    ref = straight_reference_line(200.0)
    box = box_at(10.0)
    r0 = project_street_detection(cam_obs(ref, pitch_deg=0.0), ref, box, touches_ground=True, est_distance_m=None).range_m
    r1 = project_street_detection(cam_obs(ref, pitch_deg=5.0), ref, box, touches_ground=True, est_distance_m=None).range_m
    assert r1 == pytest.approx(2.5 / math.tan(math.radians(math.degrees(math.atan(2.5 / 10.0)) - 5.0)), rel=0.03)
    assert r1 > r0  # camera tilted up: the same pixel looks shallower relative to the horizon -> farther


def test_far_range_dropped_and_horizon_fallback():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref)
    assert project_street_detection(obs, ref, box_at(45.0), touches_ground=True, est_distance_m=None) is None
    p = project_street_detection(obs, ref, (300, 300, 40, 20), touches_ground=True, est_distance_m=12.0)
    assert p.method == "estimated_distance" and p.range_m == 12.0  # bottom edge above the horizon
    assert p.sigma_m == pytest.approx(0.4 + 1.2 + 1.0)
    assert project_street_detection(obs, ref, (300, 300, 40, 20), touches_ground=True, est_distance_m=None) is None
    assert project_street_detection(obs, ref, (300, 300, 40, 20), touches_ground=True, est_distance_m=40.0) is None


def test_not_touching_ground_uses_estimate():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref)
    p = project_street_detection(obs, ref, box_at(10.0), touches_ground=False, est_distance_m=9.0)
    assert p.method == "estimated_distance" and p.range_m == 9.0
    assert project_street_detection(obs, ref, box_at(10.0), touches_ground=False, est_distance_m=None) is None


def test_range_disagreement_flag_and_half_weight():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref)
    ok = project_street_detection(obs, ref, box_at(10.0), touches_ground=True, est_distance_m=12.0)
    assert ok.flags == () and ok.weight == pytest.approx(1.0)
    bad = project_street_detection(obs, ref, box_at(10.0), touches_ground=True, est_distance_m=18.0)
    assert bad.method == "ground_plane" and bad.flags == ("range_disagreement",)
    assert bad.weight == pytest.approx(0.5)


def test_range_weight_curve():
    ref = straight_reference_line(200.0)
    obs = cam_obs(ref)

    def w(r):
        return project_street_detection(obs, ref, box_at(r), touches_ground=True, est_distance_m=None).weight

    assert w(20.0) == pytest.approx(0.6, abs=0.02)
    assert w(15.0) == pytest.approx(0.8, abs=0.02)
    assert w(28.0) < w(15.0)


def test_invalid_observation():
    ref = straight_reference_line(100.0)
    with pytest.raises(ValueError):
        project_street_detection(make_observation(heading_deg=None), ref, (0, 0, 10, 10), touches_ground=True,
                                 est_distance_m=None)
    with pytest.raises(ValueError):
        project_street_detection(make_observation(), ref, (0, 0, -1, 10), touches_ground=True, est_distance_m=None)


# ------------------------------------------------------------------ overhead

GRID = RectGrid(s_top=60.0, s_bottom=0.0, t_left=10.0, t_right=-10.0, m_per_px=0.1)


def test_polyline_projection():
    out = project_overhead_polyline(GRID, [(0, 0), (100, 300), (200, 600)])
    assert out[0] == pytest.approx((60.0, 10.0)) and out[1] == pytest.approx((30.0, 0.0))
    assert out[2] == pytest.approx((0.0, -10.0))
    assert project_overhead_polyline(GRID, []) == []


def test_strip_samples_follow_edges_on_absolute_grid():
    a = [(100, 0), (100, 300), (100, 600)]  # t = 0
    b = [(80, 0), (80, 300), (80, 600)]     # t = 2
    out = strip_samples_from_traces(GRID, a, b, kind="footpath", material="paver", condition="fair",
                                    side="left", weight=0.8, observation_id="o1")
    ss = [x.s for x in out]
    assert ss[0] == 0.0 and ss[-1] == 60.0 and len(ss) == 25
    assert all(x.t_lo == pytest.approx(0.0) and x.t_hi == pytest.approx(2.0) for x in out)
    assert out[0].observation_id == "o1" and out[0].weight == 0.8


def test_strip_samples_taper_and_partial_overlap():
    a = [(100, 100), (100, 400)]           # s 50 .. 20, t = 0
    b = [(60, 200), (120, 500)]            # s 40 .. 10, t 4 -> -2
    out = strip_samples_from_traces(GRID, a, b, kind="footpath", material="paver", condition="fair", side="left",
                                    weight=1.0)
    assert [x.s for x in out] == [20.0, 22.5, 25.0, 27.5, 30.0, 32.5, 35.0, 37.5, 40.0]
    top = out[-1]  # s = 40: b at t=4, a at t=0
    assert (top.t_lo, top.t_hi) == pytest.approx((0.0, 4.0))
    bottom = out[0]  # s = 20: b is 2/3 of the way down -> t = 0
    assert (bottom.t_lo, bottom.t_hi) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert all(x.t_lo <= x.t_hi for x in out)


def test_strip_short_overlap_single_sample_and_disjoint():
    a = [(100, 10), (100, 20)]   # s 59 .. 58
    b = [(80, 10), (80, 20)]
    out = strip_samples_from_traces(GRID, a, b, kind="kerb", material="unknown", condition="unknown",
                                    side="left", weight=0.5)
    assert len(out) == 1 and 58.0 <= out[0].s <= 59.0
    c = [(80, 300), (80, 400)]
    assert strip_samples_from_traces(GRID, a, c, kind="kerb", material="unknown", condition="unknown",
                                     side="left", weight=0.5) == []


def test_strip_samples_errors():
    ok = [(100, 0), (100, 100)]
    kw = dict(kind="kerb", material="unknown", condition="unknown", side="left", weight=0.5)
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, [(0, 0)], ok, **kw)
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, [(0, 100), (0, 50)], ok, **kw)
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, ok, ok, **{**kw, "weight": -1})
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, ok, ok, **{**kw, "side": "up"})
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, ok, ok, step_m=0, **kw)
    with pytest.raises(ValueError):
        strip_samples_from_traces(GRID, ok, ok, **{**kw, "kind": "nope"})


@dataclass
class Run:
    side: str
    y_from_px: float
    y_to_px: float
    state: str
    confidence: float


def test_footpath_runs():
    runs = [Run("left", 140, 210, "missing", 0.7), Run("left", 0, 140, "intact", 0.8),
            Run("right", 0, 600, "broken", 0.5)]
    out = footpath_runs_to_states(GRID, runs, observation_id="o2")
    left = [x for x in out if x.side == "left"]
    assert left[0].s_from == pytest.approx(39.0) and left[0].s_to == pytest.approx(46.0)
    assert left[0].state is FootpathState.missing and left[1].state is FootpathState.intact
    assert left[1].s_from == pytest.approx(46.0) and left[1].s_to == pytest.approx(60.0)
    assert left[1].weight == 0.8 and left[1].observation_id == "o2"
    assert out[-1].side == "right" and out[-1].s_from == pytest.approx(0.0)


def test_footpath_runs_clip_drop_and_errors():
    out = footpath_runs_to_states(GRID, [Run("left", 550, 900, "intact", 0.5), Run("left", 700, 800, "intact", 0.5)])
    assert len(out) == 1 and out[0].s_from == pytest.approx(0.0) and out[0].s_to == pytest.approx(5.0)
    with pytest.raises(ValueError):
        footpath_runs_to_states(GRID, [Run("centre", 0, 10, "intact", 0.5)])
    with pytest.raises(ValueError):
        footpath_runs_to_states(GRID, [Run("left", 10, 0, "intact", 0.5)])
    with pytest.raises(ValueError):
        footpath_runs_to_states(GRID, [Run("left", 0, 10, "bogus", 0.5)])
    assert footpath_runs_to_states(GRID, []) == []


# ------------------------------------------------------------------ tiles

GEOREF = TileGeoref(center_lat=DEMO_ORIGIN[0], center_lon=DEMO_ORIGIN[1], zoom=19, scale=2, size_px=(1280, 1280))


@pytest.mark.parametrize("s,t", [(10.0, 0.0), (40.0, 3.5), (70.0, -4.0)])
def test_tile_roundtrip_straight(s, t):
    ref = straight_reference_line(100.0, bearing_deg=30.0)
    px, py = st_to_tile_px(ref, GEOREF, s, t)
    s2, t2 = tile_px_to_st(ref, GEOREF, px, py)
    assert (s2, t2) == pytest.approx((s, t), abs=0.01)


def test_tile_roundtrip_curved_and_orientation():
    ref = curved_reference_line()
    for s in (5.0, 50.0, 95.0):
        px, py = st_to_tile_px(ref, GEOREF, s, 2.0)
        s2, t2 = tile_px_to_st(ref, GEOREF, px, py)
        assert s2 == pytest.approx(s, abs=0.02) and t2 == pytest.approx(2.0, abs=0.02)
    n = straight_reference_line(100.0)  # north-going: s up the image, left (west) = smaller px
    (x0, y0), (x1, y1) = st_to_tile_px(n, GEOREF, 10, 0), st_to_tile_px(n, GEOREF, 20, 0)
    assert y1 < y0 and x1 == pytest.approx(x0, abs=0.01)
    assert st_to_tile_px(n, GEOREF, 10, 1)[0] < x0
