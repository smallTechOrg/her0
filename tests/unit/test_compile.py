"""hero.model.compile: the deterministic model -> Scene v2 compiler (+ the committed fixture's reproducibility)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hero.model.compile import GROUND_HALF_M, SEAM_M, STEP_M, compile_scene, station_grid
from hero.model.scene_v2 import SceneV2, load_scene
from hero.model.types import (
    Entity,
    EntityState,
    EntityType,
    FootpathState,
    ModelKind,
    ObservationKind,
)
from tests.fixtures.street_fixtures import (
    curved_reference_line,
    make_entity,
    make_fidelity_map,
    make_model,
    make_observation,
    straight_reference_line,
)

ROOT = Path(__file__).resolve().parents[2]


def compile_(model, *, fid=None, obs=None, assets=None, tile=None):
    fid = fid or make_fidelity_map(model.reference_line.length_s, 1)
    return compile_scene(model, observations=obs or {}, assets=assets or {}, tile=tile, fidelity=fid,
                         slug_name="Test street", city="Bengaluru")


def straight_model(**kw):
    return make_model(straight_reference_line(100.0), **kw)


def ent(eid="ent-0000000001", type=EntityType.pole, s=10.0, t=5.5, **kw) -> Entity:
    return make_entity(entity_id=eid, type=type, s=s, t=t, **kw)


def tile_obs():
    ref = straight_reference_line(100.0)
    lat, lon = ref.point_at(50.0, 0.0)
    return make_observation(obs_id="tile1", kind=ObservationKind.satellite_overview, zoom=19, station_s=None,
                            view=None, lat=lat, lon=lon, size_px=(1280, 1280)).model_copy(update={"scale": 2})


# ------------------------------------------------------------------ determinism


def test_compile_is_deterministic_and_survives_a_json_round_trip():
    ref = curved_reference_line()
    m = make_model(ref, entities=[ent(s=12.5, t=5.9), ent("ent-0000000002", EntityType.tree, 40.0, -6.0)],
                   states=[FootpathState.intact, FootpathState.broken, FootpathState.missing, FootpathState.intact])
    a = compile_(m).model_dump_json()
    assert compile_(m).model_dump_json() == a
    again = type(m).model_validate_json(m.model_dump_json())
    assert compile_(again).model_dump_json() == a


def test_entity_and_dict_order_do_not_matter():
    es = [ent("ent-0000000001", s=10), ent("ent-0000000002", EntityType.tree, 40.0, 5.0),
          ent("ent-0000000003", EntityType.bin, 70.0, 5.2)]
    a = compile_(straight_model(entities=es)).model_dump(exclude={"model": {"model_hash"}})
    b = compile_(straight_model(entities=list(reversed(es)))).model_dump(exclude={"model": {"model_hash"}})
    assert a == b


def test_output_is_a_valid_scene_v2_that_load_scene_accepts():
    sc = compile_(straight_model(entities=[ent()]))
    assert isinstance(sc, SceneV2) and sc.schema_version == 2
    back = load_scene(json.loads(sc.model_dump_json()))
    assert isinstance(back, SceneV2) and back.model_dump_json() == sc.model_dump_json()
    assert sc.model.version == 0 and len(sc.model.model_hash) == 64 and sc.model.compiled_by == "hero.model.compile/1"


# ------------------------------------------------------------------ path / corridor


def test_station_grid_every_2_5_m_plus_the_end():
    assert station_grid(10.0) == [0.0, 2.5, 5.0, 7.5, 10.0]
    g = station_grid(100.2)
    assert g[-1] == 100.2 and g[-2] == 100.0 and len(g) == 42
    assert all(b > a for a, b in zip(g, g[1:]))
    assert station_grid(7.5)[-2:] == [5.0, 7.5]


def test_straight_road_maps_s_lat_to_lat_minus_s():
    sc = compile_(straight_model())
    assert all(p.x == 0.0 and p.theta == 0.0 for p in sc.path.samples)
    assert [p.z for p in sc.path.samples][:3] == [0.0, -2.5, -5.0]
    assert len(sc.corridor.stations) == len(sc.path.samples) == 41
    assert sc.path.samples[-1].s == 100.0 and sc.path.length_m == pytest.approx(100.0, abs=1e-3)


@pytest.mark.parametrize("side", ["left", "right"])
def test_constant_lateral_curve_length_matches_the_curvature_formula(side):
    ref = curved_reference_line(80.0, 100.0)  # right turn: bearing increases
    m = make_model(ref, side=side)
    sc = compile_(m)
    S = sc.path.samples
    theta_dot = (S[-1].theta - S[0].theta) / ref.length_s
    assert abs(theta_dot) == pytest.approx(1 / 80.0, rel=5e-3)
    assert (theta_dot < 0) == (side == "left")  # right-hand bend: the left footpath is on the outside
    for lat in (0.0, 3.0, 6.0, -5.0):
        for i0, i1 in ((0, len(S) - 1), (4, 30)):
            pts = [(p.x + lat * math.cos(p.theta), p.z + lat * math.sin(p.theta)) for p in S[i0:i1 + 1]]
            world = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
            # theta' over the sub-range (the polyline bearing is noisy per 2.5 m step; the sum telescopes)
            local_dot = (S[i1].theta - S[i0].theta) / (S[i1].s - S[i0].s)
            assert local_dot == pytest.approx(theta_dot, rel=0.05)
            expected = (S[i1].s - S[i0].s) * abs(1.0 - lat * local_dot)
            assert world == pytest.approx(expected, abs=0.01), (side, lat, i0, i1)


def test_theta_starts_at_zero_and_matches_the_bearing_change():
    ref = curved_reference_line()
    sc = compile_(make_model(ref, side="left"))
    th = [p.theta for p in sc.path.samples]
    assert th[0] == 0.0
    assert th[-1] == pytest.approx(-100.0 / 80.0, abs=5e-3)  # a 100 m arc of R=80 m turns 1.25 rad
    sc_r = compile_(make_model(ref, side="right"))
    assert sc_r.path.samples[-1].theta == pytest.approx(-th[-1], abs=1e-4)


def test_no_tile_means_null_tile_url_and_uv_with_a_tile_uv_is_filled():
    sc = compile_(straight_model())
    assert sc.ground.tile_url is None and all(st.ground_uv is None for st in sc.corridor.stations)
    sc = compile_(straight_model(), tile=tile_obs())
    assert sc.ground.tile_url == "/media/imagery/tile1.png"
    assert sc.corridor.ground_half_m == GROUND_HALF_M and sc.corridor.step_m == STEP_M
    for st in sc.corridor.stations:
        (u0, v0), (u1, v1) = st.ground_uv
        assert 0.0 <= u0 <= 1.0 and 0.0 <= v0 <= 1.0 and 0.0 <= u1 <= 1.0 and 0.0 <= v1 <= 1.0
        assert (u0, v0) != (u1, v1)
    assert all(p.tile_px is not None for p in sc.path.samples)


def test_lane_rules_and_footpath_centre():
    sc = compile_(straight_model())
    for st in sc.corridor.stations:
        l0, l1, l2 = st.lane_lat
        assert l0 - l1 >= 1.4 - 1e-6 and l1 - l2 == pytest.approx(1.8, abs=1e-6) or st.s > 100 - SEAM_M
    st0 = sc.corridor.stations[0]
    assert st0.footpath is not None and (st0.footpath.lo, st0.footpath.hi) == (5.0, 6.5)
    assert st0.lane_lat[0] == pytest.approx(5.75) and st0.lane_lat[1] == pytest.approx(4.25 - 0.9)
    assert [s.kind for s in st0.strips] == ["carriageway"]


def test_footpath_centre_is_pushed_out_when_too_close_to_the_road_lanes():
    m = straight_model()
    prof = []
    for iv in m.profile:
        strips = [s.model_copy(update={"t_lo": (4.3, 4.3), "t_hi": (5.0, 5.0)}) if s.kind.value == "footpath" else s
                  for s in iv.strips]
        prof.append(iv.model_copy(update={"strips": strips}))
    sc = compile_(m.model_copy(update={"profile": prof}))
    st = sc.corridor.stations[0]
    assert st.lane_lat[0] - st.lane_lat[1] >= 1.4 - 1e-6
    assert st.lane_lat[0] > 4.65  # moved beyond the real centre


def test_right_side_footpath_mirrors_lateral_values():
    left = compile_(straight_model(side="left"))
    right = compile_(straight_model(side="right"))
    assert left.corridor.stations[0].lane_lat == right.corridor.stations[0].lane_lat
    assert left.corridor.stations[0].footpath.lo == right.corridor.stations[0].footpath.lo
    assert left.segment.footpath_side.value == "left" and right.segment.footpath_side.value == "right"


def test_loop_seam_blends_the_last_6_m_toward_station_zero():
    m = straight_model()
    prof = list(m.profile)
    last = prof[-1]
    strips = [s.model_copy(update={"t_lo": (7.0, 7.0), "t_hi": (8.5, 8.5)}) if s.kind.value == "footpath" else s
              for s in last.strips]
    prof[-1] = last.model_copy(update={"strips": strips})
    sc = compile_(m.model_copy(update={"profile": prof}))
    sts = sc.corridor.stations
    first, end = sts[0], sts[-1]
    assert end.lane_lat == pytest.approx(first.lane_lat, abs=1e-3)
    assert (end.footpath.lo, end.footpath.hi) == pytest.approx((first.footpath.lo, first.footpath.hi), abs=1e-3)
    before_seam = [s for s in sts if s.s <= 100 - SEAM_M][-1]
    assert before_seam.footpath.lo == pytest.approx(7.0)
    mid = [s for s in sts if s.s == 97.5][0]
    assert first.footpath.lo < mid.footpath.lo < 7.0


# ------------------------------------------------------------------ footpath states


def test_missing_interval_has_null_footpath_a_gap_object_and_interpolated_centre():
    m = straight_model(states=[FootpathState.intact, FootpathState.missing, FootpathState.intact, FootpathState.intact])
    sc = compile_(m)
    gap_iv = m.profile[1]
    for st in sc.corridor.stations:
        inside = gap_iv.s_from <= st.s < gap_iv.s_to
        assert (st.footpath is None) == inside
        if inside:
            assert st.lane_lat[0] - st.lane_lat[1] >= 1.4 - 1e-6
    (gap,) = [o for o in sc.objects if o.type == "footpath_gap"]
    assert gap.id == f"gap-{gap_iv.id}" and gap.entity_id is None and gap.action.value == "dodge" and gap.lanes == [0]
    assert gap.x_m == pytest.approx(gap_iv.s_from) and gap.length_m == pytest.approx(gap_iv.s_to - gap_iv.s_from)
    assert any(r.state is FootpathState.missing for r in sc.cross_section.footpath_coverage)


def test_blocked_interval_adds_a_dodge_debris_object_and_keeps_the_strip():
    m = straight_model(states=[FootpathState.blocked, FootpathState.intact, FootpathState.intact, FootpathState.intact])
    sc = compile_(m)
    (blk,) = [o for o in sc.objects if o.label == "footpath blocked"]
    assert blk.type == "debris" and blk.action.value == "dodge"
    assert sc.corridor.stations[0].footpath is not None and sc.corridor.stations[0].footpath.state is FootpathState.blocked


def test_coverage_ranges_tile_the_length_and_walkability_counts_only_intact():
    states = [FootpathState.intact, FootpathState.broken, FootpathState.blocked, FootpathState.intact]
    sc = compile_(straight_model(states=states))
    cov = sc.cross_section.footpath_coverage
    assert cov[0].from_m == 0.0 and cov[-1].to_m == pytest.approx(100.0)
    assert all(a.to_m == b.from_m for a, b in zip(cov, cov[1:]))
    assert [c.state.value for c in cov] == ["intact", "broken", "blocked", "intact"]
    assert sc.walkability.footpath_coverage_pct == pytest.approx(50.0, abs=0.1)
    assert sc.walkability.hazard_count == 1  # the synthetic 'blocked' object


def test_walkability_score_reacts_to_crossings_and_hazards():
    base = compile_(straight_model(states=[FootpathState.intact]))
    cross = compile_(straight_model(states=[FootpathState.intact], entities=[ent(type=EntityType.crossing, s=50, t=0)]))
    assert cross.walkability.crossing_present and cross.walkability.score >= base.walkability.score
    hazards = [ent(f"ent-00000000{i:02x}", EntityType.pothole, 5.0 + 9 * i, 5.7) for i in range(8)]
    bad = compile_(straight_model(states=[FootpathState.intact], entities=hazards))
    assert bad.walkability.hazard_count == 8 and bad.walkability.score < base.walkability.score


# ------------------------------------------------------------------ objects


def test_object_fields_lane_assignment_and_evidence():
    e = ent(s=12.5, t=5.75, type=EntityType.parked_vehicle, description="Yellow auto-rickshaw.")
    obs = {"obs-0001": make_observation()}
    sc = compile_(straight_model(entities=[e]), obs=obs, assets={e.id: f"/media/crops/x/{e.id}.png?v=1"})
    (o,) = sc.objects
    assert o.id == o.entity_id == e.id and o.type == "parked_vehicle" and o.x_m == 12.5
    assert 0 in o.lanes and not o.scenery and o.action.value == "dodge"
    assert o.lateral_m == 5.75 and o.description == "Yellow auto-rickshaw."
    assert o.evidence.observation_count == 1 and o.evidence.sources.street_view == 1
    assert o.evidence.observation_ids == ["obs-0001"] and o.billboard_url.endswith("?v=1")
    assert o.latlon.lat != 0 and o.size_m.w > 0


def test_far_from_every_lane_is_scenery_with_no_action():
    sc = compile_(straight_model(entities=[ent(type=EntityType.wall, t=14.0)]))
    (o,) = sc.objects
    assert o.scenery and o.lanes == [] and o.action.value == "none" and not o.is_hazard


def test_right_side_entities_flip_to_game_lateral():
    sc = compile_(straight_model(side="right", entities=[ent(t=-5.75)]))
    (o,) = sc.objects
    assert o.lateral_m == 5.75 and 0 in o.lanes


def test_removed_and_low_confidence_entities_are_not_objects():
    es = [ent("ent-0000000001", s=10), ent("ent-0000000002", s=20, state=EntityState.removed),
          ent("ent-0000000003", s=30, confidence=0.1), ent("ent-0000000004", s=40, state=EntityState.disputed,
                                                           confidence=0.4)]
    sc = compile_(straight_model(entities=es))
    assert [o.id for o in sc.objects] == ["ent-0000000001", "ent-0000000004"]
    assert sc.objects[1].evidence.state == "disputed"


def test_canopy_low_tree_also_emits_a_low_branch_hazard():
    tree = ent(type=EntityType.tree, s=30, t=5.75).model_copy(update={"attributes": {"canopy_low": True}})
    sc = compile_(straight_model(entities=[tree]))
    ids = {o.id: o for o in sc.objects}
    assert set(ids) == {tree.id, f"lb-{tree.id}"}
    lb = ids[f"lb-{tree.id}"]
    assert lb.type == "low_branch" and lb.action.value == "slide" and lb.entity_id == tree.id


def test_objects_stay_inside_the_segment():
    sc = compile_(straight_model(entities=[ent(s=99.99, type=EntityType.parked_vehicle)]))
    (o,) = sc.objects
    assert o.x_m < 100.0 and o.x_m + o.length_m <= 105.0 + 1e-6


# ------------------------------------------------------------------ tokens / about / generation


def test_tokens_sit_on_intact_footpath_and_never_inside_hazards():
    hz = ent(type=EntityType.pothole, s=25.0, t=5.75)
    sc = compile_(straight_model(states=[FootpathState.intact], entities=[hz]))
    assert sc.tokens and [t.id for t in sc.tokens] == [f"t{i}" for i in range(1, len(sc.tokens) + 1)]
    assert all(t.lane == 0 and t.x_m < 100 for t in sc.tokens)
    pothole = [o for o in sc.objects if o.id == hz.id][0]
    assert all(not (pothole.x_m - 0.5 <= t.x_m <= pothole.x_m + pothole.length_m + 0.5) for t in sc.tokens)
    none = compile_(straight_model(states=[FootpathState.broken]))
    assert none.tokens == []


def test_about_block_counts_photos_and_labels_fidelity():
    ref = straight_reference_line(100.0)
    a, b, c = (make_observation(obs_id=i, kind=k, capture_date=d, station_s=s, view=None if k.value.startswith("sat")
                                else None)
               for i, k, d, s in (("a", ObservationKind.streetview, "2023-04", 10.0),
                                   ("b", ObservationKind.streetview, "2024-02", 40.0),
                                   ("c", ObservationKind.satellite_crop, "2024-09", None)))
    m = make_model(ref, fused_observation_ids=["a", "b", "c", "missing"], version=2, kind=ModelKind.fused)
    sc = compile_(m, obs={"a": a, "b": b, "c": c}, fid=make_fidelity_map(100.0, 2))
    about = sc.about
    assert about.model_version == 2 and about.fidelity.level == "L2" and about.fidelity.score == 2.0
    assert about.photos.total == 4 and about.photos.street_view == 3 and about.photos.satellite == 1
    assert about.capture_dates.oldest == "2023-04" and about.capture_dates.newest == "2024-02"
    assert about.last_updated == "2026-10-30" and about.placement_verified and not about.legacy
    assert "fidelity L2" in about.summary and "4 photos" in about.summary and "model v2" in about.summary
    assert sc.generation.quality.value == "full"


def test_degraded_flag_and_quality_for_a_map_only_model():
    sc = compile_(straight_model(), fid=make_fidelity_map(100.0, 0))
    assert sc.about.degraded and sc.generation.quality.value == "generic"
    assert "map-only" in sc.about.summary and sc.about.fidelity.level == "L0"


def test_legacy_model_is_flagged_in_the_about_block():
    m = straight_model(kind=ModelKind.legacy, source="legacy_straight")
    sc = compile_(m, fid=make_fidelity_map(100.0, 0))
    assert sc.about.legacy and not sc.about.degraded and not sc.about.placement_verified
    assert "legacy" in sc.about.summary


def test_buildings_are_converted_to_game_lateral():
    from hero.model.types import Building

    b = Building(id="bld-osm-1", source="osm", osm_way_id=1, side="right", footprint=[(0, -9.0), (20, -9.0), (20, -20.0)],
                 height_m=9.0, colour_hex="#c9b79c", description="Cream block.")
    sc = compile_(straight_model(side="left").model_copy(update={"buildings": [b]}))
    (bv,) = sc.buildings
    assert bv.side == "opposite" and bv.footprint[0] == (0.0, 9.0 * -1) and bv.color_hex == "#c9b79c"
    sc = compile_(straight_model(side="right").model_copy(update={"buildings": [b]}))
    assert sc.buildings[0].side == "modelled" and sc.buildings[0].footprint[0] == (0.0, 9.0)


# ------------------------------------------------------------------ the committed fixture

FIXTURE = ROOT / "tests" / "e2e" / "fixtures" / "model_fixture"


def test_curved_fixture_scene_is_a_25_degree_arc_over_100_m_and_follows_the_footpath():
    scene_path = ROOT / "tests" / "e2e" / "fixtures" / "scene_curved_v2.json"
    sc = load_scene(scene_path.read_text())
    assert isinstance(sc, SceneV2) and sc.segment.slug == "fixture-curved-street"
    assert sc.path.length_m == pytest.approx(100.0, abs=1.0)
    assert abs(sc.path.samples[-1].theta) >= math.radians(25)
    assert sc.model.version == 2 and sc.about.fidelity.level == "L2"
    states = {c.state.value for c in sc.cross_section.footpath_coverage}
    assert {"intact", "broken", "missing"} <= states  # the footpath narrows, shifts and breaks
    widths = [st.footpath.hi - st.footpath.lo for st in sc.corridor.stations if st.footpath]
    assert max(widths) - min(widths) > 0.5
    assert any(st.footpath is None for st in sc.corridor.stations)


def test_fixture_generator_is_reproducible_and_committed_files_are_current(tmp_path):
    from tests.fixtures import make_model_fixtures as mk

    a, b = tmp_path / "a", tmp_path / "b"
    mk.write(mk.build(), a, None)
    mk.write(mk.build(), b, None)
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files_a == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
        assert (a / rel).read_bytes() == (FIXTURE / rel).read_bytes(), f"committed fixture is stale: {rel}"
