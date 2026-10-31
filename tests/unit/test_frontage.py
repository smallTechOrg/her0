"""Frontage planning: contiguous segments on both sides, interpolation rules, clamps, determinism."""

from __future__ import annotations

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model import frontage as fr
from hero.model.compile import compile_scene
from hero.model.types import Building, EntityType
from tests.fixtures.street_fixtures import make_entity, make_fidelity_map, make_model, straight_reference_line

GP = GameplayV1.model_validate(load_gameplay())


def bld(bid, s0, s1, t0, t1, side="left", h=9.0, colour=None, source="osm") -> Building:
    return Building(id=bid, source=source, side=side, footprint=[(s0, t0), (s1, t0), (s1, t1), (s0, t1)], height_m=h,
                    colour_hex=colour)


def plan_of(buildings=(), entities=(), slug="fixture-curved-street", side="left", length=100.0):
    m = make_model(straight_reference_line(length), entities=list(entities), side=side, slug=slug)
    m = m.model_copy(update={"buildings": list(buildings)})
    return compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(length, 1),
                         slug_name="T", city="C", gameplay=GP).plan


def sides(plan):
    return {s.side: s.segments for s in plan.frontage.sides}


def at(segs, s):
    return next(g for g in segs if g.s_from <= s < g.s_to)


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("length", [100.0, 52.5, 37.0])
def test_both_sides_tile_the_street_without_holes(side, length):
    p = plan_of([bld("a", 3, 20, 9, 15) if side == "left" else bld("a", 3, 20, -15, -9, "right")], side=side,
                length=length)
    for segs in sides(p).values():
        assert segs[0].s_from == 0.0 and segs[-1].s_to == pytest.approx(length)
        for a, b in zip(segs, segs[1:]):
            assert a.s_to == b.s_from
        assert all(g.s_to > g.s_from and g.height_m > 0 for g in segs)


def test_evidence_segments_carry_the_footprint_attributes():
    p = plan_of([bld("bld-1", 0, 40, 9, 15, h=12.0, colour="#c9b79c")])
    g = at(sides(p)["modelled"], 20.0)
    assert (g.kind, g.source, g.building_id, g.interpolated) == ("building", "osm", "bld-1", False)
    assert g.lat_from == g.lat_to == 9.0 and g.height_m == 12.0 and g.colour_hex == "#c9b79c" and g.storeys == 4
    assert g.confidence > 0.2


def test_two_sided_gap_blends_lat_height_and_colour_linearly():
    p = plan_of([bld("a", 0, 20, 9, 15, h=6.0, colour="#000000"), bld("b", 40, 60, 13, 19, h=14.0, colour="#c8c8c8")])
    segs = sides(p)["modelled"]
    mid = at(segs, 30.0)
    assert mid.interpolated and mid.source == "interpolated" and mid.building_id is None
    lat = (mid.lat_from + mid.lat_to) / 2
    assert lat == pytest.approx(11.0, abs=0.6) and mid.height_m == pytest.approx(10.0, abs=0.8)
    assert 80 <= int(mid.colour_hex[1:3], 16) <= 120
    first, last = at(segs, 21.0), at(segs, 39.0)
    assert 9.0 <= first.lat_from < first.lat_to < last.lat_to <= 13.0 + 1e-6  # monotone ramp between the anchors
    assert first.height_m < last.height_m


def test_one_sided_gap_copies_the_anchor_within_the_jitter_bounds():
    p = plan_of([bld("a", 0, 20, 9, 15, h=8.0, colour="#b0b0b0")])
    g = at(sides(p)["modelled"], 25.0)
    assert g.source == "interpolated" and abs(g.lat_from - 9.0) <= fr.JITTER_LAT_M + 1e-6
    assert abs(g.height_m - 8.0) <= fr.JITTER_H_M + 0.25 and g.colour_hex == "#b0b0b0"
    far = at(sides(p)["modelled"], 90.0)
    assert far.source == "prior"  # nothing within 30 m


def test_prior_is_a_neutral_wall_plane_chosen_by_slug_side_and_bin():
    a = plan_of(slug="street-a")
    a2 = plan_of(slug="street-a")
    b = plan_of(slug="street-b")
    assert a.frontage.model_dump_json() == a2.frontage.model_dump_json()
    assert a.frontage.model_dump_json() != b.frontage.model_dump_json()
    for side, segs in sides(a).items():
        for g in segs:
            assert g.source == "prior" and g.interpolated and 6.0 <= g.height_m <= 11.0 and g.colour_hex in fr.PALETTE
            assert g.confidence == fr.CONF_PRIOR
    mod = sides(a)["modelled"][0]
    assert mod.lat_from == pytest.approx(6.5 + fr.PRIOR_MODELLED_OFFSET_M)       # footpath.hi + 1.5
    opp = sides(a)["opposite"][0]
    assert opp.lat_from == pytest.approx(-4.25 - fr.PRIOR_OPPOSITE_OFFSET_M)    # outer opposite strip edge - 1.5


