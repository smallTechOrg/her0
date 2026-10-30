"""Geometry: haversine, bearing, segment local frame <-> lat/lon, Web-Mercator tile pixels.

Local frame (spec/data.md): ``x_m`` runs along the segment from ``start``; ``lat_m`` is
lateral offset from the centreline, positive toward the modelled footpath side.
Lat/lon <-> metres uses an equirectangular approximation, accurate to millimetres over the
<= 150 m segments this project handles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from hero.scene.schema import FootpathSide, FrameToPx, Lane, LatLon, SceneObject

EARTH_RADIUS_M = 6_371_008.8  # mean radius: distances and the local frame
WGS84_RADIUS_M = 6_378_137.0  # Web Mercator (Google tiles) uses the WGS84 semi-major axis
METERS_PER_DEG_LAT = EARTH_RADIUS_M * math.pi / 180.0
TILE_SIZE_PX = 256
EQUATOR_METERS_PER_PX_Z0 = 2 * math.pi * WGS84_RADIUS_M / TILE_SIZE_PX  # 156543.03 m/px at zoom 0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, clockwise from north in [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _lateral_unit_vector(heading_deg: float, footpath_side: FootpathSide) -> tuple[float, float]:
    """(north, east) unit vector pointing to the footpath side of the travel direction."""
    h = math.radians(heading_deg)
    if footpath_side is FootpathSide.left:
        return math.sin(h), -math.cos(h)
    return -math.sin(h), math.cos(h)


@dataclass(frozen=True)
class LocalFrame:
    origin_lat: float
    origin_lon: float
    heading_deg: float
    length_m: float
    footpath_side: FootpathSide

    @classmethod
    def from_endpoints(cls, start_lat: float, start_lon: float, end_lat: float, end_lon: float,
                       footpath_side: FootpathSide) -> LocalFrame:
        return cls(
            origin_lat=start_lat,
            origin_lon=start_lon,
            heading_deg=bearing_deg(start_lat, start_lon, end_lat, end_lon),
            length_m=haversine_m(start_lat, start_lon, end_lat, end_lon),
            footpath_side=footpath_side,
        )

    @property
    def _meters_per_deg_lon(self) -> float:
        return METERS_PER_DEG_LAT * math.cos(math.radians(self.origin_lat))

    def to_latlon(self, x_m: float, lat_m: float) -> LatLon:
        h = math.radians(self.heading_deg)
        side_n, side_e = _lateral_unit_vector(self.heading_deg, self.footpath_side)
        north = x_m * math.cos(h) + lat_m * side_n
        east = x_m * math.sin(h) + lat_m * side_e
        return LatLon(
            lat=self.origin_lat + north / METERS_PER_DEG_LAT,
            lon=self.origin_lon + east / self._meters_per_deg_lon,
        )

    def from_latlon(self, lat: float, lon: float) -> tuple[float, float]:
        """Inverse of ``to_latlon``: returns ``(x_m, lat_m)``."""
        north = (lat - self.origin_lat) * METERS_PER_DEG_LAT
        east = (lon - self.origin_lon) * self._meters_per_deg_lon
        h = math.radians(self.heading_deg)
        side_n, side_e = _lateral_unit_vector(self.heading_deg, self.footpath_side)
        x_m = north * math.cos(h) + east * math.sin(h)
        lat_m = north * side_n + east * side_e
        return x_m, lat_m

    def midpoint(self) -> LatLon:
        return self.to_latlon(self.length_m / 2.0, 0.0)


# ------------------------------------------------------------------ Web Mercator


@dataclass(frozen=True)
class TileGeoref:
    """A Google Static Maps tile: centred on ``center`` at ``zoom``/``scale``, ``size_px`` actual pixels."""

    center_lat: float
    center_lon: float
    zoom: int
    scale: int
    size_px: tuple[int, int]

    @property
    def world_px(self) -> float:
        return TILE_SIZE_PX * (2 ** self.zoom) * self.scale


def meters_per_px(lat: float, zoom: int, scale: int) -> float:
    return EQUATOR_METERS_PER_PX_Z0 * math.cos(math.radians(lat)) / (2 ** zoom) / scale


def _world_xy(lat: float, lon: float, world_px: float) -> tuple[float, float]:
    phi = math.radians(lat)
    x = (lon + 180.0) / 360.0 * world_px
    y = (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0 * world_px
    return x, y


def latlon_to_tile_px(lat: float, lon: float, georef: TileGeoref) -> tuple[float, float]:
    cx, cy = _world_xy(georef.center_lat, georef.center_lon, georef.world_px)
    x, y = _world_xy(lat, lon, georef.world_px)
    return x - cx + georef.size_px[0] / 2.0, y - cy + georef.size_px[1] / 2.0


def tile_px_to_latlon(px: float, py: float, georef: TileGeoref) -> LatLon:
    w = georef.world_px
    cx, cy = _world_xy(georef.center_lat, georef.center_lon, w)
    x = cx + px - georef.size_px[0] / 2.0
    y = cy + py - georef.size_px[1] / 2.0
    lon = x / w * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / w
    lat = math.degrees(math.atan(math.sinh(n)))
    return LatLon(lat=lat, lon=lon)


def compute_frame_to_px(frame: LocalFrame, georef: TileGeoref) -> FrameToPx:
    """Affine mapping local-frame metres to tile pixels (``ground.frame_to_px`` in the Scene)."""
    origin = latlon_to_tile_px(frame.origin_lat, frame.origin_lon, georef)
    along_end = frame.to_latlon(1.0, 0.0)
    across_end = frame.to_latlon(0.0, 1.0)
    ax, ay = latlon_to_tile_px(along_end.lat, along_end.lon, georef)
    cx, cy = latlon_to_tile_px(across_end.lat, across_end.lon, georef)
    # rounded to 1e-6 px: the raw differences carry ~1e-8 libm noise that differs between macOS and Linux, which made
    # committed fixtures platform-specific
    r6 = lambda v: round(v, 6) + 0.0  # noqa: E731
    return FrameToPx(
        origin_px=(r6(origin[0]), r6(origin[1])),
        along_px_per_m=(r6(ax - origin[0]), r6(ay - origin[1])),
        across_px_per_m=(r6(cx - origin[0]), r6(cy - origin[1])),
    )


def apply_frame_to_px(affine: FrameToPx, x_m: float, lat_m: float) -> tuple[float, float]:
    ox, oy = affine.origin_px
    ax, ay = affine.along_px_per_m
    cx, cy = affine.across_px_per_m
    return ox + x_m * ax + lat_m * cx, oy + x_m * ay + lat_m * cy


# ------------------------------------------------------------------ lane helpers


def mean_lane_lat_m(lane_indices: Sequence[int], lanes: Sequence[Lane]) -> float:
    """Lateral position of something occupying ``lane_indices`` (spec: mean of the lanes' lat_m)."""
    by_index = {lane.index: lane.lat_m for lane in lanes}
    try:
        values = [by_index[i] for i in lane_indices]
    except KeyError as exc:
        raise ValueError(f"unknown lane index {exc.args[0]}") from exc
    if not values:
        raise ValueError("at least one lane index is required")
    return sum(values) / len(values)


def object_lat_m(obj: SceneObject, lanes: Sequence[Lane]) -> float:
    return mean_lane_lat_m(obj.lanes, lanes)
