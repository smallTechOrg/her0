"""Observation coverage cells and visible regions (spec/data.md -> "Fidelity"; architecture -> Contracts).

``observation_cells(obs, ref)`` says which stretch of the reference line (and which side) one observation
actually shows; ``compute_fidelity`` counts cells per bin and ``merge`` uses ``visible_regions`` to decide
whether the absence of a detection is evidence. Pure: no network, DB or clock.

Cells (``s_c`` = the camera station on the reference line, ``view`` relative to the direction of travel)::

    forward  [s_c, s_c + 15]  both sides      back   [s_c - 15, s_c]  both sides
    left     [s_c - 6, s_c + 6] left only     right  [s_c - 6, s_c + 6] right only
    oblique  [s_c - 6, s_c + 6] both sides
    closeup  (user_photo only) [s_c - 6, s_c + 6], side by facing
    overhead_hires  (satellite_crop with zoom >= 20) the s-runs whose (s, 0) and (s, +-8) fall inside the crop

Interpretation notes
--------------------
* A street-level observation with no recorded ``station_s`` is placed with ``ref.to_st`` on its lat/lon; one with no
  recorded ``view`` takes it from ``heading_deg`` against the line bearing (45 degree sectors), else ``oblique``.
* Cells are clipped to ``[0, length_s]``; a cell that clips to nothing is dropped.
* ``visible_regions`` cuts every street-level cell into pieces of at most ``REGION_PIECE_M`` metres, each weighted by
  ``range_weight`` of its distance from the camera station (so absence far from the camera counts for less).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from hero.model.batch import VisibleRegion
from hero.model.calibrate import range_weight
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind, ViewKind
from hero.scene.geo import meters_per_px

FWD_RANGE_M = 15.0
SIDE_RANGE_M = 6.0
HIRES_MIN_ZOOM = 20
HIRES_OFFSET_M = 8.0
HIRES_STEP_M = 0.5
REGION_PIECE_M = 5.0

CellKind = Literal["forward", "back", "left", "right", "oblique", "closeup", "overhead_hires"]
CellSide = Literal["left", "right", "both"]
STREET_KINDS: tuple[str, ...] = ("forward", "back", "left", "right", "oblique")


@dataclass(frozen=True)
class CoverageCell:
    observation_id: str
    kind: CellKind
    s_from: float
    s_to: float
    side: CellSide
    station_s: float


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def camera_station(obs: Observation, ref: ReferenceLine) -> float:
    """Station of the camera on the reference line (recorded ``station_s`` else the projection of its lat/lon)."""
    if obs.station_s is not None:
        return float(obs.station_s)
    return float(ref.to_st(obs.lat, obs.lon).s)


def effective_view(obs: Observation, ref: ReferenceLine, station_s: float) -> str:
    if obs.view is not None:
        return obs.view.value
    if obs.heading_deg is None:
        return ViewKind.oblique.value
    d = _wrap180(obs.heading_deg - ref.bearing_deg(min(max(station_s, 0.0), ref.length_s)))
    if abs(d) <= 45.0:
        return ViewKind.forward.value
    if abs(d) >= 135.0:
        return ViewKind.back.value
    return ViewKind.right.value if d > 0 else ViewKind.left.value


def _cell(obs_id: str, kind: CellKind, a: float, b: float, side: CellSide, station: float,
          length: float) -> CoverageCell | None:
    lo, hi = max(0.0, a), min(length, b)
    if hi - lo <= 1e-9:
        return None
    return CoverageCell(obs_id, kind, round(lo, 3), round(hi, 3), side, round(station, 3))


def _street_cells(obs: Observation, ref: ReferenceLine) -> list[CoverageCell]:
    sc = camera_station(obs, ref)
    view = effective_view(obs, ref, sc)
    L = ref.length_s
    spec: dict[str, tuple[float, float, CellSide]] = {
        "forward": (sc, sc + FWD_RANGE_M, "both"),
        "back": (sc - FWD_RANGE_M, sc, "both"),
        "left": (sc - SIDE_RANGE_M, sc + SIDE_RANGE_M, "left"),
        "right": (sc - SIDE_RANGE_M, sc + SIDE_RANGE_M, "right"),
        "oblique": (sc - SIDE_RANGE_M, sc + SIDE_RANGE_M, "both"),
    }
    a, b, side = spec[view]
    out = [_cell(obs.id, view, a, b, side, sc, L)]  # type: ignore[arg-type]
    if obs.kind is ObservationKind.user_photo:
        close_side: CellSide = side if view in ("left", "right") else "both"
        out.append(_cell(obs.id, "closeup", sc - SIDE_RANGE_M, sc + SIDE_RANGE_M, close_side, sc, L))
    return [c for c in out if c is not None]


def _crop_extent_m(obs: Observation) -> tuple[float, float]:
    mpp = obs.meters_per_px
    if mpp is None and obs.zoom is not None:
        mpp = meters_per_px(obs.lat, obs.zoom, obs.scale or 1)
    if mpp is None:
        raise ValueError(f"satellite observation {obs.id} has neither meters_per_px nor zoom")
    return obs.width_px * mpp / 2.0, obs.height_px * mpp / 2.0


def _hires_cells(obs: Observation, ref: ReferenceLine) -> list[CoverageCell]:
    if obs.zoom is None or obs.zoom < HIRES_MIN_ZOOM:
        return []
    half_e, half_n = _crop_extent_m(obs)
    cx, cy = ref.latlon_to_xy(obs.lat, obs.lon)

    def inside(s: float, t: float) -> bool:
        x, y = ref.latlon_to_xy(*ref.point_at(s, t))
        return abs(x - cx) <= half_e and abs(y - cy) <= half_n

    L = ref.length_s
    n = int(math.floor(L / HIRES_STEP_M + 1e-9))
    samples = [i * HIRES_STEP_M for i in range(n + 1)]
    if L - samples[-1] > 1e-9:
        samples.append(L)
    runs: list[tuple[float, float]] = []
    start: float | None = None
    prev = 0.0
    for s in samples:
        ok = inside(s, 0.0) and inside(s, HIRES_OFFSET_M) and inside(s, -HIRES_OFFSET_M)
        if ok and start is None:
            start = s
        if not ok and start is not None:
            runs.append((start, prev))
            start = None
        prev = s
    if start is not None:
        runs.append((start, prev))
    sc = (ref.to_st(obs.lat, obs.lon).s)
    cells = [_cell(obs.id, "overhead_hires", a, b, "both", sc, L) for a, b in runs if b > a]
    return [c for c in cells if c is not None]


def observation_cells(obs: Observation, ref: ReferenceLine) -> list[CoverageCell]:
    """Coverage cells of one observation (empty for the satellite overview and for low-zoom crops)."""
    if obs.kind in (ObservationKind.streetview, ObservationKind.user_photo):
        return _street_cells(obs, ref)
    if obs.kind is ObservationKind.satellite_crop:
        return _hires_cells(obs, ref)
    return []


def visible_regions(obs: Observation, ref: ReferenceLine) -> list[VisibleRegion]:
    """Regions where this observation would have shown an object, weighted by range from the camera station."""
    out: list[VisibleRegion] = []
    for c in observation_cells(obs, ref):
        if c.kind not in STREET_KINDS:
            continue
        n = max(1, math.ceil((c.s_to - c.s_from) / REGION_PIECE_M - 1e-9))
        for i in range(n):
            a = c.s_from + (c.s_to - c.s_from) * i / n
            b = c.s_from + (c.s_to - c.s_from) * (i + 1) / n
            w = range_weight(abs((a + b) / 2.0 - c.station_s))  # distance of the piece midpoint from the camera
            out.append(VisibleRegion(obs.id, round(a, 3), round(b, 3), c.side, round(w, 4)))
    return out
