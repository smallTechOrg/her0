"""Server-side texture baking: the ground strip and the frontage ribbons (slice S1e, Phase 2.1 r2).

Everything here is a pure, deterministic function of (canonical model, optional street plan, stored observations and
their image files): no clock, no network, no unseeded randomness; JPEG parameters are fixed so the *bytes* are stable.

Frame (spec/portability.md s3, spec/data.md "Texture references" + r2 amendment)
-------------------------------------------------------------------------------
* **Ground strip** - one straightened, road-aligned image of the band ``t in [-14, +14]``, ``s in [0, L]`` built from
  the zoom-20 satellite crops (and, below them, the overview tile) stored as observations. **Row 0 = s = L, column 0 =
  t = +14** (image up = travel, image left = left of travel), never mirrored::

      u = (14 - t) / 28          v = (L - s) / L           (glTF UV origin = top-left)

  Every gap is painted *into the image* - nearest-bin interpolation (feathered, with pavement grain) or, where no
  imagery exists within 30 m, a procedural asphalt / paver / earth pattern drawn from the cross-section - so there is
  never a blank or flat texel. ``bin_source`` records, per 5 m bin, ``satellite_hires | satellite_overview |
  interpolated | procedural``.
* **Frontage ribbons** - one per side (``"modelled"`` = the footpath side, ``"opposite"``), ``u = s / L`` (column 0 =
  s = 0), ``v`` = 0 at 12 m above ground to 1 at the ground (row 0 = 12 m), i.e. ``v = 1 - h/12`` clamped. Street View
  / user-photo crops are rectified onto the wall plane (camera pose + FOV) and cross-faded between neighbouring
  stations by view-angle weight; columns without evidence are interpolated from the nearest covered columns (<= 30 m)
  or drawn as a neutral procedural facade. ``bin_source``: ``user_photo | street_view | interpolated | none``.

Public API (S6 mesh atlases and S1c recompile import these)
-----------------------------------------------------------
``bake_textures(model, plan_or_stations, observations, data_dir, *, out_dir=None) -> TextureRefs``
    Bake everything, write the content-addressed JPEGs to ``<data_dir>/textures/<slug>/`` and return the refs
    (``ground``, ``frontage["modelled"|"opposite"]``, ``tints``, ``procedural_seed``). Re-running with unchanged input
    returns identical refs and rewrites nothing. ``plan_or_stations`` may be ``None``, a ``StreetPlan`` (or its JSON
    dict) or a list of ``PlanStation`` - it only refines cross-section bands and wall distances.
``bake_ground(...) -> GroundBake`` / ``bake_frontage(...) -> FrontageBake``
    The image-level halves (``.image`` is a PIL RGB image, ``.bin_source`` the per-bin report) - no files written.
``encode_jpeg(img) -> bytes``, ``content_sha8(data) -> str``, ``texture_name(...)``, ``store_texture(...)``
    Deterministic encoding (q 82, baseline, 4:2:0, no EXIF/ICC) and content-addressed storage.
``stack_ribbons(left, right) -> Image``, ``ground_uv(s, t, L)``, ``facade_uv(s, h, L, side)``,
``load_texture(data_dir, ref_or_url) -> Image``
    Pure helpers for the mesh builder's atlases and UVs. ``stack_ribbons`` puts ``Frontage_L`` on the top half and
    ``Frontage_R`` on the bottom half (portability s3).
``procedural_ground_image`` / ``procedural_facade_image`` / ``cross_section_samples``
    The building blocks (also unit-tested directly).
``hero.imagery.tints`` holds the colour helpers (state tints for vertex colours, entity median colours, facade palette).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from hero.imagery import tints
from hero.model.coverage import camera_station
from hero.model.refline import ReferenceLine
from hero.model.street_plan_types import PlanStation, StreetPlan, t_of_lat
from hero.model.texture_types import FrontageStripRef, GroundStripRef, TextureRefs
from hero.model.types import CanonicalModel, Observation, ObservationKind
from hero.scene.geo import TileGeoref, latlon_to_tile_px

# --------------------------------------------------------------------------- constants

T_LO = -14.0
T_HI = 14.0
BAND_M = T_HI - T_LO
BIN_M = 5.0
GROUND_MAX_PX = 1000          # rows of the ground strip (longest side <= 1024)
GROUND_MIN_MPP = 0.1          # finest pixel pitch (a 100 m street -> 280 x 1000 px)
FRONTAGE_MAX_W = 1024
FRONTAGE_H = 128              # rows per ribbon (12 m -> 9.4 cm/px); two ribbons stack to <= 256 rows
FRONTAGE_TOP_M = 12.0
JPEG_QUALITY = 82
FILL_SPAN_M = 35.0            # farthest a gap is painted from covered pixels
BIN_INTERP_M = 30.0           # a bin is "interpolated" when covered content lies within this many metres
COVERED_FRAC = 0.9            # central band (|t| <= 8) fraction that makes a bin "covered"
CENTRAL_T = 8.0
ROW_STRIP_PX = 4
SV_CAMERA_HEIGHT_M = 2.5
USER_CAMERA_HEIGHT_M = 1.5
FRONTAGE_REACH_M = 25.0
MIN_WALL_DIST_M = 0.5
MIN_ANGLE_SIN2 = 0.18         # ray must be >= ~25 deg off the travel axis to count
TEXTURE_URL_PREFIX = "/media/textures"
_DYNAMIC_TYPES = {"parked_vehicle", "vendor_stall", "debris", "pothole", "low_branch"}

# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class GroundBake:
    image: Image.Image
    bin_source: list[str]
    m_per_px: float
    s_from: float
    s_to: float


@dataclass(frozen=True)
class FrontageBake:
    side: str                       # "modelled" | "opposite"
    image: Image.Image
    bin_source: list[str]
    s_from: float
    s_to: float


@dataclass(frozen=True)
class Band:
    kind: str                       # footpath|carriageway|kerb|forecourt|verge|...
    t_lo: float
    t_hi: float
    state: str | None = None        # footpath state for footpath bands


# --------------------------------------------------------------------------- small utilities


def procedural_seed_for(slug: str) -> int:
    return int(hashlib.sha1(f"{slug}|texture-seed".encode("utf-8")).hexdigest()[:8], 16)


def content_sha8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def encode_jpeg(img: Image.Image) -> bytes:
    """Deterministic JPEG: q 82, baseline, 4:2:0, no EXIF / ICC / comment, no optimiser tables."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False, progressive=False, subsampling=2)
    return buf.getvalue()


