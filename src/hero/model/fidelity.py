"""Fidelity levels per 5 m bin (spec/data.md -> "Fidelity").

``compute_fidelity`` is a pure function of the *set* of fused observations, so it is monotone: adding an observation
never lowers a bin's level (every rule below only ever gains support from more cells).

    L0  no street-level cell covers the bin
    L1  >= 1 street-level observation covers it
    L2  >= 2 distinct stations (>= 6 m apart) cover it AND a direct left cell AND a direct right cell cover it
    L3  L2 AND a user_photo closeup cell covers it

Interpretation notes
--------------------
* "Distinct stations" is the size of the greedy maximal subset of covering stations with pairwise gaps >= 6 m (greedy
  by ascending station is optimal for this 1-D packing, and it is monotone under added points).
* ``views`` = number of covering street-level observations; ``sides`` = number of covering observations with a direct
  left / right cell.
* Observation ids in ``model.fused_observation_ids`` that are absent from ``observations`` are ignored (reported
  nowhere: the caller owns the lookup); ids are visited in sorted order.
"""

from __future__ import annotations

import math
from typing import Mapping

from hero.model.coverage import STREET_KINDS, CoverageCell, observation_cells
from hero.model.refline import ReferenceLine
from hero.model.types import CanonicalModel, FidelityBin, FidelityMap, FidelityOverall, Observation

BIN_M = 5.0
FWD_RANGE_M = 15.0
SIDE_RANGE_M = 6.0
MIN_STATION_GAP_M = 6.0
MIN_OVERLAP_M = 2.5

LEVEL_LABELS = {
    0: "Map only: satellite and map data, no street photos",
    1: "Single viewpoint: every part seen from one place",
    2: "Multi-view: every part seen from at least two places, both sides",
    3: "Close-up: also includes close-up photos",
}


def _covers(cell: CoverageCell, s_from: float, s_to: float) -> bool:
    overlap = min(cell.s_to, s_to) - max(cell.s_from, s_from)
    need = min(MIN_OVERLAP_M, s_to - s_from)  # a bin shorter than 2.5 m must be covered whole
    return overlap >= need - 1e-9


def count_stations(stations: list[float]) -> int:
    """Size of the greedy subset of ``stations`` whose consecutive gaps are all >= ``MIN_STATION_GAP_M``."""
    n, last = 0, -math.inf
    for s in sorted(stations):
        if s - last >= MIN_STATION_GAP_M - 1e-9:
            n += 1
            last = s
    return n


def level_hint(level: int, *, stations: int, left: int, right: int) -> str:
    """The cheapest missing condition for the next level (deterministic)."""
    if level == 0:
        return "Add a Street View frame facing forward or back from within 15 m (gives the first street-level view, L1)."
    if level == 1:
        need = []
        if stations < 2:
            need.append("a second viewpoint at least 6 m away")
        if left < 1:
            need.append("a left-facing view")
        if right < 1:
            need.append("a right-facing view")
        return ("Add a Street View frame facing left or right from another station within 15 m (needs "
                + ", ".join(need) + " for L2).")
    if level == 2:
        return "Add a close-up photo of this stretch to reach L3."
    return ""


def compute_fidelity(model: CanonicalModel, observations: Mapping[str, Observation], *,
                     bin_m: float = BIN_M) -> FidelityMap:
    if bin_m <= 0:
        raise ValueError("bin_m must be positive")
    ref = ReferenceLine.from_model(model.reference_line)
    length = model.reference_line.length_s
    cells: list[CoverageCell] = []
    for oid in sorted(set(model.fused_observation_ids)):
        obs = observations.get(oid)
        if obs is not None:
            cells.extend(observation_cells(obs, ref))
    nbins = max(1, math.ceil(length / bin_m - 1e-9))
    bins: list[FidelityBin] = []
    hist = {"L0": 0.0, "L1": 0.0, "L2": 0.0, "L3": 0.0}
    weighted = 0.0
    for i in range(nbins):
        a, b = round(i * bin_m, 3), round(min(length, (i + 1) * bin_m), 3)
        street = [c for c in cells if c.kind in STREET_KINDS and _covers(c, a, b)]
        closeup = [c for c in cells if c.kind == "closeup" and _covers(c, a, b)]
        hires = any(c.kind == "overhead_hires" and c.s_from - 1e-9 <= (a + b) / 2.0 <= c.s_to + 1e-9 for c in cells)
        views = {c.observation_id for c in street}
        stations = count_stations([c.station_s for c in street])
        left = len({c.observation_id for c in street if c.kind == "left"})
        right = len({c.observation_id for c in street if c.kind == "right"})
        if not views:
            level = 0
        elif stations >= 2 and left >= 1 and right >= 1:
            level = 3 if closeup else 2
        else:
            level = 1
        bins.append(FidelityBin(index=i, s_from=a, s_to=b, level=level, views=len(views), stations=stations,
                                sides={"left": left, "right": right}, overhead_hires=hires,
                                hint=level_hint(level, stations=stations, left=left, right=right)))
        hist[f"L{level}"] += b - a
        weighted += level * (b - a)
    total = sum(hist.values()) or 1.0
    score = round(weighted / total, 2)
    overall = FidelityOverall(level=min(3, int(math.floor(score + 1e-9))), score=score,
                              histogram_m={k: round(v, 3) for k, v in hist.items()})
    return FidelityMap(bin_m=bin_m, length_s=length, bins=bins, overall=overall)
