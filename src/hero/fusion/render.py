"""Top-down render of a ``CanonicalModel`` for the critique step (and debugging).

Strips (by kind; the footpath tinted by interval state), entities (marker + id label, colour = confidence band),
the reference line with a labelled s tick every 10 m, drawn over an optional satellite tile (``georef`` given) or a
dark plain background. Travel is toward increasing ``s``. Pure: no network, DB or clock.
"""

from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from hero.model.refline import ReferenceLine
from hero.model.types import CanonicalModel, EntityState, FootpathState, StripKind

STRIP_COLORS: dict[str, tuple[int, int, int]] = {
    "footpath": (60, 200, 90), "kerb": (200, 200, 200), "verge": (120, 160, 80), "planting": (40, 140, 60),
    "parking": (90, 130, 200), "cycle_lane": (200, 120, 200), "carriageway": (110, 110, 120),
    "median": (170, 150, 90), "shoulder": (140, 140, 100), "forecourt": (190, 160, 120), "unknown": (150, 150, 150),
}
FOOTPATH_STATE_COLORS = {
    FootpathState.intact: (60, 200, 90), FootpathState.broken: (255, 180, 0), FootpathState.missing: (240, 50, 50),
    FootpathState.blocked: (240, 50, 50), FootpathState.encroached: (255, 140, 0), FootpathState.unknown: (170, 170, 170),
}
BACKGROUND = (28, 30, 34)
MARGIN_M = 4.0
TICK_EVERY_M = 10.0
MAX_SIDE_PX = 1280
MIN_SCALE = 3.0       # px per metre in plain mode
MAX_SCALE = 12.0
PLAIN_T_HALF_M = 14.0


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _conf_color(conf: float) -> tuple[int, int, int]:
    return (60, 220, 90) if conf >= 0.7 else (255, 200, 0) if conf >= 0.4 else (255, 80, 80)


