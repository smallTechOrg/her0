"""Top-down preview: the satellite tile with the scene drawn over it (lanes, coverage, buildings,
objects coloured by action with their ids, tokens). Used for the review loop and the thumbnail."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from hero.scene.geo import apply_frame_to_px
from hero.scene.schema import CoverageState, Scene

ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "jump": (255, 80, 80),
    "slide": (255, 170, 0),
    "dodge": (255, 0, 200),
    "none": (120, 220, 255),
}
COVERAGE_COLORS = {
    CoverageState.intact: (0, 220, 90, 110),
    CoverageState.broken: (255, 190, 0, 120),
    CoverageState.missing: (255, 40, 40, 130),
}
LANE_COLOR = (255, 255, 255, 40)
BUILDING_COLOR = (255, 255, 140, 70)
BUILDING_OUTLINE = (255, 255, 140)
TOKEN_COLOR = (255, 230, 0)
SEGMENT_COLOR = (255, 255, 255)
CROP_MARGIN_PX = 120
MIN_CROP_PX = 320


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        try:
            return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()


def _quad(affine, x0: float, x1: float, lat0: float, lat1: float) -> list[tuple[float, float]]:
    return [apply_frame_to_px(affine, x0, lat0), apply_frame_to_px(affine, x1, lat0),
            apply_frame_to_px(affine, x1, lat1), apply_frame_to_px(affine, x0, lat1)]


def render_topdown(scene: Scene, tile: Image.Image, *, crop: bool = True) -> Image.Image:
    base = tile.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    affine = scene.ground.frame_to_px
    length = scene.segment.length_m
    lanes = scene.cross_section.lanes
    font = _font(max(12, int(base.width / 70)))

    for lane in lanes:
        draw.polygon(_quad(affine, 0, length, lane.lat_m - lane.width_m / 2, lane.lat_m + lane.width_m / 2),
                     fill=LANE_COLOR, outline=(255, 255, 255, 90))
    footpath = lanes[0]
    for rng in scene.cross_section.footpath_coverage:
        draw.polygon(_quad(affine, rng.from_m, rng.to_m, footpath.lat_m - footpath.width_m / 2,
                           footpath.lat_m + footpath.width_m / 2), fill=COVERAGE_COLORS[rng.state])
    for building in scene.buildings:
        pts = [apply_frame_to_px(affine, x, lat) for x, lat in building.footprint]
        draw.polygon(pts, fill=BUILDING_COLOR, outline=BUILDING_OUTLINE)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        draw.text((cx, cy), building.id, fill=BUILDING_OUTLINE, font=font, anchor="mm")

    draw.line([apply_frame_to_px(affine, 0, 0), apply_frame_to_px(affine, length, 0)], fill=SEGMENT_COLOR, width=2)

    composed = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(composed)
    lane_by_index = {lane.index: lane for lane in lanes}
    for obj in scene.objects:
        color = ACTION_COLORS[obj.action.value]
        lat_lo = min(lane_by_index[i].lat_m for i in obj.lanes) - 0.8
        lat_hi = max(lane_by_index[i].lat_m for i in obj.lanes) + 0.8
        pts = _quad(affine, obj.x_m, obj.x_m + max(obj.length_m, 0.8), lat_lo, lat_hi)
        draw.polygon(pts, outline=color, width=3)
        px, py = obj.tile_px
        draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=color, outline=color)
        draw.text((px + 8, py - 8), f"{obj.id} {obj.type.value}", fill=color, font=font, stroke_width=2,
                  stroke_fill=(0, 0, 0))
    for tok in scene.tokens:
        px, py = tok.tile_px
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=TOKEN_COLOR, outline=(0, 0, 0))

    result = composed.convert("RGB")
    if not crop:
        return result
    xs, ys = [], []
    for x in (0.0, length):
        for lat in (-14.0, 24.0):
            px, py = apply_frame_to_px(affine, x, lat)
            xs.append(px)
            ys.append(py)
    left = max(0, int(min(xs) - CROP_MARGIN_PX))
    top = max(0, int(min(ys) - CROP_MARGIN_PX))
    right = min(result.width, int(max(xs) + CROP_MARGIN_PX))
    bottom = min(result.height, int(max(ys) + CROP_MARGIN_PX))
    if right - left < MIN_CROP_PX or bottom - top < MIN_CROP_PX:
        return result
    return result.crop((left, top, right, bottom))


def make_thumbnail(image: Image.Image, max_px: int = 320) -> Image.Image:
    thumb = image.copy()
    thumb.thumbnail((max_px, max_px), Image.LANCZOS)
    return thumb
