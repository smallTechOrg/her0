"""Scene JSON v2 (spec/data.md -> "Scene JSON v2", ``schema_version: 2``).

An additive superset of the v1 ``hero.scene.schema.Scene``: every v1 key keeps its meaning (``x_m`` = ``s``,
``lat_m`` = game lateral). New: ``path``, ``corridor``, ``about``, ``model`` and per-object
``entity_id / description / evidence / scenery / lateral_m / yaw_deg``. ``extra="forbid"`` everywhere.
``load_scene(dict)`` returns ``Scene`` (v1) or ``SceneV2`` by ``schema_version``.

Interpretation notes
--------------------
* v1 sub-models that are unchanged (``SegmentInfo``, ``Lane``, ``Token``, ``Walkability``, ``Generation`` ...)
  are re-used, not copied.
* ``objects[].type`` is the catalogue type verbatim (``EntityType``) plus the synthetic ``footpath_gap``.
* A scenery object (outside every lane band) has ``lanes == []`` and ``action == "none"``; any other object
  needs >= 1 lane.
* Lane separation in ``cross_section`` is validated with a 1e-6 tolerance (the lanes are length-weighted means).
* Compile-time geometry (the world-frame formulas) belongs to ``hero.model.compile``; this module validates
  structure only, plus: ``path.samples`` strictly increasing from 0 to ``path.length_m``, and ``corridor.stations``
  index-aligned with ``path.samples``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hero.model.types import EntityType, FootpathState, Material, StripKind
from hero.scene.schema import (
    MAX_TOKENS,
    MIN_LANE_SEPARATION_M,
    OBJECT_OVERHANG_M,
    Action,
    Confidence,
    FrameToPx,
    Generation,
    Lane,
    LaneIndex,
    LatLon,
    ObjectSource,
    Scene,
    SegmentInfo,
    SizeM,
    Token,
    Walkability,
)

SCHEMA_VERSION = 2
COMPILED_BY = "hero.model.compile/1"
SYNTHETIC_OBJECT_TYPES = ("footpath_gap",)
OBJECT_TYPES = tuple(t.value for t in EntityType) + SYNTHETIC_OBJECT_TYPES


class _Strict(BaseModel):
    # ``model_version`` / ``model_hash`` are spec'd JSON keys, hence no protected "model_" namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class GroundV2(_Strict):
    tile_url: str | None = None  # null -> flat colour
    size_px: tuple[int, int]
    zoom: int = Field(ge=0, le=22)
    scale: int = Field(ge=1, le=2)
    center: LatLon
    meters_per_px: float = Field(gt=0)
    frame_to_px: FrameToPx


# ------------------------------------------------------------------ path / corridor


class PathSample(_Strict):
    s: float = Field(ge=0)
    x: float
    z: float
    theta: float
    tile_px: tuple[float, float] | None = None


class Path(_Strict):
    length_m: float = Field(gt=0)
    samples: list[PathSample] = Field(min_length=2)

    @model_validator(mode="after")
    def _samples_ok(self) -> "Path":
        s = [p.s for p in self.samples]
        if abs(s[0]) > 1e-6:
            raise ValueError("path.samples must start at s=0")
        if abs(s[-1] - self.length_m) > 1e-3:
            raise ValueError(f"path.samples must end at length_m {self.length_m} (ends at {s[-1]})")
        if any(b <= a for a, b in zip(s, s[1:])):
            raise ValueError("path.samples s must be strictly increasing")
        return self


class CorridorFootpath(_Strict):
    lo: float
    hi: float
    state: FootpathState
    material: Material = Material.unknown
    height_m: float = 0.0
    confidence: Confidence


class CorridorStrip(_Strict):
    kind: StripKind
    lo: float
    hi: float
    material: Material = Material.unknown
    height_m: float = 0.0
    confidence: Confidence


class CorridorStation(_Strict):
    s: float = Field(ge=0)
    lane_lat: tuple[float, float, float]
    footpath: CorridorFootpath | None = None  # null <=> missing
    strips: list[CorridorStrip] = Field(default_factory=list)
    ground_uv: tuple[tuple[float, float], tuple[float, float]] | None = None  # null when the tile is null


class Corridor(_Strict):
    step_m: float = Field(gt=0)
    ground_half_m: float = Field(gt=0)
    stations: list[CorridorStation] = Field(min_length=2)


# ------------------------------------------------------------------ cross-section / buildings / objects


class CoverageRangeV2(_Strict):
    from_m: float = Field(ge=0)
    to_m: float = Field(gt=0)
    state: FootpathState
    confidence: Confidence

    @model_validator(mode="after")
    def _ordered(self) -> "CoverageRangeV2":
        if self.to_m <= self.from_m:
            raise ValueError(f"coverage range must have to_m > from_m (got {self.from_m}..{self.to_m})")
        return self

    @property
    def length_m(self) -> float:
        return self.to_m - self.from_m


class CrossSectionV2(_Strict):
    road_width_m: float = Field(gt=0)
    lanes: list[Lane] = Field(min_length=3, max_length=3)
    footpath_coverage: list[CoverageRangeV2] = Field(min_length=1)

    @field_validator("lanes")
    @classmethod
    def _lanes_ordered(cls, lanes: list[Lane]) -> list[Lane]:
        if [lane.index for lane in lanes] != [0, 1, 2]:
            raise ValueError("lanes must be exactly indices 0, 1, 2 in order")
        for prev, nxt in zip(lanes, lanes[1:]):
            if prev.lat_m - nxt.lat_m < MIN_LANE_SEPARATION_M - 1e-6:
                raise ValueError(
                    f"lane lat_m must decrease by >= {MIN_LANE_SEPARATION_M} m from lane {prev.index} "
                    f"to {nxt.index} (got {prev.lat_m} -> {nxt.lat_m})")
        return lanes

    @field_validator("footpath_coverage")
    @classmethod
    def _coverage_contiguous(cls, ranges: list[CoverageRangeV2]) -> list[CoverageRangeV2]:
        if abs(ranges[0].from_m) > 1e-6:
            raise ValueError("footpath_coverage must start at 0.0 (contiguous ranges)")
        for prev, nxt in zip(ranges, ranges[1:]):
            if abs(prev.to_m - nxt.from_m) > 1e-6:
                raise ValueError(f"footpath_coverage ranges must be contiguous: {prev.to_m} != {nxt.from_m}")
        return ranges


class BuildingV2(_Strict):
    id: str
    source: Literal["osm", "vision", "mixed"]
    footprint: list[tuple[float, float]] = Field(min_length=3)  # [s, lat]
    height_m: float = Field(gt=0)
    color_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    facade_url: str | None = None
    confidence: Confidence
    side: Literal["modelled", "opposite"]
    description: str = ""


class EvidenceSources(_Strict):
    street_view: int = Field(default=0, ge=0)
    satellite: int = Field(default=0, ge=0)
    upload: int = Field(default=0, ge=0)


class ObjectEvidence(_Strict):
    observation_count: int = Field(ge=0)
    sources: EvidenceSources = Field(default_factory=EvidenceSources)
    summary: str = ""
    observation_ids: list[str] = Field(default_factory=list)
    state: Literal["present", "disputed", "removed"] = "present"


class SceneObjectV2(_Strict):
    id: str
    entity_id: str | None = None  # null for synthetic gap-/blocked- objects
    type: str
    label: str
    x_m: float = Field(ge=0)
    length_m: float = Field(ge=0)
    lanes: list[LaneIndex] = Field(default_factory=list, max_length=3)
    action: Action
    lateral_m: float
    yaw_deg: float = 0.0
    size_m: SizeM
    confidence: Confidence
    scenery: bool = False
    description: str = ""
    evidence: ObjectEvidence | None = None
    source: ObjectSource | None = None
    billboard_url: str | None = None
    latlon: LatLon
    tile_px: tuple[float, float] | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in OBJECT_TYPES:
            raise ValueError(f"unknown object type {v!r}")
        return v

    @field_validator("lanes")
    @classmethod
    def _lanes_unique_sorted(cls, lanes: list[int]) -> list[int]:
        if len(set(lanes)) != len(lanes):
            raise ValueError("lanes must not repeat")
        return sorted(lanes)

    @model_validator(mode="after")
    def _scenery_rule(self) -> "SceneObjectV2":
        if self.scenery:
            if self.action != Action.none:
                raise ValueError(f"scenery object {self.id} must have action 'none'")
        elif not self.lanes:
            raise ValueError(f"object {self.id} needs >= 1 lane unless scenery")
        return self

    @property
    def is_hazard(self) -> bool:
        return self.action != Action.none

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.6


# ------------------------------------------------------------------ about / model


class AboutFidelity(_Strict):
    level: str = Field(pattern=r"^L[0-3]$")
    score: float = Field(ge=0.0, le=3.0)
    label: str
    histogram_m: dict[str, float] = Field(default_factory=dict)


class AboutPhotos(_Strict):
    total: int = Field(ge=0)
    street_view: int = Field(default=0, ge=0)
    satellite: int = Field(default=0, ge=0)
    user: int = Field(default=0, ge=0)


class CaptureDates(_Strict):
    oldest: str | None = None
    newest: str | None = None


class LowConfidencePart(_Strict):
    s_from: float
    s_to: float
    reason: str


class About(_Strict):
    model_version: int = Field(ge=0)
    fidelity: AboutFidelity
    photos: AboutPhotos
    last_updated: str
    capture_dates: CaptureDates = Field(default_factory=CaptureDates)
    low_confidence_parts: list[LowConfidencePart] = Field(default_factory=list)
    summary: str
    improve_hint: str = ""
    legacy: bool = False
    placement_verified: bool = False
    degraded: bool = False


class ModelRef(_Strict):
    version: int = Field(ge=0)
    model_hash: str
    compiled_by: str = COMPILED_BY


# ------------------------------------------------------------------ the scene


class SceneV2(_Strict):
    schema_version: Literal[2]
    segment: SegmentInfo
    ground: GroundV2
    path: Path
    corridor: Corridor
    cross_section: CrossSectionV2
    buildings: list[BuildingV2]
    objects: list[SceneObjectV2]
    tokens: list[Token] = Field(max_length=MAX_TOKENS)
    walkability: Walkability
    about: About
    generation: Generation
    model: ModelRef

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "SceneV2":
        length = self.segment.length_m
        if abs(self.path.length_m - length) > 1e-3:
            raise ValueError(f"path.length_m {self.path.length_m} must equal segment.length_m {length}")
        last = self.cross_section.footpath_coverage[-1]
        if abs(last.to_m - length) > 1e-3:
            raise ValueError(f"footpath_coverage must end at segment length_m ({length}); ends at {last.to_m}")
        stations, samples = self.corridor.stations, self.path.samples
        if len(stations) != len(samples):
            raise ValueError(f"corridor.stations ({len(stations)}) must be index-aligned with path.samples ({len(samples)})")
        for st, sm in zip(stations, samples):
            if abs(st.s - sm.s) > 1e-6:
                raise ValueError(f"corridor station s {st.s} does not match path sample s {sm.s}")
        for name, items in (("object", self.objects), ("token", self.tokens), ("building", self.buildings)):
            ids = [i.id for i in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} ids must be unique")
        for obj in self.objects:
            if not obj.x_m < length:
                raise ValueError(f"object {obj.id}: x_m {obj.x_m} must be < length_m {length}")
            if obj.x_m + obj.length_m > length + OBJECT_OVERHANG_M + 1e-6:
                raise ValueError(f"object {obj.id}: x_m + length_m exceeds length_m + {OBJECT_OVERHANG_M}")
        for tok in self.tokens:
            if not tok.x_m < length:
                raise ValueError(f"token {tok.id}: x_m {tok.x_m} must be < length_m {length}")
        return self

    # v1-compatible accessors (SegmentRecord.scene may be Scene | SceneV2)

    def hazards(self) -> list[SceneObjectV2]:
        return [o for o in self.objects if o.is_hazard]

    @property
    def tokens_in_scene(self) -> int:
        return len(self.tokens)

    def to_json(self) -> str:
        return self.model_dump_json()


def load_scene(data: dict[str, Any] | str | bytes) -> "Scene | SceneV2 | SceneV3":
    """Parse a stored/served scene, discriminated by ``schema_version`` (1 -> ``Scene``, 2 -> ``SceneV2``, 3 -> ``SceneV3``).

    Raises ``ValueError`` (a pydantic ``ValidationError`` is one) for an unknown version or an invalid scene.
    """
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("a scene must be a JSON object")
    version = data.get("schema_version")
    if version == 1:
        return Scene.model_validate(data)
    if version == 2:
        return SceneV2.model_validate(data)
    if version == 3:
        from hero.model.scene_v3 import SceneV3  # local import: scene_v3 builds on this module

        return SceneV3.model_validate(data)
    raise ValueError(f"unsupported scene schema_version {version!r} (expected 1, 2 or 3)")