def test_lateral_values_never_enter_the_carriageway_or_the_modelled_footpath():
    p = plan_of([bld("near", 0, 50, 1, 8), bld("opp", 0, 50, -3, -9, "right")])
    mod, opp = at(sides(p)["modelled"], 25.0), at(sides(p)["opposite"], 25.0)
    assert mod.lat_from == pytest.approx(6.5 + fr.MODELLED_CLEAR_M)               # footpath.hi + 0.8
    assert opp.lat_from == pytest.approx(-4.25 - fr.OPPOSITE_CLEAR_M)             # carriageway.lo - 1.0
    far = plan_of([bld("far", 0, 100, 34, 40)])
    assert all(g.lat_from <= fr.MAX_LAT_M and g.lat_to <= fr.MAX_LAT_M for g in sides(far)["modelled"])


def _wall(t, conf=0.8, eid="ent-0000000aa1"):
    wall = make_entity(entity_id=eid, type=EntityType.wall, s=30.0, t=t, confidence=conf)
    return wall.model_copy(update={"extent": wall.extent.model_copy(update={"length_s": 6.0, "width_t": 0.3}), "height_m": 2.2})


def test_confident_wall_on_the_building_line_defines_the_facade_line_and_fills_the_skyline():
    p = plan_of([bld("a", 0, 100, 14, 22)], [_wall(13.6)])
    g = at(sides(p)["modelled"], 32.0)
    assert (g.kind, g.source, g.entity_id) == ("wall", "entity", "ent-0000000aa1")
    assert g.lat_from == pytest.approx(13.6 - fr.WALL_HALF_THICKNESS_M)     # within WALL_OFF_LINE_M: stays where detected
    assert g.height_m == 9.0                                                 # raised to the building: no sky notch above a 2.2 m wall
    assert at(sides(p)["modelled"], 10.0).building_id == "a" and at(sides(p)["modelled"], 90.0).building_id == "a"


def test_confident_wall_far_in_front_of_the_building_line_is_merged_into_it_not_a_wedge():
    p = plan_of([bld("a", 0, 100, 14, 22)], [_wall(8.0)])
    g = at(sides(p)["modelled"], 32.0)
    assert (g.kind, g.source, g.entity_id, g.interpolated) == ("wall", "entity", "ent-0000000aa1", True)
    assert g.lat_from == pytest.approx(14.0) and g.lat_to == pytest.approx(14.0)   # the median building line
    assert g.height_m == 9.0
    nb = [x for x in sides(p)["modelled"] if x is not g]
    assert all(abs(x.lat_from - 14.0) < 1e-6 for x in nb)                          # no lateral step anywhere


def test_weak_wall_detections_do_not_define_the_facade():
    wall = make_entity(entity_id="ent-0000000aa2", type=EntityType.wall, s=30.0, t=8.0, confidence=0.4)
    p = plan_of([bld("a", 0, 100, 14, 22)], [wall])
    assert {g.building_id for g in sides(p)["modelled"]} == {"a"}


def test_a_building_straddling_the_other_side_is_only_used_on_its_own_side():
    p = plan_of([bld("a", 0, 100, 9, 15)])
    assert {g.source for g in sides(p)["opposite"]} == {"prior"}
    assert {g.building_id for g in sides(p)["modelled"]} == {"a"}


def test_right_footpath_model_mirrors_laterals():
    p = plan_of([bld("a", 0, 100, -15, -9, "right")], side="right")
    g = sides(p)["modelled"][0]
    assert g.building_id == "a" and g.lat_from == 9.0  # game lateral is positive toward the footpath side


def test_adjacent_identical_cells_merge_into_long_segments():
    p = plan_of([bld("a", 0, 100, 9, 15)])
    assert len(sides(p)["modelled"]) == 1
    assert len(sides(p)["opposite"]) <= 40  # prior cells vary per 5 m bin, never per 2.5 m


def test_cell_grid_covers_the_length_exactly():
    for length in (100.0, 52.5, 37.0, 2.6, 5.0):
        cells = fr.cell_grid(length)
        assert cells[0][0] == 0.0 and cells[-1][1] == length
        assert all(b - a > 0 for a, b in cells) and all(x[1] == y[0] for x, y in zip(cells, cells[1:]))
