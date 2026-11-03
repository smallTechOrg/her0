"""Colour / tint helpers shared by the texture bake and the mesh builder (slice S1e).

Pure functions: no I/O (except reading pixels of a ``PIL.Image`` handed in), no clock, no randomness.
Colours are ``(r, g, b)`` ints 0-255 or ``"#rrggbb"`` strings.

Public API
----------
``hex_to_rgb`` / ``rgb_to_hex`` / ``mix`` / ``shade``
    Small colour arithmetic.
``state_tint(state)`` -> ``(r, g, b)``
    Vertex-colour multiplier for the footpath **condition** (``intact`` / ``broken`` / ``missing`` / ...). The glb
    stores it in ``COLOR_0`` of the ground nodes, where it *multiplies* the baked texture (spec/portability.md s3).
``state_vertex_rgba(state, kind="footpath")`` -> ``(r, g, b, 255)``
    Same, folded with the pavement/asphalt darkening of the surface ``kind`` (``footpath|road|kerb|ground``).
``SURFACE_GRADE`` / ``surface_grade(kind)``
    The mild pavement / asphalt grade the bake applies to satellite pixels so they read as paving or tarmac.
``median_colour(img, box=None)`` -> ``"#rrggbb"``
    Per-channel median of an image (or a ``(x, y, w, h)`` crop of it) - the entity tint.
``entity_tint_colour(img, crop_px, entity_type)`` -> ``"#rrggbb"``
    Median of the central part of a detection crop, nudged by a per-type sanity palette.
``neutral_facade_colour(slug, side, bin_index)`` -> ``"#rrggbb"``
    Deterministic neutral facade colour (6-entry palette) used when no photo exists.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from PIL import Image

RGB = tuple[int, int, int]

# --------------------------------------------------------------------------- arithmetic


def hex_to_rgb(h: str) -> RGB:
    h = h.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected #rrggbb, got {h!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(c: Iterable[float]) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in c)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(a: RGB, b: RGB, k: float) -> RGB:
    """``a`` at ``k = 0`` to ``b`` at ``k = 1``."""
    k = max(0.0, min(1.0, k))
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))  # type: ignore[return-value]


def shade(c: RGB, gain: float) -> RGB:
    """Multiply by ``gain`` (clamped to 0-255)."""
    return tuple(max(0, min(255, int(round(v * gain)))) for v in c)  # type: ignore[return-value]


# --------------------------------------------------------------------------- condition tints

# Vertex-colour multipliers (they multiply the texture): intact is neutral, broken is warmer and darker (cracked,
# stained), missing is earthy (dust / bare ground), blocked and encroached are slightly cooler / greyer.
_STATE_TINT: dict[str, RGB] = {
    "intact": (255, 255, 255),
    "unknown": (238, 236, 232),
    "broken": (206, 186, 160),
    "missing": (150, 122, 92),
    "blocked": (222, 222, 226),
    "encroached": (226, 214, 196),
}
# Per-surface darkening applied together with the state tint (carriageway reads darker than paving).
_KIND_GAIN: dict[str, float] = {"footpath": 1.0, "road": 0.86, "kerb": 1.0, "ground": 0.94}


def state_tint(state: str) -> RGB:
    """Vertex-colour multiplier for a footpath state (``FootpathState`` value or enum); unknown names are neutral."""
    return _STATE_TINT.get(str(getattr(state, "value", state)), _STATE_TINT["unknown"])


def state_vertex_rgba(state: str, kind: str = "footpath") -> tuple[int, int, int, int]:
    """``COLOR_0`` for a ground vertex: state tint x surface darkening, alpha 255."""
    r, g, b = shade(state_tint(state), _KIND_GAIN.get(kind, 1.0))
    return r, g, b, 255


def condition_tint(condition: str) -> RGB:
    """Tint for a ``Condition`` (good/fair/poor/broken/unknown): poor and broken surfaces look worn."""
    return {
        "good": (255, 255, 255), "fair": (248, 246, 242), "poor": (222, 208, 190),
        "broken": _STATE_TINT["broken"], "unknown": _STATE_TINT["unknown"],
    }.get(str(getattr(condition, "value", condition)), _STATE_TINT["unknown"])


# --------------------------------------------------------------------------- surface grading used by the bake

# ``target`` is the asphalt / paving tone the satellite pixel is blended toward by ``blend`` after ``gain``.
SURFACE_GRADE: dict[str, dict[str, object]] = {
    "road": {"target": (74, 74, 78), "blend": 0.22, "gain": 0.88},
    "footpath": {"target": (168, 162, 152), "blend": 0.14, "gain": 0.97},
    "kerb": {"target": (196, 194, 190), "blend": 0.30, "gain": 1.0},
}

# Procedural palette (base tone of each cross-section class when no satellite pixel exists)
PROCEDURAL_TONE: dict[str, RGB] = {
    "road": (84, 84, 88),
    "footpath": (170, 164, 154),
    "footpath_broken": (152, 145, 134),
    "footpath_missing": (154, 140, 120),
    "kerb": (200, 198, 194),
    "ground": (138, 124, 100),
    "verge": (112, 118, 78),
    "forecourt": (176, 170, 160),
}


def surface_grade(kind: str) -> dict[str, object] | None:
    return SURFACE_GRADE.get(kind)


# --------------------------------------------------------------------------- crop colours


def _median_from_hist(hist: list[int]) -> int:
    total = sum(hist)
    if total == 0:
        return 128
    half, acc = total / 2.0, 0
    for i, n in enumerate(hist):
        acc += n
        if acc >= half:
            return i
    return 255


def median_colour(img: Image.Image, box: tuple[int, int, int, int] | None = None) -> str:
    """Per-channel median colour of ``img`` (or of ``box = (x, y, w, h)`` clipped to the image) as ``#rrggbb``."""
    im = img.convert("RGB")
    if box is not None:
        x, y, w, h = box
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(im.width, int(x + w)), min(im.height, int(y + h))
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise ValueError("crop box lies outside the image")
        im = im.crop((x0, y0, x1, y1))
    hist = im.histogram()
    return rgb_to_hex((_median_from_hist(hist[0:256]), _median_from_hist(hist[256:512]), _median_from_hist(hist[512:768])))


