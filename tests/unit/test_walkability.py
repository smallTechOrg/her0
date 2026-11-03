"""compute_walkability: deterministic formula from spec/data.md."""

from __future__ import annotations

import copy

from hero.scene.schema import Scene, Walkability, load_example_scene
from hero.scene.walkability import compute_walkability


def _scene_with(coverage: list[dict], objects: list[dict]) -> Scene:
    data = load_example_scene().model_dump(mode="json")
    data["cross_section"]["footpath_coverage"] = coverage
    data["objects"] = objects
    data["tokens"] = []
    return Scene.model_validate(data)


def _obj(i: int, type_: str, action: str, x_m: float) -> dict:
    base = copy.deepcopy(load_example_scene().model_dump(mode="json")["objects"][2])
    base.update({"id": f"h{i}", "type": type_, "action": action, "x_m": x_m, "length_m": 1.0, "lanes": [2]})
    return base


def test_canonical_example_scores_fifty():
    scene = load_example_scene()

    result = compute_walkability(scene)

    assert result == Walkability(footpath_coverage_pct=29.5, hazard_count=4, crossing_present=True, score=50)


def test_full_intact_coverage_with_crossing_and_no_hazards_scores_one_hundred():
    scene = _scene_with(
        [{"from_m": 0.0, "to_m": 74.6, "state": "intact", "confidence": 0.9}],
        [_obj(1, "crossing", "none", 30.0)],
    )

    result = compute_walkability(scene)

    assert result.footpath_coverage_pct == 100.0
    assert result.hazard_count == 0
    assert result.crossing_present is True
    assert result.score == 100


def test_no_footpath_no_crossing_ten_hazards_scores_zero():
    scene = _scene_with(
        [{"from_m": 0.0, "to_m": 74.6, "state": "missing", "confidence": 0.9}],
        [_obj(i, "pothole", "jump", 2.0 + i * 5) for i in range(10)],
    )

    result = compute_walkability(scene)

    assert result == Walkability(footpath_coverage_pct=0.0, hazard_count=10, crossing_present=False, score=0)


def test_more_than_ten_hazards_does_not_go_negative():
    scene = _scene_with(
        [{"from_m": 0.0, "to_m": 74.6, "state": "broken", "confidence": 0.5}],
        [_obj(i, "debris", "jump", 1.0 + i * 4) for i in range(15)],
    )
    assert compute_walkability(scene).score == 0


def test_coverage_pct_is_one_decimal_and_only_intact_counts():
    scene = _scene_with(
        [
            {"from_m": 0.0, "to_m": 10.0, "state": "intact", "confidence": 0.9},
            {"from_m": 10.0, "to_m": 40.0, "state": "broken", "confidence": 0.9},
            {"from_m": 40.0, "to_m": 60.0, "state": "intact", "confidence": 0.9},
            {"from_m": 60.0, "to_m": 74.6, "state": "missing", "confidence": 0.9},
        ],
        [],
    )

    result = compute_walkability(scene)

    assert result.footpath_coverage_pct == 40.2  # 30/74.6 = 40.21...
    assert result.crossing_present is False
    assert result.score == round(0.6 * 40.2 + 20)  # 44


def test_same_scene_always_yields_same_card():
    scene = load_example_scene()
    assert compute_walkability(scene) == compute_walkability(copy.deepcopy(scene))
