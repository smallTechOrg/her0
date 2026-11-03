"""Clip an OSM road polyline to a segment-sized reference line (spec/architecture.md -> Contracts).

Pure: no network, DB or clock. ``clip_road`` projects the segment centre onto the road, orients the road
to the direction of travel and walks ``length_m / 2`` each way by arc length. Where the road ends early the
line is extended straight along the end tangent and the extension is reported in ``ClipInfo``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from hero.model.refline import ReferenceLine

DEFAULT_MAX_SNAP_M = 25.0


@dataclass(frozen=True)
class ClipInfo:
    extrapolated_start_m: float
    extrapolated_end_m: float
    snap_m: float


def _angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two compass bearings, in ``[0, 180]``."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def straight_chord(start: tuple[float, float], end: tuple[float, float]) -> ReferenceLine:
    """A two-point reference line from ``start`` to ``end`` (``ValueError`` if they coincide)."""
    return ReferenceLine.from_latlons([start, end])


def clip_road(road_latlons: Sequence[tuple[float, float]], centre: tuple[float, float], length_m: float,
              travel_bearing_deg: float, *, max_snap_m: float = DEFAULT_MAX_SNAP_M) -> tuple[ReferenceLine, ClipInfo]:
    if not (length_m > 0 and math.isfinite(length_m)):
        raise ValueError("length_m must be positive")
    if not math.isfinite(travel_bearing_deg):
        raise ValueError("travel_bearing_deg must be finite")
    if max_snap_m < 0:
        raise ValueError("max_snap_m must be >= 0")
    road = ReferenceLine.from_latlons(list(road_latlons))
    snap = road.to_st(centre[0], centre[1])
    if snap.dist_m > max_snap_m:
        raise ValueError(f"centre is {snap.dist_m:.1f} m from the road (max_snap_m={max_snap_m})")

    s_foot = min(max(snap.s, 0.0), road.length_s)
    if _angle_diff_deg(road.bearing_deg(s_foot), travel_bearing_deg) > 90.0:
        road = ReferenceLine.from_latlons(road.latlons[::-1])
        s_foot = road.length_s - s_foot

    total = road.length_s
    start, end = s_foot - length_m / 2.0, s_foot + length_m / 2.0
    lo, hi = max(start, 0.0), min(end, total)
    pts = [road.point_at(start, 0.0)]
    pts += [ll for ll, s in zip(road.latlons, road._cum) if lo < s < hi]  # noqa: SLF001 (same package)
    pts.append(road.point_at(end, 0.0))
    clipped = ReferenceLine.from_latlons(pts)
    info = ClipInfo(extrapolated_start_m=max(0.0, -start), extrapolated_end_m=max(0.0, end - total),
                    snap_m=snap.dist_m)
    return clipped, info
