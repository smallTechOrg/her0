"""Lane, kerb, token and walkability rules are read from gameplay.json (never hard-coded in compile)."""

from __future__ import annotations

import copy

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model.compile import compile_scene, rules_from_gameplay
from hero.model.types import EntityType, FootpathState
from tests.fixtures.street_fixtures import make_entity, make_fidelity_map, make_model, straight_reference_line


def gp_with(mutate) -> GameplayV1:
    d = copy.deepcopy(load_gameplay())
    mutate(d)
    return GameplayV1.model_validate(d)


BASE = GameplayV1.model_validate(load_gameplay())


def go(gp, entities=(), states=(FootpathState.intact,)):
    m = make_model(straight_reference_line(100.0), entities=list(entities), states=list(states))
    return compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, 1), slug_name="T",
                         city="C", gameplay=gp)


def test_lane_geometry_follows_the_inset_and_gap_constants():
    a = go(BASE).plan.stations[5].lane_lat
    b = go(gp_with(lambda d: d["lanes"].update(road_edge_inset_m=1.2, road_lane_gap_m=2.2))).plan.stations[5].lane_lat
    assert a[1] == pytest.approx(4.25 - 0.9) and a[1] - a[2] == pytest.approx(1.8)
    assert b[1] == pytest.approx(4.25 - 1.2) and b[1] - b[2] == pytest.approx(2.2)


def test_min_footpath_to_edge_pushes_the_footpath_lane_out():
    wide = gp_with(lambda d: d["lanes"].update(min_footpath_to_edge_m=3.5))
    st = go(wide).plan.stations[5]
    assert st.lane_lat[0] - st.lane_lat[1] == pytest.approx(3.5)
    assert go(BASE).plan.stations[5].lane_lat[0] == pytest.approx(5.75)  # footpath centre (5.0..6.5) already clear


def test_prior_footpath_centre_uses_default_width_when_there_is_no_footpath():
    states = [FootpathState.missing]
    a = go(BASE, states=states).plan.stations[5].lane_lat[0]
    b = go(gp_with(lambda d: d["lanes"].update(default_footpath_width_m=3.0)), states=states).plan.stations[5].lane_lat[0]
    assert a == pytest.approx(4.25 + 0.15 + 1.8 / 2) and b == pytest.approx(4.25 + 0.15 + 3.0 / 2)


def test_lane_band_widths_decide_which_lane_an_object_occupies():
    pole = make_entity(type=EntityType.pole, s=20.0, t=3.6).model_copy(update={"attributes": {}})
    # lane1 centre = 3.35; band 1.6 -> reaches 4.15; a pole at 3.6 +- 0.15 is in lane 1 either way, but lane 2 (centre 1.55)
    # only reaches it when its band is widened to 4.4 m
    narrow = go(BASE, [pole]).plan.objects[0].lanes
    wide = go(gp_with(lambda d: d["lanes"]["widths_m"].update(road=4.4)), [pole]).plan.objects[0].lanes
    assert narrow == [1] and wide == [1, 2]


def test_token_spacing_and_cap_come_from_gameplay():
    few = go(gp_with(lambda d: d["tokens"].update(per_intact_m=25.0))).plan.tokens
    many = go(BASE).plan.tokens
    assert len(few) < len(many) <= BASE.tokens.max_per_street
    capped = go(gp_with(lambda d: d["tokens"].update(max_per_street=3))).plan.tokens
    assert len(capped) == 3


def test_crossing_tokens_use_the_configured_lane():
    cross = make_entity(entity_id="ent-00000000cc", type=EntityType.crossing, s=40.0, t=0.0)
    toks = go(BASE, [cross]).plan.tokens
    assert any(t.reason.value == "crossing" and t.lane == BASE.tokens.crossing_lane for t in toks)
    toks2 = go(gp_with(lambda d: d["tokens"].update(crossing_lane=2)), [cross]).plan.tokens
    assert any(t.reason.value == "crossing" and t.lane == 2 for t in toks2)


def test_walkability_weights_come_from_gameplay():
    pole = make_entity(type=EntityType.pole, s=20.0, t=5.9)
    base = go(BASE, [pole]).scene.walkability
    assert base.hazard_count == 1 and base.footpath_coverage_pct == 100.0
    assert base.score == round(0.6 * 100 + 20 * (1 - 1 / 10))
    harsher = go(gp_with(lambda d: d["scoring"]["walkability"].update(hazard_weight=50.0, hazard_norm=2.0)), [pole]).scene.walkability
    assert harsher.score == round(0.6 * 100 + 50 * (1 - 1 / 2))
    bonus = go(gp_with(lambda d: d["scoring"]["walkability"].update(crossing_bonus=0)),
               [make_entity(entity_id="ent-00000000cc", type=EntityType.crossing, s=40.0, t=0.0)]).scene.walkability
    assert bonus.crossing_present and bonus.score == 60 + 20


def test_gameplay_default_is_loaded_from_the_bundled_json_and_rules_match_it():
    m = make_model(straight_reference_line(100.0))
    a = compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, 1), slug_name="T", city="C")
    b = compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, 1), slug_name="T", city="C",
                      gameplay=BASE)
    assert a.plan.model_dump_json() == b.plan.model_dump_json() and a.scene.model_dump_json() == b.scene.model_dump_json()
    r = rules_from_gameplay(BASE)
    assert r.road_edge_inset == 0.9 and r.lane_gap == 1.8 and r.bands == (1.8, 1.6, 2.0) and r.max_tokens == 12


def test_cross_section_lane_widths_are_the_gameplay_bands():
    lanes = go(BASE).scene.cross_section.lanes
    assert [ln.width_m for ln in lanes] == [1.8, 1.6, 2.0]
