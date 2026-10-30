"""Gap analysis types (spec/data.md -> "Gap analysis"). Produced by ``hero.model.gaps.analyze_gaps``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BIN_M = 5.0

GroundEvidence = Literal["satellite_hires", "satellite_overview", "none"]
GroundRendered = Literal["satellite_hires", "satellite_overview", "interpolated", "procedural"]
FrontageEvidence = Literal["osm", "photo", "osm+photo", "none"]
FrontageRendered = Literal["osm", "vision", "interpolated", "prior"]
FootpathEvidence = Literal["traced", "state_only", "prior"]
FootpathRendered = Literal["traced", "state_only", "interpolated", "prior"]
InterpWhat = Literal["ground_texture", "frontage", "footpath_state", "kerb", "furniture_unknown"]
InterpMethod = Literal["neighbours", "prior", "osm", "procedural"]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class GroundGap(_S):
    evidence: GroundEvidence
    rendered: GroundRendered


class FrontageSideGap(_S):
    evidence: FrontageEvidence
    rendered: FrontageRendered


class FrontageGap(_S):
    modelled: FrontageSideGap
    opposite: FrontageSideGap


class FootpathGap(_S):
    evidence: FootpathEvidence
    rendered: FootpathRendered


class FurnitureGap(_S):
    evidence: Literal["street_level", "none"]


class GapBin(_S):
    index: int = Field(ge=0)
    s_from: float
    s_to: float
    fidelity_level: int = Field(ge=0, le=3)
    ground: GroundGap
    frontage: FrontageGap
    footpath: FootpathGap
    furniture: FurnitureGap
    hint: str = ""


class GapCounts(_S):
    ground: int = Field(ge=0)
    frontage_modelled: int = Field(ge=0)
    frontage_opposite: int = Field(ge=0)
    footpath: int = Field(ge=0)
    furniture: int = Field(default=0, ge=0)


class BlankCounts(_S):
    ground: int = Field(ge=0)
    frontage_modelled: int = Field(ge=0)
    frontage_opposite: int = Field(ge=0)
    footpath: int = Field(ge=0)


class Interpolation(_S):
    what: InterpWhat
    side: str  # "left" | "right" | "modelled" | "opposite" | "both"
    s_from: float
    s_to: float
    method: InterpMethod
    from_s: list[float] = Field(default_factory=list)
    note: str = ""


class GapReport(_S):
    gap_report: Literal[1] = 1
    model_version: int = Field(ge=0)
    bin_m: float = BIN_M
    length_s: float = Field(gt=0)
    bins: list[GapBin]
    uncovered: GapCounts
    blank_after_compile: BlankCounts
    interpolations: list[Interpolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bins_tile(self) -> "GapReport":
        if not self.bins:
            raise ValueError("gap report needs at least one bin")
        for i, b in enumerate(self.bins):
            if b.index != i:
                raise ValueError(f"bin {i} has index {b.index}")
        for a, b in zip(self.bins, self.bins[1:]):
            if abs(a.s_to - b.s_from) > 1e-6:
                raise ValueError("gap bins must be contiguous")
        if abs(self.bins[0].s_from) > 1e-6 or abs(self.bins[-1].s_to - self.length_s) > 1e-3:
            raise ValueError("gap bins must tile [0, length_s]")
        return self

    def blank_total(self) -> int:
        return sum(self.blank_after_compile.model_dump().values())
