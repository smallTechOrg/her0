"""``hero.export.xodr`` - structure, parse round-trip (spec/portability.md section 8) and consistency with the manifest."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hero.export import load_gameplay
from hero.export.manifest import build_manifest
from hero.export.xodr import build_xodr, check_structure, lane_edges, parse_xodr, reference_xy
from hero.model.street_plan_types import StreetPlan
from tests.fixtures import make_contract_export as mk

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tests" / "fixtures" / "export" / "contract_curved"
XSD = ROOT / "tests" / "fixtures" / "xodr" / "OpenDRIVE_1.7.xsd"
TOL = 0.05


@pytest.fixture(scope="module")
def plan() -> StreetPlan:
    return StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())


@pytest.fixture(scope="module")
def mesh():
    p, fr = mk.build_plan(json.loads(mk.SRC_SCENE.read_text()))
    return mk.build_mesh(p, fr, load_gameplay())[0]


def manifest_for(plan, mesh, xodr):
    return build_manifest(plan, mesh, glb_sha256="0" * 64, glb_bytes=1, xodr_sha256=hashlib.sha256(xodr.encode()).hexdigest())


def variants(plan):
    raw = json.loads(plan.model_dump_json())
    right = dict(raw, footpath_side="right")
    return {"left": plan, "right": StreetPlan.model_validate(right)}


def to_en(m, x, z):
    """Manifest street-frame (x, z) -> local east/north, written independently of hero.export."""
    h = math.radians(m["frame"]["heading_deg"])
    return (-z * math.sin(h) + x * math.cos(h), -z * math.cos(h) - x * math.sin(h))


@pytest.fixture(scope="module", params=["left", "right"])
def case(request, plan, mesh):
    p = variants(plan)[request.param]
    x = build_xodr(p)
    return p, x, manifest_for(p, mesh, x)


# ---------------------------------------------------------------------------------------------------- structure


def test_header_and_well_formed(case):
    p, x, m = case
    root = ET.fromstring(x)
    h = root.find("header")
    assert (h.get("revMajor"), h.get("revMinor"), h.get("vendor")) == ("1", "7", "hero")
    assert h.get("name") == p.slug
    geo = h.findtext("geoReference")
    assert f"+lat_0={p.frame.origin.lat}" in geo and f"+lon_0={p.frame.origin.lon}" in geo and "+proj=tmerc" in geo and "+units=m" in geo
    road = root.find("road")
    assert (road.get("id"), road.get("junction")) == ("1", "-1") and float(road.get("length")) == pytest.approx(p.length_m)


def test_structural_checks_pass_on_both_sides(case):
    assert check_structure(case[1]) == []


def test_structural_checks_detect_breakage(plan):
    x = build_xodr(plan)
    assert check_structure(x[: len(x) // 2])  # truncated -> not well-formed
    assert any("1.7" in i for i in check_structure(x.replace('revMinor="7"', 'revMinor="4"')))
    assert any("contiguous" in i for i in check_structure(x.replace('<lane id="2"', '<lane id="5"', 1)))
    assert any("negative" in i for i in check_structure(x.replace('<width sOffset="0" a="', '<width sOffset="0" a="-', 1)))
    assert any("length" in i for i in check_structure(x.replace('length="60"', 'length="61"', 1)))
    with pytest.raises(ValueError):
        parse_xodr("<Nope/>")


def test_geometry_s_increasing_and_lengths_sum(case):
    p, x, _ = case
    g = parse_xodr(x)["geometries"]
    assert [a["s"] for a in g] == sorted({a["s"] for a in g})
    assert sum(a["length"] for a in g) == pytest.approx(p.length_m, abs=1e-3)
    assert all(a["kind"] == "line" for a in g)


def test_straight_runs_are_merged():
    """A dead-straight 100 m reference line collapses into one geometry piece."""
    raw = json.loads((BUNDLE / "plan.json").read_text())
    pts = raw["reference_line"]["points"]
    # keep stations 0..24 but force constant bearing
    for q in pts:
        q["bearing_deg"] = 0.0
    p = StreetPlan.model_validate(raw)
    g = parse_xodr(build_xodr(p))["geometries"]
    assert len(g) == 1 and g[0]["length"] == pytest.approx(p.length_m)
    assert len(parse_xodr(build_xodr(StreetPlan.model_validate(json.loads((BUNDLE / "plan.json").read_text()))))["geometries"]) > 5


def test_lane_ids_widths_and_types(case):
    _, x, _ = case
    for sec in parse_xodr(x)["sections"]:
        for side in ("left", "right"):
            types = [ln["type"] for ln in sorted(sec[side], key=lambda ln: abs(ln["id"]))]
            assert types[0] == "driving" and set(types) <= {"driving", "curb", "sidewalk"} and types == sorted(types, key=("driving", "curb", "sidewalk").index)
            for ln in sec[side]:
                assert all(w[1] >= 0 and w[3] == w[4] == 0 for w in ln["widths"])
    assert any(ln["type"] == "sidewalk" for sec in parse_xodr(x)["sections"] for ln in sec["left" if case[0].footpath_side == "left" else "right"])


def test_footpath_missing_has_no_sidewalk_on_modelled_side(case):
    p, x, _ = case
    parsed = parse_xodr(x)
    side = p.footpath_side
    for i, st in enumerate(p.stations):
        if st.footpath is None and i + 1 < len(p.stations) and p.stations[i + 1].footpath is None and i > 0 and p.stations[i - 1].footpath is None:
            kinds = [t for t, _, _ in lane_edges(parsed, st.s + 0.5)[side]]
            assert "sidewalk" not in kinds and "curb" not in kinds
    assert len(parsed["sections"]) >= 3  # footpath appears / disappears -> separate laneSections


# ------------------------------------------------------------------------------------------- parse round-trip


def test_round_trip_reference_polyline(case):
    _, x, m = case
    parsed = parse_xodr(x)
    for q in m["reference_line"]["points"]:
        e, n = to_en(m, q["x"], q["z"])
        pe, pn = reference_xy(parsed, q["s"])
        assert math.hypot(e - pe, n - pn) < TOL, (q["s"], e, n, pe, pn)
    # headings: hdg = pi/2 - bearing
    g0 = parsed["geometries"][0]
    assert g0["hdg"] == pytest.approx(math.pi / 2 - math.radians(m["reference_line"]["points"][0]["bearing_deg"] + 0.5 * (m["reference_line"]["points"][1]["bearing_deg"] - m["reference_line"]["points"][0]["bearing_deg"])), abs=1e-3)


def test_round_trip_strip_edges(case):
    _, x, m = case
    parsed = parse_xodr(x)
    checked = {"footpath": 0, "carriageway": 0, "opp": 0}
    for lane_st, cs in zip(m["lanes"]["stations"], m["cross_section"]["stations"]):
        edges = lane_edges(parsed, lane_st["s"])
        flat = {"left": edges["left"], "right": edges["right"]}
        drv = [(a, b) for side in flat.values() for t, a, b in side if t == "driving"]
        car = next(q for q in cs["strips"] if q["kind"] == "carriageway")
        assert min(b for _, b in drv) == pytest.approx(car["t_lo"], abs=TOL) and max(b for _, b in drv) == pytest.approx(car["t_hi"], abs=TOL)
        checked["carriageway"] += 1
        sidewalks = {side: [(a, b) for t, a, b in lanes if t == "sidewalk"] for side, lanes in flat.items()}
        for q in cs["strips"]:
            if q["kind"] != "footpath":
                continue
            side = "left" if (q["t_lo"] + q["t_hi"]) / 2 > 0 else "right"
            got = sidewalks[side]
            assert got, f"no sidewalk lane on {side} at s={cs['s']}"
            lo, hi = sorted(got[0])
            # lanes cannot overlap: where the carriageway edge pokes into the footpath strip (data quirk of the stand-in plan,
            # up to 0.1 m) the sidewalk lane starts at the carriageway edge and keeps its outer edge
            e_lo, e_hi = q["t_lo"], q["t_hi"]
            if side == "left":
                e_lo = max(e_lo, car["t_hi"])
            else:
                e_hi = min(e_hi, car["t_lo"])
            assert (lo, hi) == (pytest.approx(e_lo, abs=TOL), pytest.approx(e_hi, abs=TOL)), (cs["s"], side)
            checked["footpath" if lane_st["footpath"] and abs(lane_st["footpath"]["t_lo"] - q["t_lo"]) < 1e-9 else "opp"] += 1
        if lane_st["footpath"] is None:  # footpath missing at this station: the modelled-side sidewalk has no width
            fp_side = m["segment"]["footpath_side"]
            assert all(abs(b - a) < 1e-3 for a, b in sidewalks[fp_side]), cs["s"]
    assert all(v > 0 for v in checked.values()), checked
    # kerb: curb lane edges coincide with the manifest kerb at stations where the kerb is raised
    for lane_st in m["lanes"]["stations"]:
        if lane_st["footpath"] and lane_st["kerb"]["height_m"] > 0:
            side = m["segment"]["footpath_side"]
            curb = [(a, b) for t, a, b in lane_edges(parsed, lane_st["s"])[side] if t == "curb"][0]
            assert min(abs(curb[0]), abs(curb[1])) == pytest.approx(abs(lane_st["kerb"]["t"]), abs=TOL)


def test_round_trip_entities(case):
    p, x, m = case
    objs = parse_xodr(x)["objects"]
    assert [o["entity_id"] for o in objs] == [e["id"] for e in m["entities"]] == [f.id for f in p.furniture]
    for o, e in zip(objs, m["entities"]):
        assert (o["s"], o["t"]) == (pytest.approx(e["s"], abs=1e-3), pytest.approx(e["t"], abs=1e-3))
        assert o["type"] in {"pole", "tree", "barrier", "obstacle"} and o["name"] == e["type"]
    assert {o["type"] for o in objs} >= {"pole"}


def test_consistency_with_manifest(case):
    _, x, m = case
    assert m["build"]["xodr_sha256"] == hashlib.sha256(x.encode()).hexdigest()
    root = ET.fromstring(x)
    assert float(root.find("road").get("length")) == pytest.approx(m["segment"]["length_s"], abs=1e-3)


def test_deterministic_across_processes(plan):
    code = ("import hashlib\nfrom pathlib import Path\nfrom hero.model.street_plan_types import StreetPlan\nfrom hero.export.xodr import build_xodr\n"
            f"p = StreetPlan.model_validate_json(Path({str(BUNDLE / 'plan.json')!r}).read_text())\n"
            "print(hashlib.sha256(build_xodr(p).encode()).hexdigest())\n")
    shas = set()
    for seed in ("0", "1"):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120,
                           env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT / "src")})
        assert r.returncode == 0, r.stderr
        shas.add(r.stdout.strip())
    assert shas == {hashlib.sha256(build_xodr(plan).encode()).hexdigest()}


@pytest.mark.skipif(not XSD.exists(), reason=f"OpenDRIVE_1.7.xsd not present at {XSD.relative_to(ROOT)} (not committed: licence) - XSD validation skipped")
def test_validates_against_xsd_when_present(plan):
    lxml = pytest.importorskip("lxml.etree", reason="lxml not installed - XSD validation skipped")
    schema = lxml.XMLSchema(lxml.parse(str(XSD)))
    doc = lxml.fromstring(build_xodr(plan).encode())
    schema.assertValid(doc)
