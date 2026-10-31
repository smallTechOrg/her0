"""Fairness properties of the reference spawner: 1 000 seeds x 10 loops x street lengths 40-200 m (spec section 7)."""

from __future__ import annotations

import math

import pytest

from hero.export.gameplay import load_gameplay
from hero.export.gameplay_ref import SpawnInput, StaticBlocker, generate_loop

GP = load_gameplay()
SP, OT = GP.spawner, GP.obstacle_types
W = SP.default_spawn_hints.type_weights
SEEDS = 1000
LOOPS = 10
EPS = 0.0011  # x_mm rounding slack (metres)
SPEED = GP.runner.speed_max_mps


def _lcg(n: int):
    state = (n * 2654435761 + 12345) & 0xFFFFFFFF

    def f() -> float:
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 4294967296.0
    return f


def _street(seed: int, with_static: bool) -> SpawnInput:
    r = _lcg(seed)
    length = 40.0 + math.floor(r() * 1601) / 10.0
    missing, static, keepout = [], [], []
    if seed % 3 == 0:
        a = r() * length
        missing.append((a, a + 5.0 + r() * 30.0))
    if seed % 4 == 0:
        a = r() * length
        keepout.append((a, a + 3.0 + r() * 10.0))
    if with_static:
        # statics spaced >= 25 m apart so they never form an unavoidable 3-lane wall on their own
        pos = 20.0 + r() * 10.0
        while pos < length:
            lane = math.floor(r() * 3)
            action = ("dodge", "dodge", "jump", "slide")[math.floor(r() * 4)]
            static.append(StaticBlocker(pos, pos + 0.5 + r() * 3.0, (lane,), action))
            pos += 25.0 + r() * 30.0
    dens = (0.0, 0.5, 1.0)[seed % 3]
    return SpawnInput(slug=f"prop-{seed % 17}", seed=seed, length_m=length, density=dens,
                      type_weights=W if seed % 2 else {"parked_car": 0.6, "two_wheeler": 0.3, "cone": 0.1},
                      footpath_missing=tuple(missing), static=tuple(static), keepout=tuple(keepout))


def _ov(a0, a1, b0, b1):
    return a0 < b1 and a1 > b0


def _check(inp: SpawnInput, loop: int, out: list) -> None:
    gap_min = SP.row_gap_min_m
    limit = inp.length_m - SP.seam_free_m
    free = SP.free_start_loop0_m if loop == 0 else SP.free_start_later_m
    rows: dict[int, list] = {}
    for o in out:
        rows.setdefault(o.row, []).append(o)
    prev_x = None
    prev_pass = None
    for r in sorted(rows):
        obs = rows[r]
        x = obs[0].x_mm / 1000.0
        assert all(o.x_mm == obs[0].x_mm for o in obs)
        assert free - EPS <= x <= limit + EPS, "free start / seam"
        assert len(obs) <= 2 and (loop >= SP.second_obstacle_from_loop or len(obs) == 1)
        assert len({o.lane for o in obs}) == len(obs)
        for a, b in inp.keepout:
            assert not (a + EPS < x < b - EPS), "keepout"
        for o in obs:
            ln = OT[o.type].length_m
            if o.lane == 0:
                for s0, s1 in inp.footpath_missing:
                    assert not (s0 < x + 4.2 + SP.footpath_missing_margin_m - EPS and s1 > x - SP.footpath_missing_margin_m + EPS), "lane0 on missing footpath"
            for st in inp.static:
                if o.lane in st.lanes:
                    assert not _ov(x + EPS, x + ln - EPS, st.s0, st.s1), "dynamic overlaps static in lane"
            assert 0 <= o.variant < SP.variants
            assert o.type in SP.lane_weights[str(o.lane)]
        # guaranteed passable lane in the row's zone (static + same row), and fully clear lane for early loops
        zone_lo = x - SP.gap_zone_m
        zone_hi = x + max(OT[o.type].length_m for o in obs) + SP.gap_zone_m
        near = [st for st in inp.static if _ov(zone_lo, zone_hi, st.s0, st.s1)]
        # tolerate only if statics alone already wall 3 lanes (not generated here)
        dodge_lanes = {o.lane for o in obs if OT[o.type].action == "dodge"} | {l for st in near if st.action == "dodge" for l in st.lanes}
        all_lanes = {o.lane for o in obs} | {l for st in near for l in st.lanes}
        assert len(dodge_lanes) <= 2, f"no passable lane at x={x}"
        if loop <= SP.clear_lane_until_loop:
            assert len(all_lanes) <= 2, "no fully clear lane in early loop"
        passable = {0, 1, 2} - dodge_lanes
        if prev_x is not None:
            gap = x - prev_x
            assert gap >= gap_min - EPS, f"row spacing {gap}"
            reach = 1 + math.floor(gap / (SPEED * 0.5))
            assert min(abs(a - b) for a in prev_pass for b in passable) <= reach, "unreachable"
        prev_x, prev_pass = x, passable
    # action spacing within the loop (jump/slide obstacles, dynamic vs dynamic and vs static)
    acts = [(o.lane, o.x_mm / 1000.0, o.x_mm / 1000.0 + OT[o.type].length_m) for o in out if OT[o.type].action != "dodge"]
    for i, (la, a0, a1) in enumerate(acts):
        for lb, b0, b1 in acts[i + 1:]:
            if la == lb:
                assert max(a0 - b1, b0 - a1) > SP.action_gap_m - EPS, "two jump/slide obstacles too close"
        for st in inp.static:
            if st.action != "dodge" and la in st.lanes:
                assert max(a0 - st.s1, st.s0 - a1) > SP.action_gap_m - EPS, "jump/slide too close to static one"


