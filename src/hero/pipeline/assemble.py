"""assemble_scene: merge pass A (layout), pass B (obstacles) and OSM into a validated Scene.

Nothing here is invented: coverage, buildings, trees, crossings and ramps come from pass A;
obstacles and facades from pass B; building footprints from OSM when available. This module only
clamps to the segment bounds, maps lanes/actions per spec/data.md, derives tokens from the intact
footpath + crossings rule, and computes latlon / tile_px / walkability. ``recompute_derived`` is
re-run after every review patch so derived fields never drift from the objects they describe.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image

from hero.imagery.crops import save_crop
from hero.imagery.osm import OsmGeometry
from hero.scene.geo import LocalFrame, TileGeoref, apply_frame_to_px, compute_frame_to_px
from hero.scene.schema import (
    DEFAULT_ACTIONS,
    MAX_TOKENS,
    OBJECT_OVERHANG_M,
    Action,
    CoverageState,
    LayoutGraph,
    ObjectType,
    ObstacleGraph,
    Quality,
    Scene,
)
from hero.scene.walkability import compute_walkability

ROAD_WIDTH_RANGE = (5.0, 14.0)
FOOTPATH_WIDTH_RANGE = (1.0, 4.0)
DEFAULT_FOOTPATH_WIDTH_M = 1.8
ROAD_EDGE_WIDTH_M = 1.6
ROAD_LANE_WIDTH_M = 2.0
ROAD_EDGE_INSET_M = 0.9
ROAD_LANE_OFFSET_M = 1.8
MIN_OBJECT_LENGTH_M = 0.3
MAX_OBJECT_LENGTH_M = 30.0
MIN_RANGE_LENGTH_M = 1.0
TREE_LANE_TOLERANCE_M = 0.5
TREE_LENGTH_M = 1.0
CROSSING_LENGTH_M = 3.0
RAMP_LENGTH_M = 2.0
DEDUPE_DISTANCE_M = 2.0
TOKEN_EVERY_M = 10.0
OSM_BUILDING_LATERAL_LIMIT_M = 45.0
OSM_BUILDING_ALONG_MARGIN_M = 15.0
DEFAULT_BUILDING_COLOR = "#c9b79c"
DEFAULT_SIZES: dict[ObjectType, tuple[float, float, float]] = {
    ObjectType.footpath_gap: (1.8, 0.2, 5.0),
    ObjectType.parked_vehicle: (1.8, 1.5, 4.2),
    ObjectType.pothole: (1.0, 0.1, 1.0),
    ObjectType.open_drain: (1.0, 0.1, 1.5),
    ObjectType.debris: (1.0, 0.5, 1.0),
    ObjectType.pole: (0.3, 4.0, 0.3),
    ObjectType.tree: (1.2, 5.0, 1.2),
    ObjectType.signboard: (1.0, 2.5, 0.3),
    ObjectType.low_branch: (1.8, 0.6, 1.0),
    ObjectType.crossing: (9.0, 0.0, 3.0),
    ObjectType.ramp: (1.8, 0.1, 2.0),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ------------------------------------------------------------------ cross-section


def build_lanes(road_width_m: float, footpath_width_m: float | None) -> tuple[float, list[dict[str, Any]]]:
    road = _clamp(float(road_width_m), *ROAD_WIDTH_RANGE)
    foot = _clamp(float(footpath_width_m or DEFAULT_FOOTPATH_WIDTH_M), *FOOTPATH_WIDTH_RANGE)
    footpath_lat = road / 2 + foot / 2
    road_edge_lat = road / 2 - ROAD_EDGE_INSET_M
    road_lat = road_edge_lat - ROAD_LANE_OFFSET_M
    lanes = [
        {"index": 0, "name": "footpath", "kind": "footpath", "lat_m": round(footpath_lat, 2), "width_m": round(foot, 2)},
        {"index": 1, "name": "road_edge", "kind": "road_edge", "lat_m": round(road_edge_lat, 2), "width_m": ROAD_EDGE_WIDTH_M},
        {"index": 2, "name": "road", "kind": "road", "lat_m": round(road_lat, 2), "width_m": ROAD_LANE_WIDTH_M},
    ]
    return round(road, 2), lanes


def normalise_coverage(ranges: list[dict[str, Any]], length_m: float, warnings: list[str]) -> list[dict[str, Any]]:
    """Clamp, sort, drop slivers, fill gaps (extending the previous range) and merge equal neighbours so
    the result tiles [0, length_m] exactly."""
    cleaned = []
    for rng in ranges:
        from_m = _clamp(float(rng["from_m"]), 0.0, length_m)
        to_m = _clamp(float(rng["to_m"]), 0.0, length_m)
        if to_m - from_m < MIN_RANGE_LENGTH_M:
            continue
        cleaned.append({"from_m": from_m, "to_m": to_m, "state": CoverageState(rng["state"]).value,
                        "confidence": _clamp(float(rng["confidence"]), 0.0, 1.0)})
    cleaned.sort(key=lambda r: r["from_m"])
    if not cleaned:
        warnings.append("Pass A returned no usable footpath coverage; whole segment marked broken at low confidence.")
        return [{"from_m": 0.0, "to_m": length_m, "state": "broken", "confidence": 0.3}]
    tiled: list[dict[str, Any]] = []
    cursor = 0.0
    for rng in cleaned:
        start = max(rng["from_m"], cursor)
        if rng["to_m"] <= start:
            continue
        if start > cursor:
            if tiled:
                tiled[-1]["to_m"] = start
            else:
                start = 0.0
        tiled.append({**rng, "from_m": start})
        cursor = rng["to_m"]
    tiled[0]["from_m"] = 0.0
    tiled[-1]["to_m"] = length_m
    merged: list[dict[str, Any]] = []
    for rng in tiled:
        if merged and merged[-1]["state"] == rng["state"]:
            prev = merged[-1]
            total = rng["to_m"] - prev["from_m"]
            prev["confidence"] = round((prev["confidence"] * (prev["to_m"] - prev["from_m"])
                                        + rng["confidence"] * (rng["to_m"] - rng["from_m"])) / total, 3)
            prev["to_m"] = rng["to_m"]
        else:
            merged.append(dict(rng))
    for rng in merged:
        rng["from_m"] = round(rng["from_m"], 2)
        rng["to_m"] = round(rng["to_m"], 2)
    merged[-1]["to_m"] = length_m
    return merged


# ------------------------------------------------------------------ buildings


def _osm_buildings(osm: OsmGeometry, length_m: float) -> list[dict[str, Any]]:
    out = []
    for way in osm.buildings:
        pts = way.points[:-1] if way.is_closed else way.points
        if len(pts) < 3:
            continue
        if not any(abs(lat) <= OSM_BUILDING_LATERAL_LIMIT_M and -OSM_BUILDING_ALONG_MARGIN_M <= x <= length_m + OSM_BUILDING_ALONG_MARGIN_M
                   for x, lat in pts):
            continue
        out.append({"source": "osm", "footprint": [(round(x, 2), round(lat, 2)) for x, lat in pts],
                    "height_m": round(way.height_m(), 1), "color_hex": DEFAULT_BUILDING_COLOR,
                    "facade_url": None, "confidence": 0.95})
    return out


def _vision_buildings(layout: LayoutGraph, lanes: list[dict[str, Any]], road_width_m: float, length_m: float
                      ) -> list[dict[str, Any]]:
    out = []
    footpath_outer = lanes[0]["lat_m"] + lanes[0]["width_m"] / 2 + 0.5
    opposite_outer = -(road_width_m / 2 + DEFAULT_FOOTPATH_WIDTH_M + 0.5)
    for b in layout.buildings:
        x0 = _clamp(min(b.x_from_m, b.x_to_m), -OSM_BUILDING_ALONG_MARGIN_M, length_m + OSM_BUILDING_ALONG_MARGIN_M)
        x1 = _clamp(max(b.x_from_m, b.x_to_m), -OSM_BUILDING_ALONG_MARGIN_M, length_m + OSM_BUILDING_ALONG_MARGIN_M)
        if x1 - x0 < 2.0:
            continue
        depth = _clamp(b.depth_m, 3.0, 40.0)
        if b.side == "footpath":
            near, far = footpath_outer, footpath_outer + depth
        else:
            near, far = opposite_outer, opposite_outer - depth
        out.append({"source": "vision", "footprint": [(round(x0, 2), round(near, 2)), (round(x1, 2), round(near, 2)),
                                                       (round(x1, 2), round(far, 2)), (round(x0, 2), round(far, 2))],
                    "height_m": round(_clamp(b.height_estimate_m, 2.5, 60.0), 1), "color_hex": b.color_hex,
                    "facade_url": None, "confidence": b.confidence})
    return out


def _x_range(footprint: list[tuple[float, float]]) -> tuple[float, float]:
    xs = [p[0] for p in footprint]
    return min(xs), max(xs)


def _on_footpath_side(footprint: list[tuple[float, float]]) -> bool:
    return sum(p[1] for p in footprint) / len(footprint) > 0


def _tint_buildings(buildings: list[dict[str, Any]], layout: LayoutGraph) -> None:
    """OSM footprints carry no colour: borrow the colour of the vision building that overlaps them."""
    for b in buildings:
        if b["source"] != "osm":
            continue
        x0, x1 = _x_range(b["footprint"])
        side = "footpath" if _on_footpath_side(b["footprint"]) else "opposite"
        best = None
        for vb in layout.buildings:
            if vb.side != side:
                continue
            overlap = min(x1, max(vb.x_from_m, vb.x_to_m)) - max(x0, min(vb.x_from_m, vb.x_to_m))
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, vb)
        if best is not None:
            b["color_hex"] = best[1].color_hex


# ------------------------------------------------------------------ objects


def _action_for(obj_type: ObjectType, proposed: Action | None) -> Action:
    default = DEFAULT_ACTIONS[obj_type]
    if default is Action.none or proposed is None or proposed is Action.none:
        return default
    return proposed


def _lane_for_lat(lat_m: float, lanes: list[dict[str, Any]]) -> int | None:
    """Lane whose band (half width + tolerance) contains ``lat_m``; ``None`` when it is off the course."""
    best = min(lanes, key=lambda lane: abs(lane["lat_m"] - lat_m))
    if abs(best["lat_m"] - lat_m) > best["width_m"] / 2 + TREE_LANE_TOLERANCE_M:
        return None
    return int(best["index"])


def _size(obj_type: ObjectType, size: dict[str, float] | None, length_m: float) -> dict[str, float]:
    w, h, d = DEFAULT_SIZES[obj_type]
    if size:
        w = _clamp(float(size.get("w", w)), 0.1, 12.0)
        h = _clamp(float(size.get("h", h)), 0.0, 12.0)
        d = _clamp(float(size.get("d", d)), 0.1, 30.0)
    return {"w": round(w, 2), "h": round(h, 2), "d": round(max(d, length_m if obj_type is not ObjectType.crossing else d), 2)}


def _bounded(x_m: float, length_m: float, segment_length: float) -> tuple[float, float]:
    x = _clamp(float(x_m), 0.0, max(0.0, segment_length - 0.5))
    ln = _clamp(float(length_m), MIN_OBJECT_LENGTH_M, MAX_OBJECT_LENGTH_M)
    ln = min(ln, segment_length + OBJECT_OVERHANG_M - x)
    return round(x, 2), round(ln, 2)


def layout_objects(layout: LayoutGraph, coverage: list[dict[str, Any]], lanes: list[dict[str, Any]],
                   segment_length: float, warnings: list[str]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for rng in coverage:
        if rng["state"] == CoverageState.missing.value:
            x, ln = _bounded(rng["from_m"], rng["to_m"] - rng["from_m"], segment_length)
            objects.append({"type": ObjectType.footpath_gap, "label": "footpath missing", "x_m": x, "length_m": ln,
                            "lanes": [0], "action": Action.dodge, "size_m": _size(ObjectType.footpath_gap, None, ln),
                            "confidence": rng["confidence"], "source": None, "billboard_url": None})
    skipped = 0
    for tree in layout.trees:
        lane = _lane_for_lat(tree.lat_m, lanes)
        if lane is None:
            skipped += 1
            continue
        x, ln = _bounded(tree.x_m, TREE_LENGTH_M, segment_length)
        objects.append({"type": ObjectType.tree, "label": "tree on the footpath", "x_m": x, "length_m": ln,
                        "lanes": [lane], "action": Action.dodge, "size_m": _size(ObjectType.tree, None, ln),
                        "confidence": tree.confidence, "source": None, "billboard_url": None})
    if skipped:
        warnings.append(f"{skipped} tree(s) from pass A were outside the lanes and left out of the course.")
    for crossing in layout.crossings:
        x, ln = _bounded(crossing.x_m, CROSSING_LENGTH_M, segment_length)
        objects.append({"type": ObjectType.crossing, "label": "pedestrian crossing", "x_m": x, "length_m": ln,
                        "lanes": [0, 1, 2], "action": Action.none, "size_m": _size(ObjectType.crossing, None, ln),
                        "confidence": crossing.confidence, "source": None, "billboard_url": None})
    for ramp in layout.ramps:
        x, ln = _bounded(ramp.x_m, RAMP_LENGTH_M, segment_length)
        objects.append({"type": ObjectType.ramp, "label": "kerb ramp", "x_m": x, "length_m": ln, "lanes": [0],
                        "action": Action.none, "size_m": _size(ObjectType.ramp, None, ln),
                        "confidence": ramp.confidence, "source": None, "billboard_url": None})
    return objects


def obstacle_objects(obstacles: ObstacleGraph, panos: list[Any], segment_length: float, run_id: str,
                     crops_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    pano_images: dict[int, Image.Image] = {}
    for n, det in enumerate(obstacles.objects, start=1):
        x, ln = _bounded(det.x_m, det.length_m, segment_length)
        obj_type = ObjectType(det.type)
        entry: dict[str, Any] = {
            "type": obj_type, "label": det.label[:120] or obj_type.value.replace("_", " "), "x_m": x, "length_m": ln,
            "lanes": sorted(set(int(i) for i in det.lanes)), "action": _action_for(obj_type, det.action),
            "size_m": _size(obj_type, det.size_m.model_dump(), ln), "confidence": det.confidence,
            "source": None, "billboard_url": None, "_order": n,
        }
        if 0 <= det.pano_index < len(panos):
            pano = panos[det.pano_index]
            entry["source"] = {"image_url": pano.url, "crop_px": tuple(int(v) for v in det.crop_px)}
            try:
                image = pano_images.get(det.pano_index)
                if image is None:
                    image = Image.open(pano.path).convert("RGB")
                    pano_images[det.pano_index] = image
                dest = save_crop(image, entry["source"]["crop_px"], crops_dir / f"b{n}.png")
                if dest is not None:
                    entry["billboard_url"] = f"/media/crops/{run_id}/{dest.name}"
            except OSError as exc:
                warnings.append(f"Could not cut a billboard for detection {n}: {exc}")
        objects.append(entry)
    return objects


def facade_urls(obstacles: ObstacleGraph | None, buildings: list[dict[str, Any]], panos: list[Any], run_id: str,
                crops_dir: Path) -> None:
    if obstacles is None:
        return
    pano_images: dict[int, Image.Image] = {}
    for f_index, facade in enumerate(obstacles.facades, start=1):
        if not (0 <= facade.pano_index < len(panos)):
            continue
        fx0, fx1 = min(facade.x_from_m, facade.x_to_m), max(facade.x_from_m, facade.x_to_m)
        best = None
        for b in buildings:
            if not _on_footpath_side(b["footprint"]) or b.get("facade_url"):
                continue
            bx0, bx1 = _x_range(b["footprint"])
            overlap = min(bx1, fx1) - max(bx0, fx0)
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, b)
        if best is None:
            continue
        try:
            image = pano_images.get(facade.pano_index)
            if image is None:
                image = Image.open(panos[facade.pano_index].path).convert("RGB")
                pano_images[facade.pano_index] = image
            dest = save_crop(image, tuple(int(v) for v in facade.crop_px), crops_dir / f"facade{f_index}.png")
        except OSError:
            continue
        if dest is not None:
            best[1]["facade_url"] = f"/media/crops/{run_id}/{dest.name}"
            if best[1]["source"] == "osm" and best[1]["color_hex"] == DEFAULT_BUILDING_COLOR:
                best[1]["color_hex"] = facade.color_hex


def dedupe_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two detections of the same type within 2 m sharing a lane are one physical object (the three
    panoramas overlap); keep the more confident one, inheriting the other's billboard if it has none."""
    kept: list[dict[str, Any]] = []
    for obj in sorted(objects, key=lambda o: -o["confidence"]):
        duplicate = None
        for other in kept:
            if other["type"] is not obj["type"] or not set(other["lanes"]) & set(obj["lanes"]):
                continue
            gap = max(obj["x_m"], other["x_m"]) - min(obj["x_m"] + obj["length_m"], other["x_m"] + other["length_m"])
            if gap <= DEDUPE_DISTANCE_M:
                duplicate = other
                break
        if duplicate is None:
            kept.append(obj)
        elif not duplicate.get("billboard_url") and obj.get("billboard_url"):
            duplicate["billboard_url"] = obj["billboard_url"]
            duplicate["source"] = obj["source"]
    return kept


