"""Ground strips: footpath / opposite footpath / road / kerb / ground L+R (spec/portability.md section 3)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from hero.export.frame import Frame
from hero.export.mesh_types import GROUND_NODES

import mesh_support as ms

CURVED = str(ms.CURVED)


@pytest.fixture(scope="module")
def mesh():
    return ms.build(CURVED)


def test_ground_nodes_present_and_materials(mesh):
    names = [n.name for n in mesh.nodes]
    for g in GROUND_NODES:
        assert g in names, g
    assert mesh.node("Kerb").material == "mat_kerb" and mesh.node("Kerb").uvs is None
    assert mesh.node("Road").material == "mat_ground" and mesh.node("Road").colors is not None


def test_top_faces_point_up_and_heights(mesh):
    for name, y in (("Road", 0.0), ("Footpath", 0.15), ("Ground_L", 0.0), ("Ground_R", 0.0)):
        nd = mesh.node(name)
        tops = [p for p, n in zip(nd.positions, nd.normals) if n[1] > 0.99]
        assert tops, name
        assert {p[1] for p in tops} == {y}, name
    for nd in mesh.nodes:
        if nd.name in ("Road", "Ground_L", "Ground_R"):
            assert all(n[1] > 0.99 for n in nd.normals)  # flat, CCW up


def test_kerb_has_height_step_with_faces_and_tops(mesh):
    k = mesh.node("Kerb")
    ys = sorted({p[1] for p in k.positions})
    assert ys == [0.0, 0.15]
    vertical = [n for n in k.normals if abs(n[1]) < 0.01]
    tops = [n for n in k.normals if n[1] > 0.99]
    assert vertical and tops


def test_kerb_face_faces_the_road_modelled_side(mesh):
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    k = mesh.node("Kerb")
    for tri in range(0, len(k.indices), 3):
        vs = [k.positions[k.indices[tri + j]] for j in range(3)]
        n = k.normals[k.indices[tri]]
        if abs(n[1]) > 0.01:
            continue
        s, t = f.inverse(vs[0][0], vs[0][2])
        if abs(t - 4.7) < 1e-3:  # left footpath: t == lat; face toward the road = toward smaller t
            td = f.t_dir(s)
            assert n[0] * td[0] + n[2] * td[1] < -0.9


def test_footpath_missing_run_has_no_footpath_and_earth_ground(mesh):
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    miss = [st.s for st in plan.stations if st.footpath is None]
    assert miss, "fixture must contain a missing run"
    lo, hi = min(miss), max(miss) + 2.5
    for tri in ms.tri_st(mesh, "Footpath", f):
        smid = sum(p[0] for p in tri) / 3
        assert not (lo + 1e-6 < smid < hi - 1e-6)
    gl = mesh.node("Ground_L")  # footpath on the left: earth colour where missing
    sts = [f.inverse(p[0], p[2])[0] for p in gl.positions]
    earth = {gl.colors[i] for i, s in enumerate(sts) if lo + 0.1 < s < hi - 0.1}
    other = {gl.colors[i] for i, s in enumerate(sts) if s < lo - 0.1}
    assert earth and earth != other


def test_state_tint_on_footpath(mesh):
    cols = {c for c in mesh.node("Footpath").colors}
    assert len(cols) >= 1 and all(c[3] == 255 for c in cols)


def test_uv_rule_u_v_from_s_t(mesh):
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    L = plan.length_m
    for name in ("Road", "Footpath", "Ground_L", "Ground_R", "Footpath_Opposite"):
        nd = mesh.node(name)
        for p, uv in list(zip(nd.positions, nd.uvs))[::5]:
            s, t = f.inverse(p[0], p[2])
            if abs(t) <= 14.0:
                assert uv[0] == pytest.approx((14.0 - t) / 28.0, abs=2e-4)
            assert uv[1] == pytest.approx((L - s) / L, abs=2e-4)
            assert 0.0 <= uv[0] <= 1.0 and 0.0 <= uv[1] <= 1.0


def test_footpath_side_right_puts_footpath_on_positive_x():
    plan = ms.synthetic_plan(40.0, side="right")
    m = ms.build_plan(plan)
    assert sum(p[0] for p in m.node("Footpath").positions) / len(m.node("Footpath").positions) > 3.0
    assert sum(p[0] for p in m.node("Footpath_Opposite").positions) / len(m.node("Footpath_Opposite").positions) < -3.0
    assert m.node("Footpath").extras["side"] == "right" and m.node("Footpath_Opposite").extras["side"] == "left"
    for n in ("Road", "Ground_L", "Ground_R"):
        assert all(nm[1] > 0.99 for nm in m.node(n).normals)


def test_extras_runs_and_bins(mesh):
    plan = ms.load_plan(CURVED)
    fp = mesh.node("Footpath").extras
    assert fp["kind"] == "footpath" and fp["side"] == "left"
    assert [r["state"] for r in fp["runs"]]
    for a, b in zip(fp["runs"], fp["runs"][1:]):
        assert a["s_to"] <= b["s_from"] + 1e-9
    assert fp["runs"][0]["height_m"] == 0.15
    bins = fp["bins"]
    assert len(bins) == len(plan.ground_tex.bin_source)
    assert [b["ground_source"] for b in bins] == list(plan.ground_tex.bin_source)
    assert any(b["interpolated"] for b in bins)
    assert list(bins[0]) == ["index", "s_from", "s_to", "ground_source", "fidelity_level", "interpolated"]
    assert mesh.node("Road").extras["runs"][0]["s_to"] == pytest.approx(plan.length_m, abs=1e-3)


def test_seam_blend_pulls_end_edges_to_station_zero(mesh):
    plan = ms.load_plan(CURVED)
    f = Frame.from_plan(plan)
    road = mesh.node("Road")
    ends = [f.inverse(p[0], p[2]) for p in road.positions]
    t_end = sorted(round(t, 2) for s, t in ends if abs(s - plan.length_m) < 2e-3)
    t_start = sorted(round(t, 2) for s, t in ends if abs(s) < 2e-3)
    assert sorted(set(t_end)) == sorted(set(t_start)) == [-4.3, 4.7]
    # and mid-blend (57.5 m) the edges lie between the plan value and the station-0 value
    mid = sorted(round(t, 2) for s, t in ends if abs(s - 57.5) < 2e-3)
    assert mid and all(-4.45 < t < 4.8 for t in mid)


def test_atlas_uses_baked_strip_when_present_and_falls_back_when_not(tmp_path, capsys):
    plan = ms.load_plan(CURVED)
    mesh_missing = ms.build(CURVED, str(tmp_path))
    im = Image.open(io.BytesIO(mesh_missing.textures["atlas_ground"]))
    assert im.size[0] == 112 and im.size[1] == round(plan.length_m * 4)  # procedural
    d = tmp_path / "textures" / plan.slug
    d.mkdir(parents=True)
    Image.new("RGB", (56, 120), (10, 200, 30)).save(d / Path(plan.ground_tex.strip.url).name, "JPEG")
    m2 = ms.build_plan(plan, tmp_path)
    im2 = Image.open(io.BytesIO(m2.textures["atlas_ground"]))
    assert im2.size == (56, 120) and im2.getpixel((5, 5))[1] > 150
    # a corrupt file never raises: procedural again
    (d / Path(plan.ground_tex.strip.url).name).write_bytes(b"not a jpeg")
    m3 = ms.build_plan(plan, tmp_path)
    assert Image.open(io.BytesIO(m3.textures["atlas_ground"])).size == (112, 240)
    assert "export.baked_image_unreadable" in capsys.readouterr().out, "the fallback is logged as a warning"


def test_procedural_atlas_is_never_flat(mesh):
    im = Image.open(io.BytesIO(mesh.textures["atlas_ground"])).convert("RGB")
    assert len({im.tobytes()[i:i + 3] for i in range(0, len(im.tobytes()), 3)}) > 50
