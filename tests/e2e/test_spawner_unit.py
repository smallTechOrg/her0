"""Spawner unit properties (PRNG primitives, determinism, fairness guarantees) on the served JS module."""

from __future__ import annotations

from gameplay_harness import gameplay, harness_browser, harness_url, hpage  # noqa: F401

MASK = 0xFFFFFFFF


def py_fnv1a32(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode():
        h = ((h ^ b) & MASK) * 0x01000193 & MASK
    return h


def py_mulberry32(a: int):
    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & MASK
        t = a
        t = ((t ^ (t >> 15)) * (t | 1)) & MASK
        t = (t ^ ((t + (((t ^ (t >> 7)) * (t | 61)) & MASK)) & MASK)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296.0

    return nxt


def test_fnv1a32_known_vectors_and_matches_independent_python(hpage):  # noqa: F811
    strings = ["", "a", "foobar", "vector-street|1|0", "bellandur-main-road-n-2|424242|2"]
    got = hpage.evaluate("async (xs) => { const m = await import('/js/game/spawner.js'); return xs.map(m.fnv1a32); }", strings)
    assert got[0] == 0x811C9DC5 and got[1] == 0xE40C292C and got[2] == 0xBF9CF968  # published FNV-1a 32-bit values
    assert got == [py_fnv1a32(s) for s in strings]


def test_mulberry32_matches_independent_python_exactly(hpage):  # noqa: F811
    seeds = [0, 1, 0xDEADBEEF, 0xFFFFFFFF, py_fnv1a32("vector-street|7|1")]
    got = hpage.evaluate(
        "async (ss) => { const m = await import('/js/game/spawner.js'); return ss.map((s) => { const r = m.mulberry32(s); return Array.from({length: 40}, r); }); }",
        seeds,
    )
    for seed, draws in zip(seeds, got):
        r = py_mulberry32(seed)
        assert draws == [r() for _ in range(40)]
        assert all(0.0 <= d < 1.0 for d in draws)


PROPS_JS = """
async ({lengths, seeds, loops, statics, missing, keepout}) => {
  const m = await import('/js/game/spawner.js');
  const gp = await (await fetch('/api/gameplay.json')).json();
  const ot = gp.obstacle_types, sp = gp.spawner;
  const viol = [];
  let total = 0, twoRows = 0;
  const perLoop = {};
  for (const length_m of lengths) for (const seed of seeds) {
    const inp = { slug: 'prop-street', seed, length_m, density: 1, type_weights: sp.default_spawn_hints.type_weights,
                  footpath_missing: missing, static: statics, keepout };
    for (let L = 0; L < loops; L++) {
      const rows = m.generateLoop(gp, inp, L);
      total += rows.length; perLoop[L] = (perLoop[L] || 0) + rows.length;
      const byRow = {};
      for (const r of rows) (byRow[r[1]] = byRow[r[1]] || []).push(r);
      const rowIds = Object.keys(byRow).map(Number).sort((a, b) => a - b);
      const free = L === 0 ? sp.free_start_loop0_m : sp.free_start_later_m;
      let prevX = null;
      for (const id of rowIds) {
        const rs = byRow[id]; const x = rs[0][4] / 1000;
        if (rs.length > 2) viol.push(['rows>2', length_m, seed, L, id]);
        if (rs.length === 2) twoRows++;
        if (rs.length > 1 && L < sp.second_obstacle_from_loop) viol.push(['two-before-loop2', length_m, seed, L, id]);
        if (new Set(rs.map((r) => r[2])).size !== rs.length) viol.push(['dup-lane', length_m, seed, L, id]);
        if (prevX !== null && x - prevX < sp.row_gap_min_m - 0.002) viol.push(['gap', length_m, seed, L, id, x - prevX]);
        prevX = x;
        if (x < free - 0.001 || x > length_m - sp.seam_free_m + sp.jitter_max_m) viol.push(['bounds', length_m, seed, L, x]);
        for (const [a, b] of keepout) if (x >= a && x <= b) viol.push(['keepout', length_m, seed, L, x]);
        for (const r of rs) {
          if (!Number.isInteger(r[4])) viol.push(['x_mm-not-int', r]);
          if (r[2] === 0) for (const [s0, s1] of missing) if (s0 < x + 4.2 + sp.footpath_missing_margin_m && s1 > x - sp.footpath_missing_margin_m) viol.push(['lane0-in-gap', length_m, seed, L, x]);
          if (!(r[5] >= 0 && r[5] < sp.variants)) viol.push(['variant', r]);
        }
      }
      // passable: a runner that needs lane_tween_ms for every lane change at top speed can always get through
      // (dodge obstacles block with runner_half_m margin; reachability DP over a 0.1 m grid)
      const obs = rows.map((r) => ({ lane: r[2], a: r[4] / 1000, b: r[4] / 1000 + ot[r[3]].length_m, act: ot[r[3]].action }));
      for (const s of statics) for (const l of s.lanes) obs.push({ lane: l, a: s.s0, b: s.s1, act: s.action });
      const h = gp.runner.runner_half_m, step = 0.1, n = Math.ceil(length_m / step);
      const lag = Math.ceil((gp.runner.speed_max_mps * gp.runner.lane_tween_ms / 1000) / step);
      const fr = [0, 1, 2].map((l) => { const f = new Array(n + 1).fill(true);
        for (const o of obs) if (o.act === 'dodge' && o.lane === l) for (let i = Math.max(0, Math.floor((o.a - h) / step)); i <= Math.min(n, Math.ceil((o.b + h) / step)); i++) f[i] = false;
        return f; });
      let alive = [true, false, false], dead = false;
      for (let i = 1; i <= n && !dead; i++) {
        const nxt = [false, false, false];
        for (let l = 0; l < 3; l++) if (alive[l]) for (const l2 of [l - 1, l, l + 1]) {
          if (l2 < 0 || l2 > 2 || !fr[l2][i]) continue;
          if (l2 !== l) { let ok = true; for (let j = i; j <= Math.min(n, i + lag) && ok; j++) ok = fr[l][j] && fr[l2][j]; if (!ok) continue; }
          nxt[l2] = true;
        }
        alive = nxt; dead = !alive.some(Boolean);
      }
      if (dead) viol.push(['impassable', length_m, seed, L]);
      if (L <= sp.clear_lane_until_loop) {
        for (const r of rows) {
          const x = r[4] / 1000, len = ot[r[3]].length_m;
          const near = new Set(obs.filter((o) => o.a < x + len + sp.gap_zone_m && o.b > x - sp.gap_zone_m).map((o) => o.lane));
          if (near.size > 2) viol.push(['no-clear-lane', length_m, seed, L, x]);
        }
      }
    }
  }
  return { viol: viol.slice(0, 5), nViol: viol.length, total, twoRows, perLoop };
}
"""


def test_fairness_properties_over_many_seeds_lengths_and_blockers(hpage):  # noqa: F811
    res = hpage.evaluate(PROPS_JS, {
        "lengths": [40, 60, 100, 200], "seeds": list(range(120)), "loops": 6,
        "statics": [{"s0": 30.0, "s1": 30.4, "lanes": [0], "action": "dodge"}, {"s0": 33.0, "s1": 34.0, "lanes": [1], "action": "jump"}],
        "missing": [[40.0, 52.5]], "keepout": [[28.0, 34.0]],
    })
    assert res["nViol"] == 0, res["viol"]
    assert res["total"] > 500 and res["twoRows"] > 0  # not vacuous: obstacles and two-obstacle rows really occur
    pl = res["perLoop"]
    assert pl["5"] > pl["0"], f"difficulty must ramp with the loop: {pl}"


def test_determinism_seed_sensitivity_and_density(hpage):  # noqa: F811
    res = hpage.evaluate("""async () => {
      const m = await import('/js/game/spawner.js');
      const gp = await (await fetch('/api/gameplay.json')).json();
      const mk = (seed, density) => ({ slug: 's', seed, length_m: 200, density, type_weights: gp.spawner.default_spawn_hints.type_weights, footpath_missing: [], static: [], keepout: [] });
      const run = (inp) => [0, 1, 2, 3].flatMap((L) => m.generateLoop(gp, inp, L));
      const a = run(mk(5, 1)), b = run(mk(5, 1)), c = run(mk(6, 1));
      const lo = [...Array(40).keys()].reduce((n, s) => n + run(mk(s, 0)).length, 0);
      const hi = [...Array(40).keys()].reduce((n, s) => n + run(mk(s, 1)).length, 0);
      // type mix follows the hints: all weight on pothole biases lane 1/2 picks to pothole
      const heavy = { ...mk(1, 1), type_weights: { parked_car: 0, two_wheeler: 0, cone: 0, barricade: 0, pothole: 1, stall: 0, low_branch: 0 } };
      const base = { ...mk(1, 1), type_weights: { parked_car: 1, two_wheeler: 0, cone: 0, barricade: 0, pothole: 0, stall: 0, low_branch: 0 } };
      const frac = (inp, t) => { let n = 0, k = 0; for (let s = 0; s < 60; s++) for (const L of [0, 1, 2, 3]) for (const r of m.generateLoop(gp, { ...inp, seed: s }, L)) { if (r[2] > 0) { n++; if (r[3] === t) k++; } } return k / n; };
      return { same: JSON.stringify(a) === JSON.stringify(b), diff: JSON.stringify(a) !== JSON.stringify(c), lo, hi,
               potholeBias: frac(heavy, 'pothole'), carBias: frac(base, 'parked_car'), noHints: frac(mk(1, 1), 'pothole') };
    }""")
    assert res["same"] and res["diff"]
    assert res["hi"] > res["lo"], res
    assert res["potholeBias"] > res["noHints"] + 0.1, res
    assert res["carBias"] > 0.4, res


def test_edge_cases_short_street_and_empty_input(hpage):  # noqa: F811
    res = hpage.evaluate("""async () => {
      const m = await import('/js/game/spawner.js');
      const gp = await (await fetch('/api/gameplay.json')).json();
      const base = { slug: 'x', seed: 0, length_m: 20, density: 1 };
      const short = [0, 1, 2].map((L) => m.generateLoop(gp, base, L));   // type_weights/static/... omitted -> defaults
      const big = m.generateLoop(gp, { ...base, length_m: 200, seed: 4294967296 + 5 }, 3);
      return { short, bigLen: big.length, mm: m.toMm(1.0005), mm2: m.toMm(2.5) };
    }""")
    assert res["short"][0] == []  # 20 m street: loop-0 free start (30 m) swallows it
    assert res["bigLen"] > 0 and res["mm"] == 1001 and res["mm2"] == 2500  # floor(x*1000+0.5), seeds beyond 2^32 hash as text


def test_error_path_invalid_input_raises(hpage):  # noqa: F811
    res = hpage.evaluate("""async () => {
      const m = await import('/js/game/spawner.js');
      const gp = await (await fetch('/api/gameplay.json')).json();
      const errs = [];
      for (const f of [() => m.generateLoop(gp, undefined, 0), () => m.generateLoop({}, { slug: 'a', seed: 1, length_m: 100 }, 0)]) {
        try { f(); errs.push(null); } catch (e) { errs.push(e.message); }
      }
      return errs;
    }""")
    assert len(res) == 2 and "needs {slug, seed, length_m}" in res[0] and "gameplay object" in res[1], res
