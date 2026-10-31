"""``ManifestV1`` - ``street.manifest.json`` (schema ``hero-street-manifest/1``; spec/portability.md section 4).

Pydantic models, ``extra="forbid"``, field order = the spec's key order (``model_dump`` preserves it). Numbers are
rounded by the producer (3 dp, lat/lon 6 dp). ``lane`` indices: 0 footpath, 1 road edge, 2 road.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MANIFEST_SCHEMA = "hero-street-manifest/1"
WORLD_FROM_ST = "hero-st-world/1"
SPAWN_TYPES = ("parked_car", "two_wheeler", "cone", "barricade", "pothole", "stall", "low_branch")
ATTRIBUTION_LINES = [
    "Satellite imagery © Google",
    "Street-level imagery © Google",
    "Map data © OpenStreetMap contributors (ODbL)",
]
ATTRIBUTION_NOTICE = (
    "Textures derive from Google imagery and are provided for evaluation; "
    "check the Google Maps Platform terms before redistributing."
)
Action = Literal["dodge", "jump", "slide", "none"]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class LatLonM(_S):
    lat: float
    lon: float


class Build(_S):
    generator: str
    segment_slug: str
    model_version: int = Field(ge=0)
    model_hash: str
    mesh_sha256: str
    glb_bytes: int = Field(ge=0)
    xodr_sha256: str
    pygltflib: str
    pillow: str
    python: str
    compiled_by: str


class FidelityBinM(_S):
    index: int = Field(ge=0)
    s_from: float
    s_to: float
    level: int = Field(ge=0, le=3)


class ManifestFidelity(_S):
    overall_level: int = Field(ge=0, le=3)
    score: float = Field(ge=0, le=3)
    bins: list[FidelityBinM]


class SegmentM(_S):
    slug: str
    name: str
    city: str = ""
    length_s: float = Field(gt=0)
    footpath_side: Literal["left", "right"]
    fidelity: ManifestFidelity


class FrameM(_S):
    units: Literal["metres"] = "metres"
    up_axis: Literal["+Y"] = "+Y"
    handedness: Literal["right"] = "right"
    travel_at_s0: Literal["-Z"] = "-Z"
    origin: LatLonM
    heading_deg: float
    world_from_st: Literal["hero-st-world/1"] = WORLD_FROM_ST


class RefPointM(_S):
    s: float
    lat: float
    lon: float
    x: float
    z: float
    bearing_deg: float


class ReferenceLineM(_S):
    source: str
    osm_way_id: int | None = None
    name: str = ""
    length_s: float = Field(gt=0)
    step_m: float = Field(gt=0)
    points: list[RefPointM] = Field(min_length=2)


class LaneDef(_S):
    index: int = Field(ge=0, le=2)
    name: str
    kind: str
    width_m: float = Field(gt=0)


class FootpathStationM(_S):
    t_lo: float
    t_hi: float
    state: str
    material: str
    height_m: float
    confidence: float = Field(ge=0, le=1)


class KerbM(_S):
    t: float
    height_m: float = Field(ge=0)
    width_m: float = Field(ge=0)


class LaneStation(_S):
    s: float
    lane_t: tuple[float, float, float]
    footpath: FootpathStationM | None = None  # null => lane 0 has no walkable surface here
    kerb: KerbM


class LanesM(_S):
    defs: list[LaneDef] = Field(min_length=3, max_length=3)
    stations: list[LaneStation] = Field(min_length=2)


class CrossStrip(_S):
    kind: str
    side: Literal["left", "right", "centre"]
    t_lo: float
    t_hi: float
    material: str
    condition: str
    height_m: float
    confidence: float = Field(ge=0, le=1)


class CrossStation(_S):
    s: float
    strips: list[CrossStrip]


class CrossSectionM(_S):
    stations: list[CrossStation] = Field(min_length=2)


class FrontageM(_S):
    s_from: float
    s_to: float
    type: Literal["building", "wall", "gate"]
    t_from: float
    t_to: float
    height_m: float = Field(gt=0)
    colour_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    source: Literal["osm", "vision", "entity", "interpolated", "prior"]
    interpolated: bool


class FrontageSides(_S):
    left: list[FrontageM]
    right: list[FrontageM]

    @model_validator(mode="after")
    def _contiguous(self) -> "FrontageSides":
        for name in ("left", "right"):
            segs = getattr(self, name)
            for a, b in zip(segs, segs[1:]):
                if abs(a.s_to - b.s_from) > 1e-3:
                    raise ValueError(f"frontage.{name} must be contiguous in s ({a.s_to} != {b.s_from})")
        return self


class SizeMM(_S):
    w: float
    h: float
    d: float


class EntityM(_S):
    id: str
    type: str
    variant: str = ""
    s: float
    t: float
    yaw_deg: float = 0.0
    size_m: SizeMM
    length_m: float = Field(ge=0)
    colour_hex: str | None = None
    canopy_hex: str | None = None
    confidence: float = Field(ge=0, le=1)
    fidelity_level: int = Field(ge=0, le=3)
    node: str
    description: str = ""


class ColliderM(_S):
    id: str
    entity_id: str | None = None
    type: str
    label: str
    s: float
    length_m: float = Field(ge=0)
    lanes: list[int]
    action: Action
    size_m: SizeMM
    confidence: float = Field(ge=0, le=1)
    latlon: LatLonM
    tile_px: tuple[float, float] | None = None  # web-only hint


class TokenM(_S):
    id: str
    s: float
    lane: int = Field(ge=0, le=2)
    reason: Literal["intact_footpath", "crossing", "ramp"]
    latlon: LatLonM
    tile_px: tuple[float, float] | None = None


class SpawnHintsM(_S):
    density: float = Field(ge=0, le=1)
    type_weights: dict[str, float]

    @model_validator(mode="after")
    def _w(self) -> "SpawnHintsM":
        if tuple(self.type_weights) != SPAWN_TYPES:
            raise ValueError(f"type_weights must have exactly the keys {SPAWN_TYPES}")
        if abs(sum(self.type_weights.values()) - 1.0) > 1e-6:
            raise ValueError("type_weights must sum to 1.0 (+-1e-6)")
        return self


class InterpolatedM(_S):
    what: str
    side: str
    s_from: float
    s_to: float
    method: str
    note: str = ""


class EndPose(_S):
    x: float
    z: float
    yaw_rad: float


class LoopM(_S):
    seam_blend_m: float = 6.0
    end_pose: EndPose
    chain: Literal["rigid_end_to_start"] = "rigid_end_to_start"


class MeshNodeStat(_S):
    name: str
    triangles: int = Field(ge=0)
    material: str


class MeshStatsM(_S):
    primitives: int = Field(ge=0)
    triangles: int = Field(ge=0)
    vertices: int = Field(ge=0)
    materials: int = Field(ge=0)
    textures: int = Field(ge=0)
    nodes: list[MeshNodeStat]


class AttributionM(_S):
    lines: list[str] = Field(default_factory=lambda: list(ATTRIBUTION_LINES))
    notice: str = ATTRIBUTION_NOTICE


class ManifestV1(_S):
    schema_: Literal["hero-street-manifest/1"] = Field(default=MANIFEST_SCHEMA, alias="schema")
    build: Build
    segment: SegmentM
    frame: FrameM
    reference_line: ReferenceLineM
    lanes: LanesM
    cross_section: CrossSectionM
    frontage: FrontageSides
    entities: list[EntityM]
    colliders: list[ColliderM]
    tokens: list[TokenM]
    spawn_hints: SpawnHintsM
    interpolated: list[InterpolatedM]
    loop: LoopM
    mesh_stats: MeshStatsM
    attribution: AttributionM
    gameplay: dict[str, Any]
    gameplay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)

    @model_validator(mode="after")
    def _aligned(self) -> "ManifestV1":
        pts, st = self.reference_line.points, self.lanes.stations
        if len(pts) != len(st) or len(pts) != len(self.cross_section.stations):
            raise ValueError("reference_line.points, lanes.stations and cross_section.stations must be index-aligned")
        for p, a, b in zip(pts, st, self.cross_section.stations):
            if abs(p.s - a.s) > 1e-6 or abs(p.s - b.s) > 1e-6:
                raise ValueError(f"station s mismatch at s={p.s}")
        ids = [e.id for e in self.entities]
        if len(set(ids)) != len(ids):
            raise ValueError("entity ids must be unique")
        return self

    def dump(self) -> dict[str, Any]:
        """JSON-ready dict with the wire key ``schema``."""
        return self.model_dump(by_alias=True, mode="json")
