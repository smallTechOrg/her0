"""Runtime classification (fixed vs dynamic_context) and the dynamic summary / spawn hints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model import catalogue, dynamic
from hero.model.compile import compile_scene
from hero.model.dynamic import runtime_of
from hero.model.street_plan_types import SPAWN_TYPES
from hero.model.types import CanonicalModel, EntityType, FidelityMap, Observation
from tests.fixtures.street_fixtures import make_entity, make_fidelity_map, make_model, straight_reference_line

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "tests" / "fixtures" / "recorded" / "bellandur"
BAND = (5.0, 6.5)
GP = GameplayV1.model_validate(load_gameplay())


def ent(type_, t=5.8, **attrs):
    e = make_entity(type=type_, t=t)
    return e.model_copy(update={"attributes": attrs}) if attrs else e


@pytest.mark.parametrize("t", ["tree", "pole", "bollard", "bin", "railing", "signboard", "bus_stop", "drain", "manhole",
                               "kerb_cut", "ramp", "crossing", "shopfront", "wall", "gate", "low_branch"])
def test_attached_and_fixed_types_are_fixed_anywhere(t):
    assert runtime_of(ent(EntityType(t), t=-9.0), BAND) == "fixed"
    assert runtime_of(ent(EntityType(t), t=5.8), BAND) == "fixed"


@pytest.mark.parametrize("t", ["parked_vehicle", "vendor_stall", "speed_breaker"])
def test_movable_types_are_dynamic_even_on_the_footpath(t):
    assert runtime_of(ent(EntityType(t), t=5.8), BAND) == "dynamic_context"


@pytest.mark.parametrize("t", ["pothole", "debris"])
def test_pothole_and_debris_follow_the_footpath_band(t):
    assert runtime_of(ent(EntityType(t), t=5.8), BAND) == "fixed"
    assert runtime_of(ent(EntityType(t), t=0.5), BAND) == "dynamic_context"
    assert runtime_of(ent(EntityType(t), t=BAND[1] + 0.01), BAND) == "dynamic_context"


def test_other_is_fixed_only_when_it_blocks_the_footpath_inside_the_band():
    assert runtime_of(ent(EntityType.other, blocks_footpath=True), BAND) == "fixed"
    assert runtime_of(ent(EntityType.other, blocks_footpath=True, t=0.0), BAND) == "dynamic_context"
    assert runtime_of(ent(EntityType.other), BAND) == "dynamic_context"
    assert runtime_of(ent(EntityType.other, blocks_footpath=False), BAND) == "dynamic_context"


def test_lateral_argument_overrides_the_model_t_for_right_footpath_models():
    e = ent(EntityType.pothole, t=-5.8)  # right footpath: t negative, game lateral positive
    assert runtime_of(e, BAND) == "dynamic_context"
    assert runtime_of(e, BAND, lateral=5.8) == "fixed"


def test_catalogue_runtime_column_matches_the_spec_table():
    dyn = {t.value for t, c in catalogue.CATALOGUE.items() if c.runtime == "dynamic_context"}
    pos = {t.value for t, c in catalogue.CATALOGUE.items() if c.runtime == "by_position"}
    assert dyn == {"parked_vehicle", "vendor_stall", "speed_breaker"}
    assert pos == {"pothole", "debris", "other"}


def test_band_helpers():
    assert dynamic.widen((5.0, 6.5)) == (4.7, 6.8)
    assert dynamic.prior_band(4.25) == pytest.approx((4.4, 5.9))


# ------------------------------------------------------------------ in the compile


def _compile(entities, side="left", fused=None):
    ref = straight_reference_line(100.0)
    m = make_model(ref, entities=entities, side=side)
    return m, compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, 1),
                            slug_name="Test", city="Bengaluru")


def test_dynamic_entities_leave_objects_furniture_and_tokens_but_are_counted():
    ents = [make_entity(entity_id="ent-0000000001", type=EntityType.parked_vehicle, s=20.0, t=5.9),
            make_entity(entity_id="ent-0000000002", type=EntityType.pole, s=30.0, t=5.9),
            make_entity(entity_id="ent-0000000003", type=EntityType.vendor_stall, s=40.0, t=5.9)]
    m, r = _compile(ents)
    assert r.scene.dynamic_context.excluded_count == 2
    assert r.scene.dynamic_context.entity_ids == ["ent-0000000001", "ent-0000000003"]
    assert r.plan.dynamic_context.entity_ids == r.scene.dynamic_context.entity_ids
    assert {o.entity_id for o in r.plan.objects} == {"ent-0000000002"}
    assert {f.id for f in r.plan.furniture} == {"ent-0000000002"}
    assert "1 parked vehicle" in r.scene.about.dynamic_summary and "1 vendor stall" in r.scene.about.dynamic_summary


def test_dynamic_entities_do_not_move_tokens():
    from hero.model.types import FootpathState

    def go(entities):
        m = make_model(straight_reference_line(100.0), entities=entities, states=[FootpathState.intact])
        return compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, 1),
                             slug_name="T", city="C").plan.tokens

    base = go([])
    assert len(base) >= 8
    car = make_entity(type=EntityType.parked_vehicle, s=14.0, t=5.9)
    assert go([car]) == base


def test_parked_vehicle_on_the_footpath_still_counts_in_the_footpath_state_but_not_in_hazards():
    ents = [make_entity(type=EntityType.parked_vehicle, s=20.0, t=5.9)]
    m, r = _compile(ents)
    assert r.scene.walkability.hazard_count == 0 and r.plan.objects == []


def test_model_json_is_untouched_by_the_classification():
    m = CanonicalModel.model_validate_json((REC / "model_v1.json").read_text())
    before = m.model_dump_json()
    obs = {o["id"]: Observation.model_validate(o) for o in json.loads((REC / "observations.json").read_text())}
    fid = FidelityMap.model_validate_json((REC / "fidelity_v1.json").read_text())
    compile_scene(m, observations=obs, assets={}, tile=None, fidelity=fid, slug_name="B", city="C")
    assert m.model_dump_json() == before
    assert before == CanonicalModel.model_validate_json((REC / "model_v1.json").read_text()).model_dump_json()


def test_bellandur_has_no_vehicle_among_fixed_things_and_counts_match_the_model():
    m = CanonicalModel.model_validate_json((REC / "model_v1.json").read_text())
    obs = {o["id"]: Observation.model_validate(o) for o in json.loads((REC / "observations.json").read_text())}
    fid = FidelityMap.model_validate_json((REC / "fidelity_v1.json").read_text())
    r = compile_scene(m, observations=obs, assets={}, tile=None, fidelity=fid, slug_name="B", city="C")
    kinds = {o.type for o in r.plan.objects} | {f.type for f in r.plan.furniture}
    assert not kinds & {"parked_vehicle", "vendor_stall", "speed_breaker"}
    live = [e for e in m.entities if e.state.value != "removed" and e.confidence >= 0.2]
    parked = [e for e in live if e.type is EntityType.parked_vehicle]
    assert len(parked) > 20
    st = r.scene.dynamic_context.by_type["parked_vehicle"]
    assert st.count == len(parked) and st.per_100m == pytest.approx(len(parked), abs=0.1)
    assert r.scene.dynamic_context.excluded_count == len(r.scene.dynamic_context.entity_ids) >= len(parked)
    assert sum(st.lane_share) == pytest.approx(1.0, abs=0.01)


# ------------------------------------------------------------------ spawn hints


def test_spawn_hints_without_enough_entities_are_the_prior_and_weights_sum_to_one():
    h = dynamic.spawn_hints([], 100.0, GP)
    prior = GP.spawner.default_spawn_hints
    assert h.density == prior.density and h.type_weights == prior.type_weights
    two = [make_entity(entity_id=f"ent-00000000{i}", type=EntityType.parked_vehicle) for i in (1, 2)]
    assert dynamic.spawn_hints(two, 100.0, GP).type_weights == prior.type_weights


def test_spawn_hints_mix_observed_and_prior_and_density_saturates():
    ents = [make_entity(entity_id=f"ent-00000000{i}", type=EntityType.parked_vehicle) for i in range(1, 10)]
    ents = [e.model_copy(update={"subtype": "two-wheeler" if i % 3 else "car"}) for i, e in enumerate(ents)]
    ents.append(make_entity(entity_id="ent-00000000ff", type=EntityType.vendor_stall))
    h = dynamic.spawn_hints(ents, 100.0, GP)
    assert tuple(h.type_weights) == SPAWN_TYPES and sum(h.type_weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert h.type_weights["two_wheeler"] > h.type_weights["parked_car"] > h.type_weights["barricade"]
    assert h.density == pytest.approx(10 / 40.0, abs=1e-3)
    assert dynamic.spawn_hints(ents * 20, 50.0, GP).density == 1.0


def test_debris_splits_between_cone_and_barricade_and_potholes_merge_with_speed_breakers():
    ents = [make_entity(entity_id=f"ent-0000000a0{i}", type=t) for i, t in
            enumerate([EntityType.debris] * 4 + [EntityType.pothole, EntityType.speed_breaker])]
    h = dynamic.spawn_hints(ents, 100.0, GP)
    prior = GP.spawner.default_spawn_hints.type_weights
    # observed: cone 2, barricade 2, pothole 2 of 6 -> each 1/3 of the 0.7 share
    assert h.type_weights["cone"] == pytest.approx(0.7 / 3 + 0.3 * prior["cone"], abs=2e-4)
    assert h.type_weights["pothole"] == pytest.approx(0.7 / 3 + 0.3 * prior["pothole"], abs=2e-4)
    assert h.type_weights["parked_car"] == pytest.approx(0.3 * prior["parked_car"], abs=2e-4)


def test_spawn_hints_lane_weights_follow_the_lanes_and_sum_to_one():
    ents = [make_entity(entity_id=f"ent-00000000{i}", type=EntityType.parked_vehicle) for i in range(1, 5)]
    h = dynamic.spawn_hints(ents, 100.0, GP, lanes=[1, 1, 2, 0])
    assert h.lane_weights == (0.25, 0.5, 0.25) and sum(h.lane_weights) == 1.0
    empty = dynamic.spawn_hints([], 100.0, GP)  # no observation: derived from the gameplay base tables
    assert sum(empty.lane_weights) == pytest.approx(1.0, abs=1e-9) and all(w > 0 for w in empty.lane_weights)