def texture_name(kind: str, version: int, data: bytes) -> str:
    """``ground_v<N>_<sha8>.jpg`` / ``frontage_<side>_v<N>_<sha8>.jpg``."""
    return f"{kind}_v{version}_{content_sha8(data)}.jpg"


def store_texture(textures_root: Path, slug: str, kind: str, version: int, data: bytes) -> tuple[str, Path]:
    """Write ``data`` content-addressed (idempotent; atomic) and return ``(url, path)``."""
    name = texture_name(kind, version, data)
    folder = textures_root / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if not (path.exists() and path.stat().st_size == len(data)):
        tmp = folder / f".{name}.tmp{os.getpid()}"
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return f"{TEXTURE_URL_PREFIX}/{slug}/{name}", path


def load_texture(data_dir: Path, ref_or_url: Any) -> Image.Image:
    """Open a baked texture from a ``GroundStripRef`` / ``FrontageStripRef`` / ``/media/textures/...`` url."""
    url = ref_or_url if isinstance(ref_or_url, str) else ref_or_url.url
    rel = url[len("/media/"):] if url.startswith("/media/") else url.lstrip("/")
    with Image.open(Path(data_dir) / rel) as im:
        return im.convert("RGB")


def stack_ribbons(left: Image.Image, right: Image.Image) -> Image.Image:
    """Facade atlas: ``Frontage_L`` ribbon on the top half, ``Frontage_R`` on the bottom (spec/portability.md s3)."""
    w = max(left.width, right.width)
    left = left.convert("RGB").resize((w, left.height)) if left.width != w else left.convert("RGB")
    right = right.convert("RGB").resize((w, right.height)) if right.width != w else right.convert("RGB")
    atlas = Image.new("RGB", (w, left.height + right.height))
    atlas.paste(left, (0, 0))
    atlas.paste(right, (0, left.height))
    return atlas


def ground_uv(s: float, t: float, length: float) -> tuple[float, float]:
    """UV of ground point ``(s, t)`` in the ground strip: ``u = (14 - t)/28``, ``v = (L - s)/L``."""
    return (T_HI - t) / BAND_M, (length - s) / length


def facade_uv(s: float, h: float, length: float, side: str) -> tuple[float, float]:
    """UV in the stacked facade atlas: ``u = s/L``; ``v = v0 + (1 - h/12) * 0.5`` (``v0`` 0 left, 0.5 right)."""
    v0 = 0.0 if side == "left" else 0.5
    hh = max(0.0, min(FRONTAGE_TOP_M, h))
    return s / length, v0 + (1.0 - hh / FRONTAGE_TOP_M) * 0.5


def _wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def _bins(length: float) -> list[tuple[float, float]]:
    n = max(1, math.ceil(length / BIN_M - 1e-9))
    return [(i * BIN_M, min(length, (i + 1) * BIN_M)) for i in range(n)]


# --------------------------------------------------------------------------- inputs: plan / cross-section


def _stations_of(plan_or_stations: Any) -> tuple[list[PlanStation] | None, StreetPlan | None]:
    x = plan_or_stations
    if x is None:
        return None, None
    if isinstance(x, StreetPlan):
        return list(x.stations), x
    if isinstance(x, dict):
        if "stations" in x and "frontage" in x:
            plan = StreetPlan.model_validate(x)
            return list(plan.stations), plan
        raise ValueError("a plan dict must be a street-plan/1 document")
    if hasattr(x, "stations"):
        return list(x.stations), x if isinstance(x, StreetPlan) else None
    seq = list(x)
    out = [s if isinstance(s, PlanStation) else PlanStation.model_validate(s) for s in seq]
    return out, None


def _bands_from_station(st: PlanStation, side: str) -> list[Band]:
    bands: list[Band] = []
    if st.footpath is not None:
        a, b = sorted((t_of_lat(st.footpath.lo, side), t_of_lat(st.footpath.hi, side)))
        bands.append(Band("footpath", a, b, st.footpath.state.value))
    for strip in st.strips:
        a, b = sorted((t_of_lat(strip.lo, side), t_of_lat(strip.hi, side)))
        kind = strip.kind.value
        bands.append(Band(kind, a, b, "intact" if kind == "footpath" else None))
    kt = t_of_lat(st.kerb.lat, side)
    bands.append(Band("kerb", kt - max(st.kerb.width_m, 0.1) / 2, kt + max(st.kerb.width_m, 0.1) / 2))
    return bands


def _bands_from_model(model: CanonicalModel, s: float) -> list[Band]:
    iv = model.profile[-1]
    for cand in model.profile:
        if s < cand.s_to:
            iv = cand
            break
    f = 0.0 if iv.length <= 0 else min(1.0, max(0.0, (s - iv.s_from) / iv.length))
    bands: list[Band] = []
    for st in iv.strips:
        kind = st.kind.value
        state = None
        if kind == "footpath":
            if st.side == model.footpath_side:
                state = iv.footpath_state.value
            else:
                state = "broken" if st.condition.value in ("broken", "poor") else "intact"
        bands.append(Band(kind, st.t_lo_at(f), st.t_hi_at(f), state))
    if not any(b.kind == "carriageway" for b in bands):
        bands.append(Band("carriageway", -3.5, 3.5))
    return bands


def cross_section_samples(model: CanonicalModel, plan_or_stations: Any = None, step: float = 2.5
                          ) -> list[tuple[float, list[Band]]]:
    """Per-station ``Band`` lists (``t`` left-positive) on the 2.5 m grid, from the plan when given else the model."""
    length = model.reference_line.length_s
    stations, _plan = _stations_of(plan_or_stations)
    out: list[tuple[float, list[Band]]] = []
    if stations:
        for st in stations:
            out.append((st.s, _bands_from_station(st, model.footpath_side)))
    else:
        n = max(1, math.ceil(length / step - 1e-9))
        for s in [i * step for i in range(n)] + [length]:
            out.append((s, _bands_from_model(model, min(s, length - 1e-6))))
    return out


# --------------------------------------------------------------------------- noise / procedural patterns