# Per-type tone limits (so a sky-blue or road-grey median cannot make a tree blue or a pole bright pink).
_TYPE_TONE: dict[str, tuple[RGB, float]] = {
    "tree": ((74, 104, 56), 0.55),   # (reference tone, how far the median is pulled toward it)
    "pole": ((140, 144, 148), 0.35),
    "bollard": ((200, 200, 196), 0.25),
    "bin": ((70, 100, 80), 0.25),
    "railing": ((90, 94, 98), 0.35),
}


def entity_tint_colour(img: Image.Image, crop_px: tuple[int, int, int, int], entity_type: str) -> str:
    """Median of the central 60 % of a detection crop, pulled toward the type's reference tone."""
    x, y, w, h = crop_px
    cx0, cy0 = x + int(w * 0.2), y + int(h * 0.2)
    cw, ch = max(1, int(w * 0.6)), max(1, int(h * 0.6))
    med = hex_to_rgb(median_colour(img, (cx0, cy0, cw, ch)))
    ref = _TYPE_TONE.get(str(getattr(entity_type, "value", entity_type)))
    if ref is not None:
        med = mix(med, ref[0], ref[1])
    return rgb_to_hex(med)


# --------------------------------------------------------------------------- neutral facade palette

FACADE_PALETTE: tuple[str, ...] = ("#c9b79c", "#b8b0a2", "#d2c6b0", "#a9a193", "#bfae94", "#b4b9ba")


def neutral_facade_colour(slug: str, side: str, bin_index: int) -> str:
    """Deterministic palette pick: ``sha1(slug|side|bin)`` (same recipe as the compile's prior facade)."""
    d = hashlib.sha1(f"{slug}|{side}|{bin_index}".encode("utf-8")).digest()
    return FACADE_PALETTE[d[0] % len(FACADE_PALETTE)]
