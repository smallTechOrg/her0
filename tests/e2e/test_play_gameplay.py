"""Gameplay logic (Runner + gameplay.js + spawner) driven in Chromium on the served modules: health system, tokens, lane
logic, run numbers / seed override, deterministic obstacles, difficulty ramp, guaranteed passable lanes (autopilot),
legacy ``new Runner(scene)`` compatibility and the 'no constants defined in JS' rule."""

from __future__ import annotations

import re

from gameplay_harness import FRONTEND, GAMEPLAY_PATH, gameplay, harness_browser, harness_url, hpage  # noqa: F401

# Shared JS helpers (installed on window.__g by `install`).
HELPERS = """
async () => {
  const { Runner } = await import('/js/game/runner.js');
  const gpm = await import('/js/game/gameplay.js');
  const gp = await gpm.loadGameplay();
  const man = await (await fetch('/fixtures/export/contract_curved/street.manifest.json')).json();
  // straight stub Track with the S2 Track API (world / laneLat / footpathAt / applyCopy / length)
  const laneT = man.lanes.stations[0].lane_t;
  const track = { length: man.segment.length_s, world: (s, lat) => ({ x: lat, z: -s, theta: 0 }), laneLat: (s, l) => laneT[l],
                  footpathAt: () => null, applyCopy: (p) => p };
  const L = man.segment.length_s;
  const playData = (over = {}) => ({
    slug: man.segment.slug, length_m: L, track, lanes: gp.lanes.names.map((name) => ({ name })),
    colliders: over.colliders || [], tokens: over.tokens || [],
    spawnInput: over.spawnInput || { density: 0.8, type_weights: man.spawn_hints.type_weights, footpath_missing: [], static: [], keepout: [] },
  });
  const col = (id, s, lane, action = 'dodge', length_m = 1) => ({ id, type: 'debris', label: 'box', s, length_m, lanes: [lane], action });
  window.__g = { Runner, gp, gpm, man, track, L, playData, col };
  return true;
}
"""


def _install(page):
    page.evaluate(HELPERS)


def test_gameplay_loads_from_manifest_or_endpoint_and_derives_legacy_constants(hpage, gameplay):  # noqa: F811
    res = hpage.evaluate("""async () => {
      const gpm = await import('/js/game/gameplay.js');
      const fromApi = await gpm.loadGameplay();
      const man = await (await fetch('/fixtures/export/contract_curved/street.manifest.json')).json();
      const fromMan = await gpm.loadGameplay(man);
      return { fromApi, same: fromMan === man.gameplay, C: { ...gpm.C } };
    }""")
    assert res["fromApi"] == gameplay and res["same"]
    r, t = gameplay["runner"], gameplay["tokens"]
    assert res["C"]["HIT_DAMAGE"] == r["hit_damage"] and res["C"]["SPEED_MAX"] == r["speed_max_mps"] and res["C"]["TOKEN_VALUE"] == t["value"]
    assert res["C"]["HEALTH_MAX"] == r["health_max"] and res["C"]["START_GRACE_S"] == r["start_grace_s"]


def test_loadgameplay_error_path_when_endpoint_fails(hpage):  # noqa: F811
    hpage.route("**/api/gameplay.json", lambda route: route.fulfill(status=500, body="boom"))
    err = hpage.evaluate("""async () => {
      const gpm = await import('/js/game/gameplay.js?fresh=1');
      try { await gpm.loadGameplay(); return null; } catch (e) { return String(e.message); }
    }""")
    assert err and "500" in err


def test_no_game_module_defines_the_old_gameplay_constants():
    names = "LANE_TWEEN_MS|JUMP_MS|SLIDE_MS|SPEED_START|SPEED_MAX|HIT_DAMAGE|INVULN_S|START_GRACE_S|TOKEN_VALUE|HEALTH_MAX"
    pat = re.compile(rf"\b(?:{names})\b\s*[:=](?!=)")
    files = sorted((FRONTEND / "js" / "game").glob("*.js"))
    assert len(files) >= 15, files   # the whole game dir, not a hand-picked list
    offenders = []
    for f in files:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, offenders


