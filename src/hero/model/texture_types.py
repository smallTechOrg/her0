"""Texture references (spec/data.md -> "Texture references" + r2 bake-frame amendment).

Produced by ``hero.imagery.bake.bake_textures``, consumed by compile and the mesh builder. The ground strip is
indexed by the OpenDRIVE frame: ``t`` left-positive in ``[t_lo, t_hi] = [-14, +14]``; column 0 = ``t_hi``,
row 0 = ``s_to``; ``u = (t_hi - t)/(t_hi - t_lo)``, ``v = (s_to - s)/(s_to - s_from)``. Never mirrored.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GroundBinSource = Literal["satellite_hires", "satellite_overview", "interpolated", "procedural"]
FrontageBinSource = Literal["user_photo", "street_view", "interpolated", "none"]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroundStripRef(_S):
    url: str  # "/media/textures/<slug>/ground_v<N>_<sha8>.jpg"
    size_px: tuple[int, int]  # (width, height); longest side <= 1024
    s_from: float
    s_to: float
    t_lo: float = -14.0
    t_hi: float = 14.0
    m_per_px: float = Field(gt=0)
    bin_source: list[GroundBinSource]  # one per 5 m bin


class FrontageStripRef(_S):
    side: Literal["modelled", "opposite"]
    url: str
    size_px: tuple[int, int]  # (<=1024, <=256)
    s_from: float
    s_to: float
    bin_source: list[FrontageBinSource]


class TextureRefs(_S):
    ground: GroundStripRef | None = None
    frontage: dict[str, FrontageStripRef] = Field(default_factory=dict)  # keys "modelled" / "opposite"
    tints: dict[str, str] = Field(default_factory=dict)  # entity_id -> "#rrggbb"
    procedural_seed: int = 0
