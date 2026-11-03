"""hero.model.refline: (s, t) frame on straight and circular reference lines, RectGrid, model round trip."""

from __future__ import annotations

import math
import random

import pytest

from hero.model.refline import ReferenceLine, RectGrid
from tests.fixtures.street_fixtures import curved_reference_line, straight_reference_line

ARC_RADIUS = 80.0
ARC_LENGTH = 100.0


@pytest.fixture(scope="module")
def arc() -> ReferenceLine:
    # 0.05 m chords: the inner-side offset wedge at vertices is then < 1 cm for |t| <= 15 (see refline docstring)
    return curved_reference_line(ARC_RADIUS, ARC_LENGTH, step_m=0.05)


def test_arc_length_matches_the_circle_within_five_hundredths_of_a_percent(arc):
    assert arc.length_s == pytest.approx(ARC_LENGTH, rel=5e-4)


def test_to_st_inverts_point_at_within_a_centimetre_on_a_circular_arc(arc):
    rng = random.Random(20261030)
    for _ in range(200):
        s = rng.uniform(1.0, arc.length_s - 1.0)
        t = rng.uniform(-15.0, 15.0)
        got = arc.to_st(*arc.point_at(s, t))
        assert got.s == pytest.approx(s, abs=0.01)
        assert got.t == pytest.approx(t, abs=0.01)
        assert got.dist_m == pytest.approx(abs(t), abs=0.01)
        assert got.clamped is False


def test_left_is_positive_t_on_a_north_bound_line():
    ref = straight_reference_line(50.0, bearing_deg=0.0)
    lat, lon = ref.point_at(10.0, 4.0)
    west = ref.to_st(lat, lon)
    assert west.t == pytest.approx(4.0, abs=1e-3)
    assert lon < ref.latlons[0][1]  # left of north-bound travel is west
    assert ref.to_st(*ref.point_at(10.0, -4.0)).t == pytest.approx(-4.0, abs=1e-3)


def test_bearing_and_turning_follow_a_right_hand_bend(arc):
    assert arc.bearing_deg(0.0) == pytest.approx(0.0, abs=0.5)
    expected_turn = math.degrees(ARC_LENGTH / ARC_RADIUS)
    assert arc.turning_deg(0.0, arc.length_s) == pytest.approx(expected_turn, abs=1.0)
    left = curved_reference_line(ARC_RADIUS, ARC_LENGTH, turn="left", step_m=0.5)
    assert left.turning_deg(0.0, left.length_s) < -50.0


def test_bearing_is_in_zero_to_360_and_wraps():
    south_west = straight_reference_line(30.0, bearing_deg=225.0)
    assert south_west.bearing_deg(5.0) == pytest.approx(225.0, abs=0.5)
    nnw = straight_reference_line(30.0, bearing_deg=350.0)
    assert 0.0 <= nnw.bearing_deg(0.0) < 360.0


def test_to_st_beyond_the_ends_extends_the_tangent_and_flags_clamped():
    ref = straight_reference_line(40.0)
    past = ref.to_st(*ref.point_at(52.0, 3.0))
    assert past.clamped is True
    assert past.s == pytest.approx(52.0, abs=1e-2) and past.t == pytest.approx(3.0, abs=1e-2)
    before = ref.to_st(*ref.point_at(-6.0, -2.0))
    assert before.clamped is True and before.s == pytest.approx(-6.0, abs=1e-2)


def test_xy_clamps_to_the_line():
    ref = straight_reference_line(40.0)
    assert ref.xy(-5.0) == ref.xy(0.0)
    assert ref.xy(1000.0) == ref.xy(ref.length_s)
    x, y = ref.xy(20.0)
    assert (x, y) == pytest.approx((0.0, 20.0), abs=1e-6)


def test_sample_s_includes_both_ends_without_duplicates():
    ref = straight_reference_line(10.0)
    assert ref.sample_s(2.5) == pytest.approx([0.0, 2.5, 5.0, 7.5, 10.0])
    odd = ref.sample_s(4.0)
    assert odd[0] == 0.0 and odd[-1] == pytest.approx(ref.length_s) and len(odd) == 4
    with pytest.raises(ValueError):
        ref.sample_s(0.0)


def test_from_latlons_needs_two_distinct_points():
    with pytest.raises(ValueError):
        ReferenceLine.from_latlons([(12.97, 77.64)])
    with pytest.raises(ValueError):
        ReferenceLine.from_latlons([(12.97, 77.64), (12.97, 77.64)])
    with pytest.raises(ValueError):
        ReferenceLine.from_latlons([(95.0, 77.64), (12.97, 77.64)])


def test_consecutive_duplicate_points_are_dropped():
    ref = ReferenceLine.from_latlons([(12.97, 77.64), (12.97, 77.64), (12.9709, 77.64)])
    assert len(ref.latlons) == 2 and ref.length_s == pytest.approx(100.0, rel=0.01)


def test_model_round_trip_preserves_geometry_to_centimetres(arc):
    model = arc.to_model(source="osm_nominatim", osm_way_id=25173540, name="100 Feet Road", extrapolated_m=(1.0, 2.0))
    assert model.source == "osm_nominatim" and model.extrapolated_m.end == 2.0
    assert model.points[0].s == 0.0 and model.points[-1].s == pytest.approx(model.length_s, abs=1e-3)
    again = ReferenceLine.from_model(type(model).model_validate_json(model.model_dump_json()))
    assert again.length_s == pytest.approx(arc.length_s, abs=0.05)
    assert again.xy(50.0) == pytest.approx(arc.xy(50.0), abs=0.05)


def test_to_model_rejects_an_unknown_source():
    with pytest.raises(ValueError):
        straight_reference_line().to_model(source="somewhere", osm_way_id=None, name=None)


def test_rect_grid_axes_and_round_trip():
    grid = RectGrid(s_top=60.0, s_bottom=10.0, t_left=14.0, t_right=-14.0, m_per_px=0.1)
    assert grid.size_px == (280, 500)
    assert grid.px_to_st(0, 0) == pytest.approx((60.0, 14.0))
    assert grid.px_to_st(280, 500) == pytest.approx((10.0, -14.0))
    s, t = grid.px_to_st(123.0, 321.0)
    assert grid.st_to_px(s, t) == pytest.approx((123.0, 321.0))


@pytest.mark.parametrize("kwargs", [
    dict(s_top=10.0, s_bottom=20.0, t_left=5.0, t_right=-5.0, m_per_px=0.1),
    dict(s_top=20.0, s_bottom=10.0, t_left=-5.0, t_right=5.0, m_per_px=0.1),
    dict(s_top=20.0, s_bottom=10.0, t_left=5.0, t_right=-5.0, m_per_px=0.0),
])
def test_rect_grid_rejects_inverted_or_degenerate_windows(kwargs):
    with pytest.raises(ValueError):
        RectGrid(**kwargs)