def test_legacy_runner_scene_call_is_backward_compatible(hpage):  # noqa: F811
    res = hpage.evaluate("""async () => {
      const { Runner, prepareLegacy } = await import('/js/game/runner.js');
      await prepareLegacy();
      const scene = await (await fetch('/fx/scene_example.json')).json();
      const r = new Runner(scene);
      const snap0 = r.snapshot();
      const ev = [];
      r.input('right');
      for (let i = 0; i < 60 * 9; i++) ev.push(...r.update(1 / 60));
      return { snap0, active: r.activeObstacles(), snap: r.snapshot(), types: [...new Set(ev.map((e) => e.type))], lanes: r.lanes.length, speed: r.speed };
    }""")
    expected_keys = {"distance_m", "score", "tokens", "lane", "lane_name", "action", "loops", "speed", "health", "hits", "invulnerable",
                     "last_hit", "crashed_into", "elapsed_s"}
    assert expected_keys <= set(res["snap0"])
    assert res["snap0"]["health"] == 100 and res["snap0"]["lane_name"] == "footpath" and res["snap0"]["speed"] == 6
    assert res["active"] == []  # no startRun() => no dynamic obstacles: the legacy game is unchanged
    assert res["snap"]["lane"] == 1 and res["snap"]["lane_name"] == "road_edge" and res["snap"]["distance_m"] > 40


def test_run_number_persists_in_local_storage_and_seed_query_overrides(hpage):  # noqa: F811
    _install(hpage)
    first = hpage.evaluate("""() => { const g = window.__g; const r = new g.Runner(g.playData(), { gameplay: g.gp });
      const a = r.startRun(); r.reset(); const b = r.startRun(); r.reset(); const c = r.startRun();
      return { a, b, c, stored: localStorage.getItem('hero.run.' + g.man.segment.slug) }; }""")
    assert [first["a"]["run_number"], first["b"]["run_number"], first["c"]["run_number"]] == [1, 2, 3]
    assert [first["a"]["seed"], first["b"]["seed"], first["c"]["seed"]] == [1, 2, 3]  # seed defaults to the run number
    assert first["stored"] == "3"
    hpage.goto(hpage.hero_url + "/__harness.html?seed=424242")  # survives a reload; ?seed= overrides only the seed
    _install(hpage)
    second = hpage.evaluate("""() => { const g = window.__g; const r = new g.Runner(g.playData(), { gameplay: g.gp });
      const a = r.startRun(); return { a, dyn: r.dynamic }; }""")
    assert second["a"] == {"run_number": 4, "seed": 424242}
    assert second["dyn"]["run_number"] == 4 and second["dyn"]["seed"] == 424242 and "obstacles" in second["dyn"] and second["dyn"]["rules"]["prng"] == "mulberry32"
    hpage.goto(hpage.hero_url + "/__harness.html?seed=abc")  # malformed override is ignored
    _install(hpage)
    third = hpage.evaluate("""() => { const g = window.__g; const r = new g.Runner(g.playData(), { gameplay: g.gp, seed: 9, runNumber: 50 });
      return r.startRun(); }""")
    assert third == {"run_number": 50, "seed": 9}  # explicit options win; the counter is not touched
    assert hpage.evaluate("() => localStorage.getItem('hero.run.contract-curved-street')") == "4"


def test_same_seed_same_obstacles_other_run_number_other_obstacles(hpage):  # noqa: F811
    _install(hpage)
    res = hpage.evaluate("""() => { const g = window.__g;
      const lists = (seed) => { const r = new g.Runner(g.playData(), { gameplay: g.gp, seed, runNumber: 1 }); r.startRun();
        const out = []; for (let L = 0; L < 4; L++) out.push(r._obstaclesFor(L).map((o) => [o.id, o.type, o.x_m, o.variant])); return out; };
      return { a: lists(11), b: lists(11), c: lists(12) }; }""")
    assert res["a"] == res["b"] and res["a"] != res["c"]
    assert sum(len(x) for x in res["a"]) > 0


def test_health_system_grace_invulnerability_and_crash(hpage, gameplay):  # noqa: F811
    _install(hpage)
    r = gameplay["runner"]
    res = hpage.evaluate("""() => { const g = window.__g;
      const pd = g.playData({ colliders: Array.from({ length: 12 }, (_, i) => g.col('c' + i, 12 + i * 3, 0, 'dodge', 1)) });
      const run = new g.Runner(pd, { gameplay: g.gp, seed: 0, runNumber: 1 });
      const log = []; let crashAt = null;
      for (let i = 0; i < 60 * 60 && !run.crashed; i++) for (const e of run.update(1 / 60)) {
        if (e.type === 'hit') log.push({ t: run.elapsed, health: e.health, id: e.object.id });
        if (e.type === 'crash') crashAt = { t: run.elapsed, id: e.object.id };
      }
      const snap = run.snapshot();
      run.reset();
      return { log, crashAt, snap, afterReset: run.snapshot() }; }""")
    log = res["log"]
    assert log[0]["t"] >= r["start_grace_s"] - 1e-6, "no hit during the start grace"
    assert [h["health"] for h in log[:3]] == [r["health_max"] - r["hit_damage"] * i for i in (1, 2, 3)]
    gaps = [b["t"] - a["t"] for a, b in zip(log, log[1:])]
    assert all(g >= r["invuln_s"] - 1e-6 for g in gaps), gaps  # invulnerability window between hits
    assert res["crashAt"] and log[-1]["health"] == 0 and len(log) == r["health_max"] // r["hit_damage"]
    assert res["snap"]["crashed_into"]["id"] == res["crashAt"]["id"] and res["snap"]["health"] == 0
    assert res["afterReset"]["health"] == r["health_max"] and res["afterReset"]["crashed_into"] is None and res["afterReset"]["hits"] == 0


