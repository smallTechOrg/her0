"""Scene JSON contract (spec/data.md): the canonical example validates; rule violations do not."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from hero.scene import schema
from hero.scene.schema import (
    DEFAULT_ACTIONS,
    LOW_CONFIDENCE_THRESHOLD,
    Action,
    LayoutGraph,
    ObjectType,
    ObstacleGraph,
    PatchOp,
    ReviewResult,
    Scene,
    SceneObject,
    example_scene_path,
    load_example_scene,
)


@pytest.fixture()
def example() -> dict:
    return json.loads(example_scene_path().read_text())


def test_canonical_example_file_validates_and_round_trips(example):
    scene = Scene.model_validate(example)

    assert scene.schema_version == 1
    assert scene.segment.slug == "100-feet-road-indiranagar-w"
    assert [o.id for o in scene.objects] == ["o1", "o2", "o3", "o4", "o5"]
    assert scene.model_dump(mode="json") == Scene.model_validate(scene.model_dump(mode="json")).model_dump(mode="json")


def test_load_example_scene_returns_typed_scene():
    scene = load_example_scene()
    assert isinstance(scene, Scene)
    assert scene.walkability.score == 50


def test_hazards_exclude_action_none_and_low_confidence_flagged():
    scene = load_example_scene()

    hazards = scene.hazards()

    assert [h.id for h in hazards] == ["o1", "o2", "o3", "o4"]
    assert LOW_CONFIDENCE_THRESHOLD == 0.6
    assert [o.id for o in scene.objects if o.is_low_confidence] == ["o4"]
    assert scene.tokens_in_scene == 2


def test_default_action_mapping_covers_every_object_type():
    assert set(DEFAULT_ACTIONS) == set(ObjectType)
    assert DEFAULT_ACTIONS[ObjectType.pothole] == Action.jump
    assert DEFAULT_ACTIONS[ObjectType.low_branch] == Action.slide
    assert DEFAULT_ACTIONS[ObjectType.crossing] == Action.none
    assert DEFAULT_ACTIONS[ObjectType.parked_vehicle] == Action.dodge


def test_rejects_wrong_schema_version(example):
    example["schema_version"] = 2
    with pytest.raises(ValidationError):
        Scene.model_validate(example)


def test_rejects_unknown_object_type_and_action(example):
    bad = copy.deepcopy(example)
    bad["objects"][0]["type"] = "ufo"
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)
    bad = copy.deepcopy(example)
    bad["objects"][0]["action"] = "fly"
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)


def test_rejects_empty_or_out_of_range_lanes(example):
    bad = copy.deepcopy(example)
    bad["objects"][0]["lanes"] = []
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)
    bad = copy.deepcopy(example)
    bad["objects"][0]["lanes"] = [0, 3]
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)


def test_rejects_object_outside_segment_length(example):
    bad = copy.deepcopy(example)
    bad["objects"][2]["x_m"] = 74.6  # must be < length_m
    with pytest.raises(ValidationError, match="x_m"):
        Scene.model_validate(bad)
    bad = copy.deepcopy(example)
    bad["objects"][2]["x_m"] = 72.0
    bad["objects"][2]["length_m"] = 8.0  # 80 > 74.6 + 5
    with pytest.raises(ValidationError, match="length"):
        Scene.model_validate(bad)


def test_rejects_coverage_that_does_not_tile_the_segment(example):
    gap = copy.deepcopy(example)
    gap["cross_section"]["footpath_coverage"][1]["from_m"] = 23.0
    with pytest.raises(ValidationError, match="contiguous"):
        Scene.model_validate(gap)
    short = copy.deepcopy(example)
    short["cross_section"]["footpath_coverage"][-1]["to_m"] = 70.0
    with pytest.raises(ValidationError, match="length_m"):
        Scene.model_validate(short)
    bad_state = copy.deepcopy(example)
    bad_state["cross_section"]["footpath_coverage"][0]["state"] = "perfect"
    with pytest.raises(ValidationError):
        Scene.model_validate(bad_state)


def test_rejects_lanes_not_strictly_decreasing_by_1_4m(example):
    bad = copy.deepcopy(example)
    bad["cross_section"]["lanes"][1]["lat_m"] = 4.5  # 5.4 - 4.5 = 0.9 < 1.4
    with pytest.raises(ValidationError, match="1.4"):
        Scene.model_validate(bad)


def test_rejects_confidence_outside_unit_interval_and_short_footprint(example):
    bad = copy.deepcopy(example)
    bad["objects"][0]["confidence"] = 1.2
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)
    bad = copy.deepcopy(example)
    bad["buildings"][0]["footprint"] = [[0, 7.5], [28, 7.5]]
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)


def test_rejects_duplicate_object_ids(example):
    bad = copy.deepcopy(example)
    bad["objects"][1]["id"] = "o1"
    with pytest.raises(ValidationError, match="unique"):
        Scene.model_validate(bad)


def test_rejects_more_than_twelve_tokens(example):
    bad = copy.deepcopy(example)
    token = bad["tokens"][0]
    bad["tokens"] = [dict(token, id=f"t{i}") for i in range(13)]
    with pytest.raises(ValidationError):
        Scene.model_validate(bad)


def test_nullable_fields_accept_null(example):
    example["objects"][0]["billboard_url"] = None
    example["objects"][0]["source"] = None
    example["buildings"][0]["facade_url"] = None
    scene = Scene.model_validate(example)
    assert scene.objects[0].billboard_url is None
    assert scene.buildings[0].facade_url is None


def test_scene_object_standalone_validation_is_independent_of_segment():
    obj = SceneObject.model_validate(
        {
            "id": "x", "type": "pole", "label": "pole", "x_m": 3.0, "length_m": 0.3, "lanes": [0],
            "action": "dodge", "size_m": {"w": 0.3, "h": 3.0, "d": 0.3}, "confidence": 0.9,
            "source": None, "billboard_url": None,
            "latlon": {"lat": 0.0, "lon": 0.0}, "tile_px": [1.0, 2.0],
        }
    )
    assert obj.type is ObjectType.pole
    assert obj.mean_lane_index == 0.0


def test_pass_output_graphs_validate_shapes():
    layout = LayoutGraph.model_validate(
        {
            "road_width_m": 9.0, "footpath_width_m": 1.8,
            "footpath_coverage": [{"from_m": 0, "to_m": 50, "state": "intact", "confidence": 0.8}],
            "buildings": [{"x_from_m": 0, "x_to_m": 20, "depth_m": 10, "side": "footpath",
                           "height_estimate_m": 9, "color_hex": "#aabbcc", "confidence": 0.9}],
            "trees": [{"x_m": 5, "lat_m": 6, "confidence": 0.7}],
            "crossings": [{"x_m": 40, "confidence": 0.8}], "ramps": [], "notes": "",
        }
    )
    assert layout.buildings[0].side == "footpath"
    obstacles = ObstacleGraph.model_validate(
        {
            "objects": [{"type": "pothole", "label": "pothole", "pano_index": 1, "crop_px": [1, 2, 3, 4],
                         "x_m": 40, "lanes": [2], "length_m": 1, "size_m": {"w": 1, "h": 0.1, "d": 1},
                         "action": "jump", "confidence": 0.6}],
            "facades": [], "notes": "n",
        }
    )
    assert obstacles.objects[0].action is Action.jump
    with pytest.raises(ValidationError):
        LayoutGraph.model_validate({"road_width_m": 9.0, "footpath_width_m": 1.8, "footpath_coverage": [],
                                    "buildings": [{"x_from_m": 0, "x_to_m": 1, "depth_m": 1, "side": "moon",
                                                   "height_estimate_m": 1, "color_hex": "#000000", "confidence": 1}],
                                    "trees": [], "crossings": [], "ramps": [], "notes": ""})


def test_review_result_limits_ops_and_critique_length():
    ops = [{"op": "remove", "object_id": "o1"}] * 10
    result = ReviewResult.model_validate({"accepted": False, "critique": "move o1", "patch_ops": ops})
    assert len(result.patch_ops) == 10 and isinstance(result.patch_ops[0], PatchOp)
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"accepted": False, "critique": "x", "patch_ops": ops + ops[:1]})
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"accepted": True, "critique": "y" * 1501, "patch_ops": []})
    with pytest.raises(ValidationError):
        PatchOp.model_validate({"op": "teleport", "object_id": "o1"})


def test_module_exports_scene_contract_names():
    for name in ("Scene", "SceneObject", "Token", "Building", "CrossSection", "Lane", "CoverageRange",
                 "Ground", "FrameToPx", "SegmentInfo", "Walkability", "Generation", "LatLon",
                 "ObjectType", "Action", "CoverageState", "Quality", "FootpathSide", "LaneKind"):
        assert hasattr(schema, name), name
