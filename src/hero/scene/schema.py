"""Scene JSON contract (spec/data.md, ``schema_version`` 1) plus the pass-output graphs.

Shared by the pipeline (producer), the API (serialiser) and the game (consumer, via JSON).
Every numeric rule from spec/data.md is enforced here so an invalid scene never reaches disk.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
LOW_CONFIDENCE_THRESHOLD = 0.6
MAX_TOKENS = 12
MAX_PATCH_OPS = 10
MAX_CRITIQUE_CHARS = 1500
MIN_LANE_SEPARATION_M = 1.4
OBJECT_OVERHANG_M = 5.0
EXAMPLE_SCENE_PATH = Path(__file__).with_name("example_scene.json")

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
LaneIndex = Annotated[int, Field(ge=0, le=2)]


# ------------------------------------------------------------------ enums


class FootpathSide(StrEnum):
    left = "left"
    right = "right"


class SegmentStatus(StrEnum):
    draft = "draft"
    generating = "generating"
    ready = "ready"
    failed = "failed"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    failed_budget = "failed_budget"


class Quality(StrEnum):
    full = "full"
    satellite_only = "satellite_only"
    generic = "generic"


class ObjectType(StrEnum):
    footpath_gap = "footpath_gap"
    parked_vehicle = "parked_vehicle"
    pothole = "pothole"
    open_drain = "open_drain"
    debris = "debris"
    pole = "pole"
    tree = "tree"
    signboard = "signboard"
    low_branch = "low_branch"
    crossing = "crossing"
    ramp = "ramp"


class Action(StrEnum):
    jump = "jump"
    slide = "slide"
    dodge = "dodge"
    none = "none"


class LaneKind(StrEnum):
    footpath = "footpath"
    road_edge = "road_edge"
    road = "road"


class CoverageState(StrEnum):
    intact = "intact"
    broken = "broken"
    missing = "missing"


class TokenReason(StrEnum):
    intact_footpath = "intact_footpath"
    crossing = "crossing"
    ramp = "ramp"


DEFAULT_ACTIONS: dict[ObjectType, Action] = {
    ObjectType.footpath_gap: Action.dodge,
    ObjectType.parked_vehicle: Action.dodge,
    ObjectType.pole: Action.dodge,
    ObjectType.tree: Action.dodge,
    ObjectType.signboard: Action.dodge,
    ObjectType.pothole: Action.jump,
    ObjectType.open_drain: Action.jump,
    ObjectType.debris: Action.jump,
    ObjectType.low_branch: Action.slide,
    ObjectType.crossing: Action.none,
    ObjectType.ramp: Action.none,
}


# ------------------------------------------------------------------ building blocks


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LatLon(_Strict):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class SegmentInfo(_Strict):
    slug: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    city: str
    start: LatLon
    end: LatLon
    length_m: float = Field(gt=0)
    heading_deg: float = Field(ge=0.0, lt=360.0)
    footpath_side: FootpathSide


class FrameToPx(_Strict):
    """Affine ``px = origin_px + x_m * along + lat_m * across`` (spec/data.md, computed by hero.scene.geo)."""

    origin_px: tuple[float, float]
    along_px_per_m: tuple[float, float]
    across_px_per_m: tuple[float, float]


class Ground(_Strict):
    tile_url: str
    size_px: tuple[int, int]
    zoom: int = Field(ge=0, le=22)
    scale: int = Field(ge=1, le=2)
    center: LatLon
    meters_per_px: float = Field(gt=0)
    frame_to_px: FrameToPx


class Lane(_Strict):
    index: LaneIndex
    name: str
    kind: LaneKind
    lat_m: float
    width_m: float = Field(gt=0)


class CoverageRange(_Strict):
    from_m: float = Field(ge=0)
    to_m: float = Field(gt=0)
    state: CoverageState
    confidence: Confidence

    @model_validator(mode="after")
    def _ordered(self) -> CoverageRange:
        if self.to_m <= self.from_m:
            raise ValueError(f"coverage range must have to_m > from_m (got {self.from_m}..{self.to_m})")
        return self

    @property
    def length_m(self) -> float:
        return self.to_m - self.from_m


class CrossSection(_Strict):
    road_width_m: float = Field(gt=0)
    lanes: list[Lane] = Field(min_length=3, max_length=3)
    footpath_coverage: list[CoverageRange] = Field(min_length=1)

    @field_validator("lanes")
    @classmethod
    def _lanes_ordered(cls, lanes: list[Lane]) -> list[Lane]:
        if [lane.index for lane in lanes] != [0, 1, 2]:
            raise ValueError("lanes must be exactly indices 0, 1, 2 in order")
        for prev, nxt in zip(lanes, lanes[1:]):
            if prev.lat_m - nxt.lat_m < MIN_LANE_SEPARATION_M:
                raise ValueError(
                    f"lane lat_m must decrease by >= {MIN_LANE_SEPARATION_M} m from lane {prev.index} "
                    f"to {nxt.index} (got {prev.lat_m} -> {nxt.lat_m})"
                )
        return lanes

    @field_validator("footpath_coverage")
    @classmethod
    def _coverage_contiguous(cls, ranges: list[CoverageRange]) -> list[CoverageRange]:
        if abs(ranges[0].from_m) > 1e-6:
            raise ValueError("footpath_coverage must start at 0.0 (contiguous ranges)")
        for prev, nxt in zip(ranges, ranges[1:]):
            if abs(prev.to_m - nxt.from_m) > 1e-6:
                raise ValueError(
                    f"footpath_coverage ranges must be contiguous: {prev.to_m} != {nxt.from_m}"
                )
        return ranges

    def lane(self, index: int) -> Lane:
        return self.lanes[index]


class Building(_Strict):
    id: str
    source: Literal["osm", "vision"]
    footprint: list[tuple[float, float]] = Field(min_length=3)
    height_m: float = Field(gt=0)
    color_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    facade_url: str | None = None
    confidence: Confidence


class SizeM(_Strict):
    w: float = Field(ge=0)
    h: float = Field(ge=0)
    d: float = Field(ge=0)


class ObjectSource(_Strict):
    image_url: str
    crop_px: tuple[int, int, int, int]


class SceneObject(_Strict):
    id: str
    type: ObjectType
    label: str
    x_m: float = Field(ge=0)
    length_m: float = Field(ge=0)
    lanes: list[LaneIndex] = Field(min_length=1, max_length=3)
    action: Action
    size_m: SizeM
    confidence: Confidence
    source: ObjectSource | None = None
    billboard_url: str | None = None
    latlon: LatLon
    tile_px: tuple[float, float]

    @field_validator("lanes")
    @classmethod
    def _lanes_unique_sorted(cls, lanes: list[int]) -> list[int]:
        if len(set(lanes)) != len(lanes):
            raise ValueError("lanes must not repeat")
        return sorted(lanes)

    @property
    def is_hazard(self) -> bool:
        return self.action is not Action.none

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD

    @property
    def mean_lane_index(self) -> float:
        return sum(self.lanes) / len(self.lanes)


class Token(_Strict):
    id: str
    x_m: float = Field(ge=0)
    lane: LaneIndex
    reason: TokenReason
    latlon: LatLon
    tile_px: tuple[float, float]


class Walkability(_Strict):
    footpath_coverage_pct: float = Field(ge=0.0, le=100.0)
    hazard_count: int = Field(ge=0)
    crossing_present: bool
    score: int = Field(ge=0, le=100)


class Generation(_Strict):
    run_id: str
    quality: Quality
    confidence_overall: Confidence
    review_iterations: int = Field(ge=0)
    models: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ the Scene


class Scene(_Strict):
    schema_version: Literal[1]
    segment: SegmentInfo
    ground: Ground
    cross_section: CrossSection
    buildings: list[Building]
    objects: list[SceneObject]
    tokens: list[Token] = Field(max_length=MAX_TOKENS)
    walkability: Walkability
    generation: Generation

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Scene:
        length = self.segment.length_m
        last = self.cross_section.footpath_coverage[-1]
        if abs(last.to_m - length) > 1e-3:
            raise ValueError(
                f"footpath_coverage must end at segment length_m ({length}); ends at {last.to_m}"
            )
        ids = [o.id for o in self.objects]
        if len(set(ids)) != len(ids):
            raise ValueError("object ids must be unique")
        token_ids = [t.id for t in self.tokens]
        if len(set(token_ids)) != len(token_ids):
            raise ValueError("token ids must be unique")
        for obj in self.objects:
            if not obj.x_m < length:
                raise ValueError(f"object {obj.id}: x_m {obj.x_m} must be < length_m {length}")
            if obj.x_m + obj.length_m > length + OBJECT_OVERHANG_M + 1e-6:
                raise ValueError(
                    f"object {obj.id}: x_m + length_m ({obj.x_m + obj.length_m}) exceeds "
                    f"length_m + {OBJECT_OVERHANG_M}"
                )
        for tok in self.tokens:
            if not tok.x_m < length:
                raise ValueError(f"token {tok.id}: x_m {tok.x_m} must be < length_m {length}")
        return self

    def hazards(self) -> list[SceneObject]:
        return [o for o in self.objects if o.is_hazard]

    @property
    def tokens_in_scene(self) -> int:
        return len(self.tokens)

    def to_json(self) -> str:
        return self.model_dump_json()


# ------------------------------------------------------------------ pass outputs (spec/data.md)


class LayoutCoverage(_Strict):
    from_m: float = Field(ge=0)
    to_m: float = Field(gt=0)
    state: CoverageState
    confidence: Confidence


class LayoutBuilding(_Strict):
    x_from_m: float
    x_to_m: float
    depth_m: float = Field(gt=0)
    side: Literal["footpath", "opposite"]
    height_estimate_m: float = Field(gt=0)
    color_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    confidence: Confidence


class LayoutTree(_Strict):
    x_m: float
    lat_m: float
    confidence: Confidence


class LayoutPoint(_Strict):
    x_m: float
    confidence: Confidence


class LayoutGraph(_Strict):
    """Pass A output (satellite tile -> layout)."""

    road_width_m: float = Field(gt=0)
    footpath_width_m: float = Field(gt=0)
    footpath_coverage: list[LayoutCoverage]
    buildings: list[LayoutBuilding]
    trees: list[LayoutTree]
    crossings: list[LayoutPoint]
    ramps: list[LayoutPoint]
    notes: str = ""


class ObstacleDetection(_Strict):
    type: ObjectType
    label: str
    pano_index: int = Field(ge=0)
    crop_px: tuple[int, int, int, int]
    x_m: float
    lanes: list[LaneIndex] = Field(min_length=1, max_length=3)
    length_m: float = Field(ge=0)
    size_m: SizeM
    action: Action
    confidence: Confidence


class FacadeDetection(_Strict):
    x_from_m: float
    x_to_m: float
    color_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    pano_index: int = Field(ge=0)
    crop_px: tuple[int, int, int, int]


class ObstacleGraph(_Strict):
    """Pass B output (street-view panoramas -> obstacles + facades)."""

    objects: list[ObstacleDetection]
    facades: list[FacadeDetection]
    notes: str = ""


class PatchOp(BaseModel):
    """Review patch operation (spec/agent.md): op-specific fields are validated by ``apply_patch``."""

    model_config = ConfigDict(extra="allow")

    op: Literal["move", "remove", "add", "relabel", "set_coverage"]
    object_id: str | None = None
    x_m: float | None = None
    lane: LaneIndex | None = None
    object: SceneObject | None = None
    type: ObjectType | None = None
    action: Action | None = None
    confidence: Confidence | None = None
    from_m: float | None = None
    to_m: float | None = None
    state: CoverageState | None = None


class ReviewResult(_Strict):
    accepted: bool
    critique: str = Field(max_length=MAX_CRITIQUE_CHARS)
    patch_ops: list[PatchOp] = Field(max_length=MAX_PATCH_OPS)


# ------------------------------------------------------------------ example


def example_scene_path() -> Path:
    return EXAMPLE_SCENE_PATH


def load_example_scene() -> Scene:
    """The canonical example from spec/data.md, validated."""
    return Scene.model_validate(json.loads(EXAMPLE_SCENE_PATH.read_text(encoding="utf-8")))
