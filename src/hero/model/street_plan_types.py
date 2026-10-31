"""``StreetPlan`` - the compile's geometry half (spec/data.md r1 body re-read by the r2 rule).

Pure data, **never served, never stored**. Produced by ``hero.model.street_plan.plan_street``; consumed by
``hero.export.mesh_builder`` (geometry) and ``hero.export.manifest`` (lanes, colliders, tokens, entities).
Internally it keeps *game lateral* ``lat`` (positive toward the modelled footpath side) and ``footpath_side``;
the exporter converts to the OpenDRIVE ``t`` (left-positive): ``t = lat`` for a left footpath, ``t = -lat`` for a right
one (``t_of_lat``). It has no ``schema_version`` / ``segment`` / ``about`` / ``generation`` / ``walkability``
(those belong to the lite scene).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hero.model.gap_types import Interpolation
from hero.model.scene_v2 import CorridorFootpath, CorridorStrip, SceneObjectV2
from hero.model.texture_types import GroundBinSource, GroundStripRef, FrontageStripRef
from hero.scene.schema import Token

PLAN_ID = "street-plan/1"
SPAWN_TYPES = ("parked_car", "two_wheeler", "cone", "barricade", "pothole", "stall", "low_branch")
FIXED_FURNITURE_TYPES = (
    "tree", "pole", "bin", "bollard", "railing", "signboard", "bus_stop",
    "drain", "manhole", "kerb_cut", "crossing", "ramp",
)
FrontageKind = Literal["building", "wall", "gate"]
FrontageSource = Literal["osm", "vision", "entity", "interpolated", "prior"]
MAX_SIZE_M = {  # (w, h) caps per furniture type so no data error can produce a screen-filling object
    "tree": (5.5, 10.0), "pole": (0.5, 9.0), "bin": (1.2, 1.4), "bollard": (0.4, 1.2),
    "railing": (0.3, 1.5), "signboard": (1.6, 2.6), "bus_stop": (2.0, 3.0),
}


def t_of_lat(lat: float, footpath_side: str) -> float:
    """Game lateral -> OpenDRIVE t (left positive)."""
    return lat if footpath_side == "left" else -lat


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class PlanOrigin(_S):
    lat: float
    lon: float


class PlanFrame(_S):
    origin: PlanOrigin
    heading_deg: float


class RefPoint(_S):
    s: float = Field(ge=0)
    lat: float
    lon: float
    bearing_deg: float
    tile_px: tuple[float, float] | None = None


class PlanReferenceLine(_S):
    source: str = "osm_nominatim"
    osm_way_id: int | None = None
    name: str = ""
    length_s: float = Field(gt=0)
    step_m: float = Field(default=2.5, gt=0)
    points: list[RefPoint] = Field(min_length=2)  # 2.5 m grid + the end point, strictly increasing s


class Kerb(_S):
    lat: float  # boundary between carriageway and footpath
    height_m: float = Field(ge=0)
    width_m: float = Field(default=0.2, ge=0)


class PlanStation(_S):
    s: float = Field(ge=0)
    lane_lat: tuple[float, float, float]
    footpath: CorridorFootpath | None = None  # null <=> missing
    strips: list[CorridorStrip] = Field(default_factory=list)  # modelled side then beyond, ordered by lo
    kerb: Kerb
    ground_src: GroundBinSource = "procedural"


class GroundTexPlan(_S):
    mode: Literal["baked_strip", "procedural"]
    procedural_seed: int = 0
    bin_m: float = 5.0
    bin_source: list[GroundBinSource]
    strip: GroundStripRef | None = None

    @model_validator(mode="after")
    def _mode_strip(self) -> "GroundTexPlan":
        if (self.mode == "baked_strip") != (self.strip is not None):
            raise ValueError("ground_tex.strip must be set iff mode == baked_strip")
        return self


class FrontageSegment(_S):
    s_from: float = Field(ge=0)
    s_to: float = Field(gt=0)
    lat_from: float
    lat_to: float
    height_m: float = Field(gt=0)
    kind: FrontageKind
    colour_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    storeys: int = Field(default=0, ge=0)
    source: FrontageSource
    building_id: str | None = None
    entity_id: str | None = None
    interpolated: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


class FrontageSidePlan(_S):
    side: Literal["modelled", "opposite"]
    texture: FrontageStripRef | None = None
    segments: list[FrontageSegment]


class FrontagePlan(_S):
    step_m: float = 2.5
    sides: list[FrontageSidePlan] = Field(min_length=2, max_length=2)  # "modelled" then "opposite"


class FurniturePlan(_S):
    id: str
    type: str
    variant: str = ""
    s: float = Field(ge=0)
    lat: float
    yaw_deg: float = 0.0
    size_m: dict[Literal["w", "h", "d"], float]
    length_m: float = Field(default=0.0, ge=0)
    colour_hex: str | None = None
    canopy_hex: str | None = None
    object_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    fidelity_level: int = Field(default=1, ge=0, le=3)
    description: str = ""

    @field_validator("type")
    @classmethod
    def _fixed(cls, v: str) -> str:
        if v not in FIXED_FURNITURE_TYPES:
            raise ValueError(f"furniture type {v!r} is not a fixed type")
        return v


class SpawnHints(_S):
    density: float = Field(ge=0.0, le=1.0)
    type_weights: dict[str, float]
    lane_weights: tuple[float, float, float]  # informational share of dynamic obstacles per lane (sums to 1)

    @model_validator(mode="after")
    def _weights(self) -> "SpawnHints":
        if abs(sum(self.lane_weights) - 1.0) > 1e-6 or any(w < 0 for w in self.lane_weights):
            raise ValueError("lane_weights must be >= 0 and sum to 1.0 (+-1e-6)")
        if tuple(self.type_weights) != SPAWN_TYPES:
            raise ValueError(f"type_weights keys must be exactly {SPAWN_TYPES} in order")
        if abs(sum(self.type_weights.values()) - 1.0) > 1e-6:
            raise ValueError("type_weights must sum to 1.0 (+-1e-6)")
        if any(w < 0 for w in self.type_weights.values()):
            raise ValueError("type_weights must be >= 0")
        return self


class PlanDynamicContext(_S):
    excluded_count: int = Field(ge=0)
    entity_ids: list[str] = Field(default_factory=list)
    spawn_hints: SpawnHints


class FidelityBin(_S):
    index: int = Field(ge=0)
    s_from: float
    s_to: float
    level: int = Field(ge=0, le=3)


class PlanFidelity(_S):
    overall_level: int = Field(ge=0, le=3)
    score: float = Field(ge=0.0, le=3.0)
    bins: list[FidelityBin]


class StreetPlan(_S):
    plan: Literal["street-plan/1"] = PLAN_ID
    slug: str
    name: str
    city: str = ""
    model_version: int = Field(ge=0)
    model_hash: str
    compiled_by: str = "hero.model.compile/2"
    footpath_side: Literal["left", "right"]
    length_m: float = Field(gt=0)
    frame: PlanFrame
    reference_line: PlanReferenceLine
    corridor_step_m: float = Field(default=2.5, gt=0)
    stations: list[PlanStation] = Field(min_length=2)  # index-aligned with reference_line.points
    ground_tex: GroundTexPlan
    frontage: FrontagePlan
    furniture: list[FurniturePlan] = Field(default_factory=list)  # sorted by (s, id)
    objects: list[SceneObjectV2] = Field(default_factory=list)  # FIXED hazards + synthetic gap-/blocked- objects
    tokens: list[Token] = Field(default_factory=list)
    dynamic_context: PlanDynamicContext
    fidelity: PlanFidelity
    interpolated: list[Interpolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> "StreetPlan":
        pts = self.reference_line.points
        if len(pts) != len(self.stations):
            raise ValueError(f"stations ({len(self.stations)}) must be index-aligned with reference_line.points ({len(pts)})")
        for p, st in zip(pts, self.stations):
            if abs(p.s - st.s) > 1e-6:
                raise ValueError(f"station s {st.s} != reference point s {p.s}")
        if abs(pts[-1].s - self.length_m) > 1e-3 or abs(self.reference_line.length_s - self.length_m) > 1e-3:
            raise ValueError("reference line must end at length_m")
        n_bins = -(-self.length_m // self.ground_tex.bin_m)
        if len(self.ground_tex.bin_source) != int(n_bins):
            raise ValueError(f"ground_tex.bin_source must have {int(n_bins)} entries")
        if [s.side for s in self.frontage.sides] != ["modelled", "opposite"]:
            raise ValueError("frontage.sides must be modelled then opposite")
        for side in self.frontage.sides:
            segs = side.segments
            if not segs:
                raise ValueError(f"frontage side {side.side} has no segments (holes not allowed)")
            if abs(segs[0].s_from) > 1e-6 or abs(segs[-1].s_to - self.length_m) > 1e-3:
                raise ValueError(f"frontage {side.side} must tile [0, length_m]")
            for a, b in zip(segs, segs[1:]):
                if abs(a.s_to - b.s_from) > 1e-6:
                    raise ValueError(f"frontage {side.side} segments must be contiguous ({a.s_to} != {b.s_from})")
        ids = [f.id for f in self.furniture]
        if len(set(ids)) != len(ids):
            raise ValueError("furniture ids must be unique")
        if self.furniture != sorted(self.furniture, key=lambda f: (f.s, f.id)):
            raise ValueError("furniture must be sorted by (s, id)")
        return self
