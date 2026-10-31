"""write_glb: byte-level layout, determinism, colour space, error paths (spec/portability.md section 3)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_contract_export as mce  # noqa: E402

from hero.export.glb import GlbError, base_color_factor, split_glb, write_glb  # noqa: E402
from hero.export.mesh_types import MeshMaterial, MeshNode, StreetMesh  # noqa: E402

STANDIN = ROOT / "tests" / "fixtures" / "export" / "contract_curved" / "street.glb"


@pytest.fixture(scope="module")
def mesh() -> StreetMesh:
    plan, fr = mce.build_plan(json.loads(mce.SRC_SCENE.read_text()))
    return mce.build_mesh(plan, fr, mce.load_gameplay())[0]


def tiny_mesh(**node_kw) -> StreetMesh:
    nd = MeshNode("Road", "mat_ground", [(0, 0, 0), (1, 0, 0), (0, 0, -1)], [(0, 1, 0)] * 3, [0, 1, 2],
                  uvs=[(0, 0), (1, 0), (0, 1)], colors=[(255, 255, 255, 255)] * 3, extras={"kind": "road"})
    for k, v in node_kw.items():
        setattr(nd, k, v)
    return StreetMesh({"schema": "hero-street-glb/1"}, [nd], [MeshMaterial("mat_ground", "#ffffff", "atlas_ground")],
                      {"atlas_ground": b"\xff\xd8\xff\xe0jpeg"}, 1.0)


def test_matches_standin_bytes(mesh):
    """The production writer reproduces the S1a stand-in writer byte for byte (same layout contract)."""
    assert write_glb(mesh) == STANDIN.read_bytes()


def test_deterministic_in_process(mesh):
    a, b = write_glb(mesh), write_glb(copy.deepcopy(mesh))
    assert a == b
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_deterministic_across_subprocesses_and_hash_seeds():
    code = (
        "import sys,json,hashlib;sys.path.insert(0,%r);import make_contract_export as m;"
        "from hero.export.glb import write_glb;"
        "p,f=m.build_plan(json.loads(m.SRC_SCENE.read_text()));mesh=m.build_mesh(p,f,m.load_gameplay())[0];"
        "print(hashlib.sha256(write_glb(mesh)).hexdigest())" % str(ROOT / "tests" / "fixtures")
    )
    outs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT), check=True)
        outs.append(r.stdout.strip())
    assert outs[0] == outs[1]
    assert outs[0] == hashlib.sha256(STANDIN.read_bytes()).hexdigest()


def test_container_layout(mesh):
    data = write_glb(mesh)
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    assert (magic, version, total) == (b"glTF", 2, len(data))
    jlen, jtype = struct.unpack_from("<II", data, 12)
    assert jtype == 0x4E4F534A and jlen % 4 == 0
    blen, btype = struct.unpack_from("<II", data, 20 + jlen)
    assert btype == 0x004E4942 and blen % 4 == 0
    assert 20 + jlen + 8 + blen == len(data)
    doc, binary = split_glb(data)
    assert len(binary) == blen
    # compact separators, ordered keys, ASCII only, no timestamps / hosts
    raw = data[20:20 + jlen]
    raw.decode("ascii")
    assert b": " not in raw and b", " not in raw
    assert list(doc)[:4] == ["asset", "scene", "scenes", "nodes"]
    assert doc["asset"]["version"] == "2.0" and doc["asset"]["generator"] == "hero.export/1"
    assert "extensionsRequired" not in doc and "extensionsUsed" not in doc
    assert doc["buffers"] == [{"byteLength": doc["buffers"][0]["byteLength"]}] and len(doc["buffers"]) == 1
    assert doc["nodes"][0] == {"name": "Street", "children": list(range(1, len(mesh.nodes) + 1))}
    assert [n["name"] for n in doc["nodes"][1:]] == [n.name for n in mesh.nodes]
    assert all(set(n) <= {"name", "mesh", "extras"} for n in doc["nodes"][1:]), "no transforms: identity by omission"


def test_buffer_views_are_aligned_and_non_overlapping(mesh):
    doc, binary = split_glb(write_glb(mesh))
    end = 0
    for v in doc["bufferViews"]:
        assert v["byteOffset"] % 4 == 0
        assert v["byteOffset"] >= end
        end = v["byteOffset"] + v["byteLength"]
    assert end == doc["buffers"][0]["byteLength"] <= len(binary)


def test_base_color_factor_is_linear_not_srgb():
    """glTF baseColorFactor is linear: mat_kerb #b9b9b9 (sRGB 185) -> ~0.485, not 0.725."""
    f = base_color_factor("#b9b9b9")
    assert f[3] == 1.0
    assert f[0] == f[1] == f[2] == pytest.approx(0.485150, abs=1e-5)
    assert base_color_factor("#ffffff") == [1.0, 1.0, 1.0, 1.0]
    assert base_color_factor("#000000") == [0.0, 0.0, 0.0, 1.0]
    doc, _ = split_glb(write_glb(tiny_mesh()))
    assert doc["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]


def test_materials_are_opaque_single_sided(mesh):
    doc, _ = split_glb(write_glb(mesh))
    for m in doc["materials"]:
        assert m["doubleSided"] is False and m["alphaMode"] == "OPAQUE"
        assert m["pbrMetallicRoughness"]["metallicFactor"] == 0.0
    ground = next(m for m in doc["materials"] if m["name"] == "mat_ground")
    assert ground["pbrMetallicRoughness"]["roughnessFactor"] == 1.0
    assert "baseColorTexture" in ground["pbrMetallicRoughness"]
    assert doc["samplers"] == [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]


def test_uint32_indices_when_65535_vertices_or_more():
    n = 65535
    pos = [(float(i % 255), 0.0, -float(i // 255)) for i in range(n)]
    nd = MeshNode("Road", "mat_ground", pos, [(0.0, 1.0, 0.0)] * n, [0, 1, 2, n - 3, n - 2, n - 1],
                  uvs=[(0.0, 0.0)] * n, colors=[(1, 2, 3, 4)] * n, extras={})
    mesh = StreetMesh({}, [nd], [MeshMaterial("mat_ground")], {}, 1.0)
    doc, _ = split_glb(write_glb(mesh))
    ia = doc["accessors"][doc["meshes"][0]["primitives"][0]["indices"]]
    assert ia["componentType"] == 5125 and ia["max"] == [n - 1]


def test_uint16_indices_below_threshold(mesh):
    doc, _ = split_glb(write_glb(mesh))
    for m in doc["meshes"]:
        assert doc["accessors"][m["primitives"][0]["indices"]]["componentType"] == 5123


def test_no_textures_means_no_texture_arrays():
    nd = tiny_mesh().nodes[0]
    mesh = StreetMesh({}, [nd], [MeshMaterial("mat_ground")], {}, 1.0)
    doc, _ = split_glb(write_glb(mesh))
    assert "textures" not in doc and "images" not in doc and "samplers" not in doc


@pytest.mark.parametrize("mutate, msg", [
    (lambda m: setattr(m.nodes[0], "indices", [0, 1, 3]), "index out of range"),
    (lambda m: setattr(m.nodes[0], "indices", [0, 1]), "multiple of 3"),
    (lambda m: setattr(m.nodes[0], "indices", []), "no geometry"),
    (lambda m: setattr(m.nodes[0], "positions", [(0, 0, 0), (float("nan"), 0, 0), (0, 0, -1)]), "NaN"),
    (lambda m: setattr(m.nodes[0], "normals", [(0, 1, 0)] * 2), "normals"),
    (lambda m: setattr(m.nodes[0], "uvs", [(0, 0)]), "uvs"),
    (lambda m: setattr(m.nodes[0], "colors", [(1, 2, 3, 300)] * 3), "COLOR_0"),
    (lambda m: setattr(m.nodes[0], "material", "mat_nope"), "unknown material"),
    (lambda m: setattr(m, "textures", {}), "not in mesh.textures"),
    (lambda m: setattr(m, "textures", {"atlas_ground": b"notjpeg"}), "not a JPEG"),
    (lambda m: setattr(m, "nodes", []), "no nodes"),
    (lambda m: m.nodes.append(copy.deepcopy(m.nodes[0])), "duplicate node names"),
    (lambda m: setattr(m.materials[0], "base_color_hex", "red"), "bad colour"),
])
def test_invalid_meshes_raise(mutate, msg):
    m = tiny_mesh()
    mutate(m)
    with pytest.raises(GlbError, match=msg):
        write_glb(m)
