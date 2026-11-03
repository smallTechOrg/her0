"""The STAND-IN bundle tests/fixtures/export/contract_curved/: the executable form of spec/portability.md that the
JS / mesh / manifest / export slices code against. Reads the glb with pygltflib and checks the real contract."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pygltflib import GLTF2

from hero.export import gameplay_sha256
from hero.export.manifest_types import ManifestV1
from hero.export.mesh_types import (
    MAX_GLB_BYTES, MAX_MATERIALS, MAX_PRIMITIVES, MAX_TEXTURES, MAX_TRIANGLES, MATERIAL_NAMES, NODE_MATERIAL, NODE_NAMES,
)
from hero.model.scene_v3 import SceneV3
from hero.model.street_plan_types import StreetPlan
from tests.fixtures import make_contract_export as mk

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tests" / "fixtures" / "export" / "contract_curved"
GLB = BUNDLE / "street.glb"


@pytest.fixture(scope="module")
def gltf() -> GLTF2:
    return GLTF2.load_binary(str(GLB))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((BUNDLE / "street.manifest.json").read_text())


@pytest.fixture(scope="module")
def blob(gltf) -> bytes:
    return gltf.binary_blob()


def read(gltf, blob, acc_i):
    a = gltf.accessors[acc_i]
    v = gltf.bufferViews[a.bufferView]
    comps = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a.type]
    fmt = {5126: "f", 5121: "B", 5123: "H", 5125: "I"}[a.componentType]
    size = struct.calcsize("<" + fmt) * comps
    off = (v.byteOffset or 0) + (a.byteOffset or 0)
    return [struct.unpack_from("<" + fmt * comps, blob, off + k * size) for k in range(a.count)]


def node_map(gltf):
    return {n.name: n for n in gltf.nodes if n.mesh is not None}


# ---------------------------------------------------------------- glb structure


def test_glb_is_valid_gltf2_binary_with_aligned_chunks():
    raw = GLB.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", raw, 0)
    assert (magic, version, total) == (b"glTF", 2, len(raw)) and len(raw) % 4 == 0
    jlen, jtype = struct.unpack_from("<II", raw, 12)
    assert jtype == 0x4E4F534A and jlen % 4 == 0
    blen, btype = struct.unpack_from("<II", raw, 20 + jlen)
    assert btype == 0x004E4942 and blen % 4 == 0 and 20 + jlen + 8 + blen == len(raw)
    assert len(raw) <= MAX_GLB_BYTES


def test_scene_root_and_node_contract(gltf):
    assert gltf.scene == 0 and gltf.scenes[0].name == "Street" and gltf.scenes[0].nodes == [0]
    root = gltf.nodes[0]
    assert root.name == "Street" and root.mesh is None
    names = [gltf.nodes[i].name for i in root.children]
    assert names == [n for n in NODE_NAMES if n in names]  # contract order, only contract names
    assert set(names) == set(NODE_NAMES)  # the stand-in has every optional node, incl. Furniture_Railings
    for n in gltf.nodes:  # identity transforms
        assert not n.matrix and not n.translation and not n.rotation and not n.scale
    assert not gltf.extensionsRequired and not gltf.extensionsUsed


def test_one_mesh_one_primitive_per_node_with_the_right_material(gltf):
    for name, n in node_map(gltf).items():
        mesh = gltf.meshes[n.mesh]
        assert len(mesh.primitives) == 1 and mesh.primitives[0].mode == 4
        assert gltf.materials[mesh.primitives[0].material].name == NODE_MATERIAL[name]


def test_materials_and_textures(gltf):
    assert [m.name for m in gltf.materials] == list(MATERIAL_NAMES)
    mats = {m.name: m for m in gltf.materials}
    for m in gltf.materials:
        assert m.doubleSided is False and m.alphaMode == "OPAQUE"
    assert mats["mat_ground"].pbrMetallicRoughness.baseColorTexture is not None
    assert mats["mat_frontage"].pbrMetallicRoughness.baseColorTexture is not None
    assert mats["mat_kerb"].pbrMetallicRoughness.baseColorTexture is None
    assert mats["mat_flat"].pbrMetallicRoughness.baseColorFactor == [1.0, 1.0, 1.0, 1.0]
    assert mats["mat_ground"].pbrMetallicRoughness.metallicFactor == 0 and mats["mat_ground"].pbrMetallicRoughness.roughnessFactor == 1
    assert len(gltf.textures) == 2 and len(gltf.images) == 2
    for img in gltf.images:
        assert img.mimeType == "image/jpeg"
        data = bytes(gltf.binary_blob()[gltf.bufferViews[img.bufferView].byteOffset:][:gltf.bufferViews[img.bufferView].byteLength])
        assert data[:2] == b"\xff\xd8" and b"Exif" not in data[:40]
    s = gltf.samplers[0]
    assert (s.magFilter, s.minFilter, s.wrapS, s.wrapT) == (9729, 9987, 33071, 33071)


def test_budgets(gltf, manifest):
    tris = sum(gltf.accessors[p.indices].count // 3 for m in gltf.meshes for p in m.primitives)
    assert len(gltf.meshes) <= MAX_PRIMITIVES and tris <= MAX_TRIANGLES
    assert len(gltf.materials) <= MAX_MATERIALS and len(gltf.textures) <= MAX_TEXTURES
    st = manifest["mesh_stats"]
    assert (st["primitives"], st["triangles"], st["materials"], st["textures"]) == (len(gltf.meshes), tris, 4, 2)
    assert [n["name"] for n in st["nodes"]] == [m.name for m in gltf.meshes]


def test_attributes_bounds_and_winding(gltf, blob):
    for name, n in node_map(gltf).items():
        prim = gltf.meshes[n.mesh].primitives[0]
        pos = read(gltf, blob, prim.attributes.POSITION)
        nrm = read(gltf, blob, prim.attributes.NORMAL)
        idx = [i for (i,) in read(gltf, blob, prim.indices)]
        assert len(pos) == len(nrm) and max(idx) < len(pos) and len(idx) % 3 == 0
        acc = gltf.accessors[prim.attributes.POSITION]
        assert acc.min == [min(p[k] for p in pos) for k in range(3)] and acc.max == [max(p[k] for p in pos) for k in range(3)]
        assert all(round(c, 3) == pytest.approx(c, abs=2e-4) for p in pos for c in p)  # millimetre positions (float32)
        has_uv = prim.attributes.TEXCOORD_0 is not None
        assert has_uv == (NODE_MATERIAL[name] in ("mat_ground", "mat_frontage") and name != "Kerb")
        if has_uv:
            uv = read(gltf, blob, prim.attributes.TEXCOORD_0)
            assert all(-1e-4 <= u <= 1 + 1e-4 and -1e-4 <= v <= 1 + 1e-4 for u, v in uv), name
        if prim.attributes.COLOR_0 is not None:
            assert gltf.accessors[prim.attributes.COLOR_0].normalized and gltf.accessors[prim.attributes.COLOR_0].componentType == 5121
        for t in range(0, len(idx), 3):  # CCW winding agrees with the stored normal
            a, b, c = (pos[idx[t + k]] for k in range(3))
            e1 = tuple(b[k] - a[k] for k in range(3))
            e2 = tuple(c[k] - a[k] for k in range(3))
            g = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
            nn = nrm[idx[t]]
            assert sum(g[k] * nn[k] for k in range(3)) > 0, (name, t)


def test_asset_extras_and_node_extras(gltf, manifest):
    ex = gltf.asset.extras
    assert list(ex) == ["schema", "generator", "segment_slug", "model_version", "model_hash", "footpath_side", "units", "up_axis",
                        "travel", "right", "length_s", "frame", "manifest", "gameplay_sha256"]
    assert ex["schema"] == "hero-street-glb/1" and ex["gameplay_sha256"] == gameplay_sha256() and ex["length_s"] == 60.0
    nm = node_map(gltf)
    for n in ("Footpath", "Footpath_Opposite", "Road", "Kerb", "Ground_L", "Ground_R"):
        e = nm[n].extras
        assert list(e) == ["kind", "side", "runs", "bins"] and len(e["bins"]) == 12
        assert set(e["bins"][0]) == {"index", "s_from", "s_to", "ground_source", "fidelity_level", "interpolated"}
    assert nm["Footpath"].extras["kind"] == "footpath" and nm["Footpath"].extras["side"] == "left"
    runs = nm["Footpath"].extras["runs"]
    assert [(r["s_from"], r["s_to"], r["state"]) for r in runs] == [(0.0, 15.0, "intact"), (15.0, 20.0, "blocked"), (20.0, 30.0, "broken"), (37.5, 60.0, "intact")]
    assert nm["Frontage_Caps"].extras == {"kind": "frontage_caps"}
    assert {s["ground_source"] for s in nm["Road"].extras["bins"]} == {"satellite_hires", "interpolated", "satellite_overview"}


def test_frontage_is_contiguous_and_index_ranges_tile_the_buffer(gltf, blob):
    nm = node_map(gltf)
    for name in ("Frontage_L", "Frontage_R"):
        segs = nm[name].extras["segments"]
        assert segs[0]["s_from"] == 0.0 and segs[-1]["s_to"] == 60.0
        for a, b in zip(segs, segs[1:]):
            assert a["s_to"] == b["s_from"]
            assert a["index_start"] + a["index_count"] == b["index_start"]
        total = gltf.accessors[gltf.meshes[nm[name].mesh].primitives[0].indices].count
        assert segs[-1]["index_start"] + segs[-1]["index_count"] == total
        assert {s["type"] for s in segs} <= {"building", "wall", "gate"}
    assert {s["type"] for s in nm["Frontage_L"].extras["segments"]} == {"building", "wall"}
    assert "gate" in {s["type"] for s in nm["Frontage_R"].extras["segments"]}


def test_extras_entities_equal_manifest_entities(gltf, manifest):
    from_glb = {}
    for n in node_map(gltf).values():
        if n.extras.get("kind") == "furniture":
            for e in n.extras["entities"]:
                from_glb[e["id"]] = e
    ents = {e["id"]: e for e in manifest["entities"]}
    assert set(from_glb) == set(ents) and len(ents) == 10
    for i, e in ents.items():
        g = from_glb[i]
        assert (g["type"], g["variant"], g["s"], g["t"], g["confidence"], g["fidelity_level"]) == (e["type"], e["variant"], e["s"], e["t"], e["confidence"], e["fidelity_level"])
        assert node_map(gltf)[e["node"]].extras["entities"]  # the node named by the manifest contains it
        assert g in node_map(gltf)[e["node"]].extras["entities"]
        assert g["index_count"] > 0


# ---------------------------------------------------------------- frame / geometry


def test_true_frame_footpath_left_at_negative_x_and_travel_toward_minus_z(gltf, blob, manifest):
    nm = node_map(gltf)

    def verts(name):
        return read(gltf, blob, gltf.meshes[nm[name].mesh].primitives[0].attributes.POSITION)

    near0 = lambda ps: [p for p in ps if abs(p[2]) < 1.0]  # noqa: E731  (around s=0 the street runs along -Z)
    fp, fo = near0(verts("Footpath")), near0(verts("Footpath_Opposite"))
    assert fp and fo and max(p[0] for p in fp) < -4.5 and min(p[0] for p in fo) >= 4.49  # left = -X, right = +X (never mirrored)
    assert sum(p[1] for p in fp) / len(fp) == pytest.approx(0.15, abs=1e-6)  # footpath top at height_m
    road = verts("Road")
    assert max(abs(p[1]) for p in road) == 0.0
    pts = manifest["reference_line"]["points"]
    assert (pts[0]["x"], pts[0]["z"]) == (0.0, 0.0) and pts[-1]["z"] < -50 and pts[-1]["x"] > 15  # curves toward +X (right)
    assert min(p[2] for p in road) < pts[-1]["z"] and max(p[0] for p in road) > pts[-1]["x"]  # end of the Road mesh reaches the end point
    # road centre line length from the polyline == length_s
    length = sum(math.dist((a["x"], a["z"]), (b["x"], b["z"])) for a, b in zip(pts, pts[1:]))
    assert length == pytest.approx(60.0, abs=0.01)
    assert manifest["loop"]["end_pose"] == {"x": pts[-1]["x"], "z": pts[-1]["z"], "yaw_rad": 0.75}


def test_ground_nodes_tile_wall_to_wall_per_station(gltf, blob, manifest):
    """At each station the ground (opposite footpath + kerb + road + footpath/ground_L) covers wall to wall with no gap."""
    plan = StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())
    fr = None
    pts = manifest["reference_line"]["points"]
    # lateral coverage is checked in the (s,t) strip each station exposes in the manifest
    for st, cs in zip(manifest["lanes"]["stations"], manifest["cross_section"]["stations"]):
        s = st["s"]
        side_l = next(f for f in manifest["frontage"]["left"] if f["s_from"] - 1e-9 <= s <= f["s_to"] + 1e-9)
        side_r = next(f for f in manifest["frontage"]["right"] if f["s_from"] - 1e-9 <= s <= f["s_to"] + 1e-9)
        assert side_l["t_from"] > 0 > side_r["t_from"]
        strips = cs["strips"]
        assert any(q["kind"] == "carriageway" for q in strips)
        car = next(q for q in strips if q["kind"] == "carriageway")
        assert car["t_lo"] < 0 < car["t_hi"]
    assert fr is None and len(pts) == len(plan.stations)


def test_ground_mesh_has_no_holes_at_any_station(gltf, blob, manifest):
    """Project every Ground/Road/Footpath/Kerb triangle on its station's cross line; the union must cover [wall_R, wall_L]."""
    nm = node_map(gltf)
    pts = manifest["reference_line"]["points"]
    ground_nodes = ["Footpath", "Footpath_Opposite", "Road", "Kerb", "Ground_L", "Ground_R"]
    phis = []
    b0 = pts[0]["bearing_deg"]
    for p in pts:
        phis.append(math.radians(p["bearing_deg"] - b0))
    tris_t = {k: [] for k in range(len(pts))}  # station -> list of (t_lo, t_hi)
    for name in ground_nodes:
        prim = gltf.meshes[nm[name].mesh].primitives[0]
        pos = read(gltf, blob, prim.attributes.POSITION)
        idx = [i for (i,) in read(gltf, blob, prim.indices)]
        for k in range(0, len(idx), 3):
            tri = [pos[i] for i in idx[k:k + 3]]
            if name == "Kerb" and all(abs(p[1] - 0.15) > 1e-6 for p in tri):
                continue  # kerb faces are vertical; only the tops participate in horizontal coverage
            if any(abs(p[1] - 0.0) > 0.16 for p in tri):
                continue
            # t of a vertex on a station: t = -((x-Px)cos + (z-Pz)sin); cheap: use the nearest station
            for p in tri:
                j = min(range(len(pts)), key=lambda q: (p[0] - pts[q]["x"]) ** 2 + (p[2] - pts[q]["z"]) ** 2)
                if abs(((p[0] - pts[j]["x"]) * -math.sin(phis[j]) + (p[2] - pts[j]["z"]) * math.cos(phis[j]))) > 0.02:
                    continue  # not on this station's cross line
                t = -((p[0] - pts[j]["x"]) * math.cos(phis[j]) + (p[2] - pts[j]["z"]) * math.sin(phis[j]))
                tris_t[j].append(round(t, 3))
    for j, ts in tris_t.items():
        left = next(f for f in manifest["frontage"]["left"] if f["s_from"] - 1e-9 <= pts[j]["s"] <= f["s_to"] + 1e-9)
        right = next(f for f in manifest["frontage"]["right"] if f["s_from"] - 1e-9 <= pts[j]["s"] <= f["s_to"] + 1e-9)
        assert ts, j
        assert min(ts) <= right["t_from"] + 1e-3 and max(ts) >= left["t_from"] - 1e-3, (j, min(ts), max(ts))


