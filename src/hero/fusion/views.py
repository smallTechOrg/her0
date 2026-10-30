"""Image payloads for perception: street-level batches and rectified overhead windows.

``build_street_batches`` groups contiguous stations into batches of <= ``max_images`` frames (deterministic
order); ``build_overhead_windows`` rectifies the satellite imagery into <= 4 straightened windows
(up = travel, image-left = left of travel, s/t ticks drawn by ``hero.imagery.rectify``). Every image carries a
caption stating its geometry, and the prompt helpers repeat it as a table so the model can never lose it.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from hero.llm.client import LLMImage
from hero.model.refline import RectGrid, ReferenceLine
from hero.model.types import Observation, ObservationKind
from hero.prompts import render_prompt

WINDOW_LENGTH_M = 50.0
WINDOW_T_HALF_M = 14.0
WINDOW_M_PER_PX = 0.1
MAX_OVERHEAD_WINDOWS = 4
MIN_TILE_COVERAGE = 0.6
VIEW_ORDER = {"forward": 0, "back": 1, "left": 2, "right": 3, "oblique": 4}


@dataclass(frozen=True)
class StreetBatch:
    batch_id: str
    observations: list[Observation]
    images: list[LLMImage]
    captions: list[str]


@dataclass(frozen=True)
class OverheadWindow:
    index: int                      # 1-based == image_index in the overhead call
    observation: Observation        # the satellite observation the window was rectified from
    grid: RectGrid
    image: LLMImage
    caption: str = ""


# ------------------------------------------------------------------ helpers


def wrap180(deg: float) -> float:
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def relative_heading_deg(obs: Observation, ref: ReferenceLine) -> float | None:
    """Camera heading relative to the direction of travel at the camera's station (clockwise +, right of travel)."""
    if obs.heading_deg is None or obs.station_s is None:
        return None
    return wrap180(obs.heading_deg - ref.bearing_deg(obs.station_s))


def _view_name(obs: Observation) -> str:
    return obs.view.value if obs.view is not None else "oblique"


def street_caption(obs: Observation, ref: ReferenceLine, index: int, total: int) -> str:
    rel = relative_heading_deg(obs, ref)
    s = obs.station_s if obs.station_s is not None else 0.0
    parts = [
        f"Image {index} of {total} | observation {obs.id} | view: {_view_name(obs)}",
        f"camera station s={s:.1f} m of {ref.length_s:.0f} m along the street",
    ]
    if obs.offset_t is not None:
        parts.append(f"camera offset t={obs.offset_t:+.1f} m (left of the reference line is +)")
    if obs.heading_deg is not None and rel is not None:
        parts.append(f"camera heading {obs.heading_deg:.0f} deg compass = {rel:+.0f} deg relative to travel "
                     f"({_rel_words(rel)})")
    parts.append(f"fov {obs.fov_deg or 90:.0f} deg, pitch {obs.pitch_deg or 0:.0f} deg")
    parts.append(f"image size {obs.width_px}x{obs.height_px} px (box_px is in these pixels)")
    parts.append("forward = direction of travel (increasing s)")
    return " | ".join(parts)


def _rel_words(rel: float) -> str:
    a = abs(rel)
    if a <= 35:
        return "looking ahead along the street"
    if a >= 145:
        return "looking back along the street"
    return "looking to the right of travel" if rel > 0 else "looking to the left of travel"


def _batch_id(prefix: str, parts: Sequence[str]) -> str:
    return f"{prefix}-" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


# ------------------------------------------------------------------ street-level


def build_street_batches(observations: Sequence[Observation], ref: ReferenceLine, *, data_dir: Path,
                         max_images: int) -> list[StreetBatch]:
    """Contiguous station windows of <= ``max_images`` street-level frames, in deterministic order.

    Only street-level observations with a station are used; order = ``(station_s, view, id)``.
    """
    if max_images < 1:
        raise ValueError("max_images must be >= 1")
    usable = sorted((o for o in observations if o.is_street_level and o.station_s is not None),
                    key=lambda o: (round(o.station_s, 3), VIEW_ORDER.get(_view_name(o), 9), o.id))
    batches: list[StreetBatch] = []
    for start in range(0, len(usable), max_images):
        chunk = usable[start:start + max_images]
        captions = [street_caption(o, ref, i, len(chunk)) for i, o in enumerate(chunk, 1)]
        images = [LLMImage(data=(Path(data_dir) / o.file_path).read_bytes(), media_type=o.media_type,
                           caption=cap, width_px=o.width_px, height_px=o.height_px)
                  for o, cap in zip(chunk, captions)]
        batches.append(StreetBatch(batch_id=_batch_id("sv", [o.id for o in chunk]), observations=list(chunk),
                                   images=images, captions=captions))
    return batches


def street_frame_table(batch: StreetBatch, ref: ReferenceLine) -> str:
    rows = []
    for i, o in enumerate(batch.observations, 1):
        rel = relative_heading_deg(o, ref)
        rows.append(f"- image {i}: view {_view_name(o)}, station s={o.station_s:.1f} m, "
                    f"heading {'n/a' if rel is None else f'{rel:+.0f} deg'} relative to travel, "
                    f"{o.width_px}x{o.height_px} px")
    return "\n".join(rows)


def street_prompt(batch: StreetBatch, ref: ReferenceLine, *, footpath_side: str) -> str:
    return render_prompt("perceive_streetlevel", n_images=len(batch.images), length_m=f"{ref.length_s:.0f}",
                         footpath_side=footpath_side, frame_table=street_frame_table(batch, ref))


# ------------------------------------------------------------------ overhead


