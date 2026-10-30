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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from hero.model.coverage import camera_station
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind

PANO_WIDTH = 2048
PANO_SCHEMA = "hero-panoramas/1"
MANIFEST_NAME = "panoramas.json"
SV_CAMERA_HEIGHT_M = 2.5      # Street View car camera height (projection centre above the road)
JPEG_QUALITY = 85
MAX_SIDE_M = 20.0             # panoramas farther off the line than this belong to another street
END_MARGIN_M = 12.0           # panoramas just past either end are still useful (seen from the last metres)
FEATHER = 0.12                # fraction of the frame edge over which a frame's weight fades to 0
MIN_FRAMES = 1


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


def stitch_panorama(frames: Sequence[tuple[Image.Image, float, float, float]], width: int = PANO_WIDTH) -> Image.Image:
    """Re-project pinhole frames ``(image, heading_deg, pitch_deg, fov_deg)`` onto one equirectangular image."""
    if not frames:
        raise ValueError("no frames to stitch")
    height = width // 2
    de, dn, du = _directions(width, height)
    acc = np.zeros((height, width, 3), dtype=np.float32)
    wsum = np.zeros((height, width), dtype=np.float32)
    for image, heading, pitch, fov in frames:
        img = np.asarray(image.convert("RGB"), dtype=np.float32)
        ih, iw = img.shape[:2]
        fwd, right, up = _basis(float(heading), float(pitch))
        z = de * fwd[0] + dn * fwd[1] + du * fwd[2]
        front = z > 1e-3
        zs = np.where(front, z, 1.0)
        x = (de * right[0] + dn * right[1] + du * right[2]) / zs
        y = (de * up[0] + dn * up[1] + du * up[2]) / zs
        half = math.tan(math.radians(float(fov)) / 2.0)
        nx, ny = x / half, y / half          # -1 .. 1 inside the frame
        inside = front & (np.abs(nx) < 1.0) & (np.abs(ny) < 1.0)
        if not inside.any():
            continue
        edge = np.minimum(1.0 - np.abs(nx), 1.0 - np.abs(ny))
        w = np.clip(edge / FEATHER, 0.0, 1.0) * inside
        w *= (90.0 / max(float(fov), 1.0)) ** 3   # a zoomed frame has more pixels per degree: let it win
        px = (nx + 1.0) * 0.5 * (iw - 1)
        py = (1.0 - ny) * 0.5 * (ih - 1)
        ys, xs = np.nonzero(inside)
        colours = _sample_bilinear(img, px[ys, xs], py[ys, xs])
        acc[ys, xs] += colours * w[ys, xs, None]
        wsum[ys, xs] += w[ys, xs]
    covered = wsum > 1e-6
    out = np.zeros_like(acc)
    out[covered] = acc[covered] / wsum[covered][:, None]
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
                    ref: ReferenceLine, *, width: int = PANO_WIDTH) -> dict[str, Any]:
    """Stitch and store every Street View panorama of the street; return the ``hero-panoramas/1`` manifest."""
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
        sha = hashlib.sha256(("\n".join(sorted(f"{o.sha256}:{o.heading_deg}:{o.pitch_deg}:{o.fov_deg}" for o in obs))
                              + f"|{width}").encode()).hexdigest()[:8]
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in pano_id)
        dest = out_dir / f"{safe}_{sha}.jpg"
        if not dest.is_file():
            image = stitch_panorama(frames, width=width)
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            dest.write_bytes(buf.getvalue())
        for f in frames:
            f[0].close()
        panos.append({
            "pano_id": pano_id, "s": round(s, 3), "t": round(t, 3), "lat": first.lat, "lon": first.lon,
            "frames": len(frames), "capture_date": first.capture_date,
            "url": f"/media/panoramas/{slug}/{dest.name}", "width": width, "height": width // 2,
        })
    panos.sort(key=lambda p: p["s"])
    manifest = {
        "schema": PANO_SCHEMA, "slug": slug, "key": key, "length_s": length_s,
        "camera_height_m": SV_CAMERA_HEIGHT_M, "bearing0_deg": round(ref.bearing_deg(0.0), 3), "panoramas": panos,
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1))
    return manifest


def load_panoramas(slug: str, data_dir: Path) -> dict[str, Any] | None:
    """The stored manifest of ``slug`` if every image it lists still exists (else ``None``)."""
    path = panoramas_dir(data_dir, slug) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    for p in manifest.get("panoramas", []):
        if not (panoramas_dir(data_dir, slug) / Path(p["url"]).name).is_file():
            return None
    return manifest

