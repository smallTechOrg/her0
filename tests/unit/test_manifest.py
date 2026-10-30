"""``hero.export.manifest.build_manifest`` against the stand-in bundle (the oracle) and the committed JSON Schema."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from hero.export import SCHEMAS_DIR, gameplay_bytes, load_gameplay
from hero.export.manifest import build_manifest, manifest_bytes
from hero.export.manifest_types import ManifestV1
from hero.export.xodr import build_xodr
from hero.model.street_plan_types import StreetPlan
from tests.fixtures import make_contract_export as mk

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tests" / "fixtures" / "export" / "contract_curved"


@pytest.fixture(scope="module")
def plan() -> StreetPlan:
    return StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())


@pytest.fixture(scope="module")
def mesh():
    v2 = json.loads(mk.SRC_SCENE.read_text())
    p, fr = mk.build_plan(v2)
    m, _ = mk.build_mesh(p, fr, load_gameplay())
    return m


def make(plan, mesh, **kw):
    glb = (BUNDLE / "street.glb").read_bytes()
    x = build_xodr(plan)
    return build_manifest(plan, mesh, glb_sha256=hashlib.sha256(glb).hexdigest(), glb_bytes=len(glb),
                          xodr_sha256=hashlib.sha256(x.encode()).hexdigest(), **kw)


@pytest.fixture(scope="module")
def manifest(plan, mesh) -> dict:
    return make(plan, mesh)


# ---- minimal JSON Schema checker (jsonschema is not a dependency): the subset pydantic emits


def check_schema(inst, sch, root, path="$"):
    if "$ref" in sch:
        return check_schema(inst, root["$defs"][sch["$ref"].split("/")[-1]], root, path)
    if "anyOf" in sch:
        errs = []
        for sub in sch["anyOf"]:
            try:
                return check_schema(inst, sub, root, path)
            except AssertionError as e:
                errs.append(str(e))
        raise AssertionError(f"{path}: no anyOf branch matches: {errs}")
    if "const" in sch:
        assert inst == sch["const"], f"{path}: {inst!r} != const {sch['const']!r}"
    if "enum" in sch:
        assert inst in sch["enum"], f"{path}: {inst!r} not in enum"
    t = sch.get("type")
    if t == "object":
        assert isinstance(inst, dict), f"{path}: not an object"
        props = sch.get("properties", {})
        for k in sch.get("required", []):
            assert k in inst, f"{path}: missing {k}"
        if sch.get("additionalProperties") is False:
            extra = set(inst) - set(props)
            assert not extra, f"{path}: unexpected keys {extra}"
        for k, v in inst.items():
            if k in props:
                check_schema(v, props[k], root, f"{path}.{k}")
            elif isinstance(sch.get("additionalProperties"), dict):
                check_schema(v, sch["additionalProperties"], root, f"{path}.{k}")
    elif t == "array":
        assert isinstance(inst, list), f"{path}: not an array"
        assert len(inst) >= sch.get("minItems", 0), f"{path}: too short"
        if "maxItems" in sch:
            assert len(inst) <= sch["maxItems"], f"{path}: too long"
        if "prefixItems" in sch:
            assert len(inst) == len(sch["prefixItems"])
            for k, (v, s2) in enumerate(zip(inst, sch["prefixItems"])):
                check_schema(v, s2, root, f"{path}[{k}]")
        elif "items" in sch:
            for k, v in enumerate(inst):
                check_schema(v, sch["items"], root, f"{path}[{k}]")
    elif t == "string":
        assert isinstance(inst, str), f"{path}: not a string"
        if "pattern" in sch:
            import re
            assert re.search(sch["pattern"], inst), f"{path}: pattern"
    elif t == "integer":
        assert isinstance(inst, int) and not isinstance(inst, bool), f"{path}: not an integer"
    elif t == "number":
        assert isinstance(inst, (int, float)) and not isinstance(inst, bool), f"{path}: not a number"
    elif t == "boolean":
        assert isinstance(inst, bool)
    elif t == "null":
        assert inst is None
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in sch:
            assert inst >= sch["minimum"], f"{path}: below minimum"
        if "maximum" in sch:
            assert inst <= sch["maximum"], f"{path}: above maximum"


# ---------------------------------------------------------------------------------------------------------- tests


def test_validates_pydantic_and_committed_json_schema(manifest):
    ManifestV1.model_validate(manifest)
    sch = json.loads((SCHEMAS_DIR / "manifest.schema.json").read_text())
    assert sch == ManifestV1.model_json_schema(by_alias=True), "committed schema is stale"
    check_schema(manifest, sch, sch)
    bad = copy.deepcopy(manifest)
    bad["lanes"]["stations"][0]["extra"] = 1
    with pytest.raises(AssertionError):
        check_schema(bad, sch, sch)


def test_matches_the_stand_in_except_xodr_hash(manifest):
    old = json.loads((BUNDLE / "street.manifest.json").read_text())

    def norm(d):
        d = copy.deepcopy(d)
        d["build"]["xodr_sha256"] = "-"
        return d

    a, b = norm(manifest), norm(old)
    # the stand-in integrates the street frame from exact headings, we from the plan's 2 dp bearings: sub-centimetre drift
    for p, q in zip(a["reference_line"]["points"], b["reference_line"]["points"]):
        assert math.dist((p["x"], p["z"]), (q["x"], q["z"])) < 0.01 and {**p, "x": 0, "z": 0} == {**q, "x": 0, "z": 0}
    assert abs(a["loop"]["end_pose"]["yaw_rad"] - b["loop"]["end_pose"]["yaw_rad"]) < 1e-4
    for d in (a, b):
        d["reference_line"]["points"] = d["loop"] = None
    assert a == b  # same field names and values
    assert list(manifest) == list(old)  # same key order


def test_bytes_are_deterministic_in_process_and_across_hash_seeds(plan, mesh, manifest):
    assert manifest_bytes(make(plan, mesh)) == manifest_bytes(manifest)
    code = (
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        "from hero.model.street_plan_types import StreetPlan\n"
        "from hero.export.manifest import build_manifest, manifest_bytes\n"
        "from tests.fixtures import make_contract_export as mk\n"
        "from hero.export import load_gameplay\n"
        f"B = Path({str(BUNDLE)!r})\n"
        "plan = StreetPlan.model_validate_json((B / 'plan.json').read_text())\n"
        "p, fr = mk.build_plan(json.loads(mk.SRC_SCENE.read_text()))\n"
        "mesh, _ = mk.build_mesh(p, fr, load_gameplay())\n"
        "m = build_manifest(plan, mesh, glb_sha256='0' * 64, glb_bytes=1, xodr_sha256='1' * 64)\n"
        "print(hashlib.sha256(manifest_bytes(m)).hexdigest())\n"
    )
    shas = set()
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}"}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
        assert r.returncode == 0, r.stderr
        shas.add(r.stdout.strip())
    assert len(shas) == 1


def test_gameplay_embedded_verbatim_and_hash_matches_file(manifest):
    assert manifest["gameplay"] == json.loads(gameplay_bytes())
    assert manifest["gameplay_sha256"] == hashlib.sha256(gameplay_bytes()).hexdigest()
    assert manifest["gameplay_sha256"] == hashlib.sha256((ROOT / "src/hero/export/gameplay.json").read_bytes()).hexdigest()


def test_reference_line_origin_and_loop_seam(manifest, plan):
    pts = manifest["reference_line"]["points"]
    assert (pts[0]["x"], pts[0]["z"]) == (0.0, 0.0)
    assert [p["s"] for p in pts] == [round(p.s, 3) for p in plan.reference_line.points]
    assert manifest["frame"]["origin"] == {"lat": plan.frame.origin.lat, "lon": plan.frame.origin.lon}
    e = manifest["loop"]["end_pose"]
    assert (e["x"], e["z"]) == (pts[-1]["x"], pts[-1]["z"])
    # travel at s=0 is -Z; first step goes toward -Z
    assert pts[1]["z"] < 0 and abs(pts[1]["x"]) < 0.2
    # curved street: chain length ~ length_s
    d = sum(math.dist((a["x"], a["z"]), (b["x"], b["z"])) for a, b in zip(pts, pts[1:]))
    assert abs(d - manifest["segment"]["length_s"]) < 0.05
    assert abs(e["yaw_rad"] - math.radians(pts[-1]["bearing_deg"] - pts[0]["bearing_deg"])) < 1e-3


def test_lanes_stations_cross_section_and_frontage(manifest, plan):
    st = manifest["lanes"]["stations"]
    cs = manifest["cross_section"]["stations"]
    assert len(st) == len(cs) == len(plan.stations)
    for a, b in zip(st, cs):
        assert all(x["t_lo"] <= x["t_hi"] for x in b["strips"])
        assert [x["t_lo"] for x in b["strips"]] == sorted(x["t_lo"] for x in b["strips"])
        if a["footpath"]:  # left footpath: t > 0, lane 0 centre inside the strip
            assert a["footpath"]["t_lo"] < a["lane_t"][0] < a["footpath"]["t_hi"]
    assert any(s["footpath"] is None for s in st) and any(s["footpath"] for s in st)
    for side in ("left", "right"):
        segs = manifest["frontage"][side]
        assert segs[0]["s_from"] == 0 and abs(segs[-1]["s_to"] - plan.length_m) < 1e-3
        assert all(abs(a["s_to"] - b["s_from"]) < 1e-3 for a, b in zip(segs, segs[1:]))
    assert all(f["t_from"] > 0 for f in manifest["frontage"]["left"]) and all(f["t_from"] < 0 for f in manifest["frontage"]["right"])


def test_entities_equal_glb_extras_and_plan(manifest, mesh, plan):
    extras = {}
    for n in mesh.nodes:
        for e in n.extras.get("entities", []):
            extras[e["id"]] = (n.name, e["s"], e["t"])
    assert extras and set(extras) == {e["id"] for e in manifest["entities"]}
    for e in manifest["entities"]:
        assert extras[e["id"]] == (e["node"], e["s"], e["t"])
    assert [e["id"] for e in manifest["entities"]] == [f.id for f in plan.furniture]
    assert {c["id"] for c in manifest["colliders"]} == {o.id for o in plan.objects}  # synthetic gap-/blocked- included
    assert any(c["id"].startswith("gap-") for c in manifest["colliders"])
    assert {t["id"] for t in manifest["tokens"]} == {t.id for t in plan.tokens}


def test_mesh_stats_and_attribution(manifest, mesh):
    assert manifest["mesh_stats"] == mesh.stats()
    assert len(manifest["attribution"]["lines"]) == 3 and "Google" in manifest["attribution"]["notice"]


def test_right_hand_footpath_mirrors_t(plan, mesh):
    raw = json.loads(plan.model_dump_json())
    raw["footpath_side"] = "right"
    plan_r = StreetPlan.model_validate(raw)
    m_l, m_r = make(plan, mesh), make(plan_r, mesh)
    assert ManifestV1.model_validate(m_r)
    for a, b in zip(m_l["lanes"]["stations"], m_r["lanes"]["stations"]):
        assert b["lane_t"] == [-v if v else v for v in a["lane_t"]]
        assert b["kerb"]["t"] == -a["kerb"]["t"]
    assert [f["t_from"] for f in m_r["frontage"]["right"]] == [-f["t_from"] for f in m_l["frontage"]["left"]]
    assert [e["t"] for e in m_r["entities"]] == [-e["t"] for e in m_l["entities"]]


def test_edge_plan_without_objects_tokens_furniture_or_footpath(plan, mesh):
    raw = json.loads(plan.model_dump_json())
    raw["objects"], raw["tokens"], raw["furniture"] = [], [], []
    for st in raw["stations"]:
        st["footpath"] = None
        st["kerb"]["height_m"] = 0.0
    m = make(StreetPlan.model_validate(raw), mesh)
    ManifestV1.model_validate(m)
    assert m["entities"] == m["colliders"] == m["tokens"] == []
    assert all(s["footpath"] is None for s in m["lanes"]["stations"])
    assert all(q["kind"] != "kerb" for c in m["cross_section"]["stations"] for q in c["strips"])


def test_error_paths(plan, mesh):
    with pytest.raises(KeyError):
        make(plan, mesh, gameplay={"schema": "hero-gameplay/1", "lanes": {}})
    with pytest.raises(ValidationError):
        build_manifest(plan, mesh, glb_sha256="x", glb_bytes=-1, xodr_sha256="y")
    custom = load_gameplay()
    custom["version"] = 99
    m = make(plan, mesh, gameplay=custom)
    assert m["gameplay"]["version"] == 99 and m["gameplay_sha256"] != hashlib.sha256(gameplay_bytes()).hexdigest()