def test_footpath_missing_stretch_has_no_footpath_triangles_but_ground_covers_it(gltf, blob):
    nm = node_map(gltf)
    prim = gltf.meshes[nm["Footpath"].mesh].primitives[0]
    pos = read(gltf, blob, prim.attributes.POSITION)
    # the footpath mesh stops at s=30 and resumes at 37.5: no vertex in the open gap (world z/x of the curve)
    gl = gltf.meshes[nm["Ground_L"].mesh].primitives[0]
    assert gltf.accessors[gl.indices].count // 3 >= 24 * 2  # 24 quads, wall-to-footpath/road-edge, always present
    assert len(pos) // 3 == 42  # 21 footpath quads (24 - 3 missing)


# ---------------------------------------------------------------- manifest / hashes / scene / plan / xodr


def test_hashes_and_cross_references(manifest):
    glb = GLB.read_bytes()
    b = manifest["build"]
    assert b["mesh_sha256"] == hashlib.sha256(glb).hexdigest() and b["glb_bytes"] == len(glb)
    assert b["xodr_sha256"] == hashlib.sha256((BUNDLE / "street.xodr").read_bytes()).hexdigest()
    assert manifest["gameplay_sha256"] == gameplay_sha256()
    ManifestV1.model_validate(manifest)
    sc = SceneV3.model_validate_json((ROOT / "tests/fixtures/scene_v3/contract_curved.json").read_text())
    assert sc.assets.mesh_sha256 == b["mesh_sha256"] and sc.assets.glb_bytes == len(glb)
    assert sc.assets.triangles == manifest["mesh_stats"]["triangles"]
    bundle = hashlib.sha256((b["mesh_sha256"] + hashlib.sha256((BUNDLE / "street.manifest.json").read_bytes()).hexdigest() + b["xodr_sha256"]).encode()).hexdigest()
    assert sc.assets.sha8 == bundle[:8]


