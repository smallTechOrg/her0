"""glb_validate: accepts the stand-in and writer output; rejects each class of defect (and says which)."""

from __future__ import annotations

import copy
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_contract_export as mce  # noqa: E402

from hero.export.glb import build_document, pack_glb, write_glb  # noqa: E402
from hero.export.glb_validate import validate_glb, validate_glb_file  # noqa: E402
from hero.export.mesh_types import MeshMaterial, MeshNode, StreetMesh  # noqa: E402

STANDIN = ROOT / "tests" / "fixtures" / "export" / "contract_curved" / "street.glb"


@pytest.fixture(scope="module")
def mesh() -> StreetMesh:
    plan, fr = mce.build_plan(json.loads(mce.SRC_SCENE.read_text()))
    return mce.build_mesh(plan, fr, mce.load_gameplay())[0]


def mutated(mesh, fn, *, mesh_fn=None):
    """Build the document (optionally from a modified mesh), let ``fn(doc, binary)`` damage it, re-pack."""
    m = copy.deepcopy(mesh)
    if mesh_fn:
        mesh_fn(m)
    doc, binary = build_document(m)
    b = bytearray(binary)
    fn(doc, b)
    return pack_glb(doc, bytes(b))


def node_doc(doc, name):
    n = next(n for n in doc["nodes"] if n.get("name") == name)
    return n, doc["meshes"][n["mesh"]]["primitives"][0]


def errors_of(data, **kw):
    return validate_glb(data, **kw).errors


def has(errors, text):
    return any(text in e for e in errors)


def test_standin_fixture_is_valid():
    rep = validate_glb_file(STANDIN)
    assert rep.ok, rep.errors
    assert rep.warnings == []
    assert rep.stats["primitives"] == 14 and rep.stats["textures"] == 2 and rep.stats["materials"] == 4
    assert rep.stats["triangles"] <= 12_000 and rep.stats["bytes"] <= 1_500_000


def test_writer_output_is_valid(mesh):
    rep = validate_glb(write_glb(mesh))
    assert rep.ok, rep.errors
    assert rep.to_dict()["ok"] is True


def test_generic_mode_accepts_foreign_node_names(mesh):
    data = mutated(mesh, lambda d, b: node_doc(d, "Road")[0].update(name="Whatever"))
    assert has(errors_of(data), "not in the contract set")
    assert errors_of(data, contract=False) == []


# ------------------------------------------------------------------ container


def test_bad_magic_and_truncation(mesh):
    good = write_glb(mesh)
    assert has(errors_of(b"XXXX" + good[4:]), "bad magic")
    assert has(errors_of(good[:-8]), "header length")
    assert has(errors_of(good[:10]), "shorter")
    assert has(errors_of(b""), "shorter")


def test_header_length_mismatch(mesh):
    good = bytearray(write_glb(mesh))
    struct.pack_into("<I", good, 8, len(good) + 4)
    assert has(errors_of(bytes(good)), "header length")


def test_buffer_length_inconsistent(mesh):
    data = mutated(mesh, lambda d, b: d["buffers"][0].update(byteLength=d["buffers"][0]["byteLength"] + 64))
    assert has(errors_of(data), "buffer.byteLength")


def test_json_chunk_must_be_valid_json():
    js = b"{not json}  "
    data = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js)) + struct.pack("<II", len(js), 0x4E4F534A) + js
    assert has(errors_of(data), "JSON chunk invalid")


# ------------------------------------------------------------------ accessors


