"""Canonical street model types (spec/data.md -> "Canonical street model", ``schema: "street-model/1"``).

Pure data: Pydantic v2 models (``extra="forbid"``, snake_case keys) and enums. No network, DB or
clock. This module is the contract owner for slices S1b-S1d, S2, S3 and S6; names follow
``spec/architecture.md`` -> Contracts.

Notes on interpretation
-----------------------
* Enums are ``StrEnum`` so ``EntityType.tree == "tree"`` and JSON stays plain strings.
* ``CanonicalModel.schema`` deliberately shadows ``BaseModel.schema`` (the JSON key is ``schema``).
* ``Observation`` is the Pydantic record of one ``observations`` row (``hero.db.model_repo`` returns it).
* ``CritiqueOp`` lives here (not in ``hero.fusion.schemas``) because ``hero.model.merge.apply_critique``
  consumes it and ``hero.model`` must not import ``hero.fusion``.
* ``CanonicalModel`` only validates *structure* (profile tiles ``[0, length_s]``, unique ids). Value
  rules such as the 2.5-30 m interval length are produced by merge/priors and checked by
  ``consistency_check``; legacy imports legitimately violate them.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_ID = "street-model/1"
PROFILE_TILING_TOL_M = 5e-3

Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Side = Literal["left", "right"]
StripSide = Literal["left", "right", "centre"]
RegionSide = Literal["left", "right", "both"]
CropPx = tuple[int, int, int, int]
AttrValue = str | float | bool


# ------------------------------------------------------------------ enums


class EntityType(StrEnum):
    tree = "tree"
    pole = "pole"
    parked_vehicle = "parked_vehicle"
    signboard = "signboard"
    drain = "drain"
    pothole = "pothole"
    bollard = "bollard"
    crossing = "crossing"
    ramp = "ramp"
    wall = "wall"
    gate = "gate"
    kerb_cut = "kerb_cut"
    bus_stop = "bus_stop"
    shopfront = "shopfront"
    debris = "debris"
    low_branch = "low_branch"
    manhole = "manhole"
    vendor_stall = "vendor_stall"
    bin = "bin"
    speed_breaker = "speed_breaker"
    railing = "railing"
    other = "other"


class StripKind(StrEnum):
    footpath = "footpath"
    kerb = "kerb"
    verge = "verge"
    planting = "planting"
    parking = "parking"
    cycle_lane = "cycle_lane"
    carriageway = "carriageway"
    median = "median"
    shoulder = "shoulder"
    forecourt = "forecourt"
    unknown = "unknown"


class Material(StrEnum):
    concrete = "concrete"
    paver = "paver"
    tile = "tile"
    asphalt = "asphalt"
    gravel = "gravel"
    earth = "earth"
    grass = "grass"
    stone = "stone"
    mixed = "mixed"
    unknown = "unknown"


class Condition(StrEnum):
    good = "good"
    fair = "fair"
    poor = "poor"
    broken = "broken"
    unknown = "unknown"


class FootpathState(StrEnum):
    intact = "intact"
    broken = "broken"
    missing = "missing"
    blocked = "blocked"
    encroached = "encroached"
    unknown = "unknown"


class EntityState(StrEnum):
    present = "present"
    disputed = "disputed"
    removed = "removed"


class EvidenceRole(StrEnum):
    supports = "supports"
    contradicts = "contradicts"
    prior = "prior"
    critique = "critique"
    human = "human"


class ModelKind(StrEnum):
    legacy = "legacy"
    prior = "prior"
    fused = "fused"
    edit = "edit"


class ContradictionKind(StrEnum):
    type_conflict = "type_conflict"
    presence_conflict = "presence_conflict"
    position_conflict = "position_conflict"
    attribute_conflict = "attribute_conflict"
    footpath_state_conflict = "footpath_state_conflict"


class ObservationKind(StrEnum):
    satellite_overview = "satellite_overview"
    satellite_crop = "satellite_crop"
    streetview = "streetview"
    user_photo = "user_photo"


class ObservationSource(StrEnum):
    google_static_maps = "google_static_maps"
    google_streetview = "google_streetview"
    upload = "upload"


class ViewKind(StrEnum):
    forward = "forward"
    back = "back"
    left = "left"
    right = "right"
    oblique = "oblique"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ observation (evidence record)


class Observation(_M):
    """One row of ``observations`` (spec/data.md). Never updated after insert."""

    id: str
    segment_id: str
    obs_key: str = Field(max_length=128)
    kind: ObservationKind
    source: ObservationSource
    sha256: str = Field(min_length=64, max_length=64)
    file_path: str
    media_type: str
    width_px: int
    height_px: int
    bytes: int
    lat: float
    lon: float
    heading_deg: float | None = None
    pitch_deg: float | None = None
    fov_deg: float | None = None
    zoom: int | None = None
    scale: int | None = None
    meters_per_px: float | None = None
    pano_id: str | None = None
    capture_date: str | None = Field(default=None, max_length=7)
    station_s: float | None = None
    offset_t: float | None = None
    view: ViewKind | None = None
    georef: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    acquired_run_id: str | None = None
    note: str | None = None
    created_at: datetime

    @property
    def url(self) -> str:
        return f"/media/{self.file_path}"

    @property
    def is_street_level(self) -> bool:
        return self.kind in (ObservationKind.streetview, ObservationKind.user_photo)


# ------------------------------------------------------------------ reference line


class RefPoint(_M):
    lat: float
    lon: float
    s: float


class Extrapolated(_M):
    start: float = 0.0
    end: float = 0.0


class ReferenceLineModel(_M):
    source: Literal["osm_nominatim", "osm_overpass", "straight_chord", "legacy_straight"]
    osm_way_id: int | None = None
    name: str | None = None
    points: list[RefPoint] = Field(min_length=2)
    length_s: float = Field(gt=0)
    extrapolated_m: Extrapolated = Field(default_factory=Extrapolated)


# ------------------------------------------------------------------ profile (cross-section along s)


class Strip(_M):
    id: str
    kind: StripKind
    side: StripSide
    material: Material = Material.unknown
    condition: Condition = Condition.unknown
    height_m: float = 0.0
    t_lo: tuple[float, float]  # [at s_from, at s_to]; edges vary linearly inside the interval
    t_hi: tuple[float, float]
    weight: float = Field(ge=0.0)
    confidence: Unit
    description: str = ""
    provenance: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _edges_ordered(self) -> "Strip":
        for lo, hi in zip(self.t_lo, self.t_hi):
            if hi < lo - 1e-9:
                raise ValueError(f"strip {self.id}: t_hi {self.t_hi} must be >= t_lo {self.t_lo}")
        return self

    def t_lo_at(self, f: float) -> float:
        """Lower edge at fraction ``f`` in [0, 1] of the interval."""
        return self.t_lo[0] + (self.t_lo[1] - self.t_lo[0]) * f

    def t_hi_at(self, f: float) -> float:
        return self.t_hi[0] + (self.t_hi[1] - self.t_hi[0]) * f

    @property
    def width_mean(self) -> float:
        return ((self.t_hi[0] - self.t_lo[0]) + (self.t_hi[1] - self.t_lo[1])) / 2.0


class ProfileInterval(_M):
    id: str
    s_from: float
    s_to: float
    footpath_state: FootpathState = FootpathState.unknown
    state_votes: dict[str, float] = Field(default_factory=dict)
    strips: list[Strip] = Field(default_factory=list)
    description: str = ""
    evidence_summary: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "ProfileInterval":
        if self.s_to <= self.s_from:
            raise ValueError(f"interval {self.id}: s_to {self.s_to} must exceed s_from {self.s_from}")
        return self

    @property
    def length(self) -> float:
        return self.s_to - self.s_from

    def footpath_strip(self, side: Side) -> Strip | None:
        for strip in self.strips:
            if strip.kind is StripKind.footpath and strip.side == side:
                return strip
        return None

    def confidence_for(self, side: Side) -> float:
        """Interval confidence = that of its footpath strip on ``side`` (0.0 when absent, e.g. ``missing``)."""
        strip = self.footpath_strip(side)
        return strip.confidence if strip else 0.0


# ------------------------------------------------------------------ entities / buildings


class Position(_M):
    s: float
    t: float


class Extent(_M):
    length_s: float = Field(default=0.0, ge=0.0)
    width_t: float = Field(default=0.0, ge=0.0)


class Evidence(_M):
    observation_id: str
    role: EvidenceRole
    detection_key: str | None = None
    crop_px: CropPx | None = None
    weight: float = Field(ge=0.0)
    added_in_version: int = Field(ge=0)
    note: str | None = None


class Billboard(_M):
    observation_id: str
    crop_px: CropPx


class Entity(_M):
    id: str = Field(pattern=r"^ent-[0-9a-f]+$")
    type: EntityType
    subtype: str | None = None
    state: EntityState = EntityState.present
    position: Position
    position_sigma_m: float = Field(gt=0)
    extent: Extent = Field(default_factory=Extent)
    height_m: float | None = None
    orientation_deg: float | None = None
    attributes: dict[str, AttrValue] = Field(default_factory=dict)
    description: str = ""
    confidence: Unit
    logit: float = 0.0
    provenance: list[Evidence] = Field(default_factory=list)
    billboard: Billboard | None = None
    first_seen_version: int = Field(ge=0)
    last_changed_version: int = Field(ge=0)

    def supports(self) -> list[Evidence]:
        return [e for e in self.provenance if e.role is EvidenceRole.supports]

    def contradictions(self) -> list[Evidence]:
        return [e for e in self.provenance if e.role is EvidenceRole.contradicts]


class BuildingFacade(_M):
    observation_id: str
    crop_px: CropPx
    s_from: float
    s_to: float
    weight: float = Field(ge=0.0)


class Building(_M):
    id: str
    source: Literal["osm", "vision", "mixed"]
    osm_way_id: int | None = None
    side: Side
    footprint: list[tuple[float, float]] = Field(min_length=3)  # [s, t]
    height_m: float = Field(gt=0)
    height_confidence: Unit = 0.3
    storeys: int | None = None
    colour_hex: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    facades: list[BuildingFacade] = Field(default_factory=list)
    description: str = ""
    confidence: Unit = 0.5
    provenance: list[str] = Field(default_factory=list)


class Contradiction(_M):
    id: str
    kind: ContradictionKind
    entity_ids: list[str] = Field(default_factory=list)
    interval: tuple[float, float] | None = None
    description: str
    observation_ids: list[str] = Field(default_factory=list)
    opened_in_version: int = Field(ge=0)
    resolved_in_version: int | None = None

    @property
    def is_open(self) -> bool:
        return self.resolved_in_version is None


# ------------------------------------------------------------------ fidelity


class FidelityBin(_M):
    index: int = Field(ge=0)
    s_from: float
    s_to: float
    level: int = Field(ge=0, le=3)
    views: int = Field(default=0, ge=0)
    stations: int = Field(default=0, ge=0)
    sides: dict[str, int] = Field(default_factory=lambda: {"left": 0, "right": 0})
    overhead_hires: bool = False
    hint: str = ""


class FidelityOverall(_M):
    level: int = Field(ge=0, le=3)
    score: float = Field(ge=0.0, le=3.0)
    histogram_m: dict[str, float] = Field(default_factory=lambda: {"L0": 0.0, "L1": 0.0, "L2": 0.0, "L3": 0.0})


class FidelityMap(_M):
    bin_m: float = Field(default=5.0, gt=0)
    length_s: float = Field(ge=0)
    bins: list[FidelityBin]
    overall: FidelityOverall

    def level_at(self, s: float) -> int:
        """Level of the bin containing ``s`` (clamped to the first/last bin; 0 for an empty map)."""
        if not self.bins:
            return 0
        for b in self.bins:
            if s < b.s_to:
                return b.level
        return self.bins[-1].level


# ------------------------------------------------------------------ the model


class ModelStats(_M):
    entity_count: int = 0
    removed_count: int = 0
    building_count: int = 0
    observation_count: int = 0


with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # `schema` shadows BaseModel.schema on purpose: it is the JSON key

    class CanonicalModel(_M):
        schema: Literal["street-model/1"] = SCHEMA_ID
        segment_slug: str
        version: int = Field(ge=0)
        parent_version: int | None = None
        kind: ModelKind
        run_id: str | None = None
        created_at: datetime
        legacy: bool = False
        placement_verified: bool = False
        footpath_side: Side
        reference_line: ReferenceLineModel
        profile: list[ProfileInterval]
        entities: list[Entity] = Field(default_factory=list)
        buildings: list[Building] = Field(default_factory=list)
        contradictions: list[Contradiction] = Field(default_factory=list)
        fused_observation_ids: list[str] = Field(default_factory=list)
        fidelity: FidelityMap | None = None
        stats: ModelStats = Field(default_factory=ModelStats)

        @field_validator("profile")
        @classmethod
        def _profile_not_empty(cls, v: list[ProfileInterval]) -> list[ProfileInterval]:
            if not v:
                raise ValueError("profile must contain at least one interval")
            return v

        @model_validator(mode="after")
        def _structure(self) -> "CanonicalModel":
            length = self.reference_line.length_s
            first, last = self.profile[0], self.profile[-1]
            if abs(first.s_from) > PROFILE_TILING_TOL_M:
                raise ValueError(f"profile must start at s=0 (starts at {first.s_from})")
            if abs(last.s_to - length) > PROFILE_TILING_TOL_M:
                raise ValueError(f"profile must end at length_s {length} (ends at {last.s_to})")
            for a, b in zip(self.profile, self.profile[1:]):
                if abs(a.s_to - b.s_from) > PROFILE_TILING_TOL_M:
                    raise ValueError(f"profile intervals must be contiguous: {a.id} ends {a.s_to}, {b.id} starts {b.s_from}")
            for name, items in (("entity", self.entities), ("building", self.buildings),
                                ("contradiction", self.contradictions), ("interval", self.profile)):
                ids = [i.id for i in items]
                if len(set(ids)) != len(ids):
                    raise ValueError(f"{name} ids must be unique")
            if self.kind is ModelKind.legacy and not self.legacy:
                raise ValueError("kind=legacy requires legacy=true")
            return self

        # -------- convenience (pure)

        @property
        def length_s(self) -> float:
            return self.reference_line.length_s

        def entity(self, entity_id: str) -> Entity | None:
            return next((e for e in self.entities if e.id == entity_id), None)

        def interval_at(self, s: float) -> ProfileInterval:
            """Interval containing ``s`` (clamped into ``[0, length_s]``)."""
            for iv in self.profile:
                if s < iv.s_to:
                    return iv
            return self.profile[-1]

        def computed_stats(self) -> ModelStats:
            removed = sum(1 for e in self.entities if e.state is EntityState.removed)
            return ModelStats(
                entity_count=len(self.entities) - removed,
                removed_count=removed,
                building_count=len(self.buildings),
                observation_count=len(self.fused_observation_ids),
            )


# ------------------------------------------------------------------ critique ops (consumed by merge.apply_critique)


class FlagEntityOp(_M):
    op: Literal["flag_entity"] = "flag_entity"
    entity_id: str
    reason: str = ""


class NudgeEntityOp(_M):
    op: Literal["nudge_entity"] = "nudge_entity"
    entity_id: str
    ds_m: float
    dt_m: float
    reason: str = ""


class SetFootpathStateOp(_M):
    op: Literal["set_footpath_state"] = "set_footpath_state"
    side: Side
    s_from: float
    s_to: float
    state: FootpathState
    reason: str = ""


class NudgeEdgeOp(_M):
    op: Literal["nudge_edge"] = "nudge_edge"
    kind: StripKind
    side: StripSide
    s_from: float
    s_to: float
    dt_m: float
    reason: str = ""


CritiqueOp = Annotated[
    Union[FlagEntityOp, NudgeEntityOp, SetFootpathStateOp, NudgeEdgeOp], Field(discriminator="op")
]


# ------------------------------------------------------------------ diff (spec/data.md -> Model diff)


class DiffEntityRef(_M):
    id: str
    type: str
    s: float
    t: float
    confidence: float
    description: str = ""


class DiffEntityRemoved(_M):
    id: str
    type: str
    s: float
    t: float
    reason: str = ""


class DiffEntityChanged(_M):
    id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class DiffEntities(_M):
    added: list[DiffEntityRef] = Field(default_factory=list)
    removed: list[DiffEntityRemoved] = Field(default_factory=list)
    revived: list[DiffEntityRef] = Field(default_factory=list)
    changed: list[DiffEntityChanged] = Field(default_factory=list)
    unchanged: int = 0


class DiffIntervalChange(_M):
    s_from: float
    s_to: float
    what: str


class DiffProfile(_M):
    changed_intervals: list[DiffIntervalChange] = Field(default_factory=list)


class DiffBuildings(_M):
    added: list[dict[str, Any]] = Field(default_factory=list)
    changed: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)


class FidelityImproved(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    s_from: float
    s_to: float
    from_: str = Field(alias="from")  # "L1"
    to: str


class DiffFidelity(_M):
    improved: list[FidelityImproved] = Field(default_factory=list)
    unchanged_len_m: float = 0.0
    overall: tuple[float, float] = (0.0, 0.0)


class DiffContradictions(_M):
    opened: list[dict[str, Any]] = Field(default_factory=list)
    resolved: list[dict[str, Any]] = Field(default_factory=list)


class DiffObservations(_M):
    fused_added: int = 0


class ModelDiff(_M):
    from_version: int | None = None
    to_version: int
    is_empty: bool = True
    entities: DiffEntities = Field(default_factory=DiffEntities)
    profile: DiffProfile = Field(default_factory=DiffProfile)
    buildings: DiffBuildings = Field(default_factory=DiffBuildings)
    fidelity: DiffFidelity = Field(default_factory=DiffFidelity)
    contradictions: DiffContradictions = Field(default_factory=DiffContradictions)
    observations: DiffObservations = Field(default_factory=DiffObservations)
    summary_lines: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ run report (spec/data.md -> Run report)


class ReportSkipped(_M):
    station_s: float | None = None
    view: str | None = None
    reason: str


class ReportImages(_M):
    planned: int = 0
    used_total: int = 0
    new: int = 0
    cached: int = 0
    fused_this_run: int = 0
    by_source: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    by_view: dict[str, int] = Field(default_factory=dict)
    new_observation_ids: list[str] = Field(default_factory=list)
    skipped: list[ReportSkipped] = Field(default_factory=list)


class ReportNewDetection(_M):
    id: str
    type: str
    description: str = ""
    confidence: float


class ReportChanged(_M):
    id: str
    what: str


class ReportContradicted(_M):
    id: str
    description: str = ""


class ReportDetections(_M):
    new: list[ReportNewDetection] = Field(default_factory=list)
    changed: list[ReportChanged] = Field(default_factory=list)
    removed: list[ReportChanged] = Field(default_factory=list)
    contradicted: list[ReportContradicted] = Field(default_factory=list)
    unchanged_count: int = 0


class ReportFidelitySide(_M):
    overall: float = 0.0
    histogram_m: dict[str, float] = Field(default_factory=dict)


class ReportBin(_M):
    """Fidelity of one bin (5 m) before and after the run (levels 0-3 and the fractional per-bin score)."""

    s_from: float
    s_to: float
    level_before: int = Field(ge=0, le=3)
    level_after: int = Field(ge=0, le=3)


class ReportFidelity(_M):
    before: ReportFidelitySide = Field(default_factory=ReportFidelitySide)
    after: ReportFidelitySide = Field(default_factory=ReportFidelitySide)
    improved_intervals: list[FidelityImproved] = Field(default_factory=list)
    bins: list[ReportBin] = Field(default_factory=list)  # per 5 m bin, before -> after (empty on pre-r3 reports)


class ReportQuality(_M):
    """What a run spent on quality (the "Max quality" preset or explicit passes / review rounds)."""

    preset: bool = False
    model: str | None = None
    extraction_passes: int = 1
    review_rounds_planned: int = 0
    review_rounds_run: int = 0
    early_stop: str = ""          # why the review loop ended before its last round ("" = it did not)
    extraction_calls: int = 0     # paid model calls this run (all kinds)
    cache_hits: int = 0           # extractions / reviews answered from storage at zero cost
    passes_skipped: int = 0
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    agreement: dict[str, int] = Field(default_factory=dict)  # detections_in / detections_out / unanimous / disputed


class ReportConfidenceChange(_M):
    entity_id: str
    type: str
    before: float
    after: float
    reason: str = ""


class ReportCost(_M):
    anthropic_usd: float = 0.0
    anthropic_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    google_new_requests: int = 0
    google_cached: int = 0
    google_est_usd: float = 0.0
    total_usd: float = 0.0
    global_budget_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    share_of_global_budget: float = 0.0  # anthropic_usd / global budget; above 0.25 the report carries a (non-blocking) warning

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_run_cap(cls, data: Any) -> Any:  # reports stored before the per-run cap was removed
        if isinstance(data, dict) and "run_cap_usd" in data:
            data = {k: v for k, v in data.items() if k != "run_cap_usd"}
        return data


class RunReport(_M):
    run_id: str
    segment_slug: str
    mode: Literal["generate", "acquire"]
    model: str | None = None  # the LLM chosen for this run (vision + review); null on reports written before Phase 2.1 r2
    base_version: int | None = None
    result_version: int | None = None
    version_created: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float = 0.0
    images: ReportImages = Field(default_factory=ReportImages)
    detections: ReportDetections = Field(default_factory=ReportDetections)
    profile_changes: list[str] = Field(default_factory=list)
    fidelity: ReportFidelity = Field(default_factory=ReportFidelity)
    confidence_changes: list[ReportConfidenceChange] = Field(default_factory=list)
    cost: ReportCost = Field(default_factory=ReportCost)
    quality: ReportQuality | None = None
    warnings: list[str] = Field(default_factory=list)
    degraded: bool = False
    summary: str = ""
    suggestions: list[str] = Field(default_factory=list)
