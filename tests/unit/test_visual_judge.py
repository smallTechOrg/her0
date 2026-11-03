"""The cheap PNG judge (tests/e2e/visual/judge.py): synthetic frames prove each check fires, and the committed
BEFORE / AFTER evidence screenshots are judged (AFTER passes every check, BEFORE fails: the judge discriminates)."""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from tests.e2e.visual import judge

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "phase2_1"
W, H = 390, 844
SKY, WALL, GROUND = (150, 190, 235), (120, 110, 98), (110, 104, 96)


def frame(*, sky=SKY, wall_cols=(0, 100), black_gap=None, flat_ground=False) -> Image.Image:
    img = Image.new("RGB", (W, H), sky)
    d = ImageDraw.Draw(img)
    horizon = int(H * 0.4)
    rng = random.Random(1)
    for y in range(horizon, H, 4):                       # textured ground (noise) or a flat slab
        for x in range(0, W, 6):
            v = 0 if flat_ground else rng.randint(-25, 25)
            d.rectangle([x, y, x + 6, y + 4], fill=tuple(c + v for c in GROUND))
    a, b = wall_cols
    d.rectangle([a, int(H * 0.2), b, horizon], fill=WALL)   # a building facade up to the horizon
    if black_gap:
        d.rectangle([*black_gap], fill=(0, 0, 0))
    return img


def verdict_of(img: Image.Image, tmp_path: Path) -> list[str]:
    p = tmp_path / "f.png"
    img.save(p)
    return judge.analyse(p) and judge.verdict(judge.analyse(p))


def test_clean_frame_passes(tmp_path):
    assert verdict_of(frame(), tmp_path) == []


def test_black_sky_fails(tmp_path):
    assert any("sky" in f for f in verdict_of(frame(sky=(5, 5, 8)), tmp_path))


def test_flat_ground_fails(tmp_path):
    assert any("ground" in f for f in verdict_of(frame(flat_ground=True), tmp_path))


def test_black_gap_in_frontage_is_a_hole(tmp_path):
    f = verdict_of(frame(black_gap=(20, 220, 90, 330)), tmp_path)
    assert any("left frontage holes" in x for x in f), f


def test_see_through_blue_wedge_is_a_hole(tmp_path):
    img = frame()
    ImageDraw.Draw(img).rectangle([10, 230, 95, 330], fill=(93, 147, 207))   # zenith blue showing through the wall
    assert any("left frontage holes" in x for x in verdict_of(img, tmp_path))


def test_jagged_skyline_is_flagged(tmp_path):
    img = frame(wall_cols=(0, 0))
    d = ImageDraw.Draw(img)
    for x in range(0, 100, 10):                                   # alternating tall / short columns = wedges
        top = int(H * 0.15) if (x // 10) % 2 == 0 else int(H * 0.36)
        d.rectangle([x, top, x + 9, int(H * 0.4)], fill=WALL)
    f = judge.frontage_stats(img)["left"]
    assert f["skyline_jumps"] > judge.MAX_SKYLINE_JUMPS


def test_haze_coloured_distant_building_is_not_a_hole(tmp_path):
    img = frame(wall_cols=(0, 0))
    ImageDraw.Draw(img).rectangle([300, 230, 389, 335], fill=(209, 224, 237))   # fogged far building next to sky haze
    assert judge.frontage_stats(img)["right"]["hole_frac"] == 0.0


def test_committed_after_evidence_passes_every_check():
    res = judge.judge_dir(EVIDENCE / "after")
    assert len(res) == 8
    assert {n: r["failures"] for n, r in res.items() if r["failures"]} == {}


def test_committed_before_evidence_fails_the_judge():
    res = judge.judge_dir(EVIDENCE / "before")
    assert len(res) == 8
    failing = [n for n, r in res.items() if r["failures"]]
    assert len(failing) >= 6, failing
