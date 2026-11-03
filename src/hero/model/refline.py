"""Reference line in an OpenDRIVE-style ``(s, t)`` frame, plus the straightened-window grid.

Geometry is the equirectangular local plane of ``hero.scene.geo`` (origin = the first point;
x = east, y = north, metres). ``s`` is arc length along the polyline, ``t`` the signed lateral
offset, **positive to the LEFT of the direction of travel**. Pure: no network, DB or clock.

Interpretation notes (spec/architecture.md -> Contracts leaves these open)
--------------------------------------------------------------------------
* ``to_st`` returns the nearest foot point on the polyline. Beyond either end the polyline is
  extended along the end tangent: ``s`` is then < 0 or > ``length_s`` (extrapolated, not clamped)
  and ``clamped=True``; ``t`` is the perpendicular offset from the extended line.
* ``point_at`` also extends along the end tangents for ``s`` outside ``[0, length_s]`` so it stays
  the inverse of ``to_st``. ``xy`` clamps, as specified.
* The map ``(s, t) -> point`` offsets along each segment's own normal. On the *inner* side of a
  bend, offset points within ``~|t|*turn/2`` metres of a vertex fall nearer to the next segment, so
  ``to_st(point_at(s, t))`` is exact for straight runs and for the outer side, and within
  ``|t| * turn_angle_rad`` in ``s`` for a tiny strip around inner-side vertices. Use a densely
  sampled line (as the fixtures do) when a centimetre round trip is needed on a curve.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from hero.model.types import Extrapolated, RefPoint, ReferenceLineModel
from hero.scene.geo import METERS_PER_DEG_LAT

_EPS = 1e-9
MIN_DISTINCT_M = 1e-6


@dataclass(frozen=True)
class StProjection:
    s: float
    t: float
    dist_m: float   # |input point - foot point|
    clamped: bool   # True when the point lies beyond an end (tangent extension used)


def _wrap180(deg: float) -> float:
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


class ReferenceLine:
    """Polyline in travel direction with arc-length parametrisation."""

    def __init__(self, latlons: Sequence[tuple[float, float]]):
        pts: list[tuple[float, float]] = []
        for lat, lon in latlons:
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError(f"invalid coordinate ({lat}, {lon})")
            pts.append((float(lat), float(lon)))
        self._lat0, self._lon0 = pts[0] if pts else (0.0, 0.0)
        self._m_lon = METERS_PER_DEG_LAT * math.cos(math.radians(self._lat0))
        xy: list[tuple[float, float]] = []
        kept: list[tuple[float, float]] = []
        for lat, lon in pts:
            p = ((lon - self._lon0) * self._m_lon, (lat - self._lat0) * METERS_PER_DEG_LAT)
            if xy and math.hypot(p[0] - xy[-1][0], p[1] - xy[-1][1]) < MIN_DISTINCT_M:
                continue  # drop consecutive duplicates
            xy.append(p)
            kept.append((lat, lon))
        if len(xy) < 2:
            raise ValueError("a reference line needs at least 2 distinct points")
        self._xy = xy
        self._latlons = kept
        self._cum = [0.0]
        self._tan: list[tuple[float, float]] = []  # unit tangents (east, north) per segment
        for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
            d = math.hypot(x1 - x0, y1 - y0)
            self._cum.append(self._cum[-1] + d)
            self._tan.append(((x1 - x0) / d, (y1 - y0) / d))
        self._length = self._cum[-1]

    # ---------------------------------------------------------------- constructors / serialisation

    @classmethod
    def from_latlons(cls, latlons: Sequence[tuple[float, float]]) -> "ReferenceLine":
        if len(latlons) < 2:
            raise ValueError("a reference line needs at least 2 distinct points")
        return cls(latlons)

    @classmethod
    def from_model(cls, m: ReferenceLineModel) -> "ReferenceLine":
        return cls.from_latlons([(p.lat, p.lon) for p in m.points])

    def to_model(self, *, source: str, osm_way_id: int | None, name: str | None,
                 extrapolated_m: tuple[float, float] = (0.0, 0.0)) -> ReferenceLineModel:
        """Serialise (metres to 3 dp; lat/lon to 8 dp ~ 1 mm).

        The determinism rule says 6 dp for lat/lon, but 6 dp (~0.11 m) injects length-inflating noise into
        densely sampled lines (arcs, fixtures), so 8 dp is used for reference-line vertices only.
        """
        points = [RefPoint(lat=round(lat, 8), lon=round(lon, 8), s=round(s, 3))
                  for (lat, lon), s in zip(self._latlons, self._cum)]
        return ReferenceLineModel(
            source=source, osm_way_id=osm_way_id, name=name, points=points,
            length_s=round(self._length, 3),
            extrapolated_m=Extrapolated(start=round(extrapolated_m[0], 3), end=round(extrapolated_m[1], 3)),
        )

    # ---------------------------------------------------------------- basic geometry

    @property
    def length_s(self) -> float:
        return self._length

    @property
    def latlons(self) -> list[tuple[float, float]]:
        return list(self._latlons)

    @property
    def origin(self) -> tuple[float, float]:
        return self._lat0, self._lon0

    def _seg_index(self, s: float) -> int:
        if s <= 0.0:
            return 0
        if s >= self._length:
            return len(self._tan) - 1
        return min(bisect_right(self._cum, s) - 1, len(self._tan) - 1)

    def _xy_ext(self, s: float) -> tuple[float, float]:
        i = self._seg_index(s)
        x0, y0 = self._xy[i]
        tx, ty = self._tan[i]
        d = s - self._cum[i]  # extends past the ends along the end tangent
        return x0 + tx * d, y0 + ty * d

    def xy(self, s: float) -> tuple[float, float]:
        """``(east_m, north_m)`` from the first point; ``s`` is clamped to ``[0, length_s]``."""
        return self._xy_ext(min(max(s, 0.0), self._length))

    def tangent(self, s: float) -> tuple[float, float]:
        """Unit tangent ``(east, north)`` at ``s`` (the segment starting at ``s``; last segment at the end)."""
        return self._tan[self._seg_index(min(max(s, 0.0), self._length))]

    def bearing_deg(self, s: float) -> float:
        """Compass bearing of travel at ``s``, clockwise from north, in ``[0, 360)``."""
        tx, ty = self.tangent(s)
        return math.degrees(math.atan2(tx, ty)) % 360.0

    def _to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return self._lat0 + y / METERS_PER_DEG_LAT, self._lon0 + x / self._m_lon

    def point_at(self, s: float, t: float) -> tuple[float, float]:
        """``(lat, lon)`` of ``(s, t)``; ``t`` is left-positive; ``s`` beyond the ends extends the tangent."""
        i = self._seg_index(s)
        x, y = self._xy_ext(s)
        tx, ty = self._tan[i]
        # left normal of (east, north) travel direction = (-north, east)
        return self._to_latlon(x - ty * t, y + tx * t)

    def latlon_to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self._lon0) * self._m_lon, (lat - self._lat0) * METERS_PER_DEG_LAT

    def to_st(self, lat: float, lon: float) -> StProjection:
        px, py = self.latlon_to_xy(lat, lon)
        best: tuple[float, float, float] | None = None  # (dist, s, t)
        last = len(self._tan) - 1
        for i, ((x0, y0), (tx, ty)) in enumerate(zip(self._xy, self._tan)):
            seg_len = self._cum[i + 1] - self._cum[i]
            dx, dy = px - x0, py - y0
            u = dx * tx + dy * ty
            if i == 0 and u < 0.0:
                uc = u  # extend before the start
            elif i == last and u > seg_len:
                uc = u  # extend past the end
            else:
                uc = min(max(u, 0.0), seg_len)
            fx, fy = x0 + tx * uc, y0 + ty * uc
            dist = math.hypot(px - fx, py - fy)
            side = tx * (py - fy) - ty * (px - fx)  # >0: point is left of travel
            t_signed = math.copysign(dist, side) if dist > _EPS else 0.0
            if best is None or dist < best[0] - 1e-12:
                best = (dist, self._cum[i] + uc, t_signed)
        assert best is not None
        dist, s, t = best
        return StProjection(s=s, t=t, dist_m=dist, clamped=s < 0.0 or s > self._length)

    def sample_s(self, step_m: float) -> list[float]:
        """``0, step, 2*step ...`` below ``length_s``, then ``length_s``."""
        if step_m <= 0:
            raise ValueError("step_m must be positive")
        out: list[float] = []
        n = 0
        while n * step_m < self._length - 1e-9:
            out.append(n * step_m)
            n += 1
        out.append(self._length)
        return out

    def turning_deg(self, s0: float, s1: float) -> float:
        """Signed bearing change from ``s0`` to ``s1``; clockwise (right turn) is positive, in ``(-180, 180]``."""
        return _wrap180(self.bearing_deg(s1) - self.bearing_deg(s0))

    def mean_bearing_deg(self) -> float:
        """Bearing of the chord from the first to the last point."""
        x, y = self._xy[-1]
        return math.degrees(math.atan2(x, y)) % 360.0


@dataclass(frozen=True)
class RectGrid:
    """A straightened window: up = travel, image-left = left of travel.

    ``px_to_st``: ``s = s_top - y*mpp``; ``t = t_left - x*mpp``.
    """

    s_top: float
    s_bottom: float
    t_left: float
    t_right: float
    m_per_px: float

    def __post_init__(self) -> None:
        if not self.s_top > self.s_bottom:
            raise ValueError(f"s_top ({self.s_top}) must exceed s_bottom ({self.s_bottom})")
        if not self.t_left > self.t_right:
            raise ValueError(f"t_left ({self.t_left}) must exceed t_right ({self.t_right})")
        if not self.m_per_px > 0:
            raise ValueError("m_per_px must be positive")

    @property
    def size_px(self) -> tuple[int, int]:
        """``(width, height)`` in pixels (rounded up so the whole window is covered)."""
        return (math.ceil((self.t_left - self.t_right) / self.m_per_px - 1e-9),
                math.ceil((self.s_top - self.s_bottom) / self.m_per_px - 1e-9))

    def px_to_st(self, x: float, y: float) -> tuple[float, float]:
        return self.s_top - y * self.m_per_px, self.t_left - x * self.m_per_px

    def st_to_px(self, s: float, t: float) -> tuple[float, float]:
        return (self.t_left - t) / self.m_per_px, (self.s_top - s) / self.m_per_px
