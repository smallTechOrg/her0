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

Heading = Literal["forward", "back", "left", "right", "forward_left", "forward_right", "back_left", "back_right"]
HEADING_OFFSET_DEG: dict[str, float] = {"forward": 0.0, "forward_right": 45.0, "right": 90.0, "back_right": 135.0,
                                        "back": 180.0, "back_left": 225.0, "left": 270.0, "forward_left": 315.0}
SPACING_RANGE = (4.0, 40.0)
ZOOM_RANGE = (18, 21)
MAX_IMAGES_RANGE = (1, 2000)
PITCH_RANGE = (-45, 45)      # camera tilt: negative looks down (road surface, kerbs, potholes), positive up (facades)
FOV_RANGE = (20, 120)        # narrower = zoomed in: more detail per pixel from the same panorama
DEFAULT_PITCH = 0
DEFAULT_FOV = 90
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


MAX_TARGETS = 200
TARGET_KINDS = ("streetview", "satellite_crop")


class PlanTarget(BaseModel):
    """One explicitly requested frame (``ImageryPlan.targets``): a Street View frame at station ``station_s``
    facing ``view`` (relative to travel), or a satellite crop (centred on the line at ``station_s``) at ``zoom``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["streetview", "satellite_crop"]
    station_s: float = Field(ge=0)
    view: Heading | None = None
    zoom: int | None = Field(default=None, ge=ZOOM_RANGE[0], le=ZOOM_RANGE[1])

    @model_validator(mode="after")
    def _kind_fields(self) -> "PlanTarget":
        if self.kind == "streetview" and self.view is None:
            raise ValueError("a streetview target needs a view (forward|back|left|right)")
        if self.kind == "satellite_crop" and self.view is not None:
            raise ValueError("a satellite_crop target has no view")
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
    sat_extra_zoom: int | None = Field(default=None, ge=ZOOM_RANGE[0], le=ZOOM_RANGE[1])  # overlapping second crop set
    targets: list[PlanTarget] | None = Field(default=None, max_length=MAX_TARGETS)
    # Every Street View station is shot at each heading x pitch x fov (one panorama gives many distinct frames).
    pitches_deg: list[int] = Field(default_factory=lambda: [DEFAULT_PITCH], min_length=1)
    fovs_deg: list[int] = Field(default_factory=lambda: [DEFAULT_FOV], min_length=1)

    @model_validator(mode="after")
    def _dedupe_headings(self) -> "ImageryPlan":
        self.headings = list(dict.fromkeys(self.headings))
        self.pitches_deg = list(dict.fromkeys(self.pitches_deg))
        self.fovs_deg = list(dict.fromkeys(self.fovs_deg))
        for p in self.pitches_deg:
            if not PITCH_RANGE[0] <= p <= PITCH_RANGE[1]:
                raise ValueError(f"pitches_deg: {p} is outside {PITCH_RANGE[0]}..{PITCH_RANGE[1]}")
        for f in self.fovs_deg:
            if not FOV_RANGE[0] <= f <= FOV_RANGE[1]:
                raise ValueError(f"fovs_deg: {f} is outside {FOV_RANGE[0]}..{FOV_RANGE[1]}")
        return self

    @property
    def frames_per_station(self) -> int:
        return len(self.headings) * len(self.pitches_deg) * len(self.fovs_deg)


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
    max_images 1-2000, pitch -45..45, fov 20..120)."""
    allowed = set(ImageryPlan.model_fields)
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ValueError(f"unknown imagery field(s): {', '.join(unknown)}")
    if overrides.get("targets") is not None:
        clash = sorted(k for k in ("sv_spacing_m", "headings", "s_range") if overrides.get(k) is not None)
        if clash:
            raise ValueError(f"targets: cannot be combined with {', '.join(clash)} (targets are planned exactly)")
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
    pitch_deg: int = DEFAULT_PITCH
    fov_deg: int = DEFAULT_FOV


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
    """``lo + k*spacing`` for every multiple within the span, plus the far end when the leftover is >= half a spacing."""
    out: list[float] = []
    n = 0
    while n * spacing <= span + 1e-9:
        out.append(lo + n * spacing)
        n += 1
    if span - (n - 1) * spacing >= 0.5 * spacing - 1e-9:
        out.append(lo + span)
    return out


def overview_zoom_for(length_m: float, lat: float, preferred: int) -> int:
    """Highest zoom <= ``preferred`` whose 1280 px tile covers ``length_m`` plus a margin."""
    zoom = preferred
    while zoom > MIN_OVERVIEW_ZOOM and meters_per_px(lat, zoom, OVERVIEW_SCALE) * OVERVIEW_TILE_PX < length_m * OVERVIEW_MARGIN:
        zoom -= 1
    return zoom


