"""Detect Google's "we don't have imagery at this zoom" placeholder (a flat light-grey tile with a line of text)."""

from __future__ import annotations

from PIL import Image

SAMPLE_PX = 64
UNIFORM_TOL = 10        # per-channel distance from the median colour that still counts as "the flat colour"
MIN_FLAT_FRACTION = 0.85
MIN_LUMA = 170          # light
MAX_CHANNEL_SPREAD = 24  # grey, not a tinted sea / roof


def looks_like_no_imagery(img: Image.Image) -> bool:
    small = img.convert("RGB").resize((SAMPLE_PX, SAMPLE_PX), Image.BOX)
    raw = small.tobytes()
    pixels = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    n = len(pixels)
    med = tuple(sorted(p[c] for p in pixels)[n // 2] for c in range(3))
    luma = 0.299 * med[0] + 0.587 * med[1] + 0.114 * med[2]
    if luma < MIN_LUMA or max(med) - min(med) > MAX_CHANNEL_SPREAD:
        return False
    flat = sum(1 for p in pixels if all(abs(p[c] - med[c]) <= UNIFORM_TOL for c in range(3)))
    return flat / n >= MIN_FLAT_FRACTION
