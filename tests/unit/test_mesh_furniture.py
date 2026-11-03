"""Fixed furniture: merged per category, thin poles, tree = trunk + blobs, capped sizes, decals 10 mm above surface."""

from __future__ import annotations

import pytest

from hero.export.frame import Frame
from hero.export.mesh_furniture import node_of
from hero.model.street_plan_types import FurniturePlan, MAX_SIZE_M

import mesh_support as ms

CURVED = str(ms.CURVED)


@pytest.fixture(scope="module")
def mesh():
    return ms.build(CURVED)


def _ent_bbox(nd, e):
    idx = nd.indices[e["index_start"]:e["index_start"] + e["index_count"]]
    pts = [nd.positions[i] for i in idx]
    return [(min(p[k] for p in pts), max(p[k] for p in pts)) for k in range(3)]


def test_categories_and_entity_extras_equal_plan(mesh):
    plan = ms.load_plan(CURVED)
    seen = {}
    for name in ("Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals"):
        nd = mesh.node(name)
        assert nd is not None and nd.material == "mat_flat" and nd.uvs is None and nd.colors is not None
        for e in nd.extras["entities"]:
            seen[e["id"]] = (name, e)
            assert node_of(e["type"]) == name
    assert sorted(seen) == sorted(f.id for f in plan.furniture)
    for f in plan.furniture:
        _, e = seen[f.id]
        t = f.lat  # left footpath: t == lat
        assert (e["s"], e["t"], e["type"], e["variant"], e["yaw_deg"]) == (round(f.s, 3), round(t, 3), f.type, f.variant, f.yaw_deg)
        assert e["confidence"] == f.confidence and e["fidelity_level"] == f.fidelity_level
        assert list(e) == ["id", "type", "variant", "s", "t", "yaw_deg", "confidence", "fidelity_level", "index_start", "index_count"]


def test_entity_index_ranges_partition_each_node(mesh):
    for nd in mesh.nodes:
        if nd.extras.get("kind") != "furniture":
            continue
        pos = 0
        for e in nd.extras["entities"]:
            assert e["index_start"] == pos and e["index_count"] > 0
            pos += e["index_count"]
        assert pos == len(nd.indices)


def test_poles_are_thin_and_trees_are_trunk_plus_canopy(mesh):
    poles = mesh.node("Furniture_Poles")
    for e in poles.extras["entities"]:
        bb = _ent_bbox(poles, e)
        assert bb[1][1] - bb[1][0] == pytest.approx(7.0, abs=0.01) or bb[1][1] - bb[1][0] <= 9.0
        if e["variant"] not in ("utility", "streetlight"):  # those add a cross-arm
            assert max(bb[0][1] - bb[0][0], bb[2][1] - bb[2][0]) <= 0.5 + 1e-6  # tapered cylinder, never a billboard
    trees = mesh.node("Furniture_Trees")
    e = trees.extras["entities"][0]
    bb = _ent_bbox(trees, e)
    assert bb[1][1] - bb[1][0] > 3.0 and bb[0][1] - bb[0][0] <= MAX_SIZE_M["tree"][0] + 1e-6
    cols = {trees.colors[i] for i in trees.indices[e["index_start"]:e["index_start"] + e["index_count"]]}
    assert len(cols) >= 3  # trunk + two canopy tints


def test_sizes_are_capped_even_for_absurd_entities():
    plan = ms.synthetic_plan(40.0)
    d = plan.model_dump()
    d["furniture"] = [
        {**d["furniture"][0], "id": "ent-a", "type": "tree", "s": 10.0, "size_m": {"w": 60.0, "h": 90.0, "d": 60.0}},
        {**d["furniture"][0], "id": "ent-b", "type": "pole", "variant": "", "s": 20.0, "size_m": {"w": 9.0, "h": 99.0, "d": 9.0}},
        {**d["furniture"][0], "id": "ent-c", "type": "bin", "s": 25.0, "size_m": {"w": 9.0, "h": 9.0, "d": 9.0}},
        {**d["furniture"][0], "id": "ent-d", "type": "signboard", "s": 30.0, "size_m": {"w": 9.0, "h": 9.0, "d": 9.0}},
    ]
    from hero.model.street_plan_types import StreetPlan
    m = ms.build_plan(StreetPlan.model_validate(d))
    for node, eid, typ in (("Furniture_Trees", "ent-a", "tree"), ("Furniture_Poles", "ent-b", "pole"),
                           ("Furniture_Boxes", "ent-c", "bin"), ("Furniture_Boxes", "ent-d", "signboard")):
        nd = m.node(node)
        e = next(x for x in nd.extras["entities"] if x["id"] == eid)
        bb = _ent_bbox(nd, e)
        cw, ch = MAX_SIZE_M[typ]
        assert bb[1][1] - bb[1][0] <= ch + 1.8 + 1e-6  # signboards hang 1.7 m above the surface
        assert max(bb[0][1] - bb[0][0], bb[2][1] - bb[2][0]) <= cw * 1.5 + 1e-6  # yaw-free axis boxes <= cap * sqrt2


def test_decals_sit_10mm_above_surface(mesh):
    dec = mesh.node("Furniture_Decals")
    ents = {e["id"]: e for e in dec.extras["entities"]}
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    for fp in plan.furniture:
        if fp.id not in ents:
            continue
        bb = _ent_bbox(dec, ents[fp.id])
        assert bb[1][0] == bb[1][1]  # flat
        assert bb[1][0] in (0.01, 0.16)  # carriageway / forecourt or footpath surface + 10 mm
    assert dec.extras["category"] == "decals"
    assert all(n[1] > 0.99 for n in dec.normals)
    del f


def test_no_dynamic_types_can_be_furniture():
    for bad in ("parked_vehicle", "pedestrian", "stall", "vehicle", "debris"):
        with pytest.raises(ValueError):
            FurniturePlan(id="x", type=bad, s=1.0, lat=1.0, size_m={"w": 1, "h": 1, "d": 1}, confidence=0.5)


def test_no_furniture_node_when_none():
    d = ms.synthetic_plan(40.0).model_dump()
    d["furniture"] = []
    from hero.model.street_plan_types import StreetPlan
    m = ms.build_plan(StreetPlan.model_validate(d))
    assert not [n for n in m.nodes if n.name.startswith("Furniture_")]
