"""Street View panoramas for the play page (the "walk the real street" backdrop).

The game projects the *real* Street View panorama nearest to the camera onto the street geometry and cross-fades
to the next one as the runner moves, the way Street View navigation does. This module rebuilds those panoramas
from the Street View frames already stored as observations: every frame of one ``pano_id`` is a pinhole view
(``heading_deg``, ``pitch_deg``, ``fov_deg``) of the same spherical image, so they are re-projected onto one
equirectangular image per panorama. Frames are blended by how central the pixel is in each frame (feathered
edges) and narrower (zoomed) frames outweigh wide ones where they overlap, so a Max-quality fetch (8 headings x 3
tilts x 2 zooms) gives a sharp, near-complete sphere while 4 level frames still give a usable band.

Equirectangular convention: column 0 = compass bearing 0 (north), ``u = bearing / 360``; row 0 = straight up,
``v = 0.5 - elevation / 180``. Gaps (no frame covers the direction) are filled with the mean colour of the covered
pixels in the same row band, so the sky stays sky-coloured and the ground stays ground-coloured.

Public API
----------
``stitch_panorama(frames, width=PANO_WIDTH) -> Image`` - pure function of the frames (each ``(image, heading_deg,
pitch_deg, fov_deg)``).
``build_panoramas(model_slug, length_s, observations, data_dir, ref) -> dict`` - writes
``<data_dir>/panoramas/<slug>/<pano_id>_<sha8>.jpg`` for every Street View panorama on the street (content
addressed: unchanged input rewrites nothing), writes ``panoramas.json`` next to them and returns that manifest
(``hero-panoramas/1``: the panoramas with their ``s`` / ``t`` on the reference line). It runs as part of the export
(Regenerate / recompile); ``GET /api/segments/{slug}/panoramas.json`` only serves the stored file.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from hero.model.coverage import camera_station
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind

PANO_WIDTH = 2048             # the live dome's sphere (sky / cross-fade); the master sphere is as wide as the frames allow
PANO_MAX_WIDTH = 8192
PANO_BAND_ROWS = 512          # the master is stitched in row bands (an 8K sphere does not fit in memory at once)
PANO_SCHEMA = "hero-panoramas/1"
MANIFEST_NAME = "panoramas.json"
SV_CAMERA_HEIGHT_M = 2.5      # Street View car camera height (projection centre above the road)
JPEG_QUALITY = 85
MAX_SIDE_M = 20.0             # panoramas farther off the line than this belong to another street
END_MARGIN_M = 12.0           # panoramas just past either end are still useful (seen from the last metres)
FEATHER = 0.12                # fraction of the frame edge over which a frame's weight fades to 0
MIN_FRAMES = 1
GROUND_BAND_M = 14.0          # the ground strip covers t in [-14, +14] (same frame as the glb ground UVs)
GROUND_MPP = 0.05             # 5 cm/px: a 100 m street -> 2000 x 560 px
GROUND_MAX_PX = 4096
GROUND_BLEND_M = 2.0          # cross-fade width between neighbouring panoramas' views of the ground
GROUND_MIN_ELEV_DEG = 1.5     # a panorama paints the ground only where it looks down at least this much


def panorama_key(observations: Sequence[Observation]) -> str:
    """Content key of the panorama set: the sorted (pano_id, sha256) of every Street View frame."""
    parts = sorted(f"{o.pano_id}:{o.sha256}" for o in observations if _is_sv(o))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:8]


def _is_sv(o: Observation) -> bool:
    return o.kind == ObservationKind.streetview and bool(o.pano_id) and o.heading_deg is not None


def _basis(heading_deg: float, pitch_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Camera axes (forward, right, up) in the (east, north, up) frame for a compass heading and a pitch."""
    h, p = math.radians(heading_deg), math.radians(pitch_deg)
    fwd = np.array([math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p)])
    right = np.array([math.cos(h), -math.sin(h), 0.0])
    up = np.cross(right, fwd)
    return fwd, right, up


