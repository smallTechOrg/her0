"""Frontage planning: evidence, gap decisions and the contiguous facade segments of each side.

The frontage of a side is a sequence of ``FrontageSegment`` that tiles ``[0, length]`` (the plan validator forbids
holes). It is built on a 2.5 m cell grid in three steps:

1. ``evidence`` - per cell, the nearest thing facing the street on that side: a building footprint (``osm`` /
   ``mixed`` -> source ``osm``, ``vision`` -> ``vision``) or a ``wall`` / ``gate`` entity (source ``entity``), found by
   sampling the cell 8 times; a cell has evidence when >= half of the samples hit something within ``RANGE_M`` (35 m).
2. ``decisions`` - per cell: ``evidence``, ``neighbours`` (nearest evidence cells within ``NEIGHBOUR_M`` (30 m) on one or
   both sides) or ``prior``. Both ``hero.model.gaps.analyze_gaps`` (which needs no stations) and ``plan_side`` use the
   same decisions, so the gap report and the plan agree.
3. ``plan_side`` - turns decisions into segments: evidence as it is; two-sided neighbours blend lat/height/colour
   linearly between the anchors; one-sided copy the anchor plus a seeded jitter (<= 0.4 m lateral, <= 1.2 m height);
   prior uses a neutral wall plane (modelled: footpath.hi + 1.5; opposite: outer opposite strip edge - 1.5, else
   carriageway.lo - 3.0), height 6-11 m and a 6-entry palette, chosen by ``sha1(slug|side|bin)``.
   Lateral values are clamped so the facade is never inside the carriageway or the modelled footpath
   (``[footpath.hi + 0.8, 30]`` / ``[-30, opposite strip edge - 1.0]``).

Interpretation notes
--------------------
* A wall/gate detection is a short, point-like extent, so it is widened to ``MIN_ENTITY_SPAN_M`` (and a gate to its
  ``width_t``) around its extent centre; removed / below-``MIN_OBJECT_CONFIDENCE`` entities are ignored.
* Heights/colours of interpolated segments are quantised (0.25 m, 8 per colour channel) so neighbouring cells merge.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from dataclasses import dataclass
from typing import Callable, Sequence

from hero.model.street_plan_types import FrontageSegment
from hero.model.types import CanonicalModel, Entity, EntityState, EntityType

CELL_M = 2.5
RANGE_M = 35.0
MAX_LAT_M = 30.0
NEIGHBOUR_M = 30.0
SAMPLES = 8
MIN_ENTITY_SPAN_M = 2.5
MIN_ENTITY_CONFIDENCE = 0.5  # weaker wall/gate detections stay objects but do not define the facade line
WALL_HALF_THICKNESS_M = 0.15
MODELLED_CLEAR_M = 0.8
OPPOSITE_CLEAR_M = 1.0
PRIOR_MODELLED_OFFSET_M = 1.5
PRIOR_OPPOSITE_OFFSET_M = 1.5
PRIOR_OPPOSITE_FALLBACK_M = 3.0
JITTER_LAT_M = 0.4
MAX_SLOPE = 0.25         # max |d lat / d s| (m per m) of any frontage segment
WALL_OFF_LINE_M = 1.0    # a wall / gate further than this from the local building line is merged into that line
OUTLIER_M = 1.5          # a building cell further than this from the median of its neighbours is snapped to it
WALL_WINDOW = 3
MEDIAN_HALF_WINDOW = 2   # cells each side (5-cell / 12.5 m median)
JITTER_H_M = 1.2
DEFAULT_COLOUR = "#b8b0a2"
WALL_COLOUR = "#8c8479"
GATE_COLOUR = "#6f6a63"
PALETTE = ("#b8b0a2", "#c9b79c", "#a9a9a0", "#b5a08a", "#9da3a8", "#c2b8a8")
PRIOR_H_RANGE = (6.0, 11.0)
CONF_EVIDENCE_FLOOR = 0.2
CONF_NEIGHBOURS = 0.35
CONF_PRIOR = 0.2

SIDES = ("modelled", "opposite")


@dataclass(frozen=True)
class EvCell:
    kind: str           # building | wall | gate
    source: str         # osm | vision | entity
    lat_from: float     # signed game lateral of the street-facing edge at the cell start / end
    lat_to: float
    height_m: float
    colour_hex: str
    storeys: int
    building_id: str | None
    entity_id: str | None
    confidence: float
    snapped: bool = False  # lateral replaced by the neighbourhood median (outlier) -> honest ``interpolated`` marking


@dataclass(frozen=True)
class SideEvidence:
    cells: tuple[EvCell | None, ...]
    cover_m: tuple[float, ...]   # evidence length inside each cell
    bounds: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Decision:
    kind: str                     # evidence | neighbours | prior
    left: int | None = None       # cell index of the left/right evidence anchor (neighbours)
    right: int | None = None


def cell_grid(length: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    a = 0.0
    while a < length - 1e-3:
        b = min(length, a + CELL_M)
        if length - b < 1e-3:
            b = length
        out.append((round(a, 6), round(b, 6)))
        a = b
    return out


def bin_of(s: float, bin_m: float = 5.0) -> int:
    return int((s + 1e-9) // bin_m)


# ------------------------------------------------------------------ evidence


def _poly_cut(poly: Sequence[tuple[float, float]], s: float) -> list[tuple[float, float]]:
    """Intervals of ``t`` of the polygon [s, t] cut by the line s = const (even-odd rule)."""
    ys: list[float] = []
    n = len(poly)
    for i in range(n):
        (s1, l1), (s2, l2) = poly[i], poly[(i + 1) % n]
        if s1 == s2:
            continue
        if (s1 <= s < s2) or (s2 <= s < s1):
            ys.append(l1 + (l2 - l1) * (s - s1) / (s2 - s1))
    ys.sort()
    return [(ys[i], ys[i + 1]) for i in range(0, len(ys) - 1, 2)]


def _entity_span(e: Entity) -> tuple[float, float]:
    length = max(e.extent.length_s, MIN_ENTITY_SPAN_M)
    if e.type is EntityType.gate:
        length = max(length, e.extent.width_t)
    centre = e.position.s + e.extent.length_s / 2.0
    return centre - length / 2.0, centre + length / 2.0


@dataclass(frozen=True)
class _Cand:
    key: str
    near: float            # |lat| of the street-facing edge
    kind: str
    source: str
    height_m: float
    colour_hex: str
    storeys: int
    building_id: str | None
    entity_id: str | None
    confidence: float


def _candidates(model: CanonicalModel, side: str) -> tuple[list, list]:
    mod = model.footpath_side
    want = mod if side == "modelled" else ("right" if mod == "left" else "left")
    buildings = [b for b in sorted(model.buildings, key=lambda x: x.id) if b.side == want]
    sign = 1.0 if mod == "left" else -1.0
    walls = []
    for e in sorted(model.entities, key=lambda x: x.id):
        if e.type not in (EntityType.wall, EntityType.gate):
            continue
        if e.state is EntityState.removed or e.confidence < MIN_ENTITY_CONFIDENCE:
            continue
        lat = sign * e.position.t
        if (lat > 0) != (side == "modelled"):
            continue
        walls.append(e)
    return buildings, walls


def _cands_at(s: float, buildings, walls, sign: float, side: str) -> list[_Cand]:
    out: list[_Cand] = []
    for b in buildings:
        poly = [(float(p[0]), float(p[1])) for p in b.footprint]
        near = None
        for lo, hi in _poly_cut(poly, s):
            l0, l1 = sorted((sign * lo, sign * hi))
            if side == "modelled":
                if l1 <= 0:
                    continue
                a = max(l0, 0.0)
            else:
                if l0 >= 0:
                    continue
                a = max(-l1, 0.0)
            if a > RANGE_M:
                continue
            near = a if near is None else min(near, a)
        if near is not None:
            out.append(_Cand(f"b:{b.id}", near, "building", "vision" if b.source == "vision" else "osm", b.height_m,
                             (b.colour_hex or DEFAULT_COLOUR).lower(),
                             b.storeys if b.storeys is not None else max(1, round(b.height_m / 3.0)), b.id, None,
                             float(b.confidence)))
    for e in walls:
        lo, hi = _entity_span(e)
        if lo <= s < hi:
            near = max(0.0, abs(sign * e.position.t) - WALL_HALF_THICKNESS_M)
            if near > RANGE_M:
                continue
            gate = e.type is EntityType.gate
            out.append(_Cand(f"e:{e.id}", near, "gate" if gate else "wall", "entity",
                             e.height_m if e.height_m and e.height_m > 0 else 2.0,
                             GATE_COLOUR if gate else WALL_COLOUR, 0, None, e.id, float(e.confidence)))
    return out


def evidence(model: CanonicalModel, side: str) -> SideEvidence:
    """Per-cell evidence of one side (``"modelled"`` / ``"opposite"``). Lateral values are signed game lateral."""
    sign = 1.0 if model.footpath_side == "left" else -1.0  # t -> lat
    out_side = 1.0 if side == "modelled" else -1.0
    buildings, walls = _candidates(model, side)
    length = model.reference_line.length_s
    cells: list[EvCell | None] = []
    cover: list[float] = []
    for a, b in cell_grid(length):
        samples = [a + (k + 0.5) * (b - a) / SAMPLES for k in range(SAMPLES)]
        per = [_cands_at(s, buildings, walls, sign, side) for s in samples]
        hits: dict[str, list[tuple[float, float]]] = {}
        meta: dict[str, _Cand] = {}
        for s, cs in zip(samples, per):
            for c in cs:
                hits.setdefault(c.key, []).append((s, c.near))
                meta.setdefault(c.key, c)
        # the street-facing candidate = the one that wins (smallest near edge) at most samples
        wins: dict[str, int] = {}
        for cs in per:
            if cs:
                best = min(cs, key=lambda c: (c.near, c.key))
                wins[best.key] = wins.get(best.key, 0) + 1
        covered = sum(1 for cs in per if cs)
        frac = covered / SAMPLES
        cover.append(round(frac * (b - a), 6))
        if frac < 0.5 or not wins:
            cells.append(None)
            continue
        key = min(wins, key=lambda k: (-wins[k], sum(n for _, n in hits[k]) / len(hits[k]), k))
        pts = hits[key]

        def near_at(s: float) -> float:
            sx, nx = min(pts, key=lambda p: abs(p[0] - s))
            return nx

        # linear fit of near edge over the hit samples, evaluated at the cell ends (exact for straight edges)
        if len(pts) >= 2 and pts[-1][0] > pts[0][0]:
            slope = (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0])
            n_a = pts[0][1] + slope * (a - pts[0][0])
            n_b = pts[0][1] + slope * (b - pts[0][0])
            n_a, n_b = max(0.0, n_a), max(0.0, n_b)
        else:
            n_a = n_b = near_at(a)
        m = meta[key]
        cells.append(EvCell(m.kind, m.source, out_side * n_a, out_side * n_b, m.height_m, m.colour_hex, m.storeys,
                            m.building_id, m.entity_id, m.confidence))
    return SideEvidence(tuple(cells), tuple(cover), tuple(cell_grid(length)))


def decisions(ev: SideEvidence) -> list[Decision]:
    n = len(ev.cells)
    mids = [(a + b) / 2.0 for a, b in ev.bounds]
    out: list[Decision] = []
    for i in range(n):
        if ev.cells[i] is not None:
            out.append(Decision("evidence"))
            continue
        left = next((j for j in range(i - 1, -1, -1) if ev.cells[j] is not None), None)
        right = next((j for j in range(i + 1, n) if ev.cells[j] is not None), None)
        if left is not None and mids[i] - mids[left] > NEIGHBOUR_M:
            left = None
        if right is not None and mids[right] - mids[i] > NEIGHBOUR_M:
            right = None
        out.append(Decision("neighbours", left, right) if (left is not None or right is not None) else Decision("prior"))
    return out


# ------------------------------------------------------------------ planning


def _hash_floats(slug: str, side: str, bin_i: int, n: int) -> list[float]:
    d = hashlib.sha1(f"{slug}|{side}|{bin_i}".encode()).digest()
    return [int.from_bytes(d[4 * i:4 * i + 4], "big") / 2 ** 32 for i in range(n)]


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hex(c: Sequence[float]) -> str:
    q = [min(255, max(0, int(round(v / 8.0)) * 8)) for v in c]
    return "#%02x%02x%02x" % tuple(min(255, v) for v in q)


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


@dataclass(frozen=True)
class Bounds:
    """Per-station lateral limits, supplied by the caller: ``modelled_floor(s)`` (>= footpath.hi + 0.8),
    ``opposite_ceiling(s)`` (<= opposite strip edge - 1.0, negative), and the prior wall planes."""

    modelled_floor: Callable[[float], float]
    opposite_ceiling: Callable[[float], float]
    modelled_prior: Callable[[float], float]
    opposite_prior: Callable[[float], float]


def _median(v: Sequence[float]) -> float:
    x = sorted(v)
    n = len(x)
    return x[n // 2] if n % 2 else (x[n // 2 - 1] + x[n // 2]) / 2.0


def _limit_slope(lat_a: float, lat_b: float, length: float) -> tuple[float, float]:
    """Keep ``|lat_b - lat_a| <= MAX_SLOPE * length``: a steeper edge is replaced by its flat mean."""
    if abs(lat_b - lat_a) <= MAX_SLOPE * length + 1e-9:
        return lat_a, lat_b
    m = (lat_a + lat_b) / 2.0
    return m, m


def smooth_evidence(ev: SideEvidence) -> SideEvidence:
    """Robust wall line: building cells whose street-facing edge jumps (footprint corners, far courtyards, sampling
    across two buildings) are snapped to the median of the evidence cells within +-``MEDIAN_HALF_WINDOW`` cells when they
    are more than ``OUTLIER_M`` away from it, and no cell edge is steeper than ``MAX_SLOPE`` (a steeper edge becomes flat
    at its mean). Walls/gates (confident entities) take part in the median; a wall/gate more than ``WALL_OFF_LINE_M``
    from the median building line of its neighbourhood is merged into that line (lateral = the line, height = the lowest
    neighbouring building, colour = the nearest building) so a 2 m gate never carves a wedge/sky notch into a 16 m
    facade. It keeps its kind / entity id (objects stay linked) and is marked ``snapped`` (honestly interpolated)."""
    n = len(ev.cells)
    mids = [None if c is None else (c.lat_from + c.lat_to) / 2.0 for c in ev.cells]
    out: list[EvCell | None] = []
    for i, c in enumerate(ev.cells):
        if c is None or c.kind != "building":
            out.append(c)
            continue
        near = [mids[j] for j in range(max(0, i - MEDIAN_HALF_WINDOW), min(n, i + MEDIAN_HALF_WINDOW + 1))
                if mids[j] is not None]
        med = _median(near)  # type: ignore[arg-type]
        a, b = c.lat_from, c.lat_to
        snapped = False
        if abs(mids[i] - med) > OUTLIER_M:  # type: ignore[operator]
            a = b = med
            snapped = True
        else:
            a, b = _limit_slope(a, b, ev.bounds[i][1] - ev.bounds[i][0])
        out.append(dataclasses.replace(c, lat_from=a, lat_to=b, snapped=snapped))
    first = out
    out = []
    for i, c in enumerate(first):
        if c is None or c.kind == "building":
            out.append(c)
            continue
        win = range(max(0, i - WALL_WINDOW), min(n, i + WALL_WINDOW + 1))
        bld = [first[j] for j in win if first[j] is not None and first[j].kind == "building"]  # type: ignore[union-attr]
        if not bld:
            out.append(c)
            continue
        med = _median([(b.lat_from + b.lat_to) / 2.0 for b in bld])
        nearest = min(bld, key=lambda b: (abs(first.index(b) - i), first.index(b)))
        low = min(b.height_m for b in bld)
        if abs((c.lat_from + c.lat_to) / 2.0 - med) <= WALL_OFF_LINE_M:
            # on the building line: stays where it is but fills the skyline (no sky notch above a 2 m gate)
            out.append(dataclasses.replace(c, height_m=max(c.height_m, low), colour_hex=nearest.colour_hex,
                                           snapped=True))
            continue
        out.append(dataclasses.replace(c, lat_from=med, lat_to=med, height_m=max(c.height_m, low),
                                       colour_hex=nearest.colour_hex, snapped=True))
    return SideEvidence(tuple(out), ev.cover_m, ev.bounds)


def _ramp(anchor: float, other: float, dist: float) -> float:
    """Value ``dist`` metres from ``anchor`` moving towards ``other`` at no more than ``MAX_SLOPE``."""
    step = MAX_SLOPE * dist
    return anchor + max(-step, min(step, other - anchor))


def plan_side(model: CanonicalModel, side: str, ev: SideEvidence, dec: Sequence[Decision], bounds: Bounds,
              *, slug: str, bin_m: float = 5.0) -> list[FrontageSegment]:
    modelled = side == "modelled"
    ev = smooth_evidence(ev)

    def clamp(lat: float, s: float) -> float:
        if modelled:
            return min(MAX_LAT_M, max(bounds.modelled_floor(s), lat))
        return max(-MAX_LAT_M, min(bounds.opposite_ceiling(s), lat))

    segs: list[FrontageSegment] = []
    for i, ((a, b), d) in enumerate(zip(ev.bounds, dec)):
        bi = bin_of(a, bin_m)
        if d.kind == "evidence":
            c = ev.cells[i]
            assert c is not None
            segs.append(FrontageSegment(
                s_from=a, s_to=b, lat_from=round(clamp(c.lat_from, a), 3), lat_to=round(clamp(c.lat_to, b), 3),
                height_m=round(c.height_m, 3), kind=c.kind, colour_hex=c.colour_hex, storeys=c.storeys,  # type: ignore[arg-type]
                source=c.source, building_id=c.building_id, entity_id=c.entity_id,  # type: ignore[arg-type]
                interpolated=c.snapped,
                confidence=round(min(CONF_NEIGHBOURS if c.snapped else 1.0, max(0.0, c.confidence)), 3)))
            continue
        if d.kind == "neighbours":
            L = ev.cells[d.left] if d.left is not None else None
            R = ev.cells[d.right] if d.right is not None else None
            if L is not None and R is not None:
                sl, sr = ev.bounds[d.left][1], ev.bounds[d.right][0]
                f_a, f_b = (a - sl) / (sr - sl), (b - sl) / (sr - sl)
                lat_a, lat_b = _lerp(L.lat_to, R.lat_from, f_a), _lerp(L.lat_to, R.lat_from, f_b)
                if abs(lat_b - lat_a) > MAX_SLOPE * (b - a) + 1e-9:  # anchors too far apart: ramp from the nearer one
                    mid = (a + b) / 2.0
                    lat_a = lat_b = (_ramp(L.lat_to, R.lat_from, mid - sl) if mid - sl <= sr - mid
                                     else _ramp(R.lat_from, L.lat_to, sr - mid))
                fm = (f_a + f_b) / 2.0
                h = _lerp(L.height_m, R.height_m, fm)
                rgb = [_lerp(x, y, fm) for x, y in zip(_rgb(L.colour_hex), _rgb(R.colour_hex))]
                storeys = round(_lerp(L.storeys, R.storeys, fm))
                walls = L.kind != "building" and R.kind != "building"
                kind = "wall" if walls else "building"
            else:
                A = L or R
                assert A is not None
                u = _hash_floats(slug, side, bi, 3)
                dl = (u[0] * 2 - 1) * JITTER_LAT_M
                dh = (u[1] * 2 - 1) * JITTER_H_M
                lat_a = lat_b = (A.lat_to if L is not None else A.lat_from) + dl
                h = A.height_m + dh
                rgb = list(_rgb(A.colour_hex))
                storeys = A.storeys
                kind = "wall" if A.kind != "building" else "building"
            h = max(0.5, round(h * 4) / 4)
            segs.append(FrontageSegment(
                s_from=a, s_to=b, lat_from=round(clamp(lat_a, a), 3), lat_to=round(clamp(lat_b, b), 3), height_m=h,
                kind=kind, colour_hex=_hex(rgb), storeys=max(0, storeys) if kind == "building" else 0,  # type: ignore[arg-type]
                source="interpolated", building_id=None, entity_id=None, interpolated=True, confidence=CONF_NEIGHBOURS))
            continue
        u = _hash_floats(slug, side, bi, 2)
        h = round(_lerp(*PRIOR_H_RANGE, u[0]) * 4) / 4
        colour = PALETTE[int(u[1] * len(PALETTE)) % len(PALETTE)]
        prior = bounds.modelled_prior if modelled else bounds.opposite_prior
        segs.append(FrontageSegment(
            s_from=a, s_to=b, lat_from=round(clamp(prior(a), a), 3), lat_to=round(clamp(prior(b), b), 3), height_m=h,
            kind="building", colour_hex=colour, storeys=max(1, round(h / 3.0)), source="prior", building_id=None,
            entity_id=None, interpolated=True, confidence=CONF_PRIOR))
    return merge_segments(_flatten_steep(segs))


def _flatten_steep(segs: Sequence[FrontageSegment]) -> list[FrontageSegment]:
    out = []
    for g in segs:
        a, b = _limit_slope(g.lat_from, g.lat_to, g.s_to - g.s_from)
        out.append(g if (a, b) == (g.lat_from, g.lat_to) else g.model_copy(update={"lat_from": round(a, 3), "lat_to": round(b, 3)}))
    return out


def merge_segments(segs: Sequence[FrontageSegment]) -> list[FrontageSegment]:
    out: list[FrontageSegment] = []
    for sg in segs:
        if out:
            p = out[-1]
            same = (p.kind, p.source, p.interpolated, p.building_id, p.entity_id, p.colour_hex, p.height_m, p.storeys) == \
                   (sg.kind, sg.source, sg.interpolated, sg.building_id, sg.entity_id, sg.colour_hex, sg.height_m, sg.storeys)
            if same:
                sp = (p.lat_to - p.lat_from) / (p.s_to - p.s_from)
                sc = (sg.lat_to - sg.lat_from) / (sg.s_to - sg.s_from)
                if abs(p.lat_to - sg.lat_from) < 1e-3 and abs(sp - sc) < 1e-3:
                    out[-1] = p.model_copy(update={"s_to": sg.s_to, "lat_to": sg.lat_to,
                                                   "confidence": min(p.confidence, sg.confidence)})
                    continue
        out.append(sg)
    return out


def rendered_label(ev: SideEvidence, dec: Sequence[Decision], bin_lo: float, bin_hi: float) -> tuple[str, str]:
    """(evidence label among ``osm|none``, rendered label ``osm|vision|interpolated|prior``) for one bin from the
    geometry evidence. ``osm`` evidence = footprint/wall covers >= min(2.5, bin length) of the bin."""
    idx = [i for i, (a, b) in enumerate(ev.bounds) if a >= bin_lo - 1e-9 and b <= bin_hi + 1e-9]
    covered = sum(ev.cover_m[i] for i in idx)
    has = covered >= min(2.5, bin_hi - bin_lo) - 1e-9
    if has:
        srcs = [ev.cells[i].source for i in idx if ev.cells[i] is not None]  # type: ignore[union-attr]
        osm = sum(1 for x in srcs if x == "osm")
        return "osm", "osm" if osm >= len(srcs) - osm else "vision"
    kinds = {dec[i].kind for i in idx}
    return "none", "prior" if "prior" in kinds else "interpolated"


def frontage_lat_ok(seg: FrontageSegment) -> bool:
    return math.isfinite(seg.lat_from) and math.isfinite(seg.lat_to)