def test_actions_clear_jump_and_slide_obstacles_but_not_dodge_ones(hpage):  # noqa: F811
    _install(hpage)
    res = hpage.evaluate("""() => { const g = window.__g;
      const trial = (action, cmd) => {
        const pd = g.playData({ colliders: [g.col('o', 20, 0, action, 1.0)] });
        const run = new g.Runner(pd, { gameplay: g.gp, seed: 0, runNumber: 1 });
        run.elapsed = run.invulnUntil + 0.1;
        let done = false;
        for (let i = 0; i < 60 * 12; i++) {
          if (cmd && Math.abs(run.x - 18.4) < 0.12 && run.action === 'none') run.input(cmd);
          run.update(1 / 60);
        }
        return run.hits;
      };
      return { jumpClears: trial('jump', 'jump'), jumpNoAction: trial('jump', null), slideClears: trial('slide', 'slide'),
               slideWrongAction: trial('slide', 'jump'), dodgeJump: trial('dodge', 'jump') }; }""")
    assert res == {"jumpClears": 0, "jumpNoAction": 1, "slideClears": 0, "slideWrongAction": 1, "dodgeJump": 1}


def test_tokens_lane_logic_queueing_and_score(hpage, gameplay):  # noqa: F811
    _install(hpage)
    res = hpage.evaluate("""() => { const g = window.__g;
      const pd = g.playData({ tokens: [{ id: 't1', s: 10, lane: 0, reason: 'intact_footpath' }, { id: 't2', s: 30, lane: 1, reason: 'crossing' }] });
      const run = new g.Runner(pd, { gameplay: g.gp, seed: 0, runNumber: 1 });
      const ev = [];
      run.input('left');                                  // clamped: already at lane 0 -> no tween
      const clamp = { lane: run.lane, tween: run.tweenT };
      run.input('right'); const mid = { lane: run.lane, tween: run.tweenT };
      run.input('right');                                 // tween in flight -> queued (one deep)
      const queued = run.queued;
      run.update(0.2);                                    // tween done (150 ms), queue consumed -> lane 2
      const afterQueue = { lane: run.lane, queued: run.queued };
      for (let i = 0; i < 60 * 8; i++) ev.push(...run.update(1 / 60));
      return { clamp, mid, queued, afterQueue, tokens: ev.filter((e) => e.type === 'token').map((e) => e.id), snap: run.snapshot() }; }""")
    assert res["clamp"] == {"lane": 0, "tween": 1}
    assert res["mid"]["lane"] == 1 and res["mid"]["tween"] == 0 and res["queued"] == "right"
    assert res["afterQueue"] == {"lane": 2, "queued": None}
    assert res["tokens"] == []  # runner moved to lane 2: neither token (lane 0 / lane 1) is collected
    assert res["snap"]["score"] == round(res["snap"]["distance_m"]) or abs(res["snap"]["score"] - res["snap"]["distance_m"]) < 1

    res2 = hpage.evaluate("""() => { const g = window.__g;
      const pd = g.playData({ tokens: [{ id: 't1', s: 10, lane: 0 }, { id: 't2', s: 12, lane: 1 }] });
      const run = new g.Runner(pd, { gameplay: g.gp, seed: 0, runNumber: 1 });
      const got = [];
      while (run.x < 20) for (const e of run.update(0.5)) if (e.type === 'token') got.push(e.id);   // coarse steps: swept test
      return { got, score: run.score, x: run.x, tokens: run.tokens }; }""")
    assert res2["got"] == ["t1"] and res2["tokens"] == 1
    assert res2["score"] == round(res2["x"]) + gameplay["tokens"]["value"]


def test_speed_ramps_per_loop_and_caps(hpage, gameplay):  # noqa: F811
    _install(hpage)
    r = gameplay["runner"]
    res = hpage.evaluate("""() => { const g = window.__g;
      const run = new g.Runner(g.playData(), { gameplay: g.gp, seed: 0, runNumber: 1 });
      const out = []; for (const l of [0, 1, 10, 500]) { run.loops = l; out.push(run.speed); } return out; }""")
    assert res == [r["speed_start_mps"], r["speed_start_mps"] + r["speed_per_loop_mps"], r["speed_start_mps"] + 10 * r["speed_per_loop_mps"], r["speed_max_mps"]]