def _plan_targets(ref: ReferenceLine, plan: ImageryPlan) -> tuple[list[PlannedImage], PlanInfo]:
    """Exactly ``plan.targets`` (no grid, no ``max_images`` trimming) plus the overview once."""
    total = ref.length_s
    notes: list[str] = []
    items: list[PlannedImage] = []
    seen: set[tuple] = set()
    for t in plan.targets or []:
        if t.station_s > total + 1e-6:
            raise ValueError(f"target station {t.station_s:g} m lies outside the line (0..{total:.1f} m)")
        s = min(t.station_s, total)
        key = (t.kind, t.view, round(s, 3), t.zoom)
        if key in seen:
            notes.append(f"duplicate target at {s:.1f} m ignored")
            continue
        seen.add(key)
        lat, lon = ref.point_at(s, 0.0)
        if t.kind == "streetview":
            items.append(PlannedImage("streetview", t.view, s, lat, lon,
                                      (ref.bearing_deg(s) + HEADING_OFFSET_DEG[t.view or "forward"]) % 360.0, None))
        else:
            items.append(PlannedImage("satellite_crop", None, s, lat, lon, None, t.zoom or plan.sat_crop_zoom))
    n_overview = 1 if plan.include_overview else 0
    if n_overview:
        mid = total / 2.0
        lat, lon = ref.point_at(mid, 0.0)
        items.append(PlannedImage("satellite_overview", None, mid, lat, lon, None,
                                  overview_zoom_for(total, lat, plan.overview_zoom)))
    n_sv = sum(1 for i in items if i.kind == "streetview")
    n_crop = sum(1 for i in items if i.kind == "satellite_crop")
    est = n_sv * EST_COST_SV_USD + (n_crop + n_overview) * EST_COST_SAT_USD
    info = PlanInfo(trimmed=False, effective_spacing_m=0.0,
                    counts={"streetview": n_sv, "satellite_crop": n_crop, "satellite_overview": n_overview},
                    requested_spacing_m=plan.sv_spacing_m, est_cost_usd=round(est, 4), notes=notes)
    return items, info


def plan_images(ref: ReferenceLine, plan: ImageryPlan) -> tuple[list[PlannedImage], PlanInfo]:
    if plan.targets is not None:
        return _plan_targets(ref, plan)
    total = ref.length_s
    lo, hi = 0.0, total
    if plan.s_range is not None:
        lo, hi = max(0.0, plan.s_range.s_from), min(total, plan.s_range.s_to)
        if not hi > lo:
            raise ValueError(f"s_range [{plan.s_range.s_from}, {plan.s_range.s_to}] lies outside the line (0..{total:.1f} m)")
    span = hi - lo
    n_head = plan.frames_per_station  # frames per station (headings x pitches x fovs)
    notes: list[str] = []

    n_overview = 1 if plan.include_overview else 0
    n_crops = max(1, math.ceil(span / plan.sat_crop_spacing_m - 1e-9))
    # Reserve room for two stations x all headings; widen crops only when even that does not fit.
    non_sv_budget = max(0, plan.max_images - MIN_SV_STATIONS * n_head)
    if n_crops + n_overview > non_sv_budget:
        n_crops = max(0, non_sv_budget - n_overview)
        notes.append(f"close-up crops reduced to {n_crops} to stay within max_images={plan.max_images}")

    # Overlapping tile set (max quality): crops at ``sat_extra_zoom`` centred on the seams between the primary crops.
    extra_pos: list[float] = []
    if plan.sat_extra_zoom is not None and n_crops:
        extra_pos = [lo + span / 2.0] if n_crops == 1 else [lo + i * span / n_crops for i in range(1, n_crops)]
        room = max(0, non_sv_budget - n_crops - n_overview)
        if len(extra_pos) > room:
            notes.append(f"overlapping crops reduced to {room} to stay within max_images={plan.max_images}")
            extra_pos = extra_pos[:room]
    n_extra = len(extra_pos)

    requested = _stations(lo, span, plan.sv_spacing_m)
    sv_budget = plan.max_images - n_crops - n_extra - n_overview
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
        for fov in plan.fovs_deg:
            for pitch in plan.pitches_deg:
                for name in plan.headings:
                    items.append(PlannedImage("streetview", name, s, lat, lon,
                                              (bearing + HEADING_OFFSET_DEG[name]) % 360.0, None, pitch, fov))
    # Hard clamp: with a tiny max_images even one station x all headings may not fit; never plan more than the cap.
    sv_allowed = max(0, plan.max_images - n_crops - n_extra - n_overview)
    if len(items) > sv_allowed:
        items = items[:sv_allowed]
        trimmed = True
        notes.append(f"Street View frames limited to {sv_allowed} to stay within max_images={plan.max_images}")
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

    for s in extra_pos:
        lat, lon = ref.point_at(s, 0.0)
        items.append(PlannedImage("satellite_crop", None, s, lat, lon, None, plan.sat_extra_zoom))
    n_crops += n_extra
    counts = {"streetview": sum(1 for i in items if i.kind == "streetview"), "satellite_crop": n_crops, "satellite_overview": n_overview}
    est = counts["streetview"] * EST_COST_SV_USD + (n_crops + n_overview) * EST_COST_SAT_USD
    info = PlanInfo(trimmed=trimmed, effective_spacing_m=round(effective, 3), counts=counts,
                    requested_spacing_m=plan.sv_spacing_m, est_cost_usd=round(est, 4), notes=notes)
    return items, info
