"""Fixed furniture of the plan: which entities, size caps, tints, collision proxies, ordering."""

from __future__ import annotations

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model.compile import compile_scene
from hero.model.street_plan_types import FIXED_FURNITURE_TYPES, MAX_SIZE_M
from hero.model.texture_types import TextureRefs
from hero.model.types import EntityState, EntityType, Extent
from tests.fixtures.street_fixtures import make_entity, make_fidelity_map, make_model, straight_reference_line

GP = GameplayV1.model_validate(load_gameplay())


def ent(i, type_, s=10.0, t=5.8, **kw):
    return make_entity(entity_id=f"ent-{i:010x}", type=type_, s=s, t=t, **kw)


def plan_of(entities, side="left", textures=None, fid_level=1):
    m = make_model(straight_reference_line(100.0), entities=entities, side=side)
    return compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(100.0, fid_level),
                         slug_name="T", city="C", textures=textures, gameplay=GP).plan


def test_only_fixed_furniture_types_become_furniture_and_dynamic_ones_never_do():
    ents = [ent(1, EntityType.pole), ent(2, EntityType.tree, s=20), ent(3, EntityType.bin, s=30),
            ent(4, EntityType.parked_vehicle, s=40), ent(5, EntityType.vendor_stall, s=50), ent(6, EntityType.wall, s=60),
            ent(7, EntityType.gate, s=70), ent(8, EntityType.shopfront, s=80), ent(9, EntityType.pothole, s=90, t=5.8)]
    p = plan_of(ents)
    assert [f.type for f in p.furniture] == ["pole", "tree", "bin"]
    assert {f.type for f in p.furniture} <= set(FIXED_FURNITURE_TYPES)
    assert "pothole" in {o.type for o in p.objects}  # fixed footpath pothole: a collider, not furniture


def test_furniture_is_sorted_by_s_then_id_and_ids_are_unique():
    ents = [ent(3, EntityType.pole, s=30), ent(2, EntityType.bollard, s=10), ent(1, EntityType.bollard, s=10),
            ent(4, EntityType.pole, s=5)]
    p = plan_of(ents)
    assert [(f.s, f.id) for f in p.furniture] == sorted((f.s, f.id) for f in p.furniture)
    assert len({f.id for f in p.furniture}) == 4


def test_removed_and_weak_entities_are_dropped():
    ents = [ent(1, EntityType.pole, state=EntityState.removed), ent(2, EntityType.pole, s=20, confidence=0.1),
            ent(3, EntityType.pole, s=30, confidence=0.25)]
    assert [f.id for f in plan_of(ents).furniture] == ["ent-0000000003"]


def test_sizes_are_capped_per_type():
    big = ent(1, EntityType.tree).model_copy(update={"height_m": 40.0, "extent": Extent(length_s=2.0, width_t=20.0)})
    pole = ent(2, EntityType.pole, s=30).model_copy(update={"height_m": 99.0})
    p = {f.type: f for f in plan_of([big, pole]).furniture}
    assert p["tree"].size_m["w"] == MAX_SIZE_M["tree"][0] and p["tree"].size_m["h"] == MAX_SIZE_M["tree"][1]
    assert p["pole"].size_m["h"] == MAX_SIZE_M["pole"][1]


def test_missing_extent_and_height_fall_back_to_catalogue_defaults():
    b = ent(1, EntityType.bin).model_copy(update={"height_m": None, "extent": Extent()})
    f = plan_of([b]).furniture[0]
    assert f.size_m == {"w": 0.6, "h": 1.0, "d": 0.6} and f.length_m == 0.0


def test_railing_and_crossing_keep_their_length():
    r = ent(1, EntityType.railing).model_copy(update={"extent": Extent(length_s=6.0, width_t=0.1)})
    c = ent(2, EntityType.crossing, s=40.0, t=0.0).model_copy(update={"extent": Extent(length_s=3.5, width_t=9.0)})
    p = {f.type: f for f in plan_of([r, c]).furniture}
    assert p["railing"].length_m == 6.0 and p["crossing"].length_m == 3.5


def test_tints_become_colour_or_canopy_colour():
    ents = [ent(1, EntityType.pole), ent(2, EntityType.tree, s=20)]
    tex = TextureRefs(tints={"ent-0000000001": "#112233", "ent-0000000002": "#336633"})
    p = {f.type: f for f in plan_of(ents, textures=tex).furniture}
    assert p["pole"].colour_hex == "#112233" and p["pole"].canopy_hex is None
    assert p["tree"].canopy_hex == "#336633" and p["tree"].colour_hex is None
    none = {f.type: f for f in plan_of(ents).furniture}
    assert none["pole"].colour_hex is None and none["tree"].canopy_hex is None


def test_object_id_links_the_collision_proxy_only_inside_a_lane_band():
    inside, scenery = ent(1, EntityType.pole, t=5.9), ent(2, EntityType.pole, s=30, t=-14.0)
    p = {f.id: f for f in plan_of([inside, scenery]).furniture}
    assert p["ent-0000000001"].object_id == "ent-0000000001"
    assert p["ent-0000000002"].object_id is None
    assert {o.entity_id for o in plan_of([inside, scenery]).objects} == {"ent-0000000001"}


def test_lateral_is_game_lateral_for_a_right_footpath():
    p = plan_of([ent(1, EntityType.pole, t=-5.9)], side="right")
    assert p.furniture[0].lat == 5.9


def test_fidelity_level_and_description_come_from_the_model():
    f = plan_of([ent(1, EntityType.pole, description="A lamp post.")], fid_level=2).furniture[0]
    assert f.fidelity_level == 2 and f.description == "A lamp post."
    f2 = plan_of([ent(1, EntityType.pole, description="")]).furniture[0]
    assert f2.description  # generated by describe.entity_text


def test_low_branch_from_a_low_canopy_tree_is_an_object_not_furniture():
    tree = ent(1, EntityType.tree, t=5.9).model_copy(update={"attributes": {"canopy_low": True}})
    p = plan_of([tree])
    assert [f.type for f in p.furniture] == ["tree"]
    assert {o.type for o in p.objects} == {"tree", "low_branch"}


@pytest.mark.parametrize("t", sorted(FIXED_FURNITURE_TYPES))
def test_each_fixed_type_is_accepted_by_the_plan(t):
    p = plan_of([ent(1, EntityType(t))])
    assert [f.type for f in p.furniture] == [t]
