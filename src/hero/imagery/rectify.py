"""Straighten a satellite tile along the reference line: output pixel (x, y) <-> ``grid.px_to_st`` (s, t).

Up = direction of travel, image-left = left of travel, so the reference line (``t = 0``) is the vertical centre
line of a symmetric grid even on a curved road. Pure Pillow: the tile is sampled with a mesh of thin horizontal
strips, each a bilinear quad in the source (exact on straight runs, sub-pixel on gentle curves).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from hero.model.refline import RectGrid, ReferenceLine
from hero.scene.geo import TileGeoref, latlon_to_tile_px

STRIP_PX = 4
TICK_EVERY_M = 5.0
OVERLAY_COLOR = (255, 230, 0)


def rectify_window(tile_path: Path, georef: TileGeoref, ref: ReferenceLine, grid: RectGrid, *,
                   overlay: bool = True) -> Image.Image:
    width, height = grid.size_px
    if width <= 0 or height <= 0:
        raise ValueError("rectify grid is empty")
    with Image.open(tile_path) as src_img:
        src = src_img.convert("RGB")
    if src.size != tuple(georef.size_px):
        # The georef is in the tile's actual pixels (Static Maps scale 2 => 1280); refuse a mismatch silently shifting.
        raise ValueError(f"tile is {src.size[0]}x{src.size[1]} px but the georef says {tuple(georef.size_px)}")

    def src_px(x: float, y: float) -> tuple[float, float]:
        s, t = grid.px_to_st(x, y)
        lat, lon = ref.point_at(s, t)
        return latlon_to_tile_px(lat, lon, georef)

    mesh = []
    for y0 in range(0, height, STRIP_PX):
        y1 = min(y0 + STRIP_PX, height)
        nw, sw, se, ne = src_px(0, y0), src_px(0, y1), src_px(width, y1), src_px(width, y0)
        mesh.append(((0, y0, width, y1), (*nw, *sw, *se, *ne)))
    out = src.transform((width, height), Image.MESH, mesh, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
    if overlay:
        _draw_overlay(out, grid)
    return out


def _draw_overlay(img: Image.Image, grid: RectGrid) -> None:
    draw = ImageDraw.Draw(img)
    cx, _ = grid.st_to_px(grid.s_top, 0.0)
    if 0 <= cx < img.width:
        for y in range(0, img.height, 12):
            draw.line([(cx, y), (cx, min(y + 6, img.height))], fill=OVERLAY_COLOR, width=1)
    s = (int(grid.s_bottom // TICK_EVERY_M) + 1) * TICK_EVERY_M
    while s < grid.s_top:
        _, y = grid.st_to_px(s, 0.0)
        draw.line([(0, y), (8, y)], fill=OVERLAY_COLOR, width=1)
        draw.text((10, y - 5), f"{s:g}m", fill=OVERLAY_COLOR)
        s += TICK_EVERY_M
