"""Bounded review loop: ONE call per iteration (preview + tile + panos) -> {accepted, critique,
patch_ops}; ``apply_patch`` validates and clamps every op, then re-derives tokens / tile_px /
walkability. An invalid patch is ignored (iteration marked rejected) and the loop continues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hero.scene.geo import LocalFrame, TileGeoref
from hero.scene.schema import (
    MAX_CRITIQUE_CHARS,
    MAX_PATCH_OPS,
    Action,
    CoverageState,
    LaneIndex,
    ObjectType,
    Scene,
    SizeM,
)
from hero.pipeline.assemble import DEFAULT_SIZES, _action_for, _size, normalise_coverage, recompute_derived


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewNewObject(_Strict):
    type: ObjectType
    label: str = Field(max_length=120)
    x_m: float
    length_m: float = Field(ge=0)
    lanes: list[LaneIndex] = Field(min_length=1, max_length=3)
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    size_m: SizeM | None = None


class ReviewPatchOp(_Strict):
    op: Literal["move", "remove", "add", "relabel", "set_coverage"]
    object_id: str | None = None
    x_m: float | None = None
    lane: LaneIndex | None = None
    object: ReviewNewObject | None = None
    type: ObjectType | None = None
    action: Action | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    from_m: float | None = None
    to_m: float | None = None
    state: CoverageState | None = None


class ReviewOutput(_Strict):
    """Tool schema for the review call (spec/data.md ReviewResult with a model-fillable ``add`` object)."""

    accepted: bool
    critique: str = Field(max_length=MAX_CRITIQUE_CHARS)
    patch_ops: list[ReviewPatchOp] = Field(max_length=MAX_PATCH_OPS)


class PatchError(ValueError):
    pass


def scene_summary(scene: Scene) -> str:
    lines = [f"road_width_m={scene.cross_section.road_width_m}, lanes lat_m="
             + ", ".join(f"{l.index}:{l.lat_m}" for l in scene.cross_section.lanes)]  # noqa: E741
    lines.append("footpath_coverage: " + "; ".join(
        f"{r.from_m:.0f}-{r.to_m:.0f} m {r.state.value} (conf {r.confidence:.2f})"
        for r in scene.cross_section.footpath_coverage))
    lines.append(f"buildings: {len(scene.buildings)} ({sum(1 for b in scene.buildings if b.source == 'osm')} from OSM)")
    if scene.objects:
        lines.append("objects:")
        for o in scene.objects:
            lines.append(f"  {o.id}: {o.type.value} '{o.label}' x_m={o.x_m} length_m={o.length_m} lanes={o.lanes} "
                         f"action={o.action.value} conf={o.confidence:.2f}")
    else:
        lines.append("objects: none")
    lines.append(f"tokens: {len(scene.tokens)}; walkability score {scene.walkability.score}/100")
    return "\n".join(lines)


def _find(objects: list[dict[str, Any]], object_id: str | None) -> dict[str, Any]:
    for obj in objects:
        if obj["id"] == object_id:
            return obj
    raise PatchError(f"unknown object_id {object_id!r}")


def _apply_one(scene_dict: dict[str, Any], op: ReviewPatchOp) -> None:
    objects: list[dict[str, Any]] = scene_dict["objects"]
    length = scene_dict["segment"]["length_m"]
    if op.op == "move":
        obj = _find(objects, op.object_id)
        if op.x_m is None and op.lane is None:
            raise PatchError("move needs x_m and/or lane")
        if op.x_m is not None:
            obj["x_m"] = float(op.x_m)
        if op.lane is not None:
            obj["lanes"] = [int(op.lane)]
    elif op.op == "remove":
        obj = _find(objects, op.object_id)
        objects.remove(obj)
    elif op.op == "add":
        if op.object is None:
            raise PatchError("add needs an object")
        new = op.object
        obj_type = ObjectType(new.type)
        length_m = max(new.length_m, DEFAULT_SIZES[obj_type][2] if new.length_m <= 0 else new.length_m)
        objects.append({
            "id": "o?", "type": obj_type.value, "label": new.label or obj_type.value.replace("_", " "),
            "x_m": float(new.x_m), "length_m": float(length_m), "lanes": sorted(set(int(i) for i in new.lanes)),
            "action": _action_for(obj_type, new.action).value,
            "size_m": _size(obj_type, new.size_m.model_dump() if new.size_m else None, length_m),
            "confidence": float(new.confidence), "source": None, "billboard_url": None,
            "latlon": {"lat": 0.0, "lon": 0.0}, "tile_px": (0.0, 0.0),
        })
    elif op.op == "relabel":
        obj = _find(objects, op.object_id)
        if op.type is not None:
            obj["type"] = ObjectType(op.type).value
            obj["action"] = _action_for(ObjectType(op.type), op.action).value
        elif op.action is not None:
            obj["action"] = _action_for(ObjectType(obj["type"]), op.action).value
        if op.confidence is not None:
            obj["confidence"] = float(op.confidence)
    elif op.op == "set_coverage":
        if op.from_m is None or op.to_m is None or op.state is None:
            raise PatchError("set_coverage needs from_m, to_m and state")
        lo, hi = max(0.0, min(op.from_m, op.to_m)), min(length, max(op.from_m, op.to_m))
        if hi - lo < 1.0:
            raise PatchError("set_coverage range is shorter than 1 m")
        ranges = scene_dict["cross_section"]["footpath_coverage"]
        pieces: list[dict[str, Any]] = []
        for rng in ranges:
            if rng["to_m"] <= lo or rng["from_m"] >= hi:
                pieces.append(dict(rng))
                continue
            if rng["from_m"] < lo:
                pieces.append({**rng, "to_m": lo})
            if rng["to_m"] > hi:
                pieces.append({**rng, "from_m": hi})
        pieces.append({"from_m": lo, "to_m": hi, "state": CoverageState(op.state).value, "confidence": 0.75})
        scene_dict["cross_section"]["footpath_coverage"] = normalise_coverage(pieces, length, [])
        gaps = [o for o in objects if o["type"] == ObjectType.footpath_gap.value and o.get("source") is None]
        for g in gaps:
            objects.remove(g)
        for rng in scene_dict["cross_section"]["footpath_coverage"]:
            if rng["state"] == CoverageState.missing.value:
                objects.append({
                    "id": "o?", "type": ObjectType.footpath_gap.value, "label": "footpath missing",
                    "x_m": rng["from_m"], "length_m": rng["to_m"] - rng["from_m"], "lanes": [0],
                    "action": Action.dodge.value,
                    "size_m": _size(ObjectType.footpath_gap, None, rng["to_m"] - rng["from_m"]),
                    "confidence": rng["confidence"], "source": None, "billboard_url": None,
                    "latlon": {"lat": 0.0, "lon": 0.0}, "tile_px": (0.0, 0.0),
                })


@dataclass
class PatchResult:
    scene: Scene
    applied: int
    skipped: list[str]  # "<n>: <reason>" per op that could not be applied


def apply_patch(scene: Scene, ops: list[ReviewPatchOp], frame: LocalFrame, georef: TileGeoref) -> PatchResult:
    """Apply ``ops`` in order on a copy. An op that cannot be applied (unknown id, missing fields) is
    skipped and reported; raises ``PatchError`` (nothing applied) when no op could be applied or the
    patched scene fails validation."""
    if len(ops) > MAX_PATCH_OPS:
        raise PatchError(f"more than {MAX_PATCH_OPS} ops")
    scene_dict = scene.model_dump(mode="json")
    applied = 0
    skipped: list[str] = []
    for n, op in enumerate(ops, start=1):
        try:
            _apply_one(scene_dict, op)
            applied += 1
        except PatchError as exc:
            skipped.append(f"{n} ({op.op}): {exc}")
    if applied == 0:
        raise PatchError("no applicable op: " + "; ".join(skipped))
    try:
        scene_dict = recompute_derived(scene_dict, frame, georef)
        return PatchResult(scene=Scene.model_validate(scene_dict), applied=applied, skipped=skipped)
    except (ValidationError, ValueError) as exc:
        raise PatchError(f"patched scene invalid: {str(exc)[:400]}") from exc