def test_difficulty_ramps_and_autopilot_survives_many_loops_on_real_spawns(hpage):  # noqa: F811
    """Guaranteed passable: a greedy autopilot (lane choice by lookahead, jump/slide when asked) never gets hit across
    6 loops x 15 seeds on the real example street (its own static hazards included) - spawns are always passable."""
    res = hpage.evaluate("""async () => {
      const { Runner, prepareLegacy } = await import('/js/game/runner.js');
      await prepareLegacy();
      const scene = await (await fetch('/fx/scene_example.json')).json();
      const hits = [], perLoop = [0, 0, 0, 0, 0, 0], spawnedTypes = new Set();
      let totalDyn = 0;
      for (let seed = 1; seed <= 15; seed++) {
        const run = new Runner(scene, { seed, runNumber: seed }); run.startRun();
        const statics = run.hazards;
        run.elapsed = run.invulnUntil;       // judge the avoidance logic, not the start grace
        const dt = 1 / 60; let lastSeenLoop = -1;
        while (run.loops < 6 && !run.crashed) {
          const all = [...run.activeObstacles().map((o) => ({ ...o, abs: o.x_abs })), ...statics.flatMap((o) => [0, 1].map((k) => ({ ...o, abs: o.x_m + (run.loops + k) * run.lengthM })))];
          const ahead = all.filter((o) => o.abs + o.length_m > run.x - 0.5 && o.abs < run.x + 16);
          const bad = (lane, a, b) => ahead.some((o) => o.lanes.includes(lane) && o.action === 'dodge' && o.abs < b && o.abs + o.length_m > a);
          if (run.tweenT >= 1) {
            const here = bad(run.lane, run.x - 0.5, run.x + 12);
            if (here) {
              const free = [0, 1, 2].filter((l) => !bad(l, run.x - 0.5, run.x + 12)).sort((a, b) => Math.abs(a - run.lane) - Math.abs(b - run.lane));
              if (free.length) run.input(free[0] > run.lane ? 'right' : 'left');
            }
          }
          for (const o of ahead) if (o.lanes.includes(run.lane) && o.action !== 'dodge' && o.action !== 'none' && run.action === 'none') {
            const dur = (o.action === 'jump' ? run.rg.jump_ms : run.rg.slide_ms) / 1000;
            const d = o.abs + o.length_m / 2 - run.x - run.speed * dur / 2;   // centre the action window on the obstacle
            if (d <= 0 && d > -0.5) run.input(o.action);
          }
          for (const e of run.update(dt)) if (e.type === 'loop') lastSeenLoop = e.loops;
        }
        for (let L = 0; L < 6; L++) { const os = run._obstaclesFor(L); perLoop[L] += os.length; totalDyn += os.length; os.forEach((o) => spawnedTypes.add(o.type)); }
        hits.push([seed, run.hits, run.loops, !!run.crashed, run.hazards.length, run.lastHit && [run.lastHit.id, run.lastHit.type, run.lastHit.action]]);
      }
      return { hits, perLoop, totalDyn, types: [...spawnedTypes] };
    }""")
    bad = [h for h in res["hits"] if h[1] != 0 or h[3] or h[2] < 6]
    assert not bad, f"autopilot got hit on spawned/static obstacles: {bad}"
    assert res["totalDyn"] > 20 and len(res["types"]) >= 4, res
    assert res["perLoop"][5] > res["perLoop"][0], f"difficulty must ramp with the loop: {res['perLoop']}"


def test_runner_without_loaded_gameplay_fails_loudly(hpage):  # noqa: F811
    """No gameplay passed and none loaded yet (fresh module instance): a clear error, not a silent default."""
    err = hpage.evaluate("""async () => {
      const { Runner } = await import('/js/game/runner.js');
      try { new Runner({ slug: 's', length_m: 100, track: null, colliders: [], tokens: [], spawnInput: {} }); return null; } catch (e) { return String(e.message); }
    }""")
    assert err and "gameplay" in err and "not loaded" in err


def test_v3_path_never_fetches_gameplay_endpoint_or_legacy_path_module(hpage):  # noqa: F811
    """Importing runner.js + loadGameplay(manifest) must not hit /api/gameplay.json nor load the legacy path.js."""
    seen = []
    hpage.on("request", lambda r: seen.append(r.url))
    hpage.evaluate("""async () => {
      const gpm = await import('/js/game/gameplay.js?fresh=lazy');
      await import('/js/game/runner.js?fresh=lazy');
      const man = await (await fetch('/fixtures/export/contract_curved/street.manifest.json')).json();
      await gpm.loadGameplay(man);
    }""")
    assert not [u for u in seen if "/api/gameplay.json" in u or "/path.js" in u], seen
