"""Annotated satellite tile for pass A: segment line, START/END markers, 10 m ticks, an arrow
toward the footpath side, and (when present) the OSM ways overlay."""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

from hero.imagery.osm import OsmGeometry
from hero.model.refline import ReferenceLine
from hero.scene.geo import LocalFrame, TileGeoref, latlon_to_tile_px

LINE_COLOR = (255, 230, 0)
START_COLOR = (0, 220, 90)
END_COLOR = (255, 60, 60)
ARROW_COLOR = (0, 200, 255)
OSM_ROAD_COLOR = (120, 200, 255)
OSM_FOOT_COLOR = (255, 160, 60)
OSM_BUILDING_COLOR = (255, 255, 140)
TICK_EVERY_M = 10.0
TICK_HALF_LEN_M = 2.5
ARROW_LEN_M = 7.0


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        try:
            return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()


def to_px(frame: LocalFrame, georef: TileGeoref, x_m: float, lat_m: float) -> tuple[float, float]:
    point = frame.to_latlon(x_m, lat_m)
    return latlon_to_tile_px(point.lat, point.lon, georef)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fill, font, anchor="la") -> None:
    x, y = xy
    draw.text((x + 1, y + 1), text, fill=(0, 0, 0), font=font, anchor=anchor)
    draw.text((x, y), text, fill=fill, font=font, anchor=anchor)


def annotate_tile(tile: Image.Image, frame: LocalFrame, georef: TileGeoref, osm: OsmGeometry | None) -> Image.Image:
    out = tile.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font(22)
    small = _font(16)
    scale = max(1.0, out.width / 640.0)

    if osm is not None:
        for way in osm.ways:
            pts = [latlon_to_tile_px(lat, lon, georef) for lat, lon in way.latlons]
            color = {"highway": OSM_ROAD_COLOR, "footway": OSM_FOOT_COLOR}.get(way.kind, OSM_BUILDING_COLOR)
            draw.line(pts, fill=color, width=max(1, int(1.5 * scale)))

    start = to_px(frame, georef, 0.0, 0.0)
    end = to_px(frame, georef, frame.length_m, 0.0)
    draw.line([start, end], fill=LINE_COLOR, width=int(4 * scale))

    x_m = 0.0
    while x_m <= frame.length_m + 1e-6:
        a = to_px(frame, georef, x_m, -TICK_HALF_LEN_M)
        b = to_px(frame, georef, x_m, TICK_HALF_LEN_M)
        draw.line([a, b], fill=LINE_COLOR, width=int(2 * scale))
        label_pt = to_px(frame, georef, x_m, -TICK_HALF_LEN_M - 2.5)
        _label(draw, label_pt, f"{int(x_m)}m", LINE_COLOR, small, anchor="mm")
        x_m += TICK_EVERY_M

    r = 9 * scale
    draw.ellipse([start[0] - r, start[1] - r, start[0] + r, start[1] + r], fill=START_COLOR, outline=(0, 0, 0))
    draw.ellipse([end[0] - r, end[1] - r, end[0] + r, end[1] + r], fill=END_COLOR, outline=(0, 0, 0))
    _label(draw, (start[0] + r + 4, start[1] - 12), "START (0 m)", START_COLOR, font)
    _label(draw, (end[0] + r + 4, end[1] - 12), f"END ({frame.length_m:.0f} m)", END_COLOR, font)

    mid = frame.length_m / 2.0
    tail = to_px(frame, georef, mid, 0.0)
    head = to_px(frame, georef, mid, ARROW_LEN_M)
    draw.line([tail, head], fill=ARROW_COLOR, width=int(4 * scale))
    angle = math.atan2(head[1] - tail[1], head[0] - tail[0])
    for sign in (-1, 1):
        wing = angle + sign * math.radians(150)
        draw.line([head, (head[0] + 14 * scale * math.cos(wing), head[1] + 14 * scale * math.sin(wing))],
                  fill=ARROW_COLOR, width=int(4 * scale))
    tip = to_px(frame, georef, mid, ARROW_LEN_M + 3.0)
    _label(draw, tip, "FOOTPATH SIDE", ARROW_COLOR, font, anchor="mm")
    return out


def annotate_reference_line(tile: Image.Image, georef: TileGeoref, ref: ReferenceLine, *, footpath_side: str,
                            title: str | None = None) -> Image.Image:
    """Placement check (Phase 2): the (possibly curved) reference line, START/END, 10 m ``s`` ticks and an arrow to
    the modelled footpath side (left = ``t > 0``) drawn on the overview tile."""
    out = tile.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font, small = _font(22), _font(16)
    scale = max(1.0, out.width / 640.0)

    def px(s: float, t: float = 0.0) -> tuple[float, float]:
        lat, lon = ref.point_at(s, t)
        return latlon_to_tile_px(lat, lon, georef)

    length = ref.length_s
    steps = max(2, int(length / 2.0))
    draw.line([px(length * i / steps) for i in range(steps + 1)], fill=LINE_COLOR, width=int(4 * scale))
    s = 0.0
    while s <= length + 1e-6:
        draw.line([px(s, -TICK_HALF_LEN_M), px(s, TICK_HALF_LEN_M)], fill=LINE_COLOR, width=int(2 * scale))
        _label(draw, px(s, -TICK_HALF_LEN_M - 2.5), f"{int(s)}m", LINE_COLOR, small, anchor="mm")
        s += TICK_EVERY_M
    start, end = px(0.0), px(length)
    r = 9 * scale
    draw.ellipse([start[0] - r, start[1] - r, start[0] + r, start[1] + r], fill=START_COLOR, outline=(0, 0, 0))
    draw.ellipse([end[0] - r, end[1] - r, end[0] + r, end[1] + r], fill=END_COLOR, outline=(0, 0, 0))
    _label(draw, (start[0] + r + 4, start[1] - 12), "START (0 m)", START_COLOR, font)
    _label(draw, (end[0] + r + 4, end[1] - 12), f"END ({length:.0f} m)", END_COLOR, font)
    side = 1.0 if footpath_side == "left" else -1.0
    tail, head = px(length / 2, 0.0), px(length / 2, side * ARROW_LEN_M)
    draw.line([tail, head], fill=ARROW_COLOR, width=int(4 * scale))
    angle = math.atan2(head[1] - tail[1], head[0] - tail[0])
    for sign in (-1, 1):
        wing = angle + sign * math.radians(150)
        draw.line([head, (head[0] + 14 * scale * math.cos(wing), head[1] + 14 * scale * math.sin(wing))],
                  fill=ARROW_COLOR, width=int(4 * scale))
    _label(draw, px(length / 2, side * (ARROW_LEN_M + 3.0)), "FOOTPATH SIDE", ARROW_COLOR, font, anchor="mm")
    if title:
        _label(draw, (12, 12), title, (255, 255, 255), font)
    return out
