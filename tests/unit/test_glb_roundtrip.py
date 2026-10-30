"""write_glb -> pygltflib re-load: the loaded document must carry exactly what the StreetMesh held."""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_contract_export as mce  # noqa: E402

from hero.export.glb import read_glb, write_glb  # noqa: E402
from hero.export.mesh_types import KERB_COLOUR, NODE_MATERIAL, NODE_NAMES, StreetMesh  # noqa: E402

CT = {5126: "f", 5121: "B", 5123: "H", 5125: "I"}
NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


@pytest.fixture(scope="module")
def mesh() -> StreetMesh:
    plan, fr = mce.build_plan(json.loads(mce.SRC_SCENE.read_text()))
    return mce.build_mesh(plan, fr, mce.load_gameplay())[0]


@pytest.fixture(scope="module")
def gltf(mesh):
    return read_glb(write_glb(mesh))


def accessor_rows(g, idx):
    a = g.accessors[idx]
    v = g.bufferViews[a.bufferView]
    blob = g.binary_blob()
    n = NC[a.type]
    fmt = "<" + CT[a.componentType] * n
    size = struct.calcsize(fmt)
    base = (v.byteOffset or 0) + (a.byteOffset or 0)
    return [struct.unpack_from(fmt, blob, base + i * size) for i in range(a.count)]


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def test_structure(gltf, mesh):
    assert gltf.asset.version == "2.0"
    assert len(gltf.scenes) == 1 and gltf.scenes[0].name == "Street" and gltf.scene == 0
    root = gltf.nodes[gltf.scenes[0].nodes[0]]
    assert root.name == "Street" and root.mesh is None
    kids = [gltf.nodes[i] for i in root.children]
    assert [k.name for k in kids] == [n.name for n in mesh.nodes]
    assert [k.name for k in kids] == [n for n in NODE_NAMES if n in {m.name for m in mesh.nodes}]
    for k in kids:
        assert k.matrix is None and k.translation is None and k.rotation is None and k.scale is None
        assert len(gltf.meshes[k.mesh].primitives) == 1
    assert gltf.extensionsRequired == [] and gltf.extensionsUsed == []


def test_geometry_roundtrip(gltf, mesh):
    for nd in mesh.nodes:
        kid = next(k for k in gltf.nodes if k.name == nd.name)
        p = gltf.meshes[kid.mesh].primitives[0]
        pos = accessor_rows(gltf, p.attributes.POSITION)
        assert pos == [tuple(f32(c) for c in q) for q in nd.positions]
        nrm = accessor_rows(gltf, p.attributes.NORMAL)
        assert all(abs(a - f32(b)) == 0 for r, s in zip(nrm, nd.normals) for a, b in zip(r, s))
        assert [r[0] for r in accessor_rows(gltf, p.indices)] == nd.indices
        if nd.uvs is None:
            assert p.attributes.TEXCOORD_0 is None
        else:
            assert accessor_rows(gltf, p.attributes.TEXCOORD_0) == [tuple(f32(c) for c in q) for q in nd.uvs]
        if nd.colors is None:
            assert p.attributes.COLOR_0 is None
        else:
            assert accessor_rows(gltf, p.attributes.COLOR_0) == [tuple(c) for c in nd.colors]
            assert gltf.accessors[p.attributes.COLOR_0].normalized is True
        # accessor min/max equal the data
        a = gltf.accessors[p.attributes.POSITION]
        assert a.min == [min(r[k] for r in pos) for k in range(3)] and a.max == [max(r[k] for r in pos) for k in range(3)]


def test_extras_roundtrip(gltf, mesh):
    assert json.dumps(gltf.asset.extras, sort_keys=False) == json.dumps(mesh.asset_extras, sort_keys=False)
    for nd in mesh.nodes:
        kid = next(k for k in gltf.nodes if k.name == nd.name)
        assert json.dumps(kid.extras) == json.dumps(nd.extras)


def test_materials_and_textures_roundtrip(gltf, mesh):
    by_name = {m.name: m for m in gltf.materials}
    assert set(by_name) == {m.name for m in mesh.materials}
    for nd in mesh.nodes:
        kid = next(k for k in gltf.nodes if k.name == nd.name)
        prim = gltf.meshes[kid.mesh].primitives[0]
        assert gltf.materials[prim.material].name == NODE_MATERIAL[nd.name]
    kerb = by_name["mat_kerb"].pbrMetallicRoughness
    assert kerb.baseColorTexture is None
    expect = ((int(KERB_COLOUR[1:3], 16) / 255 + 0.055) / 1.055) ** 2.4
    assert all(c == pytest.approx(expect, abs=1e-6) for c in kerb.baseColorFactor[:3])
    assert by_name["mat_ground"].pbrMetallicRoughness.baseColorTexture is not None
    assert by_name["mat_frontage"].pbrMetallicRoughness.baseColorTexture is not None
    blob = gltf.binary_blob()
    for im in gltf.images:
        v = gltf.bufferViews[im.bufferView]
        data = blob[v.byteOffset:v.byteOffset + v.byteLength]
        assert data == mesh.textures[im.name] and im.mimeType == "image/jpeg"


def test_metres_y_up_and_length(gltf, mesh):
    """Road lies at y=0, and its horizontal extent is the street (metres, not mm/cm)."""
    road = next(k for k in gltf.nodes if k.name == "Road")
    a = gltf.accessors[gltf.meshes[road.mesh].primitives[0].attributes.POSITION]
    assert a.min[1] == a.max[1] == 0.0
    diag = math.hypot(a.max[0] - a.min[0], a.max[2] - a.min[2])
    assert 0.8 * mesh.length_s <= diag <= mesh.length_s + 30
    for k in gltf.nodes[1:]:
        acc = gltf.accessors[gltf.meshes[k.mesh].primitives[0].attributes.POSITION]
        assert acc.min[1] >= -1e-6 and acc.max[1] <= 13.0, "heights are metres above the carriageway"
    # travel is -Z at s=0: the road extends toward negative Z
    assert a.min[2] < -0.5 * mesh.length_s * 0.8


def test_resave_with_pygltflib_keeps_the_document(gltf):
    """pygltflib can re-emit the model (we never ship its bytes, but it must be able to load/describe ours)."""
    from pygltflib import GLTF2

    blob = b"".join(gltf.save_to_bytes())
    again = GLTF2.load_from_bytes(blob)
    assert [n.name for n in again.nodes] == [n.name for n in gltf.nodes]
    assert again.binary_blob()[:len(gltf.binary_blob())] == gltf.binary_blob()
