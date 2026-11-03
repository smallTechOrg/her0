"""Pixel -> (s, t) projections (spec/architecture.md -> Contracts, ``hero.model.projection``).

Pure: no network, DB or clock. All functions raise ``ValueError`` on invalid input.

Interpretation notes
--------------------
* ``ProjectedPoint`` carries an extra ``flags`` tuple (``"range_disagreement"``).
* ``strip_samples_from_traces`` / ``footpath_runs_to_states`` build records that need an ``observation_id``;
  the contract signatures have none, so it is an optional keyword (default ``""``) the caller may set (or
  ``dataclasses.replace`` afterwards).
* Strip samples sit on the *absolute* ``step_m`` grid (``s = k * step_m``) inside the overlap of the two
  traces, so samples from different windows line up with merge's 2.5 m station grid. A trace overlap shorter
  than one step yields a single sample at the overlap midpoint.
* ``FootpathRun`` is duck-typed (``side, y_from_px, y_to_px, state, confidence``); the run's confidence is used
  as the span weight. Runs are clipped to the grid's rows; empty ones are dropped.
* Range weight / sigma come from ``hero.model.calibrate`` when it is importable, else the identical spec formula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Sequence

from hero.model.batch import StateSpan, StripSample
from hero.model.refline import RectGrid, ReferenceLine
from hero.model.types import Condition, FootpathState, Material, Observation, StripKind
from hero.scene.geo import TileGeoref, latlon_to_tile_px, tile_px_to_latlon

CAMERA_HEIGHT_M = 2.5
MAX_RANGE_M = 30.0
HORIZON_MARGIN_DEG = 1.5
DISAGREE_ABS_M = 4.0
DISAGREE_REL = 0.4


@dataclass(frozen=True)
class ProjectedPoint:
    s: float
    t: float
    range_m: float
    sigma_m: float
    weight: float
    method: str  # "ground_plane" | "estimated_distance"
    flags: tuple[str, ...] = ()


class FootpathRun(Protocol):
    side: str
    y_from_px: float
    y_to_px: float
    state: str
    confidence: float


# ------------------------------------------------------------------ range weighting


def _range_weight(r: float) -> float:
    try:
        from hero.model.calibrate import range_weight  # type: ignore[attr-defined]
        return range_weight(r)
    except ImportError:
        if r <= 10.0:
            return 1.0
        if r <= 20.0:
            return 1.0 - 0.4 * (r - 10.0) / 10.0
        return max(0.3, 0.6 - 0.3 * (r - 20.0) / 10.0)


def _sigma(r: float, estimated: bool) -> float:
    try:
        from hero.model.calibrate import sigma_for_range  # type: ignore[attr-defined]
        return sigma_for_range(r, estimated)
    except ImportError:
        return 0.4 + 0.1 * r + (1.0 if estimated else 0.0)


# ------------------------------------------------------------------ street-level detections


def _ground_range(u: float, v: float, w: int, h: int, fov_deg: float, pitch_deg: float) -> tuple[float | None, float]:
    """``(horizontal range or None, azimuth offset deg)`` of pixel ``(u, v)`` for a camera at ``CAMERA_HEIGHT_M``."""
    f = (w / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    x, y = u - w / 2.0, v - h / 2.0
    p = math.radians(pitch_deg)
    fwd = f * math.cos(p) + y * math.sin(p)     # horizontal-forward component of the ray
    down = y * math.cos(p) - f * math.sin(p)    # downward component
    az = math.degrees(math.atan2(x, fwd))
    horiz = math.hypot(fwd, x)
    depression = math.degrees(math.atan2(down, horiz))
    if depression < HORIZON_MARGIN_DEG:
        return None, az
    return CAMERA_HEIGHT_M * horiz / down, az


def project_street_detection(obs: Observation, ref: ReferenceLine, box_px: tuple[int, int, int, int], *,
                             touches_ground: bool, est_distance_m: float | None) -> ProjectedPoint | None:
    if obs.heading_deg is None or obs.fov_deg is None:
        raise ValueError("observation needs heading_deg and fov_deg")
    if not 0.0 < obs.fov_deg < 180.0:
        raise ValueError("fov_deg must be in (0, 180)")
    if obs.width_px <= 0 or obs.height_px <= 0:
        raise ValueError("observation image size must be positive")
    bx, by, bw, bh = box_px
    if bw < 0 or bh < 0:
        raise ValueError("box_px width/height must be >= 0")
    u, v = bx + bw / 2.0, by + bh
    ground, az_off = _ground_range(u, v, obs.width_px, obs.height_px, obs.fov_deg, obs.pitch_deg or 0.0)
    if not touches_ground:
        ground = None
    est = est_distance_m if (est_distance_m is not None and est_distance_m > 0) else None

    flags: tuple[str, ...] = ()
    weight_mult = 1.0
    if ground is not None:
        rng, method = ground, "ground_plane"
        if est is not None and abs(est - ground) > max(DISAGREE_ABS_M, DISAGREE_REL * ground):
            weight_mult, flags = 0.5, ("range_disagreement",)
    elif est is not None:
        rng, method = est, "estimated_distance"
    else:
        return None
    if rng > MAX_RANGE_M:
        return None

    az = math.radians(obs.heading_deg + az_off)
    cx, cy = ref.latlon_to_xy(obs.lat, obs.lon)
    lat, lon = ref._to_latlon(cx + rng * math.sin(az), cy + rng * math.cos(az))  # noqa: SLF001 (same package)
    st = ref.to_st(lat, lon)
    return ProjectedPoint(s=st.s, t=st.t, range_m=rng, sigma_m=_sigma(rng, method == "estimated_distance"),
                          weight=_range_weight(rng) * weight_mult, method=method, flags=flags)


# ------------------------------------------------------------------ overhead (rectified) windows


def project_overhead_polyline(grid: RectGrid, pts_px: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    return [grid.px_to_st(x, y) for x, y in pts_px]


def _trace_st(grid: RectGrid, pts: Sequence[tuple[float, float]], name: str) -> list[tuple[float, float]]:
    if len(pts) < 2:
        raise ValueError(f"{name} needs at least 2 points")
    for a, b in zip(pts, pts[1:]):
        if b[1] < a[1]:
            raise ValueError(f"{name}: y must be non-decreasing")
    st = project_overhead_polyline(grid, pts)
    out = [st[0]]
    for p in st[1:]:
        if p[0] < out[-1][0] - 1e-12:  # s strictly decreasing along the list; drop repeated rows
            out.append(p)
    return out


def _t_at(trace: list[tuple[float, float]], s: float) -> float:
    """Linear interpolation of ``t`` at ``s`` on a trace whose ``s`` decreases along the list."""
    for (s0, t0), (s1, t1) in zip(trace, trace[1:]):
        if s1 - 1e-9 <= s <= s0 + 1e-9:
            f = (s0 - s) / (s0 - s1)
            return t0 + (t1 - t0) * min(max(f, 0.0), 1.0)
    return trace[-1][1] if s < trace[-1][0] else trace[0][1]


def strip_samples_from_traces(grid: RectGrid, edge_a_px: Sequence[tuple[float, float]],
                              edge_b_px: Sequence[tuple[float, float]], *, kind: StripKind | str,
                              material: Material | str, condition: Condition | str, side: str, weight: float,
                              step_m: float = 2.5, observation_id: str = "") -> list[StripSample]:
    if step_m <= 0:
        raise ValueError("step_m must be positive")
    if weight < 0:
        raise ValueError("weight must be >= 0")
    if side not in ("left", "right", "centre"):
        raise ValueError(f"invalid side {side!r}")
    a = _trace_st(grid, edge_a_px, "edge_a_px")
    b = _trace_st(grid, edge_b_px, "edge_b_px")
    if len(a) < 2 or len(b) < 2:
        raise ValueError("each edge needs at least 2 distinct rows")
    lo = max(a[-1][0], b[-1][0], grid.s_bottom)
    hi = min(a[0][0], b[0][0], grid.s_top)
    if hi < lo:
        return []
    k0, k1 = math.ceil(lo / step_m - 1e-9), math.floor(hi / step_m + 1e-9)
    stations = [k * step_m for k in range(k0, k1 + 1)] or [(lo + hi) / 2.0]
    kind_e, mat_e, cond_e = StripKind(kind), Material(material), Condition(condition)
    out = []
    for s in stations:
        ta, tb = _t_at(a, s), _t_at(b, s)
        out.append(StripSample(s=round(s, 6), kind=kind_e, side=side, t_lo=min(ta, tb), t_hi=max(ta, tb),  # type: ignore[arg-type]
                               material=mat_e, condition=cond_e, weight=weight, observation_id=observation_id))
    return out


def footpath_runs_to_states(grid: RectGrid, runs: Sequence[FootpathRun], *,
                            observation_id: str = "") -> list[StateSpan]:
    height = (grid.s_top - grid.s_bottom) / grid.m_per_px
    out: list[StateSpan] = []
    for r in runs:
        if r.side not in ("left", "right"):
            raise ValueError(f"invalid run side {r.side!r}")
        if r.y_to_px < r.y_from_px:
            raise ValueError("run y_to_px must be >= y_from_px")
        y0, y1 = max(r.y_from_px, 0.0), min(r.y_to_px, height)
        if y1 <= y0:
            continue
        out.append(StateSpan(observation_id=observation_id, side=r.side,  # type: ignore[arg-type]
                             s_from=grid.px_to_st(0, y1)[0], s_to=grid.px_to_st(0, y0)[0],
                             state=FootpathState(r.state), weight=min(max(float(r.confidence), 0.0), 1.0)))
    out.sort(key=lambda sp: (sp.side, sp.s_from))
    return out


# ------------------------------------------------------------------ satellite tiles


def tile_px_to_st(ref: ReferenceLine, georef: TileGeoref, px: float, py: float) -> tuple[float, float]:
    ll = tile_px_to_latlon(px, py, georef)
    st = ref.to_st(ll.lat, ll.lon)
    return st.s, st.t


def st_to_tile_px(ref: ReferenceLine, georef: TileGeoref, s: float, t: float) -> tuple[float, float]:
    lat, lon = ref.point_at(s, t)
    return latlon_to_tile_px(lat, lon, georef)