def test_position_min_max_must_match_data(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        d["accessors"][p["attributes"]["POSITION"]]["max"][0] += 0.5
    assert has(errors_of(mutated(mesh, f)), "min/max")


def test_position_requires_min_max(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        a = d["accessors"][p["attributes"]["POSITION"]]
        del a["min"], a["max"]
    assert has(errors_of(mutated(mesh, f)), "needs min and max")


def test_index_out_of_range(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        a = d["accessors"][p["indices"]]
        v = d["bufferViews"][a["bufferView"]]
        struct.pack_into("<H", b, v["byteOffset"], 60000)
        a["max"] = [60000]
    assert has(errors_of(mutated(mesh, f)), "index 60000 >= vertex count")


def test_index_accessor_wrong_component_type_for_vertex_count(mesh):
    def f(d, b):
        _, p = node_doc(d, "Kerb")
        d["accessors"][p["indices"]]["componentType"] = 5125
    errs = errors_of(mutated(mesh, f))
    assert has(errs, "need componentType")


def test_index_count_not_multiple_of_three(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        d["accessors"][p["indices"]]["count"] -= 1
    assert has(errors_of(mutated(mesh, f)), "multiple of 3")


def test_uv_out_of_range(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        a = d["accessors"][p["attributes"]["TEXCOORD_0"]]
        v = d["bufferViews"][a["bufferView"]]
        struct.pack_into("<ff", b, v["byteOffset"], 1.5, -0.25)
    assert has(errors_of(mutated(mesh, f)), "TEXCOORD_0 values outside [0, 1]")


def test_normals_must_be_unit(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        a = d["accessors"][p["attributes"]["NORMAL"]]
        v = d["bufferViews"][a["bufferView"]]
        struct.pack_into("<fff", b, v["byteOffset"], 0.0, 3.0, 0.0)
    assert has(errors_of(mutated(mesh, f)), "not unit length")


def test_nan_position_rejected(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        a = d["accessors"][p["attributes"]["POSITION"]]
        v = d["bufferViews"][a["bufferView"]]
        struct.pack_into("<f", b, v["byteOffset"], float("nan"))
        a["min"][0] = a["max"][0] = 0.0
    assert errors_of(mutated(mesh, f))


def test_misaligned_accessor_and_view(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        d["accessors"][p["attributes"]["NORMAL"]]["byteOffset"] = 2
    assert has(errors_of(mutated(mesh, f)), "aligned")

    def g(d, b):
        _, p = node_doc(d, "Road")
        v = d["bufferViews"][d["accessors"][p["attributes"]["NORMAL"]]["bufferView"]]
        v["byteOffset"] += 2
    assert has(errors_of(mutated(mesh, g)), "4-byte aligned")


def test_buffer_view_out_of_range(mesh):
    def f(d, b):
        d["bufferViews"][0]["byteLength"] = 10**8
    assert has(errors_of(mutated(mesh, f)), "outside the binary chunk")


def test_accessor_count_mismatch_between_attributes(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        d["accessors"][p["attributes"]["NORMAL"]]["count"] -= 3
    assert has(errors_of(mutated(mesh, f)), "count")


def test_color_must_be_normalized_ubyte(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        d["accessors"][p["attributes"]["COLOR_0"]]["normalized"] = False
    assert has(errors_of(mutated(mesh, f)), "COLOR_0 must be normalized")


def test_extra_attribute_rejected(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        p["attributes"]["TEXCOORD_1"] = p["attributes"]["TEXCOORD_0"]
    assert has(errors_of(mutated(mesh, f)), "not in the contract")


# ------------------------------------------------------------------ references


def test_material_ref_out_of_range(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        p["material"] = 99
    assert has(errors_of(mutated(mesh, f)), "material 99")


def test_texture_ref_out_of_range(mesh):
    def f(d, b):
        d["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"] = 7
    assert has(errors_of(mutated(mesh, f)), "baseColorTexture index 7")


def test_image_and_sampler_refs(mesh):
    def f(d, b):
        d["textures"][0]["source"] = 5
    assert has(errors_of(mutated(mesh, f)), "textures[0] source")

    def g(d, b):
        d["textures"][0]["sampler"] = 3
    assert has(errors_of(mutated(mesh, g)), "sampler")

    def h(d, b):
        v = d["bufferViews"][d["images"][0]["bufferView"]]
        b[v["byteOffset"]] = 0
    assert has(errors_of(mutated(mesh, h)), "not a JPEG")


def test_base_color_factor_range(mesh):
    def f(d, b):
        d["materials"][1]["pbrMetallicRoughness"]["baseColorFactor"] = [185, 185, 185, 1]
    assert has(errors_of(mutated(mesh, f)), "baseColorFactor")


def test_required_extension_rejected(mesh):
    def f(d, b):
        d["extensionsRequired"] = ["KHR_draco_mesh_compression"]
    assert has(errors_of(mutated(mesh, f)), "extensionsRequired")


def test_scene_and_node_refs(mesh):
    def f(d, b):
        d["scenes"][0]["nodes"] = [99]
    assert has(errors_of(mutated(mesh, f)), "out of range")


# ------------------------------------------------------------------ node-name / contract


def test_node_with_transform_rejected(mesh):
    def f(d, b):
        node_doc(d, "Road")[0]["translation"] = [0, 1, 0]
    assert has(errors_of(mutated(mesh, f)), "transforms must be identity")


def test_wrong_material_for_node(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        p["material"] = next(i for i, m in enumerate(d["materials"]) if m["name"] == "mat_kerb")
    assert has(errors_of(mutated(mesh, f)), "contract says 'mat_ground'")


def test_missing_uv_on_ground_node(mesh):
    def f(d, b):
        _, p = node_doc(d, "Road")
        del p["attributes"]["TEXCOORD_0"]
    assert has(errors_of(mutated(mesh, f)), "TEXCOORD_0")


def test_uv_forbidden_on_flat_node(mesh):
    def f(d, b):
        _, p = node_doc(d, "Furniture_Poles")
        p["attributes"]["TEXCOORD_0"] = node_doc(d, "Road")[1]["attributes"]["TEXCOORD_0"]
    assert has(errors_of(mutated(mesh, f)), "forbidden")


def test_missing_vertex_colours_on_flat_node(mesh):
    def f(d, b):
        _, p = node_doc(d, "Furniture_Poles")
        del p["attributes"]["COLOR_0"]
    assert has(errors_of(mutated(mesh, f)), "COLOR_0 missing")


def test_two_primitives_rejected(mesh):
    def f(d, b):
        n, _ = node_doc(d, "Road")
        d["meshes"][n["mesh"]]["primitives"].append(copy.deepcopy(d["meshes"][n["mesh"]]["primitives"][0]))
    assert has(errors_of(mutated(mesh, f)), "exactly one primitive")


def test_duplicate_node_names_rejected(mesh):
    def f(d, b):
        node_doc(d, "Kerb")[0]["name"] = "Road"
    assert has(errors_of(mutated(mesh, f)), "duplicate node names")


def test_root_and_scene_names(mesh):
    def f(d, b):
        d["nodes"][0]["name"] = "Root"
    assert has(errors_of(mutated(mesh, f)), "root node")

    def g(d, b):
        d["scenes"][0]["name"] = "Other"
    assert has(errors_of(mutated(mesh, g)), "one scene named")


def test_asset_extras_required(mesh):
    def f(d, b):
        del d["asset"]["extras"]
    assert has(errors_of(mutated(mesh, f)), "asset.extras.schema")

    def g(d, b):
        del d["asset"]["extras"]["gameplay_sha256"]
    assert has(errors_of(mutated(mesh, g)), "gameplay_sha256")


def test_frontage_must_be_contiguous(mesh):
    def f(d, b):
        n, _ = node_doc(d, "Frontage_L")
        n["extras"]["segments"][1]["s_from"] += 1.0
    assert has(errors_of(mutated(mesh, f)), "not contiguous")


def test_frontage_segment_index_range(mesh):
    def f(d, b):
        n, _ = node_doc(d, "Frontage_R")
        n["extras"]["segments"][0]["index_count"] = 10**6
    assert has(errors_of(mutated(mesh, f)), "index range")


def test_furniture_entity_index_range_and_duplicate_ids(mesh):
    def f(d, b):
        n, _ = node_doc(d, "Furniture_Poles")
        n["extras"]["entities"][0]["index_start"] = 1
    assert has(errors_of(mutated(mesh, f)), "index range")

    def g(d, b):
        n, _ = node_doc(d, "Furniture_Poles")
        n["extras"]["entities"][1]["id"] = n["extras"]["entities"][0]["id"]
    assert has(errors_of(mutated(mesh, g)), "duplicate entity id")


def test_ground_extras_shape(mesh):
    def f(d, b):
        n, _ = node_doc(d, "Road")
        n["extras"]["bins"][0]["ground_source"] = "google_maps"
    assert has(errors_of(mutated(mesh, f)), "ground_source")


def test_missing_extras_on_node(mesh):
    def f(d, b):
        del node_doc(d, "Road")[0]["extras"]
    assert has(errors_of(mutated(mesh, f)), "extras missing")


# ------------------------------------------------------------------ budgets


def test_triangle_budget(mesh):
    def grow(m):
        nd = m.node("Furniture_Boxes")
        k = 12_001
        nd.positions = nd.positions + [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)]
        nd.normals = nd.normals + [(0.0, 1.0, 0.0)] * 3
        nd.colors = nd.colors + [(1, 2, 3, 255)] * 3
        base = len(nd.positions) - 3
        nd.indices = nd.indices + [base, base + 1, base + 2] * k

    data = mutated(mesh, lambda d, b: None, mesh_fn=grow)
    assert has(errors_of(data), "budget: triangles")
    assert not has(errors_of(data, budgets=False), "budget")


def test_primitive_and_texture_budgets(mesh):
    def f(d, b):
        d["textures"].append(dict(d["textures"][0]))
        d["textures"].append(dict(d["textures"][0]))
    assert has(errors_of(mutated(mesh, f)), "budget: textures 4")


def test_byte_budget_scales_with_street_length(mesh):
    big = bytes(range(256)) * 6000  # 1.5 MB payload inside a valid JPEG-looking blob
    def grow(m):
        m.textures["atlas_facade"] = m.textures["atlas_facade"] + big
    data = mutated(mesh, lambda d, b: None, mesh_fn=grow)
    assert has(errors_of(data), "budget: bytes")

    def long(m):
        grow(m)
        m.asset_extras["length_s"] = 200.0
    assert not has(errors_of(mutated(mesh, lambda d, b: None, mesh_fn=long)), "budget: bytes")


# ------------------------------------------------------------------ CLI


def test_cli_exit_codes_and_json_lines(tmp_path, mesh):
    bad = tmp_path / "bad.glb"
    bad.write_bytes(b"glTF" + b"\0" * 30)
    r = subprocess.run([sys.executable, "-m", "hero.tools.validate_glb", str(STANDIN), str(bad)], capture_output=True,
                       text=True, cwd=str(ROOT))
    assert r.returncode == 1
    lines = [json.loads(x) for x in r.stdout.splitlines()]
    assert [x["ok"] for x in lines] == [True, False]
    assert lines[0]["file"] == str(STANDIN) and lines[0]["stats"]["primitives"] == 14
    r = subprocess.run([sys.executable, "-m", "hero.tools.validate_glb", str(STANDIN)], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r = subprocess.run([sys.executable, "-m", "hero.tools.validate_glb", str(tmp_path / "missing.glb")], capture_output=True,
                       text=True, cwd=str(ROOT))
    assert r.returncode == 1 and "not a file" in r.stdout


def test_tiny_foreign_glb_passes_generic_mode():
    nd = MeshNode("Anything", "m", [(0, 0, 0), (1, 0, 0), (0, 0, -1)], [(0, 1, 0)] * 3, [0, 1, 2])
    data = write_glb(StreetMesh({}, [nd], [MeshMaterial("m")], {}, 1.0))
    assert validate_glb(data, contract=False).ok
    assert not validate_glb(data).ok