# ------------------------------------------------------------------ derived fields


def _place(frame: LocalFrame, affine, x_m: float, lat_m: float) -> tuple[dict[str, float], tuple[float, float]]:
    point = frame.to_latlon(x_m, lat_m)
    px, py = apply_frame_to_px(affine, x_m, lat_m)
    return {"lat": round(point.lat, 7), "lon": round(point.lon, 7)}, (round(px, 1), round(py, 1))


def build_tokens(objects: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hazards_lane0 = [(o["x_m"], o["x_m"] + max(o["length_m"], 0.5)) for o in objects
                     if Action(o["action"]) is not Action.none and 0 in o["lanes"]]
    tokens: list[dict[str, Any]] = []
    for rng in coverage:
        if rng["state"] != CoverageState.intact.value:
            continue
        n_intervals = int(math.floor((rng["to_m"] - rng["from_m"]) / TOKEN_EVERY_M))
        for i in range(n_intervals):
            x = rng["from_m"] + TOKEN_EVERY_M * (i + 0.5)
            if any(a - 0.5 <= x <= b + 0.5 for a, b in hazards_lane0):
                continue
            tokens.append({"x_m": round(x, 2), "lane": 0, "reason": "intact_footpath"})
    for o in objects:
        if ObjectType(o["type"]) in (ObjectType.crossing, ObjectType.ramp):
            tokens.append({"x_m": o["x_m"], "lane": 1, "reason": ObjectType(o["type"]).value})
    tokens.sort(key=lambda t: t["x_m"])
    return tokens[:MAX_TOKENS]


def recompute_derived(scene_dict: dict[str, Any], frame: LocalFrame, georef: TileGeoref) -> dict[str, Any]:
    """Re-derive ids, latlon/tile_px, tokens, walkability and confidence_overall from the current
    objects + coverage (called after assembly and after every review patch)."""
    affine = compute_frame_to_px(frame, georef)
    lanes = scene_dict["cross_section"]["lanes"]
    coverage = scene_dict["cross_section"]["footpath_coverage"]
    length = scene_dict["segment"]["length_m"]
    objects = []
    for obj in scene_dict["objects"]:
        x, ln = _bounded(obj["x_m"], obj["length_m"], length)
        obj = {**obj, "x_m": x, "length_m": ln, "lanes": sorted(set(int(i) for i in obj["lanes"])) or [0]}
        lat_m = sum(lanes[i]["lat_m"] for i in obj["lanes"]) / len(obj["lanes"])
        obj["latlon"], obj["tile_px"] = _place(frame, affine, x, lat_m)
        objects.append(obj)
    objects.sort(key=lambda o: (o["x_m"], str(o["type"])))
    for i, obj in enumerate(objects, start=1):
        obj["id"] = f"o{i}"
        obj.pop("_order", None)
    tokens = []
    for i, tok in enumerate(build_tokens(objects, coverage), start=1):
        lat_m = lanes[tok["lane"]]["lat_m"]
        latlon, tile_px = _place(frame, affine, tok["x_m"], lat_m)
        tokens.append({"id": f"t{i}", **tok, "latlon": latlon, "tile_px": tile_px})
    for i, b in enumerate(scene_dict["buildings"], start=1):
        b["id"] = f"b{i}"
    scene_dict["objects"] = objects
    scene_dict["tokens"] = tokens
    scene_dict["ground"]["frame_to_px"] = affine.model_dump()
    confidences = [o["confidence"] for o in objects] + [r["confidence"] for r in coverage]
    scene_dict["generation"]["confidence_overall"] = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    scene = Scene.model_validate(scene_dict)
    scene_dict["walkability"] = compute_walkability(scene).model_dump()
    return scene_dict


# ------------------------------------------------------------------ entry point


def assemble_scene(*, segment: Any, frame: LocalFrame, tile: Any, osm: OsmGeometry | None, layout: LayoutGraph,
                   obstacles: ObstacleGraph | None, panos: list[Any], run_id: str, quality: Quality,
                   models: dict[str, str], warnings: list[str], crops_dir: Path) -> Scene:
    length = round(frame.length_m, 2)
    georef = tile.georef()
    road_width, lanes = build_lanes(layout.road_width_m, layout.footpath_width_m)
    coverage = normalise_coverage([c.model_dump() for c in layout.footpath_coverage], length, warnings)

    buildings = _osm_buildings(osm, length) if osm is not None else []
    if buildings:
        _tint_buildings(buildings, layout)
    else:
        if osm is not None:
            warnings.append("OSM returned no building footprints near the segment; using pass A building boxes.")
        buildings = _vision_buildings(layout, lanes, road_width, length)
    facade_urls(obstacles, buildings, panos, run_id, crops_dir)

    objects = layout_objects(layout, coverage, lanes, length, warnings)
    if obstacles is not None:
        objects += obstacle_objects(obstacles, panos, length, run_id, crops_dir, warnings)
    objects = dedupe_objects(objects)
    for obj in objects:
        obj["type"] = ObjectType(obj["type"]).value
        obj["action"] = Action(obj["action"]).value

    scene_dict: dict[str, Any] = {
        "schema_version": 1,
        "segment": {
            "slug": segment.slug, "name": segment.name, "city": segment.city,
            "start": segment.start.model_dump(), "end": segment.end.model_dump(),
            "length_m": length, "heading_deg": round(frame.heading_deg % 360.0, 2),
            "footpath_side": segment.footpath_side.value,
        },
        "ground": {
            "tile_url": tile.url, "size_px": list(tile.size_px), "zoom": tile.zoom, "scale": tile.scale,
            "center": {"lat": tile.center_lat, "lon": tile.center_lon}, "meters_per_px": round(tile.meters_per_px, 5),
            "frame_to_px": compute_frame_to_px(frame, georef).model_dump(),
        },
        "cross_section": {"road_width_m": road_width, "lanes": lanes, "footpath_coverage": coverage},
        "buildings": buildings,
        "objects": objects,
        "tokens": [],
        "walkability": {"footpath_coverage_pct": 0.0, "hazard_count": 0, "crossing_present": False, "score": 0},
        "generation": {"run_id": run_id, "quality": quality.value, "confidence_overall": 0.0, "review_iterations": 0,
                       "models": models, "warnings": list(warnings)},
    }
    scene_dict = recompute_derived(scene_dict, frame, georef)
    return Scene.model_validate(scene_dict)
