"""The two texture atlases of ``street.glb`` (spec/portability.md section 3 "UV rules").

* **Ground atlas** - one road-aligned strip of the band ``t in [-14, +14]``, ``s in [0, L]`` (row 0 = ``s = L``,
  column 0 = ``t = +14``). If the plan names a baked strip (``plan.ground_tex.strip``, produced by
  ``hero.imagery.bake`` under ``<data_dir>/textures/<slug>/``) it is loaded, normalised and re-encoded; otherwise (mode
  ``procedural``, file missing or unreadable) a procedural pavement/asphalt strip is painted from the plan's own
  cross-section. This is the seam to the bake step: nothing else in the mesh builder knows where pixels come from.
* **Facade atlas** - the left and right ribbons stacked (top half ``Frontage_L``, bottom half ``Frontage_R``);
  ``u = s / L``, row 0 of a half = 12 m. Baked ribbons (``plan.frontage.sides[*].texture``) are used when readable,
  otherwise a neutral procedural facade is painted per segment from the plan (colour, height, storeys).

Everything is deterministic: integer-hash noise only (no ``random``), JPEG q82 baseline 4:2:0 via Pillow.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from hero.export.frame import hex_rgb, shade
from hero.export.mesh_ground import BAND, StreetProfile
from hero.logging import get_logger
from hero.model.street_plan_types import StreetPlan

MAX_SIDE = 1024
GROUND_PX_PER_M = 4
GROUND_W = 112  # 28 m * 4 px/m
FACADE_HALF_H = 128
FACADE_PX_PER_M = 8


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=82, subsampling=2, optimize=False, progressive=False)
    return buf.getvalue()


def _noise(x: int, y: int, k: int = 0) -> int:
    h = (x * 73856093 ^ y * 19349663 ^ k * 83492791) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 0x5BD1E995) & 0xFFFFFFFF
    return (h >> 16) & 0xFF


def _clamp(c: float) -> int:
    return 0 if c < 0 else 255 if c > 255 else int(c)


def load_baked(url: str | None, slug: str, data_dir: Path) -> Image.Image | None:
    """Load ``/media/textures/<slug>/<file>`` from ``<data_dir>/textures`` (None when absent/unreadable/outside)."""
    if not url:
        return None
    name = url.rsplit("/", 1)[-1]
    root = (Path(data_dir) / "textures" / slug).resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            im.load()
            return im.convert("RGB")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:  # corrupt -> procedural fallback
        get_logger("hero.export").warning("export.baked_image_unreadable", slug=slug, file=name, error=repr(exc))
        return None


# ---------------------------------------------------------------------- ground


def ground_atlas(plan: StreetPlan, prof: StreetProfile, data_dir: Path) -> bytes:
    strip = plan.ground_tex.strip
    if plan.ground_tex.mode == "baked_strip" and strip is not None:
        im = load_baked(strip.url, plan.slug, data_dir)
        if im is not None:
            scale = min(1.0, MAX_SIDE / max(im.size))
            if scale < 1.0:
                im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
            return jpeg_bytes(im)
    return jpeg_bytes(_procedural_ground(plan, prof))


def _procedural_ground(plan: StreetPlan, prof: StreetProfile) -> Image.Image:
    L = plan.length_m
    W = GROUND_W
    H = max(16, min(MAX_SIDE, round(L * GROUND_PX_PER_M)))
    seed = plan.ground_tex.procedural_seed & 0xFFFF
    sg = 1.0 if plan.footpath_side == "left" else -1.0
    mpp_x = 2 * BAND / W
    rows: list[tuple[int, int, int]] = []
    for y in range(H):
        s = L - (y + 0.5) * L / H
        sl = prof.slice_at(s)
        x = prof.xs(sl, min(max(s, sl.a), sl.b))
        dash_on = (s % 6.0) < 3.0
        centre = 0.5 * (x.car_lo + x.car_hi)
        for px in range(W):
            t = BAND - (px + 0.5) * mpp_x
            lat = sg * t
            nz = _noise(px, y, seed) % 22 - 11
            if x.car_lo <= lat <= x.car_hi:
                base = (112, 114, 120)
                if dash_on and abs(lat - centre) < 0.07:
                    base = (214, 212, 196)
                elif _noise(px >> 1, y >> 1, seed + 1) % 17 == 0:
                    base = (96, 98, 104)  # aggregate speckle
            elif x.fp_outer is not None and x.car_hi <= lat <= x.fp_inner and x.hm > 0:
                base = (198, 198, 194)  # kerb top
            elif x.fp_outer is not None and x.fp_inner <= lat <= x.fp_outer:
                tile_x, tile_y = int((lat - x.fp_inner) / 0.4), int(s / 0.4)
                joint = abs(((lat - x.fp_inner) / 0.4) % 1.0 - 0.0) < 0.06 or abs((s / 0.4) % 1.0) < 0.06
                tone = _noise(tile_x, tile_y, seed + 2) % 18 - 9
                base = (150 + tone, 142 + tone, 128 + tone)
                if joint:
                    base = (122, 116, 104)
                if x.state in ("broken", "blocked", "encroached") and _noise(px >> 2, y >> 2, seed + 3) % 5 == 0:
                    base = (104, 96, 84)
            elif x.opp_outer is not None and x.opp_outer <= lat <= x.car_lo:
                base = (172, 166, 154) if (px + y) % 5 else (150, 144, 134)
            elif lat > x.car_hi and x.fp_outer is None:
                base = (136, 118, 92)  # earth where the footpath is missing
            else:
                base = (146, 134, 114)  # forecourt / verge
            rows.append((_clamp(base[0] + nz), _clamp(base[1] + nz), _clamp(base[2] + nz)))
    img = Image.new("RGB", (W, H))
    img.putdata(rows)
    return img


# ---------------------------------------------------------------------- facade


def facade_atlas(plan: StreetPlan, prof: StreetProfile, data_dir: Path) -> bytes:
    L = plan.length_m
    W = max(128, min(MAX_SIDE, round(L * FACADE_PX_PER_M)))
    H = FACADE_HALF_H * 2
    atlas = Image.new("RGB", (W, H))
    for which, sd in (("modelled", plan.frontage.sides[0]), ("opposite", plan.frontage.sides[1])):
        half = 0 if prof.physical(which) == "left" else 1
        im = load_baked(sd.texture.url, plan.slug, data_dir) if sd.texture is not None else None
        if im is not None:
            part = im.resize((W, FACADE_HALF_H), Image.LANCZOS)
        else:
            part = _procedural_facade(plan, sd, W, half)
        atlas.paste(part, (0, half * FACADE_HALF_H))
    return jpeg_bytes(atlas)


def _procedural_facade(plan: StreetPlan, sd, W: int, half: int) -> Image.Image:
    L = plan.length_m
    HH = FACADE_HALF_H
    cols: list[list[tuple[int, int, int]]] = []
    for x in range(W):
        s = (x + 0.5) * L / W
        k, sg = next(((k, g) for k, g in enumerate(sd.segments) if g.s_from <= s <= g.s_to), (len(sd.segments) - 1, sd.segments[-1]))
        base = hex_rgb(sg.colour_hex)
        var = _noise(k, half, 7)
        base = shade(base, 0.92 + (var % 16) / 100.0)
        col: list[tuple[int, int, int]] = []
        floor_h = 3.2 + (var % 3) * 0.2
        win_pitch = 2.6 + (var % 4) * 0.3
        for yy in range(HH):
            h = (1.0 - (yy + 0.5) / HH) * 12.0
            c = base
            if h <= sg.height_m + 0.01:
                if sg.kind == "building":
                    fh = h % floor_h
                    sx = (s - sg.s_from) % win_pitch
                    if h < 2.9 and sg.height_m > 4.0:
                        c = shade(base, 0.8) if (s * 10) % 40 > 3 else shade(base, 0.55)  # ground-floor shutters
                    elif 1.0 < fh < 2.3 and 0.6 < sx < win_pitch - 0.7:
                        c = (62, 74, 92)
                    elif fh < 0.25:
                        c = shade(base, 0.85)  # floor slab line
                elif sg.kind == "gate":
                    c = shade(base, 0.7) if int(s * 8) % 2 else shade(base, 0.5)
                else:  # wall: brick courses
                    if int(h * 10) % 3 == 0:
                        c = shade(base, 0.86)
            else:
                c = base
            n = _noise(x, yy, half + 11) % 10 - 5
            col.append((_clamp(c[0] + n), _clamp(c[1] + n), _clamp(c[2] + n)))
        cols.append(col)
    img = Image.new("RGB", (W, HH))
    img.putdata([cols[x][y] for y in range(HH) for x in range(W)])
    return img
