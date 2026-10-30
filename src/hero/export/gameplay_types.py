"""``GameplayV1`` - the game rules as data (spec/portability.md section 5; source ``gameplay.json``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TYPE_ORDER = ("parked_car", "two_wheeler", "cone", "barricade", "pothole", "stall", "low_branch")
Action = Literal["dodge", "jump", "slide"]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class LaneWidths(_S):
    road_edge: float = Field(gt=0)
    road: float = Field(gt=0)


class GpLanes(_S):
    count: Literal[3]
    names: list[str]
    road_edge_inset_m: float = Field(gt=0)
    road_lane_gap_m: float = Field(gt=0)
    min_footpath_to_edge_m: float = Field(gt=0)
    default_footpath_width_m: float = Field(gt=0)
    widths_m: LaneWidths

    @model_validator(mode="after")
    def _names(self) -> "GpLanes":
        if len(self.names) != self.count:
            raise ValueError("lanes.names must have `count` entries")
        return self


class GpRunner(_S):
    speed_start_mps: float = Field(gt=0)
    speed_per_loop_mps: float = Field(ge=0)
    speed_max_mps: float = Field(gt=0)
    lane_tween_ms: int = Field(gt=0)
    jump_ms: int = Field(gt=0)
    slide_ms: int = Field(gt=0)
    jump_height_m: float = Field(gt=0)
    runner_half_m: float = Field(gt=0)
    health_max: int = Field(gt=0)
    hit_damage: int = Field(gt=0)
    invuln_s: float = Field(ge=0)
    start_grace_s: float = Field(ge=0)

    @model_validator(mode="after")
    def _speeds(self) -> "GpRunner":
        if self.speed_max_mps < self.speed_start_mps:
            raise ValueError("speed_max_mps must be >= speed_start_mps")
        return self


class GpTokens(_S):
    value: int = Field(gt=0)
    radius_m: float = Field(gt=0)
    per_intact_m: float = Field(gt=0)
    max_per_street: int = Field(gt=0)
    crossing_lane: int = Field(ge=0, le=2)


class GpWalkability(_S):
    coverage_weight: float
    crossing_bonus: float
    hazard_weight: float
    hazard_norm: float = Field(gt=0)


class GpScoring(_S):
    distance_unit_m: float = Field(gt=0)
    walkability: GpWalkability


class SizeM(_S):
    w: float = Field(gt=0)
    h: float = Field(gt=0)
    d: float = Field(gt=0)


class ObstacleType(_S):
    action: Action
    length_m: float = Field(gt=0)
    size_m: SizeM
    label: str


class DefaultSpawnHints(_S):
    density: float = Field(ge=0, le=1)
    type_weights: dict[str, float]

    @model_validator(mode="after")
    def _w(self) -> "DefaultSpawnHints":
        if tuple(self.type_weights) != TYPE_ORDER:
            raise ValueError(f"type_weights keys must be {TYPE_ORDER} in order")
        if abs(sum(self.type_weights.values()) - 1.0) > 1e-6:
            raise ValueError("type_weights must sum to 1.0")
        return self


class GpSpawner(_S):
    seed_rule: str
    prng: Literal["mulberry32"]
    hash: Literal["fnv1a32"]
    free_start_loop0_m: float = Field(ge=0)
    free_start_later_m: float = Field(ge=0)
    seam_free_m: float = Field(ge=0)
    keepout_margin_m: float = Field(ge=0)
    footpath_missing_margin_m: float = Field(ge=0)
    row_gap_min_m: float = Field(gt=0)
    row_gap_base_m: float = Field(gt=0)
    row_gap_per_loop_m: float = Field(ge=0)
    jitter_max_m: float = Field(ge=0)
    spawn_p_base: float = Field(ge=0, le=1)
    spawn_p_per_loop: float = Field(ge=0)
    spawn_p_max: float = Field(ge=0, le=1)
    density_floor: float = Field(ge=0, le=1)
    density_gain: float = Field(ge=0, le=1)
    second_obstacle_from_loop: int = Field(ge=0)
    second_obstacle_prob: float = Field(ge=0, le=1)
    gap_zone_m: float = Field(ge=0)
    clear_lane_until_loop: int = Field(ge=0)
    action_gap_m: float = Field(ge=0)
    variants: int = Field(gt=0)
    type_boost: float = Field(ge=0)
    lane_weights: dict[str, dict[str, float]]
    default_spawn_hints: DefaultSpawnHints
    window_behind_m: float = Field(ge=0)
    window_ahead_m: float = Field(gt=0)

    @model_validator(mode="after")
    def _lane_weights(self) -> "GpSpawner":
        if list(self.lane_weights) != ["0", "1", "2"]:
            raise ValueError("lane_weights keys must be '0','1','2'")
        for lane, table in self.lane_weights.items():
            for t, w in table.items():
                if t not in TYPE_ORDER or w <= 0:
                    raise ValueError(f"lane {lane}: bad weight entry {t}={w}")
        if self.density_floor + self.density_gain > 1.0 + 1e-9:
            raise ValueError("density_floor + density_gain must be <= 1")
        return self


class GameplayV1(_S):
    schema_: Literal["hero-gameplay/1"] = Field(alias="schema")
    version: int
    lanes: GpLanes
    runner: GpRunner
    tokens: GpTokens
    scoring: GpScoring
    obstacle_types: dict[str, ObstacleType]
    spawner: GpSpawner

    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)

    @model_validator(mode="after")
    def _types(self) -> "GameplayV1":
        if tuple(self.obstacle_types) != TYPE_ORDER:
            raise ValueError(f"obstacle_types must be exactly {TYPE_ORDER} in order")
        return self