def _georef(obs: Observation):
    from hero.scene.geo import TileGeoref

    g = obs.georef
    size = g.get("size_px") or [obs.width_px, obs.height_px]
    return TileGeoref(center_lat=float(g.get("center_lat", obs.lat)), center_lon=float(g.get("center_lon", obs.lon)),
                      zoom=int(g.get("zoom", obs.zoom or 19)), scale=int(g.get("scale", obs.scale or 2)),
                      size_px=(int(size[0]), int(size[1])))


def _coverage(obs: Observation, ref: ReferenceLine, grid: RectGrid) -> float:
    """Share of a 5x3 sample lattice over the window that falls inside the tile."""
    from hero.scene.geo import latlon_to_tile_px

    georef = _georef(obs)
    w, h = georef.size_px
    hit = total = 0
    for i in range(5):
        s = grid.s_bottom + (grid.s_top - grid.s_bottom) * i / 4
        for t in (grid.t_left, 0.0, grid.t_right):
            lat, lon = ref.point_at(s, t)
            px, py = latlon_to_tile_px(lat, lon, georef)
            total += 1
            hit += 1 if (0 <= px < w and 0 <= py < h) else 0
    return hit / total


def plan_window_grids(length_s: float, *, window_m: float = WINDOW_LENGTH_M, t_half: float = WINDOW_T_HALF_M,
                      m_per_px: float = WINDOW_M_PER_PX, max_windows: int = MAX_OVERHEAD_WINDOWS) -> list[RectGrid]:
    """Windows of ``window_m`` of ``s`` tiling ``[0, length_s]`` (the last is pulled back to stay full-size);
    beyond ``max_windows`` the ones nearest the segment centre are kept."""
    if length_s <= 0:
        raise ValueError("length_s must be positive")
    span = min(window_m, length_s)
    n = max(1, math.ceil(length_s / window_m - 1e-9))
    starts = [min(i * window_m, length_s - span) for i in range(n)]
    if n > max_windows:
        order = sorted(range(n), key=lambda i: abs(starts[i] + span / 2 - length_s / 2))[:max_windows]
        starts = [starts[i] for i in sorted(order)]
    return [RectGrid(s_top=round(s0 + span, 3), s_bottom=round(s0, 3), t_left=t_half, t_right=-t_half,
                     m_per_px=m_per_px) for s0 in starts]


def overhead_caption(index: int, total: int, grid: RectGrid, size_px: tuple[int, int]) -> str:
    return (f"Image {index} of {total} | rectified overhead window | up = direction of travel, image-left = left of travel | "
            f"s_top={grid.s_top:.1f} m, s_bottom={grid.s_bottom:.1f} m, t_left={grid.t_left:.1f} m, t_right={grid.t_right:.1f} m, "
            f"metres_per_px={grid.m_per_px:.3f} | image size {size_px[0]}x{size_px[1]} px "
            f"(box_px / polylines are in these pixels) | y_px -> s = s_top - y*metres_per_px; x_px -> t = t_left - x*metres_per_px | "
            "s ticks every 10 m, t ticks every 2 m")


def build_overhead_windows(observations: Sequence[Observation], ref: ReferenceLine, *, data_dir: Path,
                           max_windows: int = MAX_OVERHEAD_WINDOWS, overlay: bool = True) -> list[OverheadWindow]:
    """Rectify the best-covering satellite observation (smallest m/px wins) into each window grid.

    Windows no tile covers (>= 60 %) are skipped. ``hero.imagery.rectify`` (S2) is imported lazily.
    """
    from PIL import Image

    from hero.imagery.rectify import rectify_window

    sats = sorted((o for o in observations if o.kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop)),
                  key=lambda o: ((o.meters_per_px or 1.0), o.id))
    picked: list[tuple[RectGrid, Observation]] = []
    for grid in plan_window_grids(ref.length_s, max_windows=max_windows):
        best: tuple[float, float, str, Observation] | None = None
        for o in sats:
            cov = _coverage(o, ref, grid)
            if cov < MIN_TILE_COVERAGE:
                continue
            key = (-cov if cov < 0.999 else -1.0, o.meters_per_px or 1.0, o.id, o)
            if best is None or key[:3] < best[:3]:
                best = key
        if best is not None:
            picked.append((grid, best[3]))
    windows: list[OverheadWindow] = []
    for i, (grid, o) in enumerate(picked, 1):
        img = rectify_window(Path(data_dir) / o.file_path, _georef(o), ref, grid, overlay=overlay)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        cap = overhead_caption(i, len(picked), grid, img.size)
        windows.append(OverheadWindow(index=i, observation=o, grid=grid, caption=cap,
                                      image=LLMImage(data=buf.getvalue(), media_type="image/png", caption=cap,
                                                     width_px=img.size[0], height_px=img.size[1])))
    return windows


def overhead_window_table(windows: Sequence[OverheadWindow]) -> str:
    return "\n".join(
        f"- image {w.index}: s from {w.grid.s_bottom:.1f} m (bottom edge) to {w.grid.s_top:.1f} m (top edge), "
        f"t from {w.grid.t_left:.1f} m (left edge) to {w.grid.t_right:.1f} m (right edge), "
        f"{w.image.width_px}x{w.image.height_px} px at {w.grid.m_per_px:.3f} m/px" for w in windows)


def overhead_prompt(windows: Sequence[OverheadWindow], ref: ReferenceLine, *, footpath_side: str) -> str:
    return render_prompt("perceive_overhead", n_windows=len(windows), length_m=f"{ref.length_s:.0f}",
                         footpath_side=footpath_side, window_table=overhead_window_table(windows))