@pytest.mark.parametrize("with_static", [False, True], ids=["bare", "with-static"])
def test_fairness_1000_seeds_x_10_loops(with_static):
    total = 0
    for seed in range(1, SEEDS + 1):
        inp = _street(seed, with_static)
        for loop in range(LOOPS):
            out = generate_loop(GP, inp, loop)
            _check(inp, loop, out)
            total += len(out)
    assert total > 5000, "properties must be exercised on real data, not vacuously"


def test_point_sweep_never_a_full_wall_without_statics():
    """At every metre-fraction of the street at most 2 lanes are physically occupied by dodge obstacles."""
    for seed in range(1, 300):
        inp = _street(seed, False)
        for loop in range(LOOPS):
            out = generate_loop(GP, inp, loop)
            for p10 in range(0, int(inp.length_m * 10), 3):
                p = p10 / 10.0
                lanes = {o.lane for o in out if OT[o.type].action == "dodge"
                         and o.x_mm / 1000.0 - EPS <= p <= o.x_mm / 1000.0 + OT[o.type].length_m + EPS}
                assert len(lanes) <= 2


def test_loops_0_to_2_keep_a_clear_lane_and_one_obstacle_per_row_before_loop_2():
    for seed in range(1, 400):
        inp = _street(seed, False)
        for loop in (0, 1):
            rows = {}
            for o in generate_loop(GP, inp, loop):
                rows.setdefault(o.row, []).append(o)
            assert all(len(v) == 1 for v in rows.values())


def test_second_obstacles_appear_from_loop_2_and_difficulty_ramps():
    twos = {k: 0 for k in range(LOOPS)}
    counts = {k: 0 for k in range(LOOPS)}
    for seed in range(1, 400):
        inp = SpawnInput(slug="ramp", seed=seed, length_m=200.0, density=1.0, type_weights=W)
        for loop in range(LOOPS):
            rows = {}
            for o in generate_loop(GP, inp, loop):
                rows.setdefault(o.row, []).append(o)
            counts[loop] += len(rows)
            twos[loop] += sum(1 for v in rows.values() if len(v) == 2)
    assert twos[0] == twos[1] == 0 and twos[2] > 0 and twos[9] > twos[2] * 0.8
    assert counts[0] < counts[4] and counts[9] >= counts[4] * 0.9  # rows get denser, then plateau


def test_row_gap_shrinks_to_minimum_with_loop():
    inp = SpawnInput(slug="gap", seed=5, length_m=200.0, density=1.0, type_weights=W)
    dists = []
    for loop in (0, 9):
        xs = sorted({o.x_mm for o in generate_loop(GP, inp, loop)})
        dists.append(min((b - a) for a, b in zip(xs, xs[1:])) if len(xs) > 1 else None)
    assert all(d is None or d >= 14000 - 1 for d in dists)


def test_impossible_static_walls_are_not_made_worse():
    """If statics already block two lanes around an x, no dodge obstacle is added in the third lane there."""
    st = (StaticBlocker(60.0, 61.0, (0,), "dodge"), StaticBlocker(60.0, 61.0, (1,), "dodge"))
    for seed in range(1, 300):
        inp = SpawnInput(slug="wall", seed=seed, length_m=200.0, density=1.0, type_weights=W, static=st)
        for loop in range(LOOPS):
            for o in generate_loop(GP, inp, loop):
                x = o.x_mm / 1000.0
                if o.lane == 2 and _ov(x - 6.0, x + OT[o.type].length_m + 6.0, 60.0, 61.0) and OT[o.type].action == "dodge":
                    pytest.fail(f"dodge obstacle in lane 2 beside a two-lane static wall at x={x}")
