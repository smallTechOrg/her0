"""Perception output contracts (spec/data.md -> "Perception output contracts").

Raw model outputs, validated by Pydantic and stored verbatim in ``observation_extractions``. Validation is
deliberately *forgiving where a retry would only waste money* (unknown enum words fall back to ``unknown`` /
``other``, over-long free text is truncated, boxes are rounded to ints) and *strict where downstream geometry
would be wrong* (polyline shape, confidences outside [0, 1], overlapping footpath runs, indices < 1).

Pixel boxes are ``[x, y, w, h]`` in the sent image's own pixel space; ``image_index`` is 1-based in caption order.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from hero.model.types import (
    Condition,
    CritiqueOp,
    EntityType,
    FootpathState,
    Material,
    StripKind,
)

Unit = Annotated[float, Field(ge=0.0, le=1.0)]
RUN_OVERLAP_TOL_PX = 1.0


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------------ tolerant coercions


def _norm(value: Any) -> Any:
    return value.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(value, str) else value


def _enum_or(enum_cls: type, fallback: str):
    def coerce(value: Any) -> Any:
        v = _norm(value)
        if isinstance(v, str):
            try:
                return enum_cls(v)
            except ValueError:
                return enum_cls(fallback)
        return v
    return coerce


def _truncate(limit: int):
    def cut(value: Any) -> Any:
        return value[:limit] if isinstance(value, str) else value
    return cut


def _box(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
        return tuple(int(round(v)) for v in value)
    return value


Box = Annotated[tuple[int, int, int, int], BeforeValidator(_box)]
Point = tuple[float, float]
EType = Annotated[EntityType, BeforeValidator(_enum_or(EntityType, "other"))]
SKind = Annotated[StripKind, BeforeValidator(_enum_or(StripKind, "unknown"))]
Mat = Annotated[Material, BeforeValidator(_enum_or(Material, "unknown"))]
Cond = Annotated[Condition, BeforeValidator(_enum_or(Condition, "unknown"))]
FState = Annotated[FootpathState, BeforeValidator(_enum_or(FootpathState, "unknown"))]
Text200 = Annotated[str, BeforeValidator(_truncate(200))]
Text300 = Annotated[str, BeforeValidator(_truncate(300))]
Attrs = dict[str, str | float | bool]


def _check_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise ValueError(f"box_px must have positive width and height, got {list(box)}")
    if x < 0 or y < 0:
        raise ValueError(f"box_px must start inside the image, got {list(box)}")
    return box


# ------------------------------------------------------------------ overhead


class OverheadStrip(_Out):
    kind: SKind
    side: Literal["left", "right", "centre"]
    material: Mat = Material.unknown
    condition: Cond = Condition.unknown
    edge_a_px: list[Point] = Field(min_length=2)
    edge_b_px: list[Point] = Field(min_length=2)
    confidence: Unit
    description: str = ""

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, v: Any) -> Any:
        v = _norm(v)
        return "centre" if v in ("center", "middle") else v

    @field_validator("edge_a_px", "edge_b_px")
    @classmethod
    def _y_non_decreasing(cls, pts: list[Point]) -> list[Point]:
        for (_, y0), (_, y1) in zip(pts, pts[1:]):
            if y1 < y0:
                raise ValueError("edge polylines must have non-decreasing y (top to bottom)")
        return pts


class FootpathRun(_Out):
    side: Literal["left", "right"]
    y_from_px: float = Field(ge=0)
    y_to_px: float = Field(ge=0)
    state: FState
    confidence: Unit
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "FootpathRun":
        if self.y_to_px <= self.y_from_px:
            raise ValueError("footpath run needs y_to_px > y_from_px")
        return self


class OverheadObject(_Out):
    type: EType
    box_px: Box
    attributes: Attrs = Field(default_factory=dict)
    description: str = ""
    confidence: Unit

    @field_validator("box_px")
    @classmethod
    def _box_ok(cls, v: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return _check_box(v)


class OverheadWindowOut(_Out):
    image_index: int = Field(ge=1)
    strips: list[OverheadStrip] = Field(default_factory=list)
    footpath_runs: list[FootpathRun] = Field(default_factory=list)
    objects: list[OverheadObject] = Field(default_factory=list)
    notes: Text300 = ""

    @model_validator(mode="after")
    def _runs_do_not_overlap(self) -> "OverheadWindowOut":
        for side in ("left", "right"):
            runs = sorted((r for r in self.footpath_runs if r.side == side), key=lambda r: r.y_from_px)
            for a, b in zip(runs, runs[1:]):
                if b.y_from_px < a.y_to_px - RUN_OVERLAP_TOL_PX:
                    raise ValueError(
                        f"footpath_runs on the {side} side overlap ({a.y_from_px}-{a.y_to_px} vs {b.y_from_px}-{b.y_to_px}); "
                        "they must tile the visible rows without overlap")
        return self


class OverheadExtraction(_Out):
    windows: list[OverheadWindowOut] = Field(default_factory=list, max_length=4)


# ------------------------------------------------------------------ street level


class FrameInfo(_Out):
    image_index: int = Field(ge=1)
    visibility_ok: bool = True
    footpath_visible: bool = True
    footpath_side: Literal["left", "right", "both", "none"] = "none"
    footpath_state: FState = FootpathState.unknown
    notes: Text200 = ""

    @field_validator("footpath_side", mode="before")
    @classmethod
    def _side(cls, v: Any) -> Any:
        return _norm(v)


class SizeM(_Out):
    w: float = Field(gt=0, le=40)
    h: float = Field(gt=0, le=40)
    d: float = Field(gt=0, le=60)


class Detection(_Out):
    local_id: str = Field(min_length=1, max_length=32)
    image_index: int = Field(ge=1)
    type: EType
    subtype: str | None = None
    box_px: Box
    touches_ground: bool = True
    est_distance_m: float | None = Field(default=None, ge=0, le=500)
    size_m: SizeM | None = None
    orientation: Literal["parallel", "perpendicular", "unknown"] = "unknown"
    attributes: Attrs = Field(default_factory=dict)
    description: str = ""
    confidence: Unit

    @field_validator("orientation", mode="before")
    @classmethod
    def _orientation(cls, v: Any) -> Any:
        v = _norm(v)
        return v if v in ("parallel", "perpendicular") else "unknown"

    @field_validator("box_px")
    @classmethod
    def _box_ok(cls, v: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return _check_box(v)


class Facade(_Out):
    image_index: int = Field(ge=1)
    box_px: Box
    side: Literal["left", "right"]
    colour_hex: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    storeys: int | None = Field(default=None, ge=1, le=60)
    use: str | None = None
    description: str = ""
    confidence: Unit

    @field_validator("box_px")
    @classmethod
    def _box_ok(cls, v: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return _check_box(v)

    @field_validator("colour_hex", mode="before")
    @classmethod
    def _hex(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) == 6 and not v.startswith("#"):
            return "#" + v
        return v


class StreetExtraction(_Out):
    frames: list[FrameInfo] = Field(default_factory=list)
    detections: list[Detection] = Field(default_factory=list)
    facades: list[Facade] = Field(default_factory=list)
    notes: Text300 = ""


# ------------------------------------------------------------------ critique

MAX_CRITIQUE_OPS = 8
MAX_CRITIQUE_CHARS = 1200


class CritiqueResult(_Out):
    """Reflection verdict. Over-long text/op lists are truncated (never rejected); op bounds
    (|nudge| <= 3 m / 1 m) are enforced by ``hero.model.merge.apply_critique``, which skips and reports violations."""

    accepted: bool
    critique: Annotated[str, BeforeValidator(_truncate(MAX_CRITIQUE_CHARS))] = ""
    ops: list[CritiqueOp] = Field(default_factory=list)

    @field_validator("ops", mode="before")
    @classmethod
    def _cap_ops(cls, v: Any) -> Any:
        return v[:MAX_CRITIQUE_OPS] if isinstance(v, list) else v
