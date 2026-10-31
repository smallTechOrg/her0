"""compute_walkability(scene): the deterministic score card (spec/data.md -> walkability).

    footpath_coverage_pct = 100 * intact_length / length_m          (1 dp)
    hazard_count          = |objects with action != none|
    crossing_present      = any object of type crossing
    score = clamp(round(0.6 * pct + (20 if crossing) + 20 * max(0, 1 - hazards / 10)), 0, 100)

``round`` is half-up (not banker's) so the card is reproducible across platforms.
"""

from __future__ import annotations

import math

from hero.scene.schema import CoverageState, ObjectType, Scene, Walkability

COVERAGE_WEIGHT = 0.6
CROSSING_BONUS = 20.0
HAZARD_WEIGHT = 20.0
HAZARD_SATURATION = 10


def _round_half_up(value: float, ndigits: int = 0) -> float:
    factor = 10 ** ndigits
    return math.floor(value * factor + 0.5) / factor


def compute_walkability(scene: Scene) -> Walkability:
    length = scene.segment.length_m
    intact = sum(r.length_m for r in scene.cross_section.footpath_coverage if r.state is CoverageState.intact)
    coverage_pct = _round_half_up(100.0 * intact / length, 1)
    coverage_pct = min(100.0, max(0.0, coverage_pct))

    hazard_count = len(scene.hazards())
    crossing_present = any(o.type is ObjectType.crossing for o in scene.objects)

    raw = (
        COVERAGE_WEIGHT * coverage_pct
        + (CROSSING_BONUS if crossing_present else 0.0)
        + HAZARD_WEIGHT * max(0.0, 1.0 - hazard_count / HAZARD_SATURATION)
    )
    score = int(min(100, max(0, _round_half_up(raw))))
    return Walkability(
        footpath_coverage_pct=coverage_pct,
        hazard_count=hazard_count,
        crossing_present=crossing_present,
        score=score,
    )
