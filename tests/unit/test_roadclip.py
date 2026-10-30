import math

import pytest

from hero.model.roadclip import clip_road, straight_chord
from tests.fixtures.street_fixtures import DEMO_ORIGIN, _latlon


def road(length=200.0, bearing=0.0, n=5):
    b = math.radians(bearing)
    return [_latlon(DEMO_ORIGIN, math.sin(b) * length * i / (n - 1), math.cos(b) * length * i / (n - 1))
            for i in range(n)]


def test_clip_middle_length_and_no_extrapolation():
    pts = road()
    ref, info = clip_road(pts, _latlon(DEMO_ORIGIN, 3.0, 100.0), 60.0, 0.0)
    assert ref.length_s == pytest.approx(60.0, abs=0.05)
    assert info.extrapolated_start_m == 0 and info.extrapolated_end_m == 0
    assert info.snap_m == pytest.approx(3.0, abs=0.05)
    st = ref.to_st(*_latlon(DEMO_ORIGIN, 0.0, 100.0))
    assert st.s == pytest.approx(30.0, abs=0.1)


def test_orientation_reversed_when_travel_opposes():
    ref, _ = clip_road(road(), _latlon(DEMO_ORIGIN, 0, 100.0), 40.0, 180.0)
    assert ref.bearing_deg(10) == pytest.approx(180.0, abs=0.5)
    assert ref.length_s == pytest.approx(40.0, abs=0.05)


def test_orientation_within_90_keeps_direction():
    ref, _ = clip_road(road(), _latlon(DEMO_ORIGIN, 0, 100.0), 40.0, 80.0)
    assert ref.bearing_deg(10) == pytest.approx(0.0, abs=0.5)


def test_extrapolates_past_end_and_start():
    pts = road(length=100.0)
    ref, info = clip_road(pts, _latlon(DEMO_ORIGIN, 0, 90.0), 60.0, 0.0)
    assert info.extrapolated_end_m == pytest.approx(20.0, abs=0.1)
    assert info.extrapolated_start_m == 0
    assert ref.length_s == pytest.approx(60.0, abs=0.05)
    _, info2 = clip_road(pts, _latlon(DEMO_ORIGIN, 0, 10.0), 60.0, 0.0)
    assert info2.extrapolated_start_m == pytest.approx(20.0, abs=0.1)
    _, info3 = clip_road(pts, _latlon(DEMO_ORIGIN, 0, 10.0), 60.0, 180.0)  # reversed: ends swap
    assert info3.extrapolated_end_m == pytest.approx(20.0, abs=0.1)


def test_road_shorter_than_clip():
    ref, info = clip_road(road(length=20.0), _latlon(DEMO_ORIGIN, 0, 10.0), 100.0, 0.0)
    assert info.extrapolated_start_m + info.extrapolated_end_m == pytest.approx(80.0, abs=0.2)
    assert ref.length_s == pytest.approx(100.0, abs=0.1)


def test_bend_is_preserved():
    o = DEMO_ORIGIN
    pts = [o, _latlon(o, 0, 50), _latlon(o, 50, 50)]  # north then east
    ref, _ = clip_road(pts, _latlon(o, 0, 50), 40.0, 0.0)
    assert ref.length_s == pytest.approx(40.0, abs=0.05)
    assert ref.turning_deg(0, ref.length_s) == pytest.approx(90.0, abs=1.0)


def test_snap_too_far_raises():
    with pytest.raises(ValueError, match="max_snap_m"):
        clip_road(road(), _latlon(DEMO_ORIGIN, 40.0, 100.0), 40.0, 0.0)
    clip_road(road(), _latlon(DEMO_ORIGIN, 40.0, 100.0), 40.0, 0.0, max_snap_m=50.0)


@pytest.mark.parametrize("length", [0.0, -5.0, float("nan")])
def test_invalid_length(length):
    with pytest.raises(ValueError):
        clip_road(road(), DEMO_ORIGIN, length, 0.0)


def test_degenerate_road_raises():
    with pytest.raises(ValueError):
        clip_road([DEMO_ORIGIN, DEMO_ORIGIN], DEMO_ORIGIN, 10.0, 0.0)
    with pytest.raises(ValueError):
        clip_road([DEMO_ORIGIN], DEMO_ORIGIN, 10.0, 0.0)


def test_straight_chord():
    a, b = DEMO_ORIGIN, _latlon(DEMO_ORIGIN, 0, 75.0)
    ref = straight_chord(a, b)
    assert ref.length_s == pytest.approx(75.0, abs=0.05)
    with pytest.raises(ValueError):
        straight_chord(a, a)
