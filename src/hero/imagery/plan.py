"""Imagery plan: which Street View frames and satellite crops to buy for a reference line.

Pure and deterministic (no network, DB or clock). The planner is also the first cost guard: it never plans
more than ``plan.max_images`` paid items (Street View frames + close-up crops + the overview). When the
requested spacing would exceed that, the station spacing is widened (never a heading dropped) and
``PlanInfo.trimmed`` says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hero.config import Settings
from hero.model.refline import ReferenceLine
from hero.scene.geo import meters_per_px

Heading = Literal["forward", "back", "left", "right"]
HEADING_OFFSET_DEG: dict[str, float] = {"forward": 0.0, "right": 90.0, "back": 180.0, "left": 270.0}
SPACING_RANGE = (4.0, 40.0)
ZOOM_RANGE = (18, 21)
MAX_IMAGES_RANGE = (1, 200)
EST_COST_SV_USD = 0.007
EST_COST_SAT_USD = 0.002
OVERVIEW_SCALE = 2
OVERVIEW_TILE_PX = 640 * OVERVIEW_SCALE
OVERVIEW_MARGIN = 1.15  # the overview must cover the line plus 15 %
MIN_OVERVIEW_ZOOM = 16
MIN_SV_STATIONS = 2


class SRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s_from: float = Field(ge=0)
    s_to: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SRange":
        if not self.s_to > self.s_from:
            raise ValueError("s_range.s_to must exceed s_range.s_from")
        return self


class ImageryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sv_spacing_m: float = Field(ge=SPACING_RANGE[0], le=SPACING_RANGE[1])
    headings: list[Heading] = Field(min_length=1)
    sat_crop_zoom: int = Field(ge=ZOOM_RANGE[0], le=ZOOM_RANGE[1])
    sat_crop_spacing_m: float = Field(gt=0)
    include_overview: bool = True
    max_images: int = Field(ge=MAX_IMAGES_RANGE[0], le=MAX_IMAGES_RANGE[1])
    s_range: SRange | None = None
    overview_zoom: int = Field(default=19, ge=14, le=21)

    @model_validator(mode="after")
    def _dedupe_headings(self) -> "ImageryPlan":
        seen: list[Heading] = []
        for h in self.headings:
            if h not in seen:
                seen.append(h)
        self.headings = seen
        return self


def default_plan(settings: Settings) -> ImageryPlan:
    return ImageryPlan(
        sv_spacing_m=settings.sv_spacing_m, headings=list(settings.sv_headings_list),  # type: ignore[arg-type]
        sat_crop_zoom=settings.sat_crop_zoom, sat_crop_spacing_m=settings.sat_crop_spacing_m,
        include_overview=True, max_images=settings.max_images_per_run, s_range=None,
        overview_zoom=settings.satellite_zoom,
    )


def merge_plan(base: ImageryPlan, overrides: dict[str, Any]) -> ImageryPlan:
    """``base`` with the non-``None`` ``overrides`` applied and re-validated. Raises ``ValueError`` (message
    names the offending field) for unknown keys or out-of-range values (spacing 4-40, >= 1 heading, zoom 18-21,
    max_images 1-200)."""
    allowed = set(ImageryPlan.model_fields)
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ValueError(f"unknown imagery field(s): {', '.join(unknown)}")
    data = base.model_dump()
    for key, value in overrides.items():
        if value is not None:
            data[key] = value
    try:
        return ImageryPlan.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "imagery"
        raise ValueError(f"{loc}: {first['msg']}") from exc


@dataclass(frozen=True)
class PlannedImage:
    kind: str  # streetview | satellite_crop | satellite_overview
    view: str | None
    station_s: float
    lat: float
    lon: float
    heading_deg: float | None
    zoom: int | None


@dataclass(frozen=True)
class PlanInfo:
    trimmed: bool
    effective_spacing_m: float
    counts: dict[str, int]
    requested_spacing_m: float = 0.0
    est_cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def paid_total(self) -> int:
        return sum(self.counts.values())


def _stations(lo: float, span: float, spacing: float) -> list[float]:
    out: list[float] = []
    n = 0
    while n * spacing < span - 1e-9:
        out.append(lo + n * spacing)
        n += 1
    out.append(lo + span)
    return out


def overview_zoom_for(length_m: float, lat: float, preferred: int) -> int:
    """Highest zoom <= ``preferred`` whose 1280 px tile covers ``length_m`` plus a margin."""
    zoom = preferred
    while zoom > MIN_OVERVIEW_ZOOM and meters_per_px(lat, zoom, OVERVIEW_SCALE) * OVERVIEW_TILE_PX < length_m * OVERVIEW_MARGIN:
        zoom -= 1
    return zoom


def plan_images(ref: ReferenceLine, plan: ImageryPlan) -> tuple[list[PlannedImage], PlanInfo]:
    total = ref.length_s
    lo, hi = 0.0, total
    if plan.s_range is not None:
        lo, hi = max(0.0, plan.s_range.s_from), min(total, plan.s_range.s_to)
        if not hi > lo:
            raise ValueError(f"s_range [{plan.s_range.s_from}, {plan.s_range.s_to}] lies outside the line (0..{total:.1f} m)")
    span = hi - lo
    n_head = len(plan.headings)
    notes: list[str] = []

    n_overview = 1 if plan.include_overview else 0
    n_crops = max(1, math.ceil(span / plan.sat_crop_spacing_m - 1e-9))
    # Reserve room for two stations x all headings; widen crops only when even that does not fit.
    non_sv_budget = max(0, plan.max_images - MIN_SV_STATIONS * n_head)
    if n_crops + n_overview > non_sv_budget:
        n_crops = max(0, non_sv_budget - n_overview)
        notes.append(f"close-up crops reduced to {n_crops} to stay within max_images={plan.max_images}")

    requested = _stations(lo, span, plan.sv_spacing_m)
    sv_budget = plan.max_images - n_crops - n_overview
    n_max = sv_budget // n_head
    trimmed = len(requested) > n_max
    if trimmed:
        n_st = max(1, n_max)
        if n_st >= 2:
            stations = [lo + i * span / (n_st - 1) for i in range(n_st)]
            effective = span / (n_st - 1)
        else:
            stations = [lo + span / 2.0]
            effective = span
        notes.append(f"Street View spacing widened from {plan.sv_spacing_m:g} m to {effective:.1f} m "
                     f"to fit max_images={plan.max_images}")
    else:
        stations, effective = requested, plan.sv_spacing_m

    items: list[PlannedImage] = []
    for s in stations:
        lat, lon = ref.point_at(s, 0.0)
        bearing = ref.bearing_deg(s)
        for name in plan.headings:
            items.append(PlannedImage("streetview", name, s, lat, lon, (bearing + HEADING_OFFSET_DEG[name]) % 360.0, None))
    if n_overview:
        mid = lo + span / 2.0
        lat, lon = ref.point_at(mid, 0.0)
        zoom = overview_zoom_for(total, lat, plan.overview_zoom)
        if zoom != plan.overview_zoom:
            notes.append(f"overview zoom lowered to {zoom} so the tile covers the whole line")
        items.append(PlannedImage("satellite_overview", None, mid, lat, lon, None, zoom))
    for i in range(n_crops):
        s = lo + (i + 0.5) * span / n_crops
        lat, lon = ref.point_at(s, 0.0)
        items.append(PlannedImage("satellite_crop", None, s, lat, lon, None, plan.sat_crop_zoom))

    counts = {"streetview": len(stations) * n_head, "satellite_crop": n_crops, "satellite_overview": n_overview}
    est = counts["streetview"] * EST_COST_SV_USD + (n_crops + n_overview) * EST_COST_SAT_USD
    info = PlanInfo(trimmed=trimmed, effective_spacing_m=round(effective, 3), counts=counts,
                    requested_spacing_m=plan.sv_spacing_m, est_cost_usd=round(est, 4), notes=notes)
    return items, info
