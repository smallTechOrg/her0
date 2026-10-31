"""hero.pipeline.assemble + review.apply_patch on synthetic pass outputs (no network, no LLM)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hero.db.repo import SegmentRecord
from hero.pipeline.assemble import assemble_scene, build_lanes, build_tokens, normalise_coverage
from hero.pipeline.review import PatchError, ReviewNewObject, ReviewPatchOp, apply_patch
from hero.pipeline.state import TileRef
from hero.scene.geo import LocalFrame
from hero.scene.schema import (
    Action,
    CoverageState,
    FootpathSide,
    LatLon,
    LayoutGraph,
    ObstacleGraph,
    Quality,
    Scene,
    SegmentStatus,
)
from datetime import UTC, datetime

START = LatLon(lat=12.97400, lon=77.64050)
END = LatLon(lat=12.97467, lon=77.64058)
LENGTH = 74.6


def _segment() -> SegmentRecord:
    now = datetime.now(UTC)
    return SegmentRecord(id="seg", slug="test-w", name="Test (west footpath)", city="Bengaluru", start=START, end=END,
                         footpath_side=FootpathSide.left, length_m=LENGTH, heading_deg=6.7, status=SegmentStatus.draft,
                         quality=None, scene=None, thumbnail_path=None, latest_run_id=None, best_score=None,
                         best_distance_m=None, created_at=now, updated_at=now)


def _tile() -> TileRef:
    return TileRef(path="/nonexistent/tile.png", url="/media/imagery/x.png", center_lat=12.974335, center_lon=77.64054,
                   zoom=19, scale=2, size_px=(1280, 1280), meters_per_px=0.1455, cache_hit=True)


def _layout(**overrides) -> LayoutGraph:
    base = dict(
        road_width_m=9.0, footpath_width_m=1.8,
        footpath_coverage=[{"from_m": 0, "to_m": 22, "state": "intact", "confidence": 0.9},
                           {"from_m": 22, "to_m": 31, "state": "missing", "confidence": 0.7},
                           {"from_m": 31, "to_m": 80, "state": "broken", "confidence": 0.6}],
        buildings=[{"x_from_m": 0, "x_to_m": 28, "depth_m": 12, "side": "footpath", "height_estimate_m": 9,
                    "color_hex": "#c9b79c", "confidence": 0.8}],
        trees=[{"x_m": 55.0, "lat_m": 5.4, "confidence": 0.7}, {"x_m": 10.0, "lat_m": 25.0, "confidence": 0.9}],
        crossings=[{"x_m": 60.0, "confidence": 0.8}], ramps=[], notes="",
    )
    base.update(overrides)
    return LayoutGraph.model_validate(base)


def _obstacles() -> ObstacleGraph:
    return ObstacleGraph.model_validate({
        "objects": [
            {"type": "parked_vehicle", "label": "car on footpath", "pano_index": 9, "crop_px": [0, 0, 10, 10],
             "x_m": 12.5, "lanes": [0, 1], "length_m": 4.2, "size_m": {"w": 1.8, "h": 1.5, "d": 4.2},
             "action": "dodge", "confidence": 0.85},
            {"type": "parked_vehicle", "label": "same car seen again", "pano_index": 9, "crop_px": [0, 0, 10, 10],
             "x_m": 13.5, "lanes": [1], "length_m": 4.0, "size_m": {"w": 1.8, "h": 1.5, "d": 4.2},
             "action": "dodge", "confidence": 0.5},
            {"type": "pothole", "label": "pothole", "pano_index": 9, "crop_px": [0, 0, 10, 10], "x_m": 120.0,
             "lanes": [2], "length_m": 1.0, "size_m": {"w": 1, "h": 0.1, "d": 1}, "action": "none",
             "confidence": 0.4},
        ],
        "facades": [], "notes": "",
    })


def _frame() -> LocalFrame:
    return LocalFrame.from_endpoints(START.lat, START.lon, END.lat, END.lon, FootpathSide.left)


def _assemble(layout=None, obstacles=None, warnings=None, tmp_path=None) -> Scene:
    return assemble_scene(
        segment=_segment(), frame=_frame(), tile=_tile(), osm=None, layout=layout or _layout(), obstacles=obstacles,
        panos=[], run_id="run-test", quality=Quality.full if obstacles else Quality.satellite_only,
        models={"vision": "m", "review": "m"}, warnings=warnings if warnings is not None else [],
        crops_dir=tmp_path or Path(tempfile.gettempdir()) / "hero-crops-unit",  # never written: no panos
    )


# ---------------------------------------------------------------- lanes / coverage


def test_lanes_follow_the_spec_formula_for_the_example_widths():
    road, lanes = build_lanes(9.0, 1.8)
    assert road == 9.0
    assert [l["lat_m"] for l in lanes] == [5.4, 3.6, 1.8]


def test_lanes_clamp_road_width_and_keep_minimum_separation():
    _, lanes = build_lanes(2.0, 0.2)
    assert lanes[0]["lat_m"] - lanes[1]["lat_m"] >= 1.4
    assert lanes[1]["lat_m"] - lanes[2]["lat_m"] >= 1.4


def test_coverage_is_clamped_gap_filled_and_merged_to_tile_the_segment():
    warnings: list[str] = []
    out = normalise_coverage([
        {"from_m": 5, "to_m": 20, "state": "intact", "confidence": 0.9},
        {"from_m": 30, "to_m": 40, "state": "intact", "confidence": 0.5},
        {"from_m": 40, "to_m": 200, "state": "broken", "confidence": 0.6},
    ], 74.6, warnings)
    assert out[0]["from_m"] == 0.0 and out[-1]["to_m"] == 74.6
    assert [r["state"] for r in out] == ["intact", "broken"]
    assert out[0]["to_m"] == 40.0  # gap filled by extending, equal neighbours merged


def test_empty_coverage_becomes_one_low_confidence_broken_range_with_a_warning():
    warnings: list[str] = []
    out = normalise_coverage([], 50.0, warnings)
    assert out == [{"from_m": 0.0, "to_m": 50.0, "state": "broken", "confidence": 0.3}]
    assert warnings


# ---------------------------------------------------------------- assembly


def test_assemble_satellite_only_scene_validates_and_derives_gap_tokens_and_walkability():
    warnings: list[str] = []
    scene = _assemble(warnings=warnings)

    assert scene.generation.quality is Quality.satellite_only
    types = [o.type.value for o in scene.objects]
    assert types.count("footpath_gap") == 1 and "crossing" in types and types.count("tree") == 1
    gap = next(o for o in scene.objects if o.type.value == "footpath_gap")
    assert (gap.x_m, gap.length_m, gap.lanes, gap.action) == (22.0, 9.0, [0], Action.dodge)
    assert any("tree" in w for w in warnings)  # the far-away tree was left out, and said so
    assert [t.reason.value for t in scene.tokens] == ["intact_footpath", "intact_footpath", "crossing"]
    assert scene.walkability.footpath_coverage_pct == pytest.approx(100 * 22 / scene.segment.length_m, abs=0.06)
    assert scene.walkability.crossing_present is True
    assert all(0 <= o.tile_px[0] <= 1280 and 0 <= o.tile_px[1] <= 1280 for o in scene.objects)
    assert len(scene.buildings) == 1 and scene.buildings[0].source == "vision"


def test_assemble_with_obstacles_dedupes_overlapping_detections_clamps_and_keeps_low_confidence():
    scene = _assemble(obstacles=_obstacles())

    vehicles = [o for o in scene.objects if o.type.value == "parked_vehicle"]
    assert len(vehicles) == 1 and vehicles[0].confidence == 0.85
    pothole = next(o for o in scene.objects if o.type.value == "pothole")
    assert pothole.x_m < LENGTH and pothole.action is Action.jump  # clamped; 'none' is not allowed for a hazard
    assert pothole.is_low_confidence
    assert [o.id for o in scene.objects] == [f"o{i}" for i in range(1, len(scene.objects) + 1)]
    assert scene.objects == sorted(scene.objects, key=lambda o: o.x_m)
    assert scene.generation.quality is Quality.full


def test_tokens_avoid_lane0_hazard_footprints_and_cap_at_twelve():
    coverage = [{"from_m": 0.0, "to_m": 200.0, "state": "intact", "confidence": 1.0}]
    objects = [{"x_m": 14.0, "length_m": 2.0, "action": "dodge", "lanes": [0], "type": "pole"}]
    tokens = build_tokens(objects, coverage)
    assert len(tokens) == 12
    assert all(not (13.5 <= t["x_m"] <= 16.5) for t in tokens)


# ---------------------------------------------------------------- review patches


def test_apply_patch_moves_removes_adds_relabels_and_recomputes_derived_fields():
    scene = _assemble(obstacles=_obstacles())
    gap = next(o for o in scene.objects if o.type.value == "footpath_gap")
    crossing = next(o for o in scene.objects if o.type.value == "crossing")
    ops = [
        ReviewPatchOp(op="move", object_id=crossing.id, x_m=65.0, lane=2),
        ReviewPatchOp(op="remove", object_id=gap.id),
        ReviewPatchOp(op="add", object=ReviewNewObject(type="pole", label="lamp post", x_m=40.0, length_m=0.3,
                                                      lanes=[0], action="dodge", confidence=0.6)),
        ReviewPatchOp(op="relabel", object_id=scene.objects[0].id, confidence=0.3),
    ]

    result = apply_patch(scene, ops, _frame(), _tile().georef())
    patched = result.scene

    assert result.applied == 4 and result.skipped == []
    types = [o.type.value for o in patched.objects]
    assert "footpath_gap" not in types and "pole" in types
    moved = next(o for o in patched.objects if o.type.value == "crossing")
    assert moved.x_m == 65.0 and moved.lanes == [2]
    assert any(t.x_m == 65.0 and t.reason.value == "crossing" for t in patched.tokens)
    assert patched.walkability.hazard_count == len(patched.hazards())
    assert [o.id for o in patched.objects] == [f"o{i}" for i in range(1, len(patched.objects) + 1)]


def test_set_coverage_rewrites_ranges_and_their_footpath_gap_objects():
    scene = _assemble()

    patched = apply_patch(scene, [ReviewPatchOp(op="set_coverage", from_m=0, to_m=74.6, state="intact")],
                          _frame(), _tile().georef()).scene

    assert [r.state for r in patched.cross_section.footpath_coverage] == [CoverageState.intact]
    assert not any(o.type.value == "footpath_gap" for o in patched.objects)
    assert patched.walkability.footpath_coverage_pct == 100.0


def test_patch_with_no_applicable_op_is_rejected_and_leaves_the_scene_untouched():
    scene = _assemble()
    before = scene.model_dump_json()

    with pytest.raises(PatchError):
        apply_patch(scene, [ReviewPatchOp(op="remove", object_id="o999")], _frame(), _tile().georef())
    with pytest.raises(PatchError):
        apply_patch(scene, [ReviewPatchOp(op="move", object_id="o1")], _frame(), _tile().georef())

    assert scene.model_dump_json() == before


def test_patch_skips_an_op_on_an_already_removed_object_but_applies_the_rest():
    scene = _assemble()
    first = scene.objects[0].id

    result = apply_patch(scene, [ReviewPatchOp(op="remove", object_id=first),
                                 ReviewPatchOp(op="relabel", object_id=first, confidence=0.1)],
                         _frame(), _tile().georef())

    assert result.applied == 1 and len(result.skipped) == 1 and "2 (relabel)" in result.skipped[0]
    assert len(result.scene.objects) == len(scene.objects) - 1
