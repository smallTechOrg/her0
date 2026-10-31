"""plan_street: the geometry half of the compile."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model.compile import compile_scene
from hero.model.gaps import analyze_gaps
from hero.model.refline import ReferenceLine
from hero.model.street_plan import plan_street
from hero.model.street_plan_types import PLAN_ID, StreetPlan, t_of_lat
from hero.model.types import EntityType, FootpathState
from tests.fixtures.street_fixtures import (
    curved_reference_line,
    make_entity,
    make_fidelity_map,
    make_model,
    straight_reference_line,
)

ROOT = Path(__file__).resolve().parents[2]
GP = GameplayV1.model_validate(load_gameplay())


def plan_of(model, length=None):
    length = length or model.reference_line.length_s
    fid = make_fidelity_map(length, 1)
    gaps = analyze_gaps(model, {}, fid, None)
    return plan_street(model, observations={}, fidelity=fid, gaps=gaps, textures=None, gameplay=GP)


def test_plan_is_valid_aligned_and_round_trips():
    m = make_model(curved_reference_line(), states=[FootpathState.intact, FootpathState.broken, FootpathState.missing,
                                                    FootpathState.intact])
    p = plan_of(m)
    assert p.plan == PLAN_ID and p.slug == m.segment_slug and p.model_version == m.version
    assert len(p.stations) == len(p.reference_line.points) == 41
    assert [s.s for s in p.stations] == [pt.s for pt in p.reference_line.points]
    assert p.reference_line.points[-1].s == p.length_m == m.reference_line.length_s
    assert StreetPlan.model_validate_json(p.model_dump_json()) == p
    assert p.frame.origin.lat == m.reference_line.points[0].lat


def test_plan_street_equals_the_plan_inside_compile_scene():
    m = make_model(curved_reference_line())
    fid = make_fidelity_map(100.0, 1)
    res = compile_scene(m, observations={}, assets={}, tile=None, fidelity=fid, slug_name="Ten", city="Bengaluru", gameplay=GP)
    direct = plan_street(m, observations={}, fidelity=fid, gaps=res.gaps, textures=None, gameplay=GP, name="Ten",
                         city="Bengaluru")
    assert direct.model_dump_json() == res.plan.model_dump_json()
    # contract signature: no tile / name / city -> defaults
    assert plan_of(m).name == "100 Feet Road"


def test_reference_line_points_follow_the_model_geometry():
    m = make_model(curved_reference_line())
    p = plan_of(m)
    ref = ReferenceLine.from_model(m.reference_line)
    for pt in p.reference_line.points[::7]:
        lat, lon = ref.point_at(pt.s, 0.0)
        assert (pt.lat, pt.lon) == (round(lat, 6), round(lon, 6))
        assert pt.bearing_deg == pytest.approx(ref.bearing_deg(pt.s), abs=0.01)
    assert abs(p.reference_line.points[-1].bearing_deg - p.reference_line.points[0].bearing_deg) > 20  # the arc turns


def test_kerb_follows_footpath_presence_and_height():
    m = make_model(straight_reference_line(100.0), states=[FootpathState.intact, FootpathState.missing])
    p = plan_of(m)
    assert p.stations[0].kerb.height_m == 0.15 and p.stations[0].kerb.width_m == 0.2
    assert p.stations[0].kerb.lat == pytest.approx(4.25)          # carriageway edge nearest the footpath
    assert p.stations[-1].footpath is None and p.stations[-1].kerb.height_m == 0.0


def test_a_kerb_cut_lowers_the_kerb_locally():
    cut = make_entity(entity_id="ent-00000000c1", type=EntityType.kerb_cut, s=40.0, t=4.5).model_copy(
        update={"extent": make_entity().extent.model_copy(update={"length_s": 2.0})})
    m = make_model(straight_reference_line(100.0), entities=[cut], states=[FootpathState.intact])
    p = plan_of(m)
    heights = {st.s: st.kerb.height_m for st in p.stations}
    assert heights[40.0] == 0.0 and heights[42.5] == 0.15 and heights[37.5] == 0.15 and heights[45.0] == 0.15


def test_stations_carry_lane_laterals_from_the_gameplay_rules():
    m = make_model(straight_reference_line(100.0), states=[FootpathState.intact])
    p = plan_of(m)
    l0, l1, l2 = p.stations[10].lane_lat
    assert l1 == pytest.approx(4.25 - GP.lanes.road_edge_inset_m)
    assert l1 - l2 == pytest.approx(GP.lanes.road_lane_gap_m)
    assert l0 - l1 >= GP.lanes.min_footpath_to_edge_m - 1e-9


def test_loop_seam_blends_the_last_6_m_toward_station_zero():
    m = make_model(curved_reference_line(), states=[FootpathState.intact])
    p = plan_of(m)
    first, last = p.stations[0], p.stations[-1]
    assert last.lane_lat == pytest.approx(first.lane_lat, abs=1e-3)


def test_right_footpath_plan_uses_game_lateral_and_t_of_lat_flips():
    m = make_model(straight_reference_line(100.0), side="right", states=[FootpathState.intact])
    p = plan_of(m)
    assert p.footpath_side == "right" and p.stations[0].footpath.lo > 0
    assert t_of_lat(5.9, p.footpath_side) == -5.9 and t_of_lat(5.9, "left") == 5.9


def test_fixed_hazards_are_objects_and_tokens_sit_on_the_footpath():
    pole = make_entity(entity_id="ent-00000000b1", type=EntityType.pole, s=33.0, t=5.9)
    m = make_model(straight_reference_line(100.0), entities=[pole], states=[FootpathState.intact])
    p = plan_of(m)
    assert [o.id for o in p.objects] == ["ent-00000000b1"] and p.objects[0].action.value == "dodge"
    assert p.tokens and all(t.lane == 0 for t in p.tokens)
    assert not any(p.objects[0].x_m - 0.5 <= t.x_m <= p.objects[0].x_m + p.objects[0].length_m + 0.5 for t in p.tokens)


def test_missing_and_blocked_intervals_add_synthetic_objects():
    m = make_model(straight_reference_line(100.0), states=[FootpathState.intact, FootpathState.missing,
                                                           FootpathState.blocked, FootpathState.intact])
    p = plan_of(m)
    assert {o.type for o in p.objects} == {"footpath_gap", "debris"}
    assert {o.id.split("-")[0] for o in p.objects} == {"gap", "blocked"}


def test_fidelity_is_copied_into_the_plan():
    m = make_model(straight_reference_line(100.0))
    p = plan_of(m)
    assert p.fidelity.overall_level == 1 and len(p.fidelity.bins) == 20 and p.fidelity.bins[3].s_from == 15.0


def test_interpolated_list_is_the_gap_report_without_furniture_entries():
    m = make_model(straight_reference_line(100.0))
    fid = make_fidelity_map(100.0, 0)
    gaps = analyze_gaps(m, {}, fid, None)
    p = plan_street(m, observations={}, fidelity=fid, gaps=gaps, textures=None, gameplay=GP)
    assert p.interpolated == [i for i in gaps.interpolations if i.what != "furniture_unknown"]
    assert any(i.what == "furniture_unknown" for i in gaps.interpolations)


def test_plan_is_deterministic_across_a_model_json_round_trip():
    from hero.model.types import CanonicalModel

    m = make_model(curved_reference_line(), entities=[make_entity(s=12.5, t=5.9)])
    a = plan_of(m).model_dump_json()
    b = plan_of(CanonicalModel.model_validate_json(m.model_dump_json())).model_dump_json()
    assert a == b and "NaN" not in a


def test_committed_plans_are_current_valid_json():
    for name in ("curved", "bellandur"):
        raw = json.loads((ROOT / "tests" / "fixtures" / "plan" / f"{name}.plan.json").read_text())
        p = StreetPlan.model_validate(raw)
        assert p.length_m == pytest.approx(100.0, abs=0.01)
        assert all(math.isfinite(st.kerb.lat) for st in p.stations)
