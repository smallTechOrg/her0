"""Continuous frontage: contiguity, extras, closure of lateral/height steps, caps."""

from __future__ import annotations

import pytest

from hero.export.frame import Frame

import mesh_support as ms

CURVED = str(ms.CURVED)


@pytest.fixture(scope="module")
def mesh():
    return ms.build(CURVED)


def test_nodes_and_materials(mesh):
    for n in ("Frontage_L", "Frontage_R"):
        assert mesh.node(n).material == "mat_frontage" and mesh.node(n).uvs is not None and mesh.node(n).colors is None
    caps = mesh.node("Frontage_Caps")
    assert caps.material == "mat_flat" and caps.colors is not None and caps.extras == {"kind": "frontage_caps"}


def test_segments_extras_match_plan_and_partition_indices(mesh):
    plan = ms.load_plan(CURVED)
    left_is_modelled = plan.footpath_side == "left"
    for node, side in (("Frontage_L", plan.frontage.sides[0 if left_is_modelled else 1]),
                       ("Frontage_R", plan.frontage.sides[1 if left_is_modelled else 0])):
        nd = mesh.node(node)
        rows = nd.extras["segments"]
        assert [(r["s_from"], r["s_to"], r["type"], r["source"]) for r in rows] == \
               [(round(g.s_from, 3), round(g.s_to, 3), g.kind, g.source) for g in side.segments]
        assert rows[0]["s_from"] == 0.0 and rows[-1]["s_to"] == pytest.approx(plan.length_m)
        pos = 0
        for r in rows:  # index ranges partition the node's index buffer, in order
            assert r["index_start"] == pos and r["index_count"] > 0 and r["index_count"] % 3 == 0
            pos += r["index_count"]
        assert pos == len(nd.indices)
        assert list(rows[0]) == ["s_from", "s_to", "type", "source", "building_id", "entity_id", "height_m",
                                 "interpolated", "confidence", "fidelity_level", "index_start", "index_count"]
        assert nd.extras["side"] == ("left" if node == "Frontage_L" else "right")


def test_wall_heights_and_facing_street(mesh):
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    for node, sign in (("Frontage_L", +1), ("Frontage_R", -1)):
        nd = mesh.node(node)
        assert max(p[1] for p in nd.positions) == pytest.approx(max(g.height_m for sd in plan.frontage.sides for g in sd.segments if
                                                                   (sd.side == "modelled") == ((node == "Frontage_L") == (plan.footpath_side == "left"))))
        for k in range(0, len(nd.indices), 3):
            v = nd.positions[nd.indices[k]]
            n = nd.normals[nd.indices[k]]
            s, t = f.inverse(v[0], v[2])
            td = f.t_dir(s)
            along_t = n[0] * td[0] + n[2] * td[1]
            if abs(along_t) > 0.5:  # a wall facing the street (not a perpendicular closing face)
                assert along_t * sign < 0, "frontage must face the street"


def test_lateral_and_height_steps_are_closed(mesh):
    """Fixture has lateral steps (8.2 -> 10.0 at 2.5 m) and height steps: perpendicular faces exist."""
    nd = mesh.node("Frontage_L")
    perp = [i for i in range(0, len(nd.indices), 3)
            if abs(nd.normals[nd.indices[i]][1]) < 0.01 and
            abs(nd.normals[nd.indices[i]][0]) + abs(nd.normals[nd.indices[i]][2]) > 0.99]
    assert len(nd.indices) // 3 > 20 and perp
    caps = mesh.node("Frontage_Caps")
    vertical_returns = [n for n in caps.normals if abs(n[1]) < 0.01]
    assert vertical_returns, "height steps need a return face"
    assert any(n[1] > 0.99 for n in caps.normals), "roof caps point up"


def test_synthetic_streets_all_sides_have_no_see_through():
    for side in ("left", "right"):
        m = ms.build_plan(ms.synthetic_plan(80.0, side))
        plan = ms.synthetic_plan(80.0, side)
        f = Frame.from_plan(plan)
        for node in ("Frontage_L", "Frontage_R"):
            ivals = []
            for tri in ms.tri_st(m, node, f):
                lo = min(p[0] for p in tri)
                hi = max(p[0] for p in tri)
                if hi - lo > 1e-6:
                    ivals.append((lo, hi))
            u = ms.union(ivals, tol=1e-3)
            assert len(u) == 1 and u[0][0] <= 1e-3 and u[0][1] >= 80.0 - 1e-3, (side, node, u)


def test_unbuilt_facade_uses_prior_colour_in_atlas(mesh):
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(mesh.textures["atlas_facade"]))
    assert im.size[1] == 256 and im.size[0] >= 128
    top, bottom = im.crop((0, 0, im.width, 128)), im.crop((0, 128, im.width, 256))
    assert top.tobytes() != bottom.tobytes()
