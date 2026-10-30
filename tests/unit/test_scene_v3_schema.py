"""SceneV3 (the lite web scene), GapReport, TextureRefs, load_scene dispatch and RunReport.model."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hero.export import SCHEMAS_DIR
from hero.model.gap_types import GapReport
from hero.model.scene_v2 import SceneV2, load_scene
from hero.model.scene_v3 import SceneV3
from hero.model.texture_types import TextureRefs
from hero.model.types import RunReport
from tests.fixtures.make_contract_export import sample_gap_report
from tests.fixtures.street_fixtures import minimal_scene_v2_dict

ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "tests" / "fixtures" / "scene_v3" / "contract_curved.json"
PLAN = ROOT / "tests" / "fixtures" / "export" / "contract_curved" / "plan.json"


def scene_dict() -> dict:
    return json.loads(SCENE.read_text())


def test_contract_scene_validates_and_round_trips():
    sc = SceneV3.model_validate(scene_dict())
    assert sc.schema_version == 3 and sc.model.compiled_by == "hero.model.compile/2"
    assert sc.assets is not None and sc.assets.glb_url.startswith("/api/segments/contract-curved-street/model.glb?v=")
    assert SceneV3.model_validate_json(sc.model_dump_json()) == sc
    assert sc.payload_bytes() < 40_000
    assert sc.dynamic_context.excluded_count == 2 and len(sc.about.interpolated) >= 3


def test_scene_has_no_geometry_keys():
    d = scene_dict()
    for forbidden in ("path", "corridor", "objects", "tokens", "furniture", "frontage", "ground_tex", "buildings"):
        assert forbidden not in d
    with pytest.raises(ValidationError):
        SceneV3.model_validate({**d, "corridor": {}})


def test_assets_may_be_null_but_extras_are_forbidden():
    d = scene_dict()
    d["assets"] = None
    assert SceneV3.model_validate(d).assets is None
    d = scene_dict()
    d["assets"]["unknown"] = 1
    with pytest.raises(ValidationError):
        SceneV3.model_validate(d)


def test_excluded_count_must_match_ids_and_model_version_must_match():
    d = scene_dict()
    d["dynamic_context"]["excluded_count"] = 5
    with pytest.raises(ValidationError):
        SceneV3.model_validate(d)
    d = scene_dict()
    d["about"]["model_version"] = 9
    with pytest.raises(ValidationError):
        SceneV3.model_validate(d)


def test_load_scene_dispatches_1_2_3():
    assert type(load_scene(scene_dict())).__name__ == "SceneV3"
    assert type(load_scene(json.dumps(scene_dict()))).__name__ == "SceneV3"
    assert isinstance(load_scene(minimal_scene_v2_dict()), SceneV2)
    with pytest.raises(ValueError):
        load_scene({"schema_version": 4})
    bad = scene_dict()
    del bad["segment"]
    with pytest.raises(ValueError):
        load_scene(bad)


def test_gap_report_validates_and_enforces_tiling():
    plan = json.loads(PLAN.read_text())
    from hero.model.street_plan_types import StreetPlan

    gr = GapReport.model_validate(sample_gap_report(StreetPlan.model_validate(plan)))
    assert gr.blank_total() == 0 and len(gr.bins) == 12
    bad = sample_gap_report(StreetPlan.model_validate(plan))
    bad["bins"][3]["s_from"] += 0.5
    with pytest.raises(ValidationError):
        GapReport.model_validate(bad)
    bad = copy.deepcopy(sample_gap_report(StreetPlan.model_validate(plan)))
    bad["bins"][0]["ground"]["rendered"] = "blank"
    with pytest.raises(ValidationError):
        GapReport.model_validate(bad)


def test_texture_refs_use_the_t_frame_and_forbid_extras():
    refs = TextureRefs.model_validate({
        "ground": {"url": "/media/textures/x/ground_v1_aabbccdd.jpg", "size_px": [56, 120], "s_from": 0.0, "s_to": 60.0,
                   "m_per_px": 0.5, "bin_source": ["satellite_hires"] * 12},
        "frontage": {}, "tints": {"ent-1": "#aabbcc"}})
    assert refs.ground is not None and (refs.ground.t_lo, refs.ground.t_hi) == (-14.0, 14.0)
    with pytest.raises(ValidationError):
        TextureRefs.model_validate({"ground": None, "lat_lo": -14})


def test_run_report_has_model_field():
    r = RunReport(run_id="r", segment_slug="s", mode="generate")
    assert r.model is None
    r2 = RunReport(run_id="r", segment_slug="s", mode="generate", model="claude-sonnet-5-5")
    assert RunReport.model_validate_json(r2.model_dump_json()).model == "claude-sonnet-5-5"


def test_committed_scene_and_gap_schemas_are_current():
    assert json.loads((SCHEMAS_DIR / "scene_v3.schema.json").read_text()) == SceneV3.model_json_schema()
    assert json.loads((SCHEMAS_DIR / "gap_report.schema.json").read_text()) == GapReport.model_json_schema()