def _directions(width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit direction (east, north, up) of every equirect pixel centre."""
    u = (np.arange(width) + 0.5) / width
    v = (np.arange(height) + 0.5) / height
    bearing = u * 2.0 * math.pi
    elev = (0.5 - v) * math.pi
    cb, sb = np.cos(bearing)[None, :], np.sin(bearing)[None, :]
    ce, se = np.cos(elev)[:, None], np.sin(elev)[:, None]
    return sb * ce, cb * ce, np.broadcast_to(se, (height, width))


def _sample_bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    x = np.clip(x, 0.0, w - 1.001)
    y = np.clip(y, 0.0, h - 1.001)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    top = img[y0, x0] * (1 - fx) + img[y0, x0 + 1] * fx
    bot = img[y0 + 1, x0] * (1 - fx) + img[y0 + 1, x0 + 1] * fx
    return top * (1 - fy) + bot * fy


def sphere_width(frames: Sequence[tuple[Image.Image, float, float, float]]) -> int:
    """Master sphere width matching the sharpest frame: pixels per degree x 360, to a power of two (2K .. 8K)."""
    best = 0.0
    for image, _h, _p, fov in frames:
        best = max(best, image.width / max(float(fov), 1.0))
    want = best * 360.0
    width = PANO_WIDTH
    while width * 1.5 < want and width < PANO_MAX_WIDTH:
        width *= 2
    return width


def _frame_rows(height: int, pitch: float, fov: float) -> tuple[int, int]:
    """Row band of the equirect a frame can touch (its elevation span, generous: the corners reach further)."""
    half = float(fov) / 2.0 * 1.5
    lo, hi = max(-90.0, pitch - half), min(90.0, pitch + half)
    r0 = int((0.5 - hi / 180.0) * height) - 1
    r1 = int((0.5 - lo / 180.0) * height) + 2
    return max(0, r0), min(height, r1)


def stitch_panorama(frames: Sequence[tuple[Image.Image, float, float, float]], width: int = PANO_WIDTH) -> Image.Image:
    """Re-project pinhole frames ``(image, heading_deg, pitch_deg, fov_deg)`` onto one equirectangular image.
    Stitched in row bands so an 8K sphere stays within memory; a frame only visits the bands its tilt can reach."""
    if not frames:
        raise ValueError("no frames to stitch")
    height = width // 2
    arrays = [(np.asarray(image.convert("RGB"), dtype=np.float32), float(h), float(p), float(f)) for image, h, p, f in frames]
    out = np.zeros((height, width, 3), dtype=np.float32)
    covered = np.zeros((height, width), dtype=bool)
    u = (np.arange(width) + 0.5) / width
    bearing = u * 2.0 * math.pi
    cb, sb = np.cos(bearing)[None, :], np.sin(bearing)[None, :]
    for r0 in range(0, height, PANO_BAND_ROWS):
        r1 = min(height, r0 + PANO_BAND_ROWS)
        elev = (0.5 - (np.arange(r0, r1) + 0.5) / height) * math.pi
        ce, se = np.cos(elev)[:, None], np.sin(elev)[:, None]
        de, dn, du = sb * ce, cb * ce, np.broadcast_to(se, (r1 - r0, width))
        acc = np.zeros((r1 - r0, width, 3), dtype=np.float32)
        wsum = np.zeros((r1 - r0, width), dtype=np.float32)
        for img, heading, pitch, fov in arrays:
            f0, f1 = _frame_rows(height, pitch, fov)
            if f1 <= r0 or f0 >= r1:
                continue
            ih, iw = img.shape[:2]
            fwd, right, up = _basis(heading, pitch)
            z = de * fwd[0] + dn * fwd[1] + du * fwd[2]
            front = z > 1e-3
            zs = np.where(front, z, 1.0)
            x = (de * right[0] + dn * right[1] + du * right[2]) / zs
            y = (de * up[0] + dn * up[1] + du * up[2]) / zs
            half = math.tan(math.radians(fov) / 2.0)
            nx, ny = x / half, y / half          # -1 .. 1 inside the frame
            inside = front & (np.abs(nx) < 1.0) & (np.abs(ny) < 1.0)
            if not inside.any():
                continue
            ys, xs = np.nonzero(inside)
            edge = np.minimum(1.0 - np.abs(nx[ys, xs]), 1.0 - np.abs(ny[ys, xs]))
            w = np.clip(edge / FEATHER, 0.0, 1.0) * (90.0 / max(fov, 1.0)) ** 3   # a zoomed frame has more pixels per degree
            px = (nx[ys, xs] + 1.0) * 0.5 * (iw - 1)
            py = (1.0 - ny[ys, xs]) * 0.5 * (ih - 1)
            acc[ys, xs] += _sample_bilinear(img, px, py) * w[:, None]
            wsum[ys, xs] += w
        band = wsum > 1e-6
        out[r0:r1][band] = acc[band] / wsum[band][:, None]
        covered[r0:r1] = band
    _fill_gaps(out, covered)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


def _fill_gaps(out: np.ndarray, covered: np.ndarray) -> None:
    """Paint uncovered pixels by stretching each column's nearest covered pixel up (sky) or down (ground), then
    blurring the stretched area so the fill reads as haze / tarmac rather than streaks."""
    if covered.all():
        return
    height, width = covered.shape
    overall = out[covered].mean(axis=0) if covered.any() else np.array([160.0, 170.0, 180.0])
    rows = np.arange(height)[:, None]
    has = covered.any(axis=0)
    first = np.where(has, np.argmax(covered, axis=0), 0)
    last = np.where(has, height - 1 - np.argmax(covered[::-1], axis=0), height - 1)
    cols = np.arange(width)
    above = (rows < first[None, :]) & has[None, :]
    below = (rows > last[None, :]) & has[None, :]
    top = out[first, cols]        # (width, 3) colour at the top edge of each column's coverage
    bottom = out[last, cols]
    filled = out.copy()
    filled[above] = np.broadcast_to(top[None, :, :], (height, width, 3))[above]
    filled[below] = np.broadcast_to(bottom[None, :, :], (height, width, 3))[below]
    filled[~(covered | above | below)] = overall
    # soft fill: blur the stretched colours (wide, so the seams between columns melt away)
    blurred = np.asarray(Image.fromarray(np.clip(filled + 0.5, 0, 255).astype(np.uint8), "RGB")
                         .filter(ImageFilter.GaussianBlur(max(2, width // 128))), dtype=np.float32)
    gap = ~covered
    out[gap] = blurred[gap]


# ------------------------------------------------------------------ the frozen ground


def bake_ground(panos: Sequence[tuple[float, float, np.ndarray, float]], length_s: float, ref: ReferenceLine, *,
                mpp: float = GROUND_MPP, camera_height_m: float = SV_CAMERA_HEIGHT_M) -> Image.Image:
    """The street's ground (``t`` in ``[-14, 14]``, ``s`` in ``[0, L]``) painted ONCE from the panoramas: every
    pixel takes the view of the nearest panorama (by ``s``), cross-faded over ``GROUND_BLEND_M`` at the midpoint
    between neighbours. Row 0 = ``s = L``, column 0 = ``t = +14`` (the glb ground UV frame, ``uv_ground``), so the
    play page can drape it on the ground nodes directly. Frozen, the road never swims as the runner moves.

    ``panos`` = ``(s, t, equirect RGB float array, min_elev_deg)`` per panorama (column 0 = north, row 0 = up;
    ``min_elev_deg`` = the lowest elevation its frames cover - the ground closer than that, i.e. the blind spot under
    Google's car, is painted by the neighbouring panoramas instead)."""
    if not panos:
        raise ValueError("no panoramas")
    width = max(2, min(GROUND_MAX_PX, int(round(2 * GROUND_BAND_M / mpp))))
    height = max(2, min(GROUND_MAX_PX, int(round(length_s / mpp))))
    t = GROUND_BAND_M - (np.arange(width) + 0.5) * (2 * GROUND_BAND_M / width)       # +14 .. -14
    s = length_s - (np.arange(height) + 0.5) * (length_s / height)                    # L .. 0
    # world xy of every pixel from the reference line (east, north), left normal = (-ty, tx)
    base = np.array([ref._xy_ext(float(v)) for v in s])                                # (H, 2)
    tang = np.array([ref.tangent(float(v)) for v in s])                                # (H, 2)
    px = base[:, 0][:, None] - tang[:, 1][:, None] * t[None, :]
    py = base[:, 1][:, None] + tang[:, 0][:, None] * t[None, :]
    order = sorted(range(len(panos)), key=lambda i: panos[i][0])
    ss = np.array([panos[i][0] for i in order])
    acc = np.zeros((height, width, 3), dtype=np.float32)
    wsum = np.zeros((height, width), dtype=np.float32)
    for n, i in enumerate(order):
        ps, pt, img, min_elev = panos[i]
        # row weight: 1 inside this panorama's half of the gap to each neighbour, fading over GROUND_BLEND_M; the
        # neighbours keep a small weight everywhere so a blind spot is always painted by someone
        lo = -np.inf if n == 0 else (ss[n - 1] + ps) / 2.0
        hi = np.inf if n == len(order) - 1 else (ps + ss[n + 1]) / 2.0
        near = np.clip((s - lo) / GROUND_BLEND_M + 0.5, 0.0, 1.0) * np.clip((hi - s) / GROUND_BLEND_M + 0.5, 0.0, 1.0)
        w_row = np.maximum(near, 0.02 * np.exp(-np.abs(s - ps) / 25.0))
        rows = np.nonzero(w_row > 1e-4)[0]
        if not rows.size:
            continue
        cx, cy = ref._xy_ext(float(ps))
        tx, ty = ref.tangent(float(ps))
        cx, cy = cx - ty * pt, cy + tx * pt
        dx = px[rows] - cx
        dy = py[rows] - cy
        horiz = np.hypot(dx, dy)
        elev = np.degrees(np.arctan2(-camera_height_m, horiz))
        bearing = np.degrees(np.arctan2(dx, dy)) % 360.0
        ih, iw = img.shape[:2]
        u = bearing / 360.0 * iw - 0.5
        v = (0.5 - elev / 180.0) * ih - 0.5
        colours = _sample_bilinear_wrap(img, u, v)
        seen = np.clip((elev - (min_elev + 2.0)) / 6.0, 0.0, 1.0)      # 0 inside the blind spot, 1 beyond it
        w = w_row[rows][:, None] * np.clip((-elev - GROUND_MIN_ELEV_DEG) / 3.0, 0.0, 1.0) * seen
        acc[rows] += colours * w[..., None]
        wsum[rows] += w
    covered = wsum > 1e-6
    out = np.zeros_like(acc)
    out[covered] = acc[covered] / wsum[covered][:, None]
    _fill_gaps(out, covered)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


FRONTAGE_TOP_M = 12.0         # the frontage atlas covers 0..12 m of wall (glb facade UVs: v = v0 + (1 - h/12) / 2)
FRONTAGE_W = 4096
FRONTAGE_H_SIDE = 512         # rows per ribbon (12 m -> 2.3 cm/px); Frontage_L on top, Frontage_R below
FRONTAGE_BLEND_M = 2.0


def bake_frontage(panos: Sequence[tuple[float, float, np.ndarray, float]], length_s: float, ref: ReferenceLine,
                  walls: Mapping[str, Sequence[tuple[float, float, float, float, float]]], *,
                  camera_height_m: float = SV_CAMERA_HEIGHT_M) -> Image.Image:
    """The frontage atlas painted ONCE from the panoramas, in the glb facade-UV frame: the ``Frontage_L`` ribbon on
    the top half (row 0 = 12 m up, column 0 = s = 0), ``Frontage_R`` on the bottom. Every wall pixel takes the view
    of the panorama nearest to it along the street (cross-faded over ``FRONTAGE_BLEND_M``), so the shopfronts stay
    put as the runner moves. ``walls[side]`` = ``(s_from, s_to, t_from, t_to, height_m)`` spans (left-positive ``t``);
    columns with no wall span are painted from the sky/ground of the same panorama (they are not drawn anyway)."""
    if not len(panos):
        raise ValueError("no panoramas")
    width, hside = FRONTAGE_W, FRONTAGE_H_SIDE
    out = np.zeros((2 * hside, width, 3), dtype=np.float32)
    covered = np.zeros((2 * hside, width), dtype=bool)
    s = (np.arange(width) + 0.5) * (length_s / width)                                 # 0 .. L (column)
    h = FRONTAGE_TOP_M * (1.0 - (np.arange(hside) + 0.5) / hside)                      # 12 .. 0 (row)
    base = np.array([ref._xy_ext(float(v)) for v in s])
    tang = np.array([ref.tangent(float(v)) for v in s])
    order = sorted(range(len(panos)), key=lambda i: panos[i][0])
    ss = np.array([panos[i][0] for i in order])
    for side_i, side in enumerate(("left", "right")):
        # wall t per column (nearest span; a default 10 m out when the side has no span there)
        t = np.full(width, 10.0 if side == "left" else -10.0)
        has = np.zeros(width, dtype=bool)
        for s0, s1, t0, t1, _hm in walls.get(side, ()):
            m = (s >= s0) & (s < s1)
            if s1 > s0:
                t[m] = t0 + (t1 - t0) * (s[m] - s0) / (s1 - s0)
            has |= m
        px = base[:, 0] - tang[:, 1] * t
        py = base[:, 1] + tang[:, 0] * t
        acc = np.zeros((hside, width, 3), dtype=np.float32)
        wsum = np.zeros((hside, width), dtype=np.float32)
        for n, i in enumerate(order):
            ps, pt, img, min_elev = panos[i]
            lo = -np.inf if n == 0 else (ss[n - 1] + ps) / 2.0
            hi = np.inf if n == len(order) - 1 else (ps + ss[n + 1]) / 2.0
            near = np.clip((s - lo) / FRONTAGE_BLEND_M + 0.5, 0.0, 1.0) * np.clip((hi - s) / FRONTAGE_BLEND_M + 0.5, 0.0, 1.0)
            w_col = np.maximum(near, 0.02 * np.exp(-np.abs(s - ps) / 25.0))
            cols = np.nonzero(w_col > 1e-4)[0]
            if not cols.size:
                continue
            cx, cy = ref._xy_ext(float(ps))
            tx, ty = ref.tangent(float(ps))
            cx, cy = cx - ty * pt, cy + tx * pt
            dx = (px[cols] - cx)[None, :]
            dy = (py[cols] - cy)[None, :]
            horiz = np.hypot(dx, dy)
            elev = np.degrees(np.arctan2(h[:, None] - camera_height_m, horiz))
            bearing = np.degrees(np.arctan2(dx, dy)) % 360.0
            ih, iw = img.shape[:2]
            u = bearing / 360.0 * iw - 0.5
            v = (0.5 - elev / 180.0) * ih - 0.5
            colours = _sample_bilinear_wrap(img, np.broadcast_to(u, elev.shape), v)
            seen = np.clip((elev - (min_elev + 2.0)) / 6.0, 0.0, 1.0)
            w = w_col[cols][None, :] * seen
            acc[:, cols] += colours * w[..., None]
            wsum[:, cols] += w
        band = wsum > 1e-6
        rows = slice(side_i * hside, (side_i + 1) * hside)
        out[rows][band] = acc[band] / wsum[band][:, None]
        covered[rows] = band
    _fill_gaps(out, covered)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


def _sample_bilinear_wrap(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear sample with horizontal wrap (equirect seam) and vertical clamp."""
    h, w = img.shape[:2]
    y = np.clip(y, 0.0, h - 1.001)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int32)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    xa, xb = x0 % w, (x0 + 1) % w
    top = img[y0, xa] * (1 - fx) + img[y0, xb] * fx
    bot = img[y0 + 1, xa] * (1 - fx) + img[y0 + 1, xb] * fx
    return top * (1 - fy) + bot * fy


# ------------------------------------------------------------------ building the set for a street


def panoramas_dir(data_dir: Path, slug: str) -> Path:
    return Path(data_dir) / "panoramas" / slug


def _sv_frames(observations: Sequence[Observation]) -> dict[str, list[Observation]]:
    groups: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        if _is_sv(o):
            groups[str(o.pano_id)].append(o)
    return groups


def build_panoramas(slug: str, length_s: float, observations: Sequence[Observation], data_dir: Path,
                    ref: ReferenceLine, *, width: int | None = None,
                    walls: Mapping[str, Sequence[tuple[float, float, float, float, float]]] | None = None) -> dict[str, Any]:
    """Stitch and store every Street View panorama of the street; return the ``hero-panoramas/1`` manifest.

    ``width`` fixes the master sphere width (default: as sharp as the frames allow, 2K..8K); the live dome always gets
    a 2K copy (``url``), the bakes read the master (``master_url``). ``walls`` = per physical side (``"left"`` /
    ``"right"`` of travel) the wall spans ``(s_from, s_to, t_from, t_to, height_m)`` the frontage mesh uses; with
    them the frontage atlas is baked too (``manifest["frontage"]``)."""
    out_dir = panoramas_dir(data_dir, slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = panorama_key(observations)
    panos: list[dict[str, Any]] = []
    for pano_id, obs in sorted(_sv_frames(observations).items()):
        first = obs[0]
        s = camera_station(first, ref)
        t = float(first.offset_t) if first.offset_t is not None else float(ref.to_st(first.lat, first.lon).t)
        if s < -END_MARGIN_M or s > length_s + END_MARGIN_M or abs(t) > MAX_SIDE_M:
            continue
        frames = []
        for o in obs:
            path = Path(data_dir) / o.file_path
            if not path.is_file():
                continue
            try:
                frames.append((Image.open(path), float(o.heading_deg or 0.0), float(o.pitch_deg or 0.0),
                               float(o.fov_deg or 90.0)))
            except OSError:
                continue
        if len(frames) < MIN_FRAMES:
            continue
        master_w = width or sphere_width(frames)
        sha = hashlib.sha256(("\n".join(sorted(f"{o.sha256}:{o.heading_deg}:{o.pitch_deg}:{o.fov_deg}" for o in obs))
                              + f"|{master_w}").encode()).hexdigest()[:8]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in pano_id)
        master = out_dir / f"{safe}_{sha}.jpg"
        live_w = min(master_w, PANO_WIDTH)
        live = out_dir / f"{safe}_{sha}_2k.jpg" if master_w > PANO_WIDTH else master
        if not master.is_file() or not live.is_file():
            image = stitch_panorama(frames, width=master_w)
            _save_jpeg(image, master)
            if live is not master:
                _save_jpeg(image.resize((PANO_WIDTH, PANO_WIDTH // 2), Image.LANCZOS), live)
        for f in frames:
            f[0].close()
        min_elev = min(f[2] - f[3] / 2.0 for f in frames)      # lowest elevation any frame of this sphere reaches
        panos.append({
            "pano_id": pano_id, "s": round(s, 3), "t": round(t, 3), "lat": first.lat, "lon": first.lon,
            "frames": len(frames), "capture_date": first.capture_date, "min_elev_deg": round(min_elev, 1),
            "url": f"/media/panoramas/{slug}/{live.name}", "width": live_w, "height": live_w // 2,
            "master_url": f"/media/panoramas/{slug}/{master.name}", "master_width": master_w,
        })
    panos.sort(key=lambda p: p["s"])
    ground = frontage = None
    if panos:
        views = _master_views(out_dir, panos)
        ground = _store_ground(out_dir, slug, views, length_s, ref, key)
        if walls:
            frontage = _store_frontage(out_dir, slug, views, length_s, ref, key, walls)
    manifest = {
        "schema": PANO_SCHEMA, "slug": slug, "key": key, "length_s": length_s,
        "camera_height_m": SV_CAMERA_HEIGHT_M, "bearing0_deg": round(ref.bearing_deg(0.0), 3), "panoramas": panos,
        "ground": ground, "frontage": frontage,
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1))
    return manifest


def _save_jpeg(image: Image.Image, dest: Path) -> None:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    dest.write_bytes(buf.getvalue())


class _LazyViews:
    """``(s, t, master array, min_elev)`` per panorama, decoded on first use (an 8K master is 400 MB as floats)."""

    def __init__(self, out_dir: Path, panos: list[dict[str, Any]]):
        self.out_dir, self.panos = out_dir, panos
        self._cache: dict[int, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.panos)

    def __getitem__(self, i: int) -> tuple[float, float, np.ndarray, float]:
        p = self.panos[i]
        if i not in self._cache:
            if len(self._cache) >= 2:
                self._cache.pop(next(iter(self._cache)))
            with Image.open(self.out_dir / Path(p.get("master_url") or p["url"]).name) as im:
                self._cache[i] = np.asarray(im.convert("RGB"), dtype=np.float32)
        return float(p["s"]), float(p["t"]), self._cache[i], float(p.get("min_elev_deg", -45.0))

    def __iter__(self):
        for i in range(len(self.panos)):
            yield self[i]


def _master_views(out_dir: Path, panos: list[dict[str, Any]]) -> _LazyViews:
    return _LazyViews(out_dir, panos)


def _store_ground(out_dir: Path, slug: str, views: _LazyViews, length_s: float, ref: ReferenceLine,
                  key: str) -> dict[str, Any]:
    dest = out_dir / f"ground_{key}.jpg"
    if not dest.is_file():
        _save_jpeg(bake_ground(views, length_s, ref), dest)
    with Image.open(dest) as im:
        w, h = im.size
    return {"url": f"/media/panoramas/{slug}/{dest.name}", "width": w, "height": h, "band_m": GROUND_BAND_M,
            "row0_s": length_s, "col0_t": GROUND_BAND_M}


def _store_frontage(out_dir: Path, slug: str, views: _LazyViews, length_s: float, ref: ReferenceLine, key: str,
                    walls: Mapping[str, Sequence[tuple[float, float, float, float, float]]]) -> dict[str, Any] | None:
    wkey = hashlib.sha256(json.dumps({k: [list(map(float, w)) for w in v] for k, v in sorted(walls.items())},
                                     sort_keys=True).encode()).hexdigest()[:8]
    dest = out_dir / f"frontage_{key}_{wkey}.jpg"
    if not dest.is_file():
        _save_jpeg(bake_frontage(views, length_s, ref, walls), dest)
    with Image.open(dest) as im:
        w, h = im.size
    return {"url": f"/media/panoramas/{slug}/{dest.name}", "width": w, "height": h, "top_m": FRONTAGE_TOP_M,
            "layout": "stacked_L_over_R"}


def load_panoramas(slug: str, data_dir: Path) -> dict[str, Any] | None:
    """The stored manifest of ``slug`` if every image it lists still exists (else ``None``)."""
    path = panoramas_dir(data_dir, slug) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    extras = [manifest[k] for k in ("ground", "frontage") if manifest.get(k)]
    for p in [*manifest.get("panoramas", []), *extras]:
        if not (panoramas_dir(data_dir, slug) / Path(p["url"]).name).is_file():
            return None
    return manifest

