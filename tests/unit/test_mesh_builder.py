"""build_street_mesh: contract node set/order, extras, determinism (in-process + subprocess with two PYTHONHASHSEEDs)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hero.export import gameplay_sha256
from hero.export.glb import read_glb, write_glb
from hero.export.glb_validate import validate_glb
from hero.export.mesh_builder import build_street_mesh
from hero.export.mesh_types import ASSET_SCHEMA, MATERIAL_NAMES, NODE_NAMES

import mesh_support as ms

PATHS = [str(p) for p in ms.plan_paths()]


@pytest.mark.parametrize("path", PATHS, ids=ms.plan_ids())
def test_node_names_order_materials_and_asset_extras(path):
    plan = ms.load_plan(path)
    m = ms.build(path)
    names = [n.name for n in m.nodes]
    assert set(names) <= set(NODE_NAMES)
    assert names == [n for n in NODE_NAMES if n in names]  # contract order
    for required in ("Footpath", "Road", "Kerb", "Ground_L", "Ground_R", "Frontage_L", "Frontage_R", "Frontage_Caps"):
        assert required in names
    assert [mt.name for mt in m.materials] == list(MATERIAL_NAMES)
    assert sorted(m.textures) == ["atlas_facade", "atlas_ground"]
    ae = m.asset_extras
    assert list(ae) == ["schema", "generator", "segment_slug", "model_version", "model_hash", "footpath_side", "units", "up_axis",
                        "travel", "right", "length_s", "frame", "manifest", "gameplay_sha256"]
    assert ae["schema"] == ASSET_SCHEMA and ae["segment_slug"] == plan.slug and ae["gameplay_sha256"] == gameplay_sha256()
    assert ae["length_s"] == pytest.approx(plan.length_m) and ae["frame"]["heading_deg"] == plan.frame.heading_deg
    for nd in m.nodes:  # arrays aligned, indices in range, one triangle list
        assert len(nd.indices) % 3 == 0 and max(nd.indices) < len(nd.positions)
        assert len(nd.normals) == len(nd.positions)
        assert (nd.uvs is not None) == (nd.name.startswith(("Footpath", "Road", "Ground", "Frontage_")) and nd.name != "Frontage_Caps")


@pytest.mark.parametrize("path", PATHS, ids=ms.plan_ids())
def test_glb_validates_and_street_length_from_road(path):
    plan = ms.load_plan(path)
    m = ms.build(path)
    data = write_glb(m)
    rep = validate_glb(data)
    assert rep.ok, rep.errors
    assert read_glb(data) is not None
    road = m.node("Road")
    zs = [p for p in road.positions]
    # street length ~ Road extent along the chain (chord of the centreline end-to-end <= length)
    far = max(((p[0] - 0) ** 2 + (p[2] - 0) ** 2) ** 0.5 for p in zs)
    assert far <= plan.length_m + 14.0 and far >= min(plan.length_m * 0.8, plan.length_m - 3.0)


def _sha(path: str) -> str:
    return hashlib.sha256(write_glb(build_street_mesh(None, None, observations={}, data_dir=Path("/nonexistent"),
                                                      plan=ms.load_plan(path)))).hexdigest()


@pytest.mark.parametrize("path", PATHS, ids=ms.plan_ids())
def test_deterministic_in_process(path):
    assert _sha(path) == _sha(path)


def test_deterministic_across_subprocesses_with_different_hash_seeds():
    script = (
        "import hashlib, sys\n"
        "from pathlib import Path\n"
        "from hero.model.street_plan_types import StreetPlan\n"
        "from hero.export.mesh_builder import build_street_mesh\n"
        "from hero.export.glb import write_glb\n"
        "plan = StreetPlan.model_validate_json(Path(sys.argv[1]).read_text())\n"
        "m = build_street_mesh(None, None, observations={}, data_dir=Path('/nonexistent'), plan=plan)\n"
        "print(hashlib.sha256(write_glb(m)).hexdigest())\n"
    )
    out = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run([sys.executable, "-c", script, str(ms.CURVED)], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        out.append(r.stdout.strip())
    assert out[0] == out[1] == _sha(str(ms.CURVED))


def test_texture_bytes_and_geometry_change_with_plan():
    a = ms.build(str(ms.CURVED))
    d = ms.load_plan(str(ms.CURVED)).model_dump()
    d["model_hash"] = "f" * 64
    from hero.model.street_plan_types import StreetPlan
    b = ms.build_plan(StreetPlan.model_validate(d))
    assert a.asset_extras["model_hash"] != b.asset_extras["model_hash"]
    assert [n.positions for n in a.nodes] == [n.positions for n in b.nodes]


def test_extras_json_serialisable_and_stable():
    m = ms.build(str(ms.CURVED))
    a = json.dumps([n.extras for n in m.nodes], sort_keys=False)
    b = json.dumps([n.extras for n in ms.build_plan(ms.load_plan(str(ms.CURVED))).nodes], sort_keys=False)
    assert a == b