def render_model_topdown(model: CanonicalModel, *, tile: Image.Image | None = None, georef=None,
                         ref: ReferenceLine | None = None, labels: bool = True) -> Image.Image:
    """RGB image. With ``tile`` + ``georef`` (``hero.scene.geo.TileGeoref``) the model is drawn over the tile and
    cropped to the corridor; otherwise on a plain background scaled to fit the corridor (``t`` in +-14 m)."""
    ref = ref or ReferenceLine.from_model(model.reference_line)
    length = ref.length_s

    if tile is not None and georef is not None:
        from hero.scene.geo import latlon_to_tile_px

        base = tile.convert("RGB")

        def to_px(s: float, t: float) -> tuple[float, float]:
            lat, lon = ref.point_at(s, t)
            return latlon_to_tile_px(lat, lon, georef)
    else:
        xs, ys = [], []
        for s in ref.sample_s(2.5):
            for t in (-PLAIN_T_HALF_M, PLAIN_T_HALF_M):
                lat, lon = ref.point_at(s, t)
                x, y = ref.latlon_to_xy(lat, lon)
                xs.append(x)
                ys.append(y)
        x0, x1, y0, y1 = min(xs) - MARGIN_M, max(xs) + MARGIN_M, min(ys) - MARGIN_M, max(ys) + MARGIN_M
        span = max(x1 - x0, y1 - y0, 1.0)
        scale = min(MAX_SCALE, max(MIN_SCALE, 900.0 / span))
        base = Image.new("RGB", (max(64, int((x1 - x0) * scale)), max(64, int((y1 - y0) * scale))), BACKGROUND)

        def to_px(s: float, t: float) -> tuple[float, float]:
            lat, lon = ref.point_at(s, t)
            x, y = ref.latlon_to_xy(lat, lon)
            return (x - x0) * scale, (y1 - y) * scale

    px_per_m = _px_per_m(to_px)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _font(max(11, int(px_per_m * 1.1)))

    for iv in model.profile:
        for strip in iv.strips:
            color = STRIP_COLORS.get(strip.kind.value, STRIP_COLORS["unknown"])
            if strip.kind is StripKind.footpath:
                color = FOOTPATH_STATE_COLORS.get(iv.footpath_state, color)
            poly = _strip_polygon(to_px, iv.s_from, iv.s_to, strip)
            draw.polygon(poly, fill=color + (110,), outline=color + (200,))
        if not iv.footpath_strip(model.footpath_side) and iv.footpath_state in (FootpathState.missing, FootpathState.blocked):
            lo, hi = (3.5, 6.5) if model.footpath_side == "left" else (-6.5, -3.5)
            draw.polygon(_quad(to_px, iv.s_from, iv.s_to, lo, hi), fill=(240, 50, 50, 70), outline=(240, 50, 50, 160))

    line = [to_px(s, 0.0) for s in ref.sample_s(2.5)]
    draw.line(line, fill=(255, 255, 255, 230), width=max(1, int(px_per_m * 0.15)))
    s = 0.0
    while s <= length + 1e-6:
        a, b = to_px(s, -1.2), to_px(s, 1.2)
        draw.line([a, b], fill=(255, 255, 255, 255), width=max(1, int(px_per_m * 0.1)))
        if labels:
            _label(draw, to_px(s, 1.6), f"{s:.0f}", font, (255, 255, 255))
        s += TICK_EVERY_M
    if labels:
        _label(draw, to_px(length / 2, -PLAIN_T_HALF_M + 1), "travel -> increasing s", font, (255, 255, 255))

    for ent in sorted(model.entities, key=lambda e: e.id):
        if ent.state is EntityState.removed:
            continue
        color = _conf_color(ent.confidence)
        half_s, half_t = max(ent.extent.length_s, 0.4) / 2, max(ent.extent.width_t, 0.4) / 2
        draw.polygon(_quad(to_px, ent.position.s - half_s, ent.position.s + half_s,
                           ent.position.t - half_t, ent.position.t + half_t), fill=color + (150,), outline=color + (255,))
        cx, cy = to_px(ent.position.s, ent.position.t)
        r = max(2.0, px_per_m * 0.3)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (255,), outline=(0, 0, 0, 255))
        if labels:
            tag = ent.id + ("?" if ent.state is EntityState.disputed else "")
            _label(draw, (cx + r + 2, cy - r), f"{tag} {ent.type.value} {ent.confidence:.2f}", font, color)

    out = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    if tile is not None and georef is not None:
        pts = [to_px(s, t) for s in (0.0, length) for t in (-PLAIN_T_HALF_M, PLAIN_T_HALF_M)] + line
        pad = px_per_m * MARGIN_M
        box = (max(0, int(min(p[0] for p in pts) - pad)), max(0, int(min(p[1] for p in pts) - pad)),
               min(out.width, int(max(p[0] for p in pts) + pad)), min(out.height, int(max(p[1] for p in pts) + pad)))
        if box[2] - box[0] >= 32 and box[3] - box[1] >= 32:
            out = out.crop(box)
    if max(out.size) > MAX_SIDE_PX:
        out.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.LANCZOS)
    return out


def _px_per_m(to_px: Callable[[float, float], tuple[float, float]]) -> float:
    a, b = to_px(0.0, 0.0), to_px(0.0, 1.0)
    return max(1.0, math.hypot(a[0] - b[0], a[1] - b[1]))


def _quad(to_px, s0: float, s1: float, t0: float, t1: float) -> list[tuple[float, float]]:
    return [to_px(s0, t0), to_px(s1, t0), to_px(s1, t1), to_px(s0, t1)]


def _strip_polygon(to_px, s0: float, s1: float, strip) -> list[tuple[float, float]]:
    steps = max(1, int((s1 - s0) // 2.5))
    lows, highs = [], []
    for i in range(steps + 1):
        f = i / steps
        s = s0 + (s1 - s0) * f
        lows.append(to_px(s, strip.t_lo_at(f)))
        highs.append(to_px(s, strip.t_hi_at(f)))
    return lows + highs[::-1]


def _label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font, fill) -> None:
    x, y = xy
    draw.text((x + 1, y + 1), text, fill=(0, 0, 0, 255), font=font)
    draw.text((x, y), text, fill=fill + (255,), font=font)