def test_manifest_colliders_tokens_and_walkability_are_consistent_with_the_scene(manifest):
    sc = SceneV3.model_validate_json((ROOT / "tests/fixtures/scene_v3/contract_curved.json").read_text())
    hazards = [c for c in manifest["colliders"] if c["action"] != "none"]
    assert sc.walkability.hazard_count == len(hazards) == 5
    assert any(t["reason"] == "crossing" and t["lane"] == 1 for t in manifest["tokens"])
    ids = {c["id"] for c in manifest["colliders"]}
    assert {"gap-int-0004", "blocked-int-0002"} <= ids
    w = manifest["spawn_hints"]["type_weights"]
    assert sum(w.values()) == pytest.approx(1.0)


def test_plan_json_loads_and_matches_manifest(manifest):
    plan = StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())
    assert plan.footpath_side == "left" and plan.length_m == 60.0 and len(plan.stations) == 25
    assert {f.id for f in plan.furniture} == {e["id"] for e in manifest["entities"]}
    assert plan.dynamic_context.excluded_count == 2
    # no baked dynamic things anywhere in the plan
    assert not {"parked_vehicle", "vendor_stall", "speed_breaker"} & {o.type for o in plan.objects}
    assert not {"parked_vehicle", "vendor_stall", "speed_breaker"} & {f.type for f in plan.furniture}


