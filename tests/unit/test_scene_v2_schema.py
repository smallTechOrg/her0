"""hero.model.scene_v2: Scene v2 contract + v1/v2 discrimination via load_scene."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from hero.model.scene_v2 import SceneV2, load_scene
from hero.scene.schema import Scene, load_example_scene
from tests.fixtures.street_fixtures import minimal_scene_v2_dict


def test_minimal_v2_scene_validates_and_round_trips():
    data = minimal_scene_v2_dict()
    scene = SceneV2.model_validate(data)
    assert scene.path.samples[-1].s == 20.0 and len(scene.corridor.stations) == len(scene.path.samples)
    assert scene.tokens_in_scene == 1 and len(scene.hazards()) == 1
    assert SceneV2.model_validate_json(scene.model_dump_json()) == scene
    assert json.loads(scene.to_json())["schema_version"] == 2


def test_load_scene_discriminates_by_schema_version():
    assert isinstance(load_scene(minimal_scene_v2_dict()), SceneV2)
    v1 = load_example_scene().model_dump(mode="json")
    assert isinstance(load_scene(v1), Scene)
    assert isinstance(load_scene(json.dumps(minimal_scene_v2_dict())), SceneV2)


@pytest.mark.parametrize("bad", [{}, {"schema_version": 3}, {"schema_version": "2"}])
def test_load_scene_rejects_unknown_versions(bad):
    with pytest.raises(ValueError):
        load_scene(bad)


def test_load_scene_rejects_non_objects_and_invalid_scenes():
    with pytest.raises(ValueError):
        load_scene([1, 2])  # type: ignore[arg-type]
    broken = minimal_scene_v2_dict()
    del broken["about"]
    with pytest.raises(ValidationError):
        load_scene(broken)


def test_unknown_keys_are_rejected_at_every_level():
    for path in (("surprise",), ("objects", 0, "surprise"), ("about", "surprise"), ("corridor", "stations", 0, "surprise")):
        data = minimal_scene_v2_dict()
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = 1
        with pytest.raises(ValidationError):
            SceneV2.model_validate(data)


def test_tile_url_may_be_null_but_stations_must_align_with_path():
    data = minimal_scene_v2_dict()
    assert data["ground"]["tile_url"] is None
    SceneV2.model_validate(data)
    data["corridor"]["stations"].pop()
    with pytest.raises(ValidationError, match="index-aligned"):
        SceneV2.model_validate(data)
    data = minimal_scene_v2_dict()
    data["corridor"]["stations"][2]["s"] += 0.5
    with pytest.raises(ValidationError, match="does not match"):
        SceneV2.model_validate(data)


def test_path_samples_must_increase_and_span_the_segment():
    data = minimal_scene_v2_dict()
    data["path"]["samples"][3]["s"] = data["path"]["samples"][2]["s"]
    with pytest.raises(ValidationError):
        SceneV2.model_validate(data)
    data = minimal_scene_v2_dict()
    data["path"]["length_m"] = 25.0
    with pytest.raises(ValidationError):
        SceneV2.model_validate(data)


def test_object_rules_type_lanes_scenery_and_bounds():
    base = minimal_scene_v2_dict()

    bad_type = copy.deepcopy(base)
    bad_type["objects"][0]["type"] = "spaceship"
    with pytest.raises(ValidationError, match="unknown object type"):
        SceneV2.model_validate(bad_type)

    no_lane = copy.deepcopy(base)
    no_lane["objects"][0]["lanes"] = []
    with pytest.raises(ValidationError, match="lane"):
        SceneV2.model_validate(no_lane)

    scenery = copy.deepcopy(base)
    scenery["objects"][0].update(lanes=[], scenery=True, action="none")
    assert SceneV2.model_validate(scenery).hazards() == []

    scenery_hazard = copy.deepcopy(base)
    scenery_hazard["objects"][0].update(lanes=[], scenery=True)
    with pytest.raises(ValidationError, match="scenery"):
        SceneV2.model_validate(scenery_hazard)

    past_end = copy.deepcopy(base)
    past_end["objects"][0]["x_m"] = 20.0
    with pytest.raises(ValidationError, match="x_m"):
        SceneV2.model_validate(past_end)

    gap = copy.deepcopy(base)
    gap["objects"][0].update(id="gap-pi-0001", entity_id=None, type="footpath_gap")
    assert SceneV2.model_validate(gap).objects[0].entity_id is None


def test_catalogue_types_beyond_v1_are_accepted():
    data = minimal_scene_v2_dict()
    data["objects"][0]["type"] = "bollard"
    assert SceneV2.model_validate(data).objects[0].type == "bollard"


def test_cross_section_lane_separation_and_extended_footpath_states():
    data = minimal_scene_v2_dict()
    data["cross_section"]["lanes"][1]["lat_m"] = 5.0
    with pytest.raises(ValidationError, match="decrease"):
        SceneV2.model_validate(data)
    data = minimal_scene_v2_dict()
    data["cross_section"]["footpath_coverage"] = [
        {"from_m": 0.0, "to_m": 8.0, "state": "encroached", "confidence": 0.6},
        {"from_m": 8.0, "to_m": 20.0, "state": "blocked", "confidence": 0.5}]
    assert SceneV2.model_validate(data).cross_section.footpath_coverage[1].state == "blocked"
    data["cross_section"]["footpath_coverage"][1]["to_m"] = 18.0
    with pytest.raises(ValidationError, match="end at segment length"):
        SceneV2.model_validate(data)


def test_duplicate_object_ids_and_too_many_tokens_are_rejected():
    data = minimal_scene_v2_dict()
    data["objects"].append(copy.deepcopy(data["objects"][0]))
    with pytest.raises(ValidationError, match="unique"):
        SceneV2.model_validate(data)
    data = minimal_scene_v2_dict()
    data["tokens"] = [{**data["tokens"][0], "id": f"t{i}"} for i in range(13)]
    with pytest.raises(ValidationError):
        SceneV2.model_validate(data)


def test_about_level_must_be_l0_to_l3():
    data = minimal_scene_v2_dict()
    data["about"]["fidelity"]["level"] = "L4"
    with pytest.raises(ValidationError):
        SceneV2.model_validate(data)
