"""Perception schemas + recorded extraction fixtures + ReplayLLM."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hero.fusion.schemas import CritiqueResult, OverheadExtraction, StreetExtraction
from hero.llm.client import LLMImage
from hero.llm.errors import SchemaValidationError
from hero.model.types import EntityType, FootpathState, Material, NudgeEntityOp
from tests.fixtures.replay_llm import FIXTURE_DIR, ReplayLLM, load_extraction


def _window(**over):
    w = {"image_index": 1,
         "strips": [{"kind": "footpath", "side": "left", "material": "paver", "condition": "fair",
                     "edge_a_px": [[75, 0], [76, 120], [75, 300]], "edge_b_px": [[90, 0], [91, 120], [90, 300]],
                     "confidence": 0.8, "description": "pavers"}],
         "footpath_runs": [{"side": "left", "y_from_px": 0, "y_to_px": 140, "state": "intact", "confidence": 0.8, "note": ""},
                           {"side": "left", "y_from_px": 140, "y_to_px": 210, "state": "missing", "confidence": 0.7, "note": "gap"}],
         "objects": [{"type": "tree", "box_px": [180, 90, 40, 40], "attributes": {"canopy_low": False}, "description": "t", "confidence": 0.7}],
         "notes": "n"}
    w.update(over)
    return w


def _det(**over):
    d = {"local_id": "d3", "image_index": 2, "type": "parked_vehicle", "subtype": "auto-rickshaw", "box_px": [210, 300, 180, 140],
         "touches_ground": True, "est_distance_m": 9.0, "size_m": {"w": 1.4, "h": 1.7, "d": 2.6}, "orientation": "parallel",
         "attributes": {"colour": "yellow", "blocks_footpath": True}, "description": "Yellow auto-rickshaw.", "confidence": 0.8}
    d.update(over)
    return d


# ---------------------------------------------------------------- recorded fixtures validate

@pytest.mark.parametrize("name,schema", [("overhead_curved", OverheadExtraction), ("critique_accept", CritiqueResult),
                                         ("critique_ops", CritiqueResult)])
def test_recorded_fixtures_validate_against_their_schema(name, schema):
    obj = schema.model_validate(load_extraction(name))
    assert obj is not None


def test_street_template_views_all_validate_as_street_extraction():
    tpl = json.loads((FIXTURE_DIR / "street_template.json").read_text())["by_view"]
    assert {"forward", "back", "left", "right"} <= set(tpl)
    for view, t in tpl.items():
        data = {"frames": [{"image_index": 1, **t["frame"]}],
                "detections": [{**d, "image_index": 1} for d in t["detections"]],
                "facades": [{**f, "image_index": 1} for f in t["facades"]]}
        assert StreetExtraction.model_validate(data).frames[0].image_index == 1, view


# ---------------------------------------------------------------- overhead

def test_overhead_extraction_accepts_the_spec_example():
    ex = OverheadExtraction.model_validate({"windows": [_window()]})
    w = ex.windows[0]
    assert w.strips[0].edge_a_px[1] == (76.0, 120.0)
    assert [r.state for r in w.footpath_runs] == [FootpathState.intact, FootpathState.missing]
    assert w.objects[0].type is EntityType.tree


def test_unknown_enum_words_fall_back_instead_of_failing():
    w = _window()
    w["strips"][0].update(kind="Cobbled Walkway", material="flagstone", condition="meh")
    w["footpath_runs"][0]["state"] = "kind of ok"
    w["objects"][0]["type"] = "Letter Box"
    out = OverheadExtraction.model_validate({"windows": [w]}).windows[0]
    assert out.strips[0].material is Material.unknown
    assert out.strips[0].kind.value == "unknown"
    assert out.footpath_runs[0].state is FootpathState.unknown
    assert out.objects[0].type is EntityType.other


def test_center_side_spelling_is_normalised():
    w = _window()
    w["strips"][0]["side"] = "Center"
    assert OverheadExtraction.model_validate({"windows": [w]}).windows[0].strips[0].side == "centre"


@pytest.mark.parametrize("bad_edge", [[[10, 0]], [[10, 0], [11, 100], [10, 50]]])
def test_edge_polylines_need_two_points_and_non_decreasing_y(bad_edge):
    w = _window()
    w["strips"][0]["edge_a_px"] = bad_edge
    with pytest.raises(ValidationError):
        OverheadExtraction.model_validate({"windows": [w]})


def test_footpath_runs_that_overlap_are_rejected_but_touching_runs_are_fine():
    runs = [{"side": "left", "y_from_px": 0, "y_to_px": 150, "state": "intact", "confidence": 0.8},
            {"side": "left", "y_from_px": 100, "y_to_px": 210, "state": "missing", "confidence": 0.7}]
    with pytest.raises(ValidationError, match="overlap"):
        OverheadExtraction.model_validate({"windows": [_window(footpath_runs=runs)]})
    runs[1]["y_from_px"] = 150
    OverheadExtraction.model_validate({"windows": [_window(footpath_runs=runs)]})


def test_overlap_check_is_per_side():
    runs = [{"side": "left", "y_from_px": 0, "y_to_px": 150, "state": "intact", "confidence": 0.8},
            {"side": "right", "y_from_px": 0, "y_to_px": 150, "state": "missing", "confidence": 0.7}]
    OverheadExtraction.model_validate({"windows": [_window(footpath_runs=runs)]})


def test_inverted_run_and_bad_confidence_are_rejected():
    with pytest.raises(ValidationError):
        OverheadExtraction.model_validate({"windows": [_window(footpath_runs=[
            {"side": "left", "y_from_px": 50, "y_to_px": 50, "state": "intact", "confidence": 0.8}])]})
    w = _window()
    w["strips"][0]["confidence"] = 1.4
    with pytest.raises(ValidationError):
        OverheadExtraction.model_validate({"windows": [w]})


def test_more_than_four_windows_or_index_zero_is_rejected():
    with pytest.raises(ValidationError):
        OverheadExtraction.model_validate({"windows": [_window(image_index=i) for i in range(1, 6)]})
    with pytest.raises(ValidationError):
        OverheadExtraction.model_validate({"windows": [_window(image_index=0)]})


def test_overhead_notes_are_truncated_to_300_chars():
    assert len(OverheadExtraction.model_validate({"windows": [_window(notes="x" * 900)]}).windows[0].notes) == 300


# ---------------------------------------------------------------- street

def test_street_extraction_accepts_the_spec_example_and_rounds_float_boxes():
    ex = StreetExtraction.model_validate({
        "frames": [{"image_index": 1, "visibility_ok": True, "footpath_visible": True, "footpath_side": "left",
                    "footpath_state": "broken", "notes": "n"}],
        "detections": [_det(box_px=[210.4, 299.6, 180.0, 140.2])],
        "facades": [{"image_index": 3, "box_px": [0, 120, 400, 300], "side": "left", "colour_hex": "c9b79c", "storeys": 3,
                     "use": "shops", "description": "d", "confidence": 0.7}],
        "notes": "ok"})
    assert ex.detections[0].box_px == (210, 300, 180, 140)
    assert ex.facades[0].colour_hex == "#c9b79c"
    assert ex.frames[0].footpath_state is FootpathState.broken


def test_detection_errors_are_rejected():
    for bad in ({"confidence": 1.2}, {"image_index": 0}, {"box_px": [10, 10, 0, 5]}, {"box_px": [-5, 10, 5, 5]},
                {"est_distance_m": -3}, {"size_m": {"w": 0, "h": 1, "d": 1}}, {"local_id": ""}):
        with pytest.raises(ValidationError):
            StreetExtraction.model_validate({"detections": [_det(**bad)]})


def test_unknown_detection_type_and_orientation_degrade_gracefully():
    d = StreetExtraction.model_validate({"detections": [_det(type="Hand Cart", orientation="diagonal")]}).detections[0]
    assert d.type is EntityType.other and d.orientation == "unknown"


def test_empty_street_extraction_is_valid():
    ex = StreetExtraction.model_validate({})
    assert ex.frames == [] and ex.detections == [] and ex.facades == []


def test_bad_footpath_side_in_a_frame_is_rejected():
    with pytest.raises(ValidationError):
        StreetExtraction.model_validate({"frames": [{"image_index": 1, "footpath_side": "middle"}]})


# ---------------------------------------------------------------- critique

def test_critique_result_parses_each_op_form():
    ops = [{"op": "flag_entity", "entity_id": "ent-1", "reason": "dup"},
           {"op": "nudge_entity", "entity_id": "ent-1", "ds_m": 1, "dt_m": -1, "reason": "x"},
           {"op": "set_footpath_state", "side": "left", "s_from": 0, "s_to": 10, "state": "missing", "reason": "x"},
           {"op": "nudge_edge", "kind": "footpath", "side": "left", "s_from": 0, "s_to": 10, "dt_m": 0.5, "reason": "x"}]
    res = CritiqueResult.model_validate({"accepted": False, "critique": "c", "ops": ops})
    assert [o.op for o in res.ops] == ["flag_entity", "nudge_entity", "set_footpath_state", "nudge_edge"]


def test_critique_truncates_text_and_caps_ops_at_eight_but_rejects_unknown_ops():
    op = {"op": "flag_entity", "entity_id": "ent-1", "reason": "r"}
    res = CritiqueResult.model_validate({"accepted": False, "critique": "y" * 5000, "ops": [op] * 12})
    assert len(res.critique) == 1200 and len(res.ops) == 8
    with pytest.raises(ValidationError):
        CritiqueResult.model_validate({"accepted": False, "ops": [{"op": "delete_entity", "entity_id": "e"}]})
    with pytest.raises(ValidationError):
        CritiqueResult.model_validate({"critique": "no verdict"})


def test_out_of_bound_nudges_still_parse_so_apply_critique_can_skip_and_list_them():
    res = CritiqueResult.model_validate({"accepted": False, "ops": [
        {"op": "nudge_entity", "entity_id": "ent-1", "ds_m": 9.0, "dt_m": 0.0}]})
    assert isinstance(res.ops[0], NudgeEntityOp) and res.ops[0].ds_m == 9.0


# ---------------------------------------------------------------- ReplayLLM

def _img(view="forward", n=1):
    cap = f"Image {n} of 8 | observation obs-{n} | view: {view} | camera station s=10.0 m"
    return LLMImage(data=b"x", media_type="image/jpeg", caption=cap, width_px=640, height_px=640)


def _call(llm, node_prompt, schema, images, **kw):
    return llm.vision_json(run_id="run-1", step="s", images=images, prompt=node_prompt, schema=schema, model="m",
                           prompt_name="p", **kw)


def test_replay_street_answers_per_view_with_caption_order_indices_and_unique_ids():
    llm = ReplayLLM()
    imgs = [_img("forward", 1), _img("right", 2), _img("back", 3)]
    obj, usage = _call(llm, "<node:streetlevel>\nhi", StreetExtraction, imgs)
    assert [f.image_index for f in obj.frames] == [1, 2, 3]
    ids = [d.local_id for d in obj.detections]
    assert len(ids) == len(set(ids)) and {d.image_index for d in obj.detections} == {1, 2, 3}
    assert usage.cost_usd == 0.0 and usage.calls == 1 and usage.input_tokens > 0
    # local ids repeat across calls on purpose (collision test for merge)
    obj2, _ = _call(llm, "<node:streetlevel>\nhi", StreetExtraction, imgs[:1])
    assert obj2.detections[0].local_id == "d1"


def test_replay_records_calls_by_node_and_overhead_cycles_windows():
    llm = ReplayLLM()
    obj, _ = _call(llm, "<node:overhead>\nx", OverheadExtraction, [_img(n=i) for i in range(1, 4)], structured_mode="auto_tool")
    assert [w.image_index for w in obj.windows] == [1, 2, 3]
    _call(llm, "<node:critique>\nx", CritiqueResult, [_img()])
    assert llm.call_count == 2 and len(llm.calls_for("overhead")) == 1
    rec = llm.calls_for("overhead")[0]
    assert rec.n_images == 3 and rec.structured_mode == "auto_tool" and rec.schema_name == "OverheadExtraction"


def test_replay_sequence_errors_and_invalid_answers():
    llm = ReplayLLM(street=[{"detections": []}, RuntimeError("boom"), {"detections": [{"local_id": "x"}]}])
    assert _call(llm, "<node:streetlevel>\n", StreetExtraction, [_img()])[0].detections == []
    with pytest.raises(RuntimeError):
        _call(llm, "<node:streetlevel>\n", StreetExtraction, [_img()])
    with pytest.raises(SchemaValidationError):
        _call(llm, "<node:streetlevel>\n", StreetExtraction, [_img()])
    with pytest.raises(AssertionError, match="canned"):
        _call(llm, "<node:streetlevel>\n", StreetExtraction, [_img()])
    assert llm.call_count == 4  # failed calls are still recorded


def test_replay_requires_a_known_node_tag_and_supports_cost_and_callables():
    llm = ReplayLLM(usage_cost_usd=0.05, critique=lambda images, prompt: {"accepted": True, "ops": []})
    with pytest.raises(ValueError, match="node"):
        _call(llm, "no tag here", CritiqueResult, [_img()])
    obj, usage = _call(llm, "\n<node:critique>\nx", CritiqueResult, [_img()])
    assert obj.accepted and usage.cost_usd == 0.05