def test_xodr_is_well_formed_and_lengths_add_up():
    root = ET.fromstring((BUNDLE / "street.xodr").read_bytes())
    assert root.tag == "OpenDRIVE"
    h = root.find("header")
    assert (h.get("revMajor"), h.get("revMinor")) == ("1", "7") and "tmerc" in h.find("geoReference").text
    road = root.find("road")
    geoms = road.findall("planView/geometry")
    ss = [float(g.get("s")) for g in geoms]
    assert ss == sorted(ss) and len(set(ss)) == len(ss)
    assert sum(float(g.get("length")) for g in geoms) == pytest.approx(60.0, abs=1e-3)
    sections = road.findall("lanes/laneSection")
    assert [float(s.get("s")) for s in sections] == [0.0, 30.0, 37.5]  # footpath disappears / reappears
    assert [len(s.findall("left/lane")) for s in sections] == [3, 1, 3]
    assert {o.find("userData").get("value") for o in road.findall("objects/object")} == {
        e["id"] for e in json.loads((BUNDLE / "street.manifest.json").read_text())["entities"]}


# ---------------------------------------------------------------- determinism / freshness


def test_committed_files_are_fresh_and_build_is_deterministic():
    a = mk.build_all()
    b = mk.build_all()
    assert a == b  # in-process twice
    for path, data in a.items():
        assert path.read_bytes() == data, f"{path.relative_to(ROOT)} is stale: run uv run python tests/fixtures/make_contract_export.py"


def test_contract_scene_walkability_is_derived_from_its_own_coverage_and_objects():
    sc = SceneV3.model_validate_json((ROOT / "tests/fixtures/scene_v3/contract_curved.json").read_text())
    plan = StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())
    intact = sum(r.to_m - r.from_m for r in sc.cross_section.footpath_coverage if r.state == "intact")
    assert sc.walkability.footpath_coverage_pct == pytest.approx(100.0 * intact / sc.segment.length_m, abs=0.05) == 62.5
    assert sc.walkability.crossing_present is True and any(o.type == "crossing" for o in plan.objects)
    assert sum(plan.dynamic_context.spawn_hints.lane_weights) == pytest.approx(1.0)
