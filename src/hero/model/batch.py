"""Projected evidence batches: ``hero.model.merge``'s input contract (spec/architecture.md -> Contracts).

Plain frozen dataclasses (no validation, no I/O). Producers: ``hero.fusion.convert`` (via
``hero.model.projection``) and ``hero.model.coverage.visible_regions``.

``ProfileEvidence`` is named in the Layout table but has no separate signature in Contracts; the
simplest reading is adopted: it is the union of the two profile-evidence record types a batch carries,
``StripSample | StateSpan``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

from hero.model.types import Condition, EntityType, FootpathState, Material, StripKind

SideLR = Literal["left", "right"]


@dataclass(frozen=True)
class VisibleRegion:
    observation_id: str
    s_from: float
    s_to: float
    side: Literal["left", "right", "both"]
    weight: float


@dataclass(frozen=True)
class ProjectedDetection:
    key: str
    observation_id: str
    type: EntityType
    subtype: str | None
    s: float
    t: float
    sigma_m: float
    extent_s: float
    extent_t: float
    height_m: float | None
    orientation_deg: float | None
    attributes: dict[str, str | float | bool]
    description: str
    raw_confidence: float
    crop_px: tuple[int, int, int, int] | None
    weight: float
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StripSample:
    s: float
    kind: StripKind
    side: Literal["left", "right", "centre"]
    t_lo: float
    t_hi: float
    material: Material
    condition: Condition
    weight: float
    observation_id: str


@dataclass(frozen=True)
class StateSpan:
    observation_id: str
    side: SideLR
    s_from: float
    s_to: float
    state: FootpathState
    weight: float


@dataclass(frozen=True)
class FacadeDetection:
    observation_id: str
    side: SideLR
    s_from: float
    s_to: float
    colour_hex: str | None
    storeys: int | None
    description: str
    crop_px: tuple[int, int, int, int]
    weight: float


ProfileEvidence = Union[StripSample, StateSpan]


@dataclass(frozen=True)
class ProjectedBatch:
    batch_id: str
    observation_ids: tuple[str, ...]
    detections: tuple[ProjectedDetection, ...] = field(default_factory=tuple)
    strips: tuple[StripSample, ...] = field(default_factory=tuple)
    states: tuple[StateSpan, ...] = field(default_factory=tuple)
    facades: tuple[FacadeDetection, ...] = field(default_factory=tuple)
    visible: tuple[VisibleRegion, ...] = field(default_factory=tuple)
