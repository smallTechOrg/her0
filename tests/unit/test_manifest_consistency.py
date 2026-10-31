"""The manifest's colliders / tokens must agree with the scene-level walkability (spec/data.md -> walkability; r2: the lite
scene keeps the counts, the manifest keeps the objects) and with the plan they were built from."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hero.export import load_gameplay
from hero.export.manifest import build_manifest
from hero.export.xodr import build_xodr
from hero.model.scene_v3 import SceneV3
from hero.model.street_plan_types import StreetPlan
from tests.fixtures import make_contract_export as mk

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tests" / "fixtures" / "export" / "contract_curved"


@pytest.fixture(scope="module")
def plan() -> StreetPlan:
    return StreetPlan.model_validate_json((BUNDLE / "plan.json").read_text())


@pytest.fixture(scope="module")
def scene() -> SceneV3:
    return SceneV3.model_validate_json((ROOT / "tests" / "fixtures" / "scene_v3" / "contract_curved.json").read_text())


@pytest.fixture(scope="module")
def manifest(plan) -> dict:
    p, fr = mk.build_plan(json.loads(mk.SRC_SCENE.read_text()))
    mesh = mk.build_mesh(p, fr, load_gameplay())[0]
    return build_manifest(plan, mesh, glb_sha256="0" * 64, glb_bytes=1, xodr_sha256=hashlib.sha256(build_xodr(plan).encode()).hexdigest())


def test_hazard_count_equals_actionable_colliders(manifest, scene):
    hazards = [c for c in manifest["colliders"] if c["action"] != "none"]
    assert len(hazards) == scene.walkability.hazard_count
    # NB: the stand-in scene's walkability.crossing_present (False) disagrees with its own crossing object; not asserted here
    # every collider has a lane and a position inside the street
    assert all(0 <= c["s"] <= manifest["segment"]["length_s"] and c["lanes"] for c in manifest["colliders"])


def test_tokens_follow_the_rules(manifest, scene):
    gp = manifest["gameplay"]
    toks = manifest["tokens"]
    cap = gp["scoring"]["tokens"]["max"] if "tokens" in gp.get("scoring", {}) else 12
    assert 0 < len(toks) <= cap
    special = [t for t in toks if t["reason"] in ("crossing", "ramp")]
    # one crossing/ramp token per crossing/ramp collider
    assert len(special) == len([c for c in manifest["colliders"] if c["type"] in ("crossing", "ramp")])
    intact = [t for t in toks if t["reason"] == "intact_footpath"]
    intact_m = sum(r.length_m for r in scene.cross_section.footpath_coverage if r.state.value == "intact")
    assert 0 < len(intact) <= int(intact_m // 10) + 1
    assert all(t["lane"] == 0 for t in intact)
    # no token sits inside a hazard footprint on its lane
    for t in intact:
        for c in manifest["colliders"]:
            if c["action"] != "none" and t["lane"] in c["lanes"]:
                assert not (c["s"] <= t["s"] <= c["s"] + c["length_m"]), (t["id"], c["id"])


def test_intact_coverage_matches_lane_profile(manifest, scene):
    """The manifest's modelled-side profile and the scene's footpath_coverage intervals describe the same intact length."""
    st = manifest["lanes"]["stations"]
    intact = sum(b["s"] - a["s"] for a, b in zip(st, st[1:]) if a["footpath"] and a["footpath"]["state"] == "intact")
    scene_intact = sum(r.to_m - r.from_m for r in scene.cross_section.footpath_coverage if r.state.value == "intact")
    assert intact == pytest.approx(scene_intact, abs=0.01)
    # a footpath interval the scene calls missing has no walkable surface in the manifest
    for r in scene.cross_section.footpath_coverage:
        if r.state.value == "missing":
            assert all(a["footpath"] is None for a in st if r.from_m <= a["s"] < r.to_m)


def test_manifest_equals_plan_counts(manifest, plan):
    assert len(manifest["colliders"]) == len(plan.objects) and len(manifest["tokens"]) == len(plan.tokens)
    assert len(manifest["entities"]) == len(plan.furniture)
    assert sum(1 for c in manifest["colliders"] if c["entity_id"] is None) == sum(1 for o in plan.objects if o.entity_id is None)
    # dynamic entities are in neither list
    dyn = set(plan.dynamic_context.entity_ids)
    assert not dyn & {e["id"] for e in manifest["entities"]} and not dyn & {c["entity_id"] for c in manifest["colliders"]}


def test_inconsistency_is_detected(manifest, scene):
    broken = [c for c in manifest["colliders"] if c["id"] != manifest["colliders"][0]["id"] and c["action"] != "none"]
    assert len(broken) != scene.walkability.hazard_count
