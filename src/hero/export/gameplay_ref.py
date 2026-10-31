"""Python REFERENCE of the deterministic obstacle spawner (spec/portability.md section 7).

Resolved pseudocode: ``SPAWNER_ALGORITHM.md`` next to this file. The JS port (``frontend/js/game/spawner.js``) must
produce identical ``(loop, row, lane, type, x_mm, variant)`` lists. Only ``+ - * /``, comparisons, ``floor``, ``min``,
``max`` are used on floats; integers are reduced modulo 2**32 after every operation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from hero.export.gameplay import load_gameplay
from hero.export.gameplay_types import TYPE_ORDER, GameplayV1

MASK = 0xFFFFFFFF
__all__ = ["SpawnInput", "StaticBlocker", "Spawned", "fnv1a32", "Mulberry32", "generate_loop", "generate_loops",
           "spawn_input_from_manifest", "spawn_input_to_dict", "spawn_input_from_dict", "x_mm"]


def _imul(a: int, b: int) -> int:
    return (a * b) & MASK


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = (h ^ b) & MASK
        h = _imul(h, 0x01000193)
    return h


class Mulberry32:
    def __init__(self, seed: int) -> None:
        self.a = seed & MASK

    def next(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & MASK
        t = self.a
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul(t ^ (t >> 7), t | 61)) & MASK)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296.0


def x_mm(x: float) -> int:
    return math.floor(x * 1000.0 + 0.5)


@dataclass(frozen=True)
class StaticBlocker:
    s0: float
    s1: float
    lanes: tuple[int, ...]
    action: str


@dataclass(frozen=True)
class SpawnInput:
    slug: str
    seed: int
    length_m: float
    density: float
    type_weights: Mapping[str, float]
    footpath_missing: tuple[tuple[float, float], ...] = ()
    static: tuple[StaticBlocker, ...] = ()
    keepout: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Spawned:
    loop: int
    row: int
    lane: int
    type: str
    x_mm: int
    variant: int

    @property
    def id(self) -> str:
        return f"dyn-{self.loop}-{self.row}-{self.lane}"

    def as_list(self) -> list:
        return [self.loop, self.row, self.lane, self.type, self.x_mm, self.variant]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and a1 > b0


def _dist(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(a0 - b1, b0 - a1)


def _pick(gp: GameplayV1, lane: int, u: float, tw: Mapping[str, float]) -> str:
    table = gp.spawner.lane_weights[str(lane)]
    items = [t for t in TYPE_ORDER if t in table]
    ws = [table[t] * (1.0 + gp.spawner.type_boost * tw.get(t, 0.0)) for t in items]
    total = ws[0]
    for w in ws[1:]:
        total = total + w
    x = u * total
    for t, w in zip(items, ws):
        x = x - w
        if x < 0:
            return t
    return items[-1]


def _allowed(gp: GameplayV1, inp: SpawnInput, loop: int, typ: str, lane: int, x: float,
             row: list[tuple[int, str]], emitted: list[tuple[int, str, float]]) -> bool:
    sp, ot = gp.spawner, gp.obstacle_types
    ln = ot[typ].length_m
    act = ot[typ].action
    for st in inp.static:
        if lane in st.lanes and _overlap(x, x + ln, st.s0, st.s1):
            return False
    zlen = max([ln] + [ot[rt].length_m for _, rt in row])  # zone spans the whole row (SPAWNER_ALGORITHM.md D2)
    z0, z1 = x - sp.gap_zone_m, x + zlen + sp.gap_zone_m
    s_all = {lane} | {rl for rl, _ in row}
    s_dodge = {rl for rl, rt in row if ot[rt].action == "dodge"}
    if act == "dodge":
        s_dodge.add(lane)
    for st in inp.static:
        if _overlap(z0, z1, st.s0, st.s1):
            s_all.update(st.lanes)
            if st.action == "dodge":
                s_dodge.update(st.lanes)
    if loop <= sp.clear_lane_until_loop:
        if len(s_all) > 2:
            return False
    elif act == "dodge":
        if len(s_dodge) > 2:
            return False
    if act != "dodge":
        for st in inp.static:
            if st.action != "dodge" and lane in st.lanes and _dist(x, x + ln, st.s0, st.s1) <= sp.action_gap_m:
                return False
        for ol, ot_, ox in emitted:
            if ol == lane and ot[ot_].action != "dodge" and _dist(x, x + ln, ox, ox + ot[ot_].length_m) <= sp.action_gap_m:
                return False
    return True


def generate_loop(gp: GameplayV1, inp: SpawnInput, loop: int) -> list[Spawned]:
    sp, ot = gp.spawner, gp.obstacle_types
    tw = inp.type_weights
    rng = Mulberry32(fnv1a32(f"{inp.slug}|{inp.seed}|{loop}".encode("utf-8")))
    cursor = sp.free_start_loop0_m if loop == 0 else sp.free_start_later_m
    limit = inp.length_m - sp.seam_free_m
    lf = float(loop)
    gap = max(sp.row_gap_min_m, sp.row_gap_base_m - sp.row_gap_per_loop_m * lf)
    p = min(sp.spawn_p_max, sp.spawn_p_base + sp.spawn_p_per_loop * lf) * (sp.density_floor + sp.density_gain * inp.density)
    maxlen = max(ot[t].length_m for t in TYPE_ORDER)
    row = -1
    out: list[Spawned] = []
    emitted: list[tuple[int, str, float]] = []
    while True:
        d = [rng.next() for _ in range(8)]
        x = cursor + d[0] * sp.jitter_max_m
        if x > limit:
            return out
        row += 1
        cursor = x + gap
        if not (d[1] < p):
            continue
        if any(a <= x <= b for a, b in inp.keepout):
            continue
        cands = [0, 1, 2]
        m = sp.footpath_missing_margin_m
        if any(s0 < x + maxlen + m and s1 > x - m for s0, s1 in inp.footpath_missing):
            cands.remove(0)
        if not cands:
            continue
        lane_a = cands[math.floor(d[3] * len(cands))]
        type_a = _pick(gp, lane_a, d[5], tw)
        var_a = math.floor(d[7] * sp.variants)
        two = loop >= sp.second_obstacle_from_loop and d[2] < sp.second_obstacle_prob
        if not _allowed(gp, inp, loop, type_a, lane_a, x, [], emitted):
            continue
        a = Spawned(loop, row, lane_a, type_a, x_mm(x), var_a)
        out.append(a)
        emitted.append((lane_a, type_a, x))
        if two:
            cb = [c for c in cands if c != lane_a]
            if cb:
                lane_b = cb[math.floor(d[4] * len(cb))]
                type_b = _pick(gp, lane_b, d[6], tw)
                if ot[type_a].action != "dodge" and ot[type_b].action != "dodge":
                    type_b = next(t for t in TYPE_ORDER if t in sp.lane_weights[str(lane_b)] and ot[t].action == "dodge")
                var_b = (var_a + sp.variants // 2) % sp.variants
                if _allowed(gp, inp, loop, type_b, lane_b, x, [(lane_a, type_a)], emitted):
                    out.append(Spawned(loop, row, lane_b, type_b, x_mm(x), var_b))
                    emitted.append((lane_b, type_b, x))


def generate_loops(gp: GameplayV1, inp: SpawnInput, loops: int) -> list[Spawned]:
    out: list[Spawned] = []
    for k in range(loops):
        out.extend(generate_loop(gp, inp, k))
    return out


# --- (de)serialisation + manifest adapter -------------------------------------------------------------------------

def spawn_input_to_dict(inp: SpawnInput) -> dict:
    return {"slug": inp.slug, "seed": inp.seed, "length_m": inp.length_m, "density": inp.density,
            "type_weights": dict(inp.type_weights),
            "footpath_missing": [[a, b] for a, b in inp.footpath_missing],
            "static": [{"s0": s.s0, "s1": s.s1, "lanes": list(s.lanes), "action": s.action} for s in inp.static],
            "keepout": [[a, b] for a, b in inp.keepout]}


def spawn_input_from_dict(d: Mapping[str, Any]) -> SpawnInput:
    return SpawnInput(
        slug=d["slug"], seed=int(d["seed"]), length_m=float(d["length_m"]), density=float(d["density"]),
        type_weights={k: float(v) for k, v in d["type_weights"].items()},
        footpath_missing=tuple((float(a), float(b)) for a, b in d.get("footpath_missing", [])),
        static=tuple(StaticBlocker(float(s["s0"]), float(s["s1"]), tuple(int(x) for x in s["lanes"]), s["action"])
                     for s in d.get("static", [])),
        keepout=tuple((float(a), float(b)) for a, b in d.get("keepout", [])))


def spawn_input_from_manifest(manifest: Any, *, seed: int, gp: GameplayV1 | None = None) -> SpawnInput:
    """SPAWNER_ALGORITHM.md section F. ``manifest`` is a ``ManifestV1`` or its dict form."""
    m = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else manifest
    gp = gp or load_gameplay()
    margin = gp.spawner.keepout_margin_m
    ranges: list[list[float]] = []
    for st in sorted(m["lanes"]["stations"], key=lambda s: s["s"]):
        if st.get("footpath") is None:
            lo, hi = st["s"] - 1.25, st["s"] + 1.25
            if ranges and lo <= ranges[-1][1] + 1e-9:
                ranges[-1][1] = max(ranges[-1][1], hi)
            else:
                ranges.append([lo, hi])
    static, keepout = [], []
    for c in m["colliders"]:
        if c["action"] != "none":
            static.append(StaticBlocker(c["s"], c["s"] + c["length_m"], tuple(c["lanes"]), c["action"]))
        if c["type"] in ("crossing", "ramp"):
            keepout.append((c["s"] - margin, c["s"] + c["length_m"] + margin))
    hints = m["spawn_hints"]
    return SpawnInput(slug=m["segment"]["slug"], seed=seed, length_m=m["segment"]["length_s"], density=hints["density"],
                      type_weights=dict(hints["type_weights"]), footpath_missing=tuple((a, b) for a, b in ranges),
                      static=tuple(static), keepout=tuple(keepout))