def _noise(w: int, h: int, seed: int, cell: int) -> Image.Image:
    rng = random.Random(seed)
    cw, ch = max(2, w // cell + 2), max(2, h // cell + 2)
    tile = Image.frombytes("L", (cw, ch), rng.randbytes(cw * ch))
    return tile.resize((w, h), Image.BICUBIC)


def _grain(w: int, h: int, seed: int, lo: int = 196, hi: int = 255, coarse: int = 40) -> Image.Image:
    """Multi-octave grain in ``[lo, hi]`` (multiply it onto a base colour image)."""
    a = _noise(w, h, seed * 3 + 1, coarse)
    b = _noise(w, h, seed * 3 + 2, 9)
    c = _noise(w, h, seed * 3 + 3, 2)
    g = Image.blend(Image.blend(a, b, 0.45), c, 0.4)
    g = ImageOps.autocontrast(g, cutoff=1)
    return g.point([lo + (hi - lo) * i // 255 for i in range(256)])


def _gray_rgb(img: Image.Image) -> Image.Image:
    return Image.merge("RGB", (img, img, img))


def _bright(img: Image.Image, gain: float) -> Image.Image:
    return img.point([min(255, int(i * gain)) for i in range(256)] * 3)


def _band_polys(samples: list[tuple[float, list[Band]]], kinds: set[str], length: float, mpp_s: float, mpp_t: float):
    """Yield ``(band_a, polygon_px)`` for each consecutive-sample span of every band whose kind is in ``kinds``."""
    def px(s: float, t: float) -> tuple[float, float]:
        return (T_HI - t) / mpp_t, (length - s) / mpp_s

    def keyed(bands: list[Band]) -> dict[tuple[str, int], Band]:
        seen: dict[str, int] = {}
        out: dict[tuple[str, int], Band] = {}
        for b in bands:
            i = seen.get(b.kind, 0)
            seen[b.kind] = i + 1
            out[(b.kind, i)] = b
        return out

    for (sa, ba), (sb, bb) in zip(samples, samples[1:]):
        ka, kb = keyed(ba), keyed(bb)
        for key, a in ka.items():
            if a.kind not in kinds:
                continue
            b = kb.get(key, a)
            poly = [px(sa, a.t_hi), px(sb, b.t_hi), px(sb, b.t_lo), px(sa, a.t_lo)]
            yield a, poly


def _class_mask(size: tuple[int, int], samples, kinds: set[str], length: float, mpp_s: float, mpp_t: float,
                *, skip_missing: bool = True) -> Image.Image:
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    for band, poly in _band_polys(samples, kinds, length, mpp_s, mpp_t):
        if skip_missing and band.state == "missing":
            continue
        d.polygon(poly, fill=255)
    return m


_OTHER_KINDS = {"forecourt", "verge", "planting", "shoulder", "parking", "cycle_lane", "median", "unknown"}


def procedural_ground_image(size: tuple[int, int], length: float, samples: list[tuple[float, list[Band]]],
                            seed: int) -> Image.Image:
    """Procedural pavement for the whole strip: earth ground, asphalt carriageway, paver footpaths with joints, kerb."""
    w, h = size
    mpp_s, mpp_t = length / h, BAND_M / w
    img = Image.new("RGB", size, tints.PROCEDURAL_TONE["ground"])
    d = ImageDraw.Draw(img)
    tone = tints.PROCEDURAL_TONE
    rng = random.Random(seed ^ 0x5EED)
    for band, poly in _band_polys(samples, _OTHER_KINDS, length, mpp_s, mpp_t):
        d.polygon(poly, fill=tone["verge"] if band.kind in ("verge", "planting") else tone["forecourt"])
    for _band, poly in _band_polys(samples, {"carriageway"}, length, mpp_s, mpp_t):
        d.polygon(poly, fill=tone["road"])
    for band, poly in _band_polys(samples, {"footpath"}, length, mpp_s, mpp_t):
        key = {"missing": "footpath_missing", "broken": "footpath_broken"}.get(band.state or "", "footpath")
        d.polygon(poly, fill=tone[key])
    for _band, poly in _band_polys(samples, {"kerb"}, length, mpp_s, mpp_t):
        d.polygon(poly, fill=tone["kerb"])
    # paver joints (every 0.4 m across the footpath) and cracks on broken paving
    foot = [(s, [b for b in bs if b.kind == "footpath" and b.state != "missing"]) for s, bs in samples]
    joint = tints.shade(tone["footpath"], 0.88)
    s = 0.0
    while s <= length + 1e-9:
        idx = min(range(len(foot)), key=lambda i: abs(foot[i][0] - s))
        for b in foot[idx][1]:
            y = (length - s) / mpp_s
            d.line([((T_HI - b.t_hi) / mpp_t, y), ((T_HI - b.t_lo) / mpp_t, y)], fill=joint, width=1)
            if b.state == "broken":
                for _ in range(2):
                    cx = (T_HI - rng.uniform(b.t_lo, b.t_hi)) / mpp_t
                    d.line([(cx, y), (cx + rng.uniform(-8, 8), y + rng.uniform(-6, 6))],
                           fill=tints.shade(tone["footpath_broken"], 0.6), width=1)
        s += 0.4
    base = _bright(img, 1.12)
    return ImageChops.multiply(base, _gray_rgb(_grain(w, h, seed)))


def procedural_facade_image(size: tuple[int, int], length: float, slug: str, side: str, seed: int) -> Image.Image:
    """Neutral facade: palette colour per 5 m bin, storey bands, window grid, darker shopfront band at street level."""
    w, h = size
    mpp = length / w
    img = Image.new("RGB", size, (0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, (a, b) in enumerate(_bins(length)):
        d.rectangle([int(a / mpp), 0, int(math.ceil(b / mpp)), h], fill=tints.hex_to_rgb(tints.neutral_facade_colour(slug, side, i)))
    ppm_v = h / FRONTAGE_TOP_M
    rng = random.Random(seed ^ 0xFACADE)
    win_dark = (58, 66, 76)
    shop_h = int(3.0 * ppm_v)
    # shopfront band (0-3 m): shutters / glazing darker bands
    x = 0.0
    while x < length:
        ww = rng.uniform(2.5, 4.5)
        shade_k = rng.uniform(0.55, 0.8)
        box = [int(x / mpp), h - shop_h, int(min(length, x + ww - 0.2) / mpp), h - 1]
        region = img.crop((box[0], box[1], max(box[0] + 1, box[2]), box[3] + 1))
        img.paste(_bright(region, shade_k), (box[0], box[1]))
        x += ww
    # upper storeys: 3 m spacing, windows 1.3 x 1.4 m every 3 m with a per-column jitter
    storey = 3.0
    top_storeys = int((FRONTAGE_TOP_M - 3.0) // storey)
    for k in range(top_storeys):
        y_top = h - int((3.0 + (k + 1) * storey - 0.7) * ppm_v)
        y_bot = y_top + int(1.4 * ppm_v)
        xs = 1.0 + rng.uniform(0, 0.6)
        while xs < length - 1.0:
            x0, x1 = int(xs / mpp), int((xs + 1.3) / mpp)
            d.rectangle([x0, y_top, max(x0 + 1, x1), y_bot], fill=win_dark)
            xs += 3.0
    return ImageChops.multiply(_bright(img, 1.1), _gray_rgb(_grain(w, h, seed + 7, lo=205, coarse=60)))


# --------------------------------------------------------------------------- ground bake


def _obs_path(obs: Observation, data_dir: Path) -> Path | None:
    base = Path(data_dir)
    name = Path(obs.file_path).name
    for cand in (base / obs.file_path, base / "imagery" / name, base / "images" / name, base / name):
        if cand.is_file():
            return cand
    return None


class _ImageCache:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._c: dict[str, Image.Image | None] = {}

    def get(self, obs: Observation) -> Image.Image | None:
        if obs.id not in self._c:
            p = _obs_path(obs, self.data_dir)
            im = None
            if p is not None:
                with Image.open(p) as f:
                    im = f.convert("RGB")
            self._c[obs.id] = im
        return self._c[obs.id]


def _tile_georef(obs: Observation) -> TileGeoref | None:
    g = obs.georef or {}
    zoom = obs.zoom if obs.zoom is not None else g.get("zoom")
    scale = obs.scale if obs.scale is not None else g.get("scale", 1)
    if zoom is None:
        return None
    return TileGeoref(float(g.get("center_lat", obs.lat)), float(g.get("center_lon", obs.lon)), int(zoom), int(scale or 1),
                      (int(obs.width_px), int(obs.height_px)))


def _edge_ramp(w: int, h: int, radius: int) -> Image.Image:
    rx = bytes(min(255, int(255 * min(x, w - 1 - x) / radius)) for x in range(w))
    ry = bytes(min(255, int(255 * min(y, h - 1 - y) / radius)) for y in range(h))
    ix = Image.frombytes("L", (w, 1), rx).resize((w, h), Image.NEAREST)
    iy = Image.frombytes("L", (1, h), ry).resize((w, h), Image.NEAREST)
    return ImageChops.multiply(ix, iy)


def _render_ground_layer(src: Image.Image, geo: TileGeoref, ref: ReferenceLine, size: tuple[int, int], length: float
                         ) -> tuple[Image.Image, Image.Image]:
    """Straighten ``src`` into the strip; returns ``(rgb, weight)`` (weight 0 outside the tile, ramped at its edge)."""
    w, h = size
    mpp_s = length / h
    kx, ky = src.width / geo.size_px[0], src.height / geo.size_px[1]

    def src_px(s: float, t: float) -> tuple[float, float]:
        lat, lon = ref.point_at(s, t)
        px, py = latlon_to_tile_px(lat, lon, geo)
        return px * kx, py * ky

    mesh = []
    for y0 in range(0, h, ROW_STRIP_PX):
        y1 = min(y0 + ROW_STRIP_PX, h)
        s0, s1 = length - y0 * mpp_s, length - y1 * mpp_s
        nw, sw, se, ne = src_px(s0, T_HI), src_px(s1, T_HI), src_px(s1, T_LO), src_px(s0, T_LO)
        mesh.append(((0, y0, w, y1), (*nw, *sw, *se, *ne)))
    rgb = src.transform(size, Image.MESH, mesh, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
    ramp = _edge_ramp(src.width, src.height, max(8, int(0.04 * min(src.size))))
    wt = ramp.transform(size, Image.MESH, mesh, resample=Image.BILINEAR, fillcolor=0)
    return rgb, wt


_MIN_W = 40  # ramp weights below this count as "outside the tile" (hides the black bleed of the edge pixels)


def _band_frac(mask: Image.Image, y0: int, y1: int, x0: int, x1: int) -> float:
    if y1 <= y0 or x1 <= x0:
        return 0.0
    return ImageStat.Stat(mask.crop((x0, y0, x1, y1))).mean[0] / 255.0


def _grade(acc: Image.Image, samples, length: float, mpp_s: float, mpp_t: float) -> Image.Image:
    """Make satellite pixels read as asphalt / paving: gain + blend toward a class tone inside the class polygons."""
    for kind, band_kind in (("road", "carriageway"), ("footpath", "footpath")):
        g = tints.SURFACE_GRADE[kind]
        mask = _class_mask(acc.size, samples, {band_kind}, length, mpp_s, mpp_t).filter(ImageFilter.GaussianBlur(2))
        gain = float(g["gain"])  # type: ignore[arg-type]
        graded = Image.blend(acc.point([int(i * gain) for i in range(256)] * 3),
                             Image.new("RGB", acc.size, g["target"]), float(g["blend"]))  # type: ignore[arg-type]
        acc = Image.composite(graded, acc, mask)
    return acc


def _coarse_fill(acc: Image.Image, cov: Image.Image, cell: int, mpp_s: float) -> tuple[Image.Image, Image.Image]:
    """Fill uncovered cells of a coarse grid: along ``s`` between the nearest known cells, else along ``t``.

    Returns ``(fill_rgb_full_size, filled_mask_full_size)``; cells with nothing within ``FILL_SPAN_M`` stay unfilled.
    """
    w, h = acc.size
    cw, ch = -(-w // cell), -(-h // cell)
    ab = acc.resize((cw, ch), Image.BOX).tobytes()
    a_c = [(ab[i], ab[i + 1], ab[i + 2]) for i in range(0, len(ab), 3)]
    m_c = list(cov.resize((cw, ch), Image.BOX).tobytes())
    colour: list[tuple[float, float, float] | None] = [None] * (cw * ch)
    for i, (px, m) in enumerate(zip(a_c, m_c)):
        if m >= 64:
            k = 255.0 / m
            colour[i] = (min(255.0, px[0] * k), min(255.0, px[1] * k), min(255.0, px[2] * k))
    span = max(1, int(FILL_SPAN_M / (mpp_s * cell)))
    filled = list(colour)
    for c in range(cw):
        col = [colour[r * cw + c] for r in range(ch)]
        up: list[int | None] = [None] * ch
        last = None
        for r in range(ch):
            if col[r] is not None:
                last = r
            up[r] = last
        down: list[int | None] = [None] * ch
        last = None
        for r in range(ch - 1, -1, -1):
            if col[r] is not None:
                last = r
            down[r] = last
        for r in range(ch):
            if col[r] is not None:
                continue
            u, dn = up[r], down[r]
            if u is not None and r - u > span:
                u = None
            if dn is not None and dn - r > span:
                dn = None
            if u is not None and dn is not None:
                k = (r - u) / (dn - u)
                k = k * k * (3 - 2 * k)  # smoothstep
                cu, cd = col[u], col[dn]
                filled[r * cw + c] = tuple(cu[j] + (cd[j] - cu[j]) * k for j in range(3))  # type: ignore[index]
            elif u is not None or dn is not None:
                filled[r * cw + c] = col[u if u is not None else dn]  # type: ignore[index]
    second = list(filled)
    for r in range(ch):
        row = filled[r * cw:(r + 1) * cw]
        known = [c for c in range(cw) if row[c] is not None]
        if not known:
            continue
        for c in range(cw):
            if row[c] is None:
                j = min(known, key=lambda k: abs(k - c))
                if abs(j - c) * cell * mpp_s <= FILL_SPAN_M:
                    second[r * cw + c] = row[j]
    rgb = Image.new("RGB", (cw, ch))
    rgb.putdata([tuple(int(round(v)) for v in c) if c is not None else (0, 0, 0) for c in second])
    msk = Image.new("L", (cw, ch))
    msk.putdata([255 if c is not None else 0 for c in second])
    # bleed unfilled cells' colour from neighbours before upsampling so seams do not darken
    rgb_full = rgb.resize((w, h), Image.BICUBIC)
    msk_full = msk.resize((w, h), Image.BILINEAR).point([0 if i < 250 else 255 for i in range(256)])
    return rgb_full, msk_full.filter(ImageFilter.GaussianBlur(1))


def bake_ground(model: CanonicalModel, plan_or_stations: Any, observations: Sequence[Observation], data_dir: Path,
                *, seed: int | None = None) -> GroundBake:
    """Bake the straightened ground strip image + per-5 m ``bin_source`` (no files written)."""
    length = float(model.reference_line.length_s)
    ref = ReferenceLine.from_model(model.reference_line)
    seed = procedural_seed_for(model.segment_slug) if seed is None else seed
    h = min(GROUND_MAX_PX, max(16, math.ceil(length / GROUND_MIN_MPP - 1e-9)))
    mpp_s = length / h
    w = max(16, int(round(BAND_M / mpp_s)))
    mpp_t = BAND_M / w
    size = (w, h)
    samples = cross_section_samples(model, plan_or_stations)
    cache = _ImageCache(data_dir)

    hires, overview = [], []
    for o in observations:
        if o.kind is ObservationKind.satellite_crop and (o.zoom or 0) >= 20:
            hires.append(o)
        elif o.kind in (ObservationKind.satellite_crop, ObservationKind.satellite_overview):
            overview.append(o)
    hires.sort(key=lambda o: (o.station_s if o.station_s is not None else 0.0, o.id))
    overview.sort(key=lambda o: o.id)

    acc = Image.new("RGB", size, (0, 0, 0))
    cov = Image.new("L", size, 0)
    cov_h = Image.new("L", size, 0)
    cov_o = Image.new("L", size, 0)
    for group, union in ((overview, cov_o), (hires, cov_h)):
        for o in group:
            geo, src = _tile_georef(o), cache.get(o)
            if geo is None or src is None:
                continue
            rgb, wt = _render_ground_layer(src, geo, ref, size, length)
            covered = wt.point([255 if i >= _MIN_W else 0 for i in range(256)])
            wmasked = wt.point([i if i >= _MIN_W else 0 for i in range(256)])
            free = ImageChops.multiply(covered, ImageChops.invert(cov))
            alpha = ImageChops.lighter(wmasked, free)
            acc = Image.composite(rgb, acc, alpha)
            cov = ImageChops.lighter(cov, covered)
            union.paste(ImageChops.lighter(union, covered))
    acc = _grade(acc, samples, length, mpp_s, mpp_t)

    # ---- per-bin source classification
    x0, x1 = int(round((T_HI - CENTRAL_T) / mpp_t)), int(round((T_HI + CENTRAL_T) / mpp_t))
    cov_any = ImageChops.lighter(cov_h, cov_o)
    rows: list[tuple[int, int]] = []
    kind_of: list[str | None] = []
    for a, b in _bins(length):
        y0, y1 = max(0, int(round((length - b) / mpp_s))), min(h, int(round((length - a) / mpp_s)))
        rows.append((y0, y1))
        fh, fo, fa = (_band_frac(m, y0, y1, x0, x1) for m in (cov_h, cov_o, cov_any))
        if fh >= COVERED_FRAC:
            kind_of.append("satellite_hires")
        elif fa >= COVERED_FRAC:
            kind_of.append("satellite_hires" if fh >= 0.5 else "satellite_overview")
        else:
            kind_of.append(None)
    covered_idx = [i for i, k in enumerate(kind_of) if k]
    bin_source: list[str] = []
    for i, k in enumerate(kind_of):
        if k:
            bin_source.append(k)
        elif any(abs(i - j) * BIN_M <= BIN_INTERP_M for j in covered_idx):
            bin_source.append("interpolated")
        else:
            bin_source.append("procedural")

    # ---- painting gaps into the image
    proc_img = procedural_ground_image(size, length, samples, seed)
    proc_mask = Image.new("L", size, 0)
    pd = ImageDraw.Draw(proc_mask)
    cd = ImageDraw.Draw(cov)
    for (y0, y1), src_kind in zip(rows, bin_source):
        if src_kind == "procedural":
            pd.rectangle([0, y0, w, max(y0, y1 - 1)], fill=255)
            cd.rectangle([0, y0, w, max(y0, y1 - 1)], fill=0)
    cell = max(2, int(round(0.5 / mpp_s)))
    fill_rgb, fill_mask = _coarse_fill(acc, cov, cell, mpp_s)
    grained = ImageChops.multiply(_bright(fill_rgb, 1.08), _gray_rgb(_grain(w, h, seed + 11, lo=205, coarse=24)))
    gap = Image.composite(grained, proc_img, fill_mask)
    gap = Image.composite(proc_img, gap, proc_mask)
    cov_soft = cov.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.5))
    out = Image.composite(acc, gap, cov_soft)
    return GroundBake(out, bin_source, mpp_s, 0.0, length)


# --------------------------------------------------------------------------- frontage bake


@dataclass
class _Cam:
    obs: Observation
    s_c: float
    t_c: float
    view_rel: float      # camera heading relative to the line bearing at s_c, degrees clockwise
    f: float             # focal length in px
    cx: float
    cy: float
    hc: float
    pitch: float
    half_fov: float
    kind: str            # "user_photo" | "street_view"
    weight_gain: float


def _wall_t(model: CanonicalModel, plan: StreetPlan | None, side_geom: str, which: str):
    """Return ``f(s) -> signed t of the wall plane`` for ``side_geom`` ('left'|'right')."""
    sign = 1.0 if side_geom == "left" else -1.0
    segs: list[tuple[float, float, float, float]] = []
    if plan is not None:
        for side in plan.frontage.sides:
            if side.side == which:
                for g in side.segments:
                    segs.append((g.s_from, g.s_to, t_of_lat(g.lat_from, plan.footpath_side),
                                 t_of_lat(g.lat_to, plan.footpath_side)))
    blds: list[tuple[float, float, float]] = []
    if not segs:
        for b in model.buildings:
            if b.side != side_geom:
                continue
            ss = [p[0] for p in b.footprint]
            ts = [abs(p[1]) for p in b.footprint]
            blds.append((min(ss), max(ss), min(ts)))
    edge = 6.5
    for iv in model.profile:
        for st in iv.strips:
            if st.kind.value == "footpath" and st.side == side_geom:
                edge = max(abs(st.t_lo[0]), abs(st.t_hi[0]))
    default = sign * (edge + 1.5 if which == "modelled" else 8.5)

    def f(s: float) -> float:
        for a, b, ta, tb in segs:
            if a - 1e-6 <= s <= b + 1e-6:
                k = 0.0 if b <= a else (s - a) / (b - a)
                return ta + (tb - ta) * k
        best = None
        for a, b, tmin in blds:
            if a <= s <= b and (best is None or tmin < best):
                best = tmin
        return sign * best if best is not None else default

    return f


def _build_cams(observations: Sequence[Observation], ref: ReferenceLine, cache: _ImageCache) -> list[_Cam]:
    cams: list[_Cam] = []
    for o in sorted(observations, key=lambda o: (camera_station(o, ref), o.id)):
        if o.kind not in (ObservationKind.streetview, ObservationKind.user_photo) or o.heading_deg is None:
            continue
        img = cache.get(o)
        if img is None:
            continue
        s_c = camera_station(o, ref)
        t_c = o.offset_t if o.offset_t is not None else ref.to_st(o.lat, o.lon).t
        fov = o.fov_deg or 90.0
        half = fov / 2.0
        f = (img.width / 2.0) / math.tan(math.radians(half))
        user = o.kind is ObservationKind.user_photo
        cams.append(_Cam(
            obs=o, s_c=s_c, t_c=t_c, view_rel=_wrap180(o.heading_deg - ref.bearing_deg(min(max(s_c, 0.0), ref.length_s))),
            f=f, cx=img.width / 2.0, cy=img.height / 2.0, hc=USER_CAMERA_HEIGHT_M if user else SV_CAMERA_HEIGHT_M,
            pitch=math.radians(o.pitch_deg or 0.0), half_fov=half, kind="user_photo" if user else "street_view",
            weight_gain=1.5 if user else 1.0))
    return cams


def _cam_project(cam: _Cam, sign: float, s: float, t_w: float, h: float) -> tuple[float, float, float] | None:
    """Project wall point ``(s, t_w, h)`` into the camera: ``(x_px, y_px, theta_deg)`` or ``None`` (wall behind camera)."""
    dt = sign * (t_w - cam.t_c)
    if dt < MIN_WALL_DIST_M:
        return None
    ds = s - cam.s_c
    a = -sign * math.degrees(math.atan2(dt, ds))  # azimuth clockwise from travel: left wall < 0, right wall > 0
    theta = max(-80.0, min(80.0, _wrap180(a - cam.view_rel)))
    r = math.hypot(ds, dt)
    depth = max(r * math.cos(math.radians(theta)), 1e-3)
    x = cam.cx + cam.f * math.tan(math.radians(theta))
    elev = math.atan2(h - cam.hc, depth) - cam.pitch
    y = cam.cy - cam.f * math.tan(max(-1.3, min(1.3, elev)))
    return x, y, _wrap180(a - cam.view_rel)


def bake_frontage(model: CanonicalModel, plan_or_stations: Any, observations: Sequence[Observation], data_dir: Path,
                  side: str, *, seed: int | None = None, _cache: _ImageCache | None = None) -> FrontageBake:
    """Bake one facade ribbon (``side`` = ``"modelled"`` or ``"opposite"``); no files written."""
    if side not in ("modelled", "opposite"):
        raise ValueError("side must be 'modelled' or 'opposite'")
    length = float(model.reference_line.length_s)
    ref = ReferenceLine.from_model(model.reference_line)
    seed = procedural_seed_for(model.segment_slug) if seed is None else seed
    side_geom = model.footpath_side if side == "modelled" else ("right" if model.footpath_side == "left" else "left")
    sign = 1.0 if side_geom == "left" else -1.0
    _stations, plan = _stations_of(plan_or_stations)
    wall = _wall_t(model, plan, side_geom, side)
    cache = _cache or _ImageCache(data_dir)
    W = min(FRONTAGE_MAX_W, max(32, math.ceil(length / 0.1 - 1e-9)))
    H = FRONTAGE_H
    mpp = length / W
    cams = _build_cams(observations, ref, cache)

    CELL_X, CELL_Y = 8, 16
    acc_w = [0.0] * (W * H)
    acc_c = [[0.0] * (W * H) for _ in range(3)]
    col_w_kind: list[list[float]] = [[0.0, 0.0] for _ in range(W)]  # per column: [street_view, user_photo] weight
    for cam in cams:
        # column weights
        wvec = [0.0] * W
        for x in range(W):
            s = (x + 0.5) * mpp
            if abs(s - cam.s_c) > FRONTAGE_REACH_M:
                continue
            pr = _cam_project(cam, sign, s, wall(s), cam.hc)
            if pr is None:
                continue
            ds, dt = s - cam.s_c, sign * (wall(s) - cam.t_c)
            sin2 = (dt * dt) / (ds * ds + dt * dt)
            if sin2 < MIN_ANGLE_SIN2:
                continue
            feather = max(0.0, min(1.0, (cam.half_fov - abs(pr[2])) / 10.0))
            wvec[x] = sin2 * feather * cam.weight_gain
        nz = [x for x in range(W) if wvec[x] > 0]
        if not nz:
            continue
        xa, xb = nz[0], nz[-1] + 1
        img = cache.get(cam.obs)
        assert img is not None
        mesh = []
        for gx in range(xa - xa % CELL_X, xb, CELL_X):
            gx1 = min(gx + CELL_X, W)
            for gy in range(0, H, CELL_Y):
                gy1 = min(gy + CELL_Y, H)
                quad = []
                for (px, py) in ((gx, gy), (gx, gy1), (gx1, gy1), (gx1, gy)):
                    s = px * mpp
                    hh = FRONTAGE_TOP_M * (1.0 - py / H)
                    pr = _cam_project(cam, sign, s, wall(s), hh)
                    if pr is None:
                        quad = []
                        break
                    quad += [pr[0], pr[1]]
                if quad:
                    mesh.append(((gx, gy, gx1, gy1), tuple(quad)))
        if not mesh:
            continue
        rgb = img.transform((W, H), Image.MESH, mesh, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
        valid = Image.new("L", img.size, 255).transform((W, H), Image.MESH, mesh, resample=Image.BILINEAR, fillcolor=0)
        cb, vb = rgb.tobytes(), valid.tobytes()
        ki = 1 if cam.kind == "user_photo" else 0
        for x in range(xa, xb):
            wv = wvec[x]
            if wv <= 0:
                continue
            col_w_kind[x][ki] += wv
            for y in range(H):
                p = y * W + x
                if vb[p] < 200:
                    continue
                acc_w[p] += wv
                i3 = p * 3
                acc_c[0][p] += wv * cb[i3]
                acc_c[1][p] += wv * cb[i3 + 1]
                acc_c[2][p] += wv * cb[i3 + 2]

    # ---- per-column state: covered / interpolated / procedural
    y_lo, y_hi = int(H * (1 - 6.0 / FRONTAGE_TOP_M)), int(H * (1 - 0.5 / FRONTAGE_TOP_M))
    covered = []
    for x in range(W):
        n = sum(1 for y in range(y_lo, y_hi) if acc_w[y * W + x] >= 0.05)
        covered.append(n >= 0.5 * (y_hi - y_lo))
    cov_cols = [x for x in range(W) if covered[x]]
    reach = int(BIN_INTERP_M / mpp)

    def colour_at(x: int, y: int) -> tuple[float, float, float]:
        p = y * W + x
        return acc_c[0][p] / acc_w[p], acc_c[1][p] / acc_w[p], acc_c[2][p] / acc_w[p]

    # real columns, vertically completed (extensions fade toward a horizontally smoothed edge colour: no streaks)
    firsts: dict[int, int] = {}
    lasts: dict[int, int] = {}
    top_c: dict[int, tuple[float, float, float]] = {}
    for x in cov_cols:
        known = [y for y in range(H) if acc_w[y * W + x] >= 0.05]
        firsts[x], lasts[x] = known[0], known[-1]
        n = min(len(known), 10)
        top_c[x] = tuple(sum(colour_at(x, y)[j] for y in known[:n]) / n for j in range(3))  # type: ignore[assignment]
    smooth_top: dict[int, tuple[float, float, float]] = {}
    half_win = max(1, int(3.0 / mpp))
    for x in cov_cols:
        near = [top_c[k] for k in cov_cols if abs(k - x) <= half_win]
        smooth_top[x] = tuple(sum(c[j] for c in near) / len(near) for j in range(3))  # type: ignore[assignment]
    real: dict[int, list[tuple[float, float, float]]] = {}
    col_mean: dict[int, tuple[float, float, float]] = {}
    for x in cov_cols:
        first, last = firsts[x], lasts[x]
        known = [y for y in range(first, last + 1) if acc_w[y * W + x] >= 0.05]
        col: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * H
        mean = tuple(sum(colour_at(x, y)[j] for y in known) / len(known) for j in range(3))
        for y in range(H):
            if acc_w[y * W + x] >= 0.05:
                col[y] = colour_at(x, y)
        col_mean[x] = mean  # type: ignore[assignment]
        c_first, c_last = colour_at(x, first), colour_at(x, last)
        tgt = smooth_top[x]
        for y in range(0, first):
            k = min(1.0, (first - y) / (H * 0.1)) * 0.9
            col[y] = tuple(c_first[j] + (tgt[j] - c_first[j]) * k for j in range(3))  # type: ignore[assignment]
        for y in range(last + 1, H):
            k = min(1.0, (y - last) / (H * 0.15)) * 0.6
            col[y] = tuple(c_last[j] + (mean[j] - c_last[j]) * k for j in range(3))  # type: ignore[assignment]
        yi = first
        while yi < last:
            if acc_w[yi * W + x] >= 0.05:
                yi += 1
                continue
            y0 = yi - 1
            y1 = yi
            while acc_w[y1 * W + x] < 0.05:
                y1 += 1
            for y in range(y0 + 1, y1):
                k = (y - y0) / (y1 - y0)
                col[y] = tuple(col[y0][j] + (col[y1][j] - col[y0][j]) * k for j in range(3))  # type: ignore[assignment]
            yi = y1
        real[x] = col

    proc = procedural_facade_image((W, H), length, model.segment_slug, side_geom, seed)
    pb = proc.tobytes()
    # window pattern strength for painted columns: multiply a luminance ratio of the procedural facade
    pl = proc.convert("L")
    pmean = max(1.0, ImageStat.Stat(pl).mean[0])
    plb = pl.tobytes()
    state = ["proc"] * W
    out = bytearray(W * H * 3)
    nearest_left = [-1] * W
    nearest_right = [-1] * W
    last = -1
    for x in range(W):
        if covered[x]:
            last = x
        nearest_left[x] = last
    last = -1
    for x in range(W - 1, -1, -1):
        if covered[x]:
            last = x
        nearest_right[x] = last
    for x in range(W):
        if covered[x]:
            state[x] = "cov"
            col = real[x]
            for y in range(H):
                c = col[y]
                i3 = (y * W + x) * 3
                out[i3], out[i3 + 1], out[i3 + 2] = (min(255, int(c[0] + 0.5)), min(255, int(c[1] + 0.5)), min(255, int(c[2] + 0.5)))
            continue
        a, b = nearest_left[x], nearest_right[x]
        da = x - a if a >= 0 else 10 ** 9
        db = b - x if b >= 0 else 10 ** 9
        if min(da, db) <= reach:
            state[x] = "interp"
            strength = min(1.0, min(da, db) * mpp / 1.5)
            for y in range(H):
                if a >= 0 and b >= 0 and da <= reach * 2 and db <= reach * 2:
                    k = da / (da + db)
                    k = k * k * (3 - 2 * k)
                    ca, cb_ = real[a][y], real[b][y]
                    c = tuple(ca[j] + (cb_[j] - ca[j]) * k for j in range(3))
                    ma, mb = col_mean[a], col_mean[b]
                    flat = tuple(ma[j] + (mb[j] - ma[j]) * k for j in range(3))
                else:
                    src_x = a if da <= db else b
                    c, flat = real[src_x][y], col_mean[src_x]
                c = tuple(0.4 * c[j] + 0.6 * flat[j] for j in range(3))  # flatten rows: avoids streaky smears
                ratio = 1.0 + strength * (min(1.25, max(0.7, plb[y * W + x] / pmean)) - 1.0)
                i3 = (y * W + x) * 3
                out[i3], out[i3 + 1], out[i3 + 2] = (min(255, int(c[0] * ratio + 0.5)), min(255, int(c[1] * ratio + 0.5)), min(255, int(c[2] * ratio + 0.5)))
            continue
        for y in range(H):
            i3 = (y * W + x) * 3
            out[i3:i3 + 3] = pb[i3:i3 + 3]
    img_out = Image.frombytes("RGB", (W, H), bytes(out))

    bin_source: list[str] = []
    for a, b in _bins(length):
        xs = range(int(a / mpp), max(int(a / mpp) + 1, min(W, int(math.ceil(b / mpp)))))
        n = len(xs)
        n_cov = sum(1 for x in xs if state[x] == "cov")
        n_int = sum(1 for x in xs if state[x] == "interp")
        if n_cov * 2 >= n:
            user = sum(col_w_kind[x][1] for x in xs)
            street = sum(col_w_kind[x][0] for x in xs)
            bin_source.append("user_photo" if user > 0 and user >= street else "street_view")
        elif n_int * 2 >= n or n_int > n - n_int - n_cov:
            bin_source.append("interpolated")
        else:
            bin_source.append("none")
    return FrontageBake(side, img_out, bin_source, 0.0, length)


# --------------------------------------------------------------------------- tints per entity


def _entity_tints(model: CanonicalModel, observations: Sequence[Observation], cache: _ImageCache) -> dict[str, str]:
    by_id = {o.id: o for o in observations}
    out: dict[str, str] = {}
    for e in sorted(model.entities, key=lambda e: e.id):
        if e.state.value == "removed" or e.type.value in _DYNAMIC_TYPES:
            continue
        best = None
        for ev in e.supports():
            if ev.crop_px is None or ev.observation_id not in by_id:
                continue
            if best is None or ev.weight > best.weight:
                best = ev
        if best is None:
            continue
        img = cache.get(by_id[best.observation_id])
        if img is None:
            continue
        try:
            out[e.id] = tints.entity_tint_colour(img, best.crop_px, e.type.value)
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------- main entry


def bake_textures(model: CanonicalModel, plan_or_stations: Any, observations: Sequence[Observation], data_dir: Path,
                  *, out_dir: Path | None = None) -> TextureRefs:
    """Bake ground + both frontage ribbons + entity tints; store JPEGs under ``<data_dir>/textures/<slug>/``.

    Deterministic and idempotent: identical input yields identical bytes, file names (``sha8`` of the bytes) and refs.
    """
    data_dir = Path(data_dir)
    root = Path(out_dir) if out_dir is not None else data_dir / "textures"
    slug, version = model.segment_slug, model.version
    seed = procedural_seed_for(slug)
    cache = _ImageCache(data_dir)
    g = bake_ground(model, plan_or_stations, observations, data_dir, seed=seed)
    gbytes = encode_jpeg(g.image)
    gurl, _ = store_texture(root, slug, "ground", version, gbytes)
    ground = GroundStripRef(url=gurl, size_px=g.image.size, s_from=g.s_from, s_to=g.s_to, m_per_px=round(g.m_per_px, 6),
                            bin_source=g.bin_source)  # type: ignore[arg-type]
    frontage: dict[str, FrontageStripRef] = {}
    for side in ("modelled", "opposite"):
        fb = bake_frontage(model, plan_or_stations, observations, data_dir, side, seed=seed, _cache=cache)
        fbytes = encode_jpeg(fb.image)
        furl, _ = store_texture(root, slug, f"frontage_{side}", version, fbytes)
        frontage[side] = FrontageStripRef(side=side, url=furl, size_px=fb.image.size, s_from=fb.s_from, s_to=fb.s_to,
                                          bin_source=fb.bin_source)  # type: ignore[arg-type]
    return TextureRefs(ground=ground, frontage=frontage, tints=_entity_tints(model, observations, cache),
                       procedural_seed=seed)
