"""Cheap automated judgement of the capture PNGs (pure Pillow; no numpy).

    uv run python -m tests.e2e.visual.judge DIR [--prefix after]

Checks per screenshot (390x844 game frames; the HUD occupies the top ~110 px):

* ``sky``      - the band just above the horizon (rows 14..30 % of the height, centre columns) is not black and not flat
                 grey: mean luminance >= 90 and mean blue >= mean red (a lit blue/haze sky).
* ``ground``   - the bottom 30 % of the frame has luminance std >= 6 (a textured surface, not a flat slab) and is not black.
* ``frontage`` - for every column in the left/right 28 % bands that has a skyline (first non-sky pixel below the sky
                 band), the fraction of pixels between the skyline and the horizon that are black / saturated sky blue ("holes")
                 stays <= 6 % averaged over the columns (continuous frontage, no black gaps or see-through wedges), and
                 the skyline never jumps by more than 120 px between neighbouring columns more than 4 times per band.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from PIL import Image

HORIZON_FRAC = 0.40          # the horizon sits at ~40 % of the frame height in the chase camera
SKY_ROWS = (0.14, 0.30)
HUD_ROWS = 0.13
EDGE_BAND = 0.28
MAX_HOLE_FRAC = 0.06
MAX_SKYLINE_JUMPS = 4
JUMP_PX = 120


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def is_sky(p) -> bool:
    """Sky / haze pixel: bright and blue-ish (zenith blue through horizon haze)."""
    return _lum(p) >= 120 and p[2] >= p[0] + 6 and p[2] >= p[1]


def is_black(p) -> bool:
    return _lum(p) < 10


def sky_stats(img: Image.Image) -> dict:
    w, h = img.size
    px = img.convert("RGB").load()
    xs = range(int(w * 0.35), int(w * 0.65), 3)
    ys = range(int(h * SKY_ROWS[0]), int(h * SKY_ROWS[1]), 3)
    vals = [px[x, y] for x in xs for y in ys]
    n = len(vals)
    return {"lum": sum(_lum(p) for p in vals) / n, "r": sum(p[0] for p in vals) / n, "b": sum(p[2] for p in vals) / n}


def ground_stats(img: Image.Image) -> dict:
    w, h = img.size
    px = img.convert("RGB").load()
    lum = [_lum(px[x, y]) for x in range(0, w, 3) for y in range(int(h * 0.7), h, 3)]
    return {"lum_mean": statistics.fmean(lum), "lum_std": statistics.pstdev(lum)}


def frontage_stats(img: Image.Image) -> dict:
    w, h = img.size
    px = img.convert("RGB").load()
    top, horizon = int(h * HUD_ROWS) + 4, int(h * HORIZON_FRAC)
    out = {}
    for name, cols in (("left", range(0, int(w * EDGE_BAND))), ("right", range(int(w * (1 - EDGE_BAND)), w))):
        skyline: dict[int, int] = {}
        holes, total = 0, 0
        for x in cols:
            y0 = next((y for y in range(top, horizon) if not is_sky(px[x, y])), None)
            if y0 is None or y0 > horizon - 30:
                continue                                   # no building here: open sky down to the horizon
            skyline[x] = y0
            for y in range(y0 + 2, horizon):
                total += 1
                p = px[x, y]
                if is_black(p) or (is_sky(p) and p[2] - p[0] >= 40):   # black gap / saturated blue sky seen through the wall (haze is not)
                    holes += 1
        ys = [skyline[x] for x in sorted(skyline)]
        jumps = sum(1 for a, b in zip(ys, ys[1:]) if abs(a - b) > JUMP_PX)
        out[name] = {"columns": len(skyline), "hole_frac": (holes / total) if total else 0.0, "skyline_jumps": jumps}
    return out


def analyse(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    return {"file": path.name, "sky": sky_stats(img), "ground": ground_stats(img), "frontage": frontage_stats(img)}


def verdict(stats: dict) -> list[str]:
    """Human-readable failures (empty = pass)."""
    bad = []
    s, g = stats["sky"], stats["ground"]
    if s["lum"] < 90 or s["b"] < s["r"]:
        bad.append(f"sky band black or not blue-ish (lum {s['lum']:.0f}, r {s['r']:.0f}, b {s['b']:.0f})")
    if g["lum_std"] < 6 or g["lum_mean"] < 25:
        bad.append(f"ground flat or black (mean {g['lum_mean']:.0f}, std {g['lum_std']:.1f})")
    for side, f in stats["frontage"].items():
        if f["hole_frac"] > MAX_HOLE_FRAC:
            bad.append(f"{side} frontage holes {f['hole_frac']:.1%} of {f['columns']} columns")
        if f["skyline_jumps"] > MAX_SKYLINE_JUMPS:
            bad.append(f"{side} skyline jumps {f['skyline_jumps']}x")
    return bad


def judge_dir(directory: Path, prefix: str = "") -> dict:
    files = sorted(p for p in directory.glob("*.png") if p.name.startswith(prefix))
    return {p.name: {**analyse(p), "failures": verdict(analyse(p))} for p in files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--prefix", default="")
    a = ap.parse_args(argv)
    res = judge_dir(a.dir, a.prefix)
    print(json.dumps(res, indent=2))
    return 1 if any(r["failures"] for r in res.values()) or not res else 0


if __name__ == "__main__":
    sys.exit(main())
