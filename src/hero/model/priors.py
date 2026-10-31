"""The L0 map-only prior model (kind ``prior``, version 0): spec/data.md -> "Profile rules" -> Prior.

Pure: no network, DB or clock (callers pass ``now``). Carriageway centred on the reference line, width
``lanes x 3.0`` (OSM ``lanes``, default 2) or OSM ``width``, clamped to [5, 14] m; on each side a 0.15 m kerb, a
1.5 m footpath (state ``unknown``, weight 0.15) and a forecourt. OSM sidewalk tags / mapped footway ways raise
the footpath weight to 0.4 and (for ways) replace the template footpath offset.

Interpretation notes
--------------------
* ``OsmBuilding`` is not specified in Contracts; defined here with its footprint already in ``(s, t)``.
* The prior profile is tiled in equal intervals of at most 30 m (``ceil(length / 30)`` of them).
* The forecourt strip is 3.0 m wide (template).
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hero.model.ids import building_id
from hero.model.refline import ReferenceLine
from hero.model.types import (
    Building,
    CanonicalModel,
    Condition,
    FootpathState,
    Material,
    ModelKind,
    ProfileInterval,
    ReferenceLineModel,
    Strip,
    StripKind,
)

LANE_WIDTH_M = 3.0
DEFAULT_LANES = 2
CARRIAGEWAY_MIN_M, CARRIAGEWAY_MAX_M = 5.0, 14.0
KERB_WIDTH_M = 0.15
FOOTPATH_WIDTH_M = 1.5
FORECOURT_WIDTH_M = 3.0
PRIOR_WEIGHT = 0.15
OSM_WEIGHT = 0.4
MAX_INTERVAL_M = 30.0
MAX_FOOTWAY_OFFSET_M = 25.0


@dataclass(frozen=True)
class OsmBuilding:
    osm_way_id: int
    footprint: list[tuple[float, float]]  # [(s, t)] polygon (closing point optional)
    height_m: float = 9.0
    storeys: int | None = None


@dataclass(frozen=True)
class OsmContext:
    highway_tags: dict[str, str] = field(default_factory=dict)
    footway_polylines: list[list[tuple[float, float]]] = field(default_factory=list)  # (lat, lon)
    buildings: list[OsmBuilding] = field(default_factory=list)


@dataclass(frozen=True)
class OsmPriors:
    carriageway_width_m: float
    footpath_centre_t: dict[str, float | None]   # side -> t of a mapped footway centre (None = template)
    sidewalk_mapped: dict[str, bool]             # side -> mapped by a tag or a way


def _first_float(v: Any) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", str(v))
    if not m:
        return None
    x = float(m.group(0).replace(",", "."))
    return x if math.isfinite(x) and x > 0 else None


def carriageway_width(tags: dict[str, str]) -> float:
    width = _first_float(tags["width"]) if "width" in tags else None
    if width is None:
        lanes = _first_float(tags["lanes"]) if "lanes" in tags else None
        width = (lanes if lanes is not None else DEFAULT_LANES) * LANE_WIDTH_M
    return min(max(width, CARRIAGEWAY_MIN_M), CARRIAGEWAY_MAX_M)


def osm_priors(ref: ReferenceLine, osm: OsmContext | None) -> OsmPriors:
    """Template parameters, adjusted by whatever OSM provided."""
    tags = osm.highway_tags if osm else {}
    mapped = {"left": False, "right": False}
    centre: dict[str, float | None] = {"left": None, "right": None}
    sw = str(tags.get("sidewalk", "")).lower()
    if sw in ("both", "yes", "separate"):
        mapped = {"left": True, "right": True}
    elif sw in ("left", "right"):
        mapped[sw] = True
    for key, side in (("sidewalk:left", "left"), ("sidewalk:right", "right")):
        if str(tags.get(key, "")).lower() in ("yes", "separate"):
            mapped[side] = True
    if osm:
        by_side: dict[str, list[float]] = {"left": [], "right": []}
        for line in osm.footway_polylines:
            sts = [ref.to_st(la, lo) for la, lo in line]
            sts = [p for p in sts if -5.0 <= p.s <= ref.length_s + 5.0]
            if not sts:
                continue
            t_med = statistics.median(p.t for p in sts)
            if 0.0 < abs(t_med) <= MAX_FOOTWAY_OFFSET_M:
                by_side["left" if t_med > 0 else "right"].append(t_med)
        for side, vals in by_side.items():
            if vals:
                centre[side] = statistics.median(vals)
                mapped[side] = True
    return OsmPriors(carriageway_width(tags), centre, mapped)


def _strip(sid: str, kind: StripKind, side: str, lo: float, hi: float, weight: float, *, height: float = 0.0,
           material: Material = Material.unknown, condition: Condition = Condition.unknown,
           description: str = "") -> Strip:
    return Strip(id=sid, kind=kind, side=side, material=material, condition=condition,  # type: ignore[arg-type]
                 height_m=height, t_lo=(lo, lo), t_hi=(hi, hi), weight=weight,
                 confidence=round(min(0.95, 1.0 - math.exp(-weight)), 4), description=description, provenance=[])


def _strips(pri: OsmPriors, tag: str) -> list[Strip]:
    half = pri.carriageway_width_m / 2.0
    out = [_strip(f"sp-carriageway-C{tag}", StripKind.carriageway, "centre", -half, half, PRIOR_WEIGHT,
                  description="Carriageway (map prior).")]
    for side, sign, letter in (("left", 1.0, "L"), ("right", -1.0, "R")):
        w = OSM_WEIGHT if pri.sidewalk_mapped[side] else PRIOR_WEIGHT
        inner = half + KERB_WIDTH_M
        c = pri.footpath_centre_t[side]
        if c is not None:  # a mapped footway: centre its template-width strip on it, never inside the kerb
            inner = max(inner, abs(c) - FOOTPATH_WIDTH_M / 2.0)
        outer = inner + FOOTPATH_WIDTH_M
        spans = [
            (StripKind.kerb, half, half + KERB_WIDTH_M, KERB_WIDTH_M, "kerb", PRIOR_WEIGHT),
            (StripKind.footpath, inner, outer, 0.15, "footpath", w),
            (StripKind.forecourt, outer, outer + FORECOURT_WIDTH_M, 0.0, "forecourt", PRIOR_WEIGHT),
        ]
        if inner > half + KERB_WIDTH_M + 1e-9:  # gap between kerb and a mapped footway: verge
            spans.insert(1, (StripKind.verge, half + KERB_WIDTH_M, inner, 0.0, "verge", PRIOR_WEIGHT))
        for kind, lo, hi, height, name, weight in spans:
            a, b = sorted((sign * lo, sign * hi))
            out.append(_strip(f"sp-{name}-{letter}{tag}", kind, side, a, b, weight, height=height,
                              description=f"{name.capitalize()} (map prior)."))
    return sorted(out, key=lambda s: (s.t_lo[0], s.t_hi[0]))


def _buildings(segment_slug: str, osm: OsmContext | None, length_s: float) -> list[Building]:
    out: list[Building] = []
    if not osm:
        return out
    for b in sorted(osm.buildings, key=lambda b: b.osm_way_id):
        pts = list(b.footprint)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) < 3 or not b.height_m > 0:
            continue
        s_lo, s_hi = min(p[0] for p in pts), max(p[0] for p in pts)
        if s_hi < 0.0 or s_lo > length_s:
            continue
        mean_t = sum(p[1] for p in pts) / len(pts)
        out.append(Building(
            id=building_id(osm_way_id=b.osm_way_id), source="osm", osm_way_id=b.osm_way_id,
            side="left" if mean_t >= 0 else "right", footprint=[(float(s), float(t)) for s, t in pts],
            height_m=float(b.height_m), height_confidence=0.3, storeys=b.storeys,
            description="Map-only building outline; height is an estimate.", confidence=0.5, provenance=[]))
    return out


def build_prior_model(*, segment_slug: str, footpath_side: str, ref: ReferenceLine, ref_model: ReferenceLineModel,
                      osm: OsmContext | None, now: datetime) -> CanonicalModel:
    if footpath_side not in ("left", "right"):
        raise ValueError(f"footpath_side must be 'left' or 'right', got {footpath_side!r}")
    if not segment_slug:
        raise ValueError("segment_slug is required")
    length = ref_model.length_s
    pri = osm_priors(ref, osm)
    n = max(1, math.ceil(length / MAX_INTERVAL_M - 1e-9))
    profile = []
    for i in range(n):
        s0 = round(length * i / n, 3)
        s1 = length if i == n - 1 else round(length * (i + 1) / n, 3)
        profile.append(ProfileInterval(
            id=f"pi-{i:04d}", s_from=s0, s_to=s1, footpath_state=FootpathState.unknown,
            state_votes={"unknown": PRIOR_WEIGHT}, strips=_strips(pri, f"-{i}"),
            description=f"{s0:.0f}-{s1:.0f} m: footpath state unknown (map prior).",
            evidence_summary="Map prior only."))
    model = CanonicalModel(
        segment_slug=segment_slug, version=0, parent_version=None, kind=ModelKind.prior, run_id=None,
        created_at=now, legacy=False, placement_verified=ref_model.source.startswith("osm"),
        footpath_side=footpath_side, reference_line=ref_model, profile=profile, entities=[],  # type: ignore[arg-type]
        buildings=_buildings(segment_slug, osm, length), contradictions=[], fused_observation_ids=[])
    return model.model_copy(update={"stats": model.computed_stats()})
