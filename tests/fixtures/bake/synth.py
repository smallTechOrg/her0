"""Deterministic synthetic imagery for the texture-bake tests (owned by S1e).

* ``satellite_tile`` paints coloured markers at known ``(s, t)`` points of a reference line into a fake Static-Maps tile,
  so a test can assert where the straightened strip puts them (frame test).
* ``render_wall_view`` renders what a pinhole Street View camera would see of a planar wall whose colour is an analytic
  function of ``(s, h)`` - an *independent* forward model of the projection the bake inverts.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind, ViewKind
from hero.scene.geo import TileGeoref, latlon_to_tile_px
from tests.fixtures.street_fixtures import make_observation

MARKER_RGB = {"red": (255, 0, 0), "blue": (0, 0, 255), "green": (0, 255, 0), "yellow": (255, 255, 0)}


def tile_georef(ref: ReferenceLine, centre_s: float, zoom: int, size: int, scale: int = 2) -> TileGeoref:
    lat, lon = ref.point_at(centre_s, 0.0)
    return TileGeoref(lat, lon, zoom, scale, (size, size))


def satellite_tile(ref: ReferenceLine, geo: TileGeoref, markers: list[tuple[float, float, str]], *,
                   base: tuple[int, int, int] = (150, 150, 150)) -> Image.Image:
    """Grey tile with a faint diagonal texture and a filled 14 px square for each ``(s, t, colour_name)``."""
    w, h = geo.size_px
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    for i in range(0, w + h, 24):
        d.line([(i, 0), (0, i)], fill=(base[0] - 14, base[1] - 14, base[2] - 14), width=2)
    for s, t, name in markers:
        lat, lon = ref.point_at(s, t)
        x, y = latlon_to_tile_px(lat, lon, geo)
        d.rectangle([x - 7, y - 7, x + 7, y + 7], fill=MARKER_RGB[name])
    return img


def satellite_obs(ref: ReferenceLine, geo: TileGeoref, data_dir: Path, obs_id: str, markers, *,
                  kind: ObservationKind = ObservationKind.satellite_crop, station_s: float = 50.0,
                  base: tuple[int, int, int] = (150, 150, 150)) -> Observation:
    img = satellite_tile(ref, geo, markers, base=base)
    obs = make_observation(obs_id=obs_id, kind=kind, station_s=station_s, zoom=geo.zoom, lat=geo.center_lat,
                           lon=geo.center_lon, size_px=geo.size_px)
    obs = obs.model_copy(update={"scale": geo.scale, "georef": {"center_lat": geo.center_lat, "center_lon": geo.center_lon,
                                                                  "zoom": geo.zoom, "scale": geo.scale,
                                                                  "size_px": list(geo.size_px)}})
    path = data_dir / obs.file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return obs


def wall_colour(s: float, h: float, length: float = 100.0) -> tuple[int, int, int]:
    """Smooth analytic wall: red ramps with ``s``, green with height, blue constant."""
    return (int(255 * max(0.0, min(1.0, s / length))), int(255 * max(0.0, min(1.0, h / 12.0))), 120)


def render_wall_view(ref: ReferenceLine, data_dir: Path, obs_id: str, *, s_c: float, t_c: float, heading_deg: float,
                     wall_t: float, kind: ObservationKind = ObservationKind.streetview, cam_h: float = 2.5,
                     size: int = 320, fov: float = 90.0, length: float = 100.0) -> Observation:
    """Ray-cast a pinhole camera at ``(s_c, t_c, cam_h)`` looking along compass ``heading_deg`` at the wall ``t = wall_t``."""
    f = (size / 2.0) / math.tan(math.radians(fov / 2.0))
    view_rel = math.radians(((heading_deg - ref.bearing_deg(s_c)) + 180.0) % 360.0 - 180.0)  # clockwise from travel
    img = Image.new("RGB", (size, size), (20, 20, 20))
    px = img.load()
    for j in range(size):
        for i in range(size):
            x, y = (i + 0.5 - size / 2.0) / f, -(j + 0.5 - size / 2.0) / f
            az = view_rel + math.atan(x)            # clockwise from travel
            d_s, d_t = math.cos(az), -math.sin(az)  # unit horizontal direction in (s, t)
            if abs(d_t) < 1e-9:
                continue
            lam = (wall_t - t_c) / d_t
            if lam <= 0:
                continue
            tan_e = y / math.sqrt(1.0 + x * x)
            s_hit, h_hit = s_c + lam * d_s, cam_h + lam * tan_e
            px[i, j] = wall_colour(s_hit, h_hit, length) if h_hit >= 0 else (40, 40, 40)
    obs = make_observation(obs_id=obs_id, kind=kind, station_s=s_c, view=ViewKind.left if wall_t > t_c else ViewKind.right,
                           offset_t=t_c, heading_deg=heading_deg, fov_deg=fov, size_px=(size, size))
    path = data_dir / obs.file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=96)
    return obs
