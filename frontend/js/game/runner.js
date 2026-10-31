// Pure run simulation (no Three.js): lanes, actions, loops, tokens, collisions, seeded dynamic obstacles.
// Every rule number comes from gameplay.json (spec/portability.md sections 5-7); see gameplay.js.
import { C, currentGameplay, loadGameplay } from '/js/game/gameplay.js';
import { generateLoop, toObstacle } from '/js/game/spawner.js';

export { C };

const LANE_CMDS = { left: -1, right: +1 };
const runKey = (slug) => `hero.run.${slug}`;

// Legacy-only: path.js (v1/v2 geometry) is imported on demand so the glb path never loads it.
let makeTrackFn = null;

/** Legacy v1/v2 only: load path.js and gameplay.json so the synchronous `new Runner(scene)` works afterwards. */
export async function prepareLegacy() {
  if (!makeTrackFn) makeTrackFn = (await import('/js/game/path.js?v=4')).makeTrack;
  if (!currentGameplay()) await loadGameplay();
}

// Legacy v1/v2 scene -> playData (the v3 path gets this from play_data.js).
function legacyPlayData(scene, gp) {
  if (!makeTrackFn) throw new Error('Runner: legacy scene given but legacy path module not loaded (await prepareLegacy() first)');
  const km = gp.spawner.keepout_margin_m;
  if (typeof km !== 'number') throw new Error('Runner: gameplay.spawner.keepout_margin_m missing');
  const L = scene.segment.length_m;
  const objs = scene.objects || [];
  const lastEnd = (o) => o.x_m + o.length_m;
  return {
    slug: scene.segment.slug,
    length_m: L,
    track: makeTrackFn(scene),
    colliders: objs.filter((o) => o.action !== 'none').map((o) => ({ ...o, s: o.x_m })),
    tokens: scene.tokens.map((t) => ({ ...t, s: t.x_m })),
    lanes: scene.cross_section.lanes,
    spawnInput: {
      slug: scene.segment.slug, length_m: L,
      footpath_missing: scene.cross_section.footpath_coverage.filter((c) => c.state === 'missing').map((c) => [c.from_m, c.to_m]),
      static: objs.filter((o) => o.action !== 'none').map((o) => ({ s0: o.x_m, s1: lastEnd(o), lanes: o.lanes, action: o.action })),
      keepout: objs.filter((o) => o.type === 'crossing' || o.type === 'ramp').map((o) => [o.x_m - km, lastEnd(o) + km]),
    },
  };
}

export class Runner {
  /**
   * @param playData  { length_m, track, colliders, tokens, spawnInput, lanes[, slug] } (v3) - or a legacy v1/v2 scene object
   * @param opts      { gameplay, seed = null, runNumber = null }
   */
  constructor(playData, { gameplay = null, seed = null, runNumber = null } = {}) {
    this.gp = gameplay || currentGameplay();
    if (!this.gp) throw new Error('Runner: gameplay.json is not loaded (pass { gameplay } or await loadGameplay() first)');
    const pd = playData.cross_section ? legacyPlayData(playData, this.gp) : playData;
    this.rg = this.gp.runner;
    this.slug = pd.slug || (pd.spawnInput && pd.spawnInput.slug) || (playData.segment && playData.segment.slug) || 'street';
    this.lengthM = pd.length_m;
    this.track = pd.track;
    this.lanes = pd.lanes || [];
    this.hazards = (pd.colliders || []).filter((o) => o.action !== 'none').map((o) => (o.x_m === undefined ? { ...o, x_m: o.s } : o));
    this.tokenDefs = (pd.tokens || []).map((t) => (t.x_m === undefined ? { ...t, x_m: t.s } : t));
    this.spawnInput = { ...(pd.spawnInput || {}), slug: this.slug, length_m: this.lengthM };
    this.seedOpt = seed;
    this.runNumberOpt = runNumber;
    this.seed = null;
    this.runNumber = null;
    this._loopObs = new Map();
    this.reset();
  }

  /** Fix seed / runNumber for a new run (+1 on the persisted counter unless runNumber was passed) and generate loop 0. */
  startRun() {
    let rn = this.runNumberOpt;
    if (rn === null || rn === undefined) {
      let n = 0;
      try { n = parseInt(localStorage.getItem(runKey(this.slug)) || '0', 10) || 0; } catch (_e) { n = 0; }
      rn = n + 1;
      try { localStorage.setItem(runKey(this.slug), String(rn)); } catch (_e) { /* private mode: still deterministic per page */ }
    }
    this.runNumber = rn;
    let seed = this.seedOpt;
    if (seed === null || seed === undefined) {
      const q = typeof location !== 'undefined' ? new URLSearchParams(location.search).get('seed') : null;
      seed = q !== null && /^\d+$/.test(q) ? parseInt(q, 10) : rn;
    }
    this.seed = seed;
    this._loopObs = new Map();
    this._obstaclesFor(0);
    return { run_number: this.runNumber, seed: this.seed };
  }

  _obstaclesFor(loop) {
    if (this.seed === null) return [];
    let list = this._loopObs.get(loop);
    if (!list) {
      const rows = generateLoop(this.gp, { ...this.spawnInput, seed: this.seed }, loop);
      list = rows.map((r) => toObstacle(this.gp, r));
      this._loopObs.set(loop, list);
    }
    return list;
  }

  /** Obstacles for [x - window_behind, x + window_ahead] of the run; x_abs = loop*length + x_m (returned as `x_abs`). */
  activeObstacles() {
    if (this.seed === null) return [];
    const sp = this.gp.spawner;
    const lo = this.x - sp.window_behind_m, hi = this.x + sp.window_ahead_m;
    const out = [];
    const first = Math.max(0, Math.floor(lo / this.lengthM));
    const last = Math.floor(hi / this.lengthM);
    for (let k = first; k <= last; k++) {
      for (const o of this._obstaclesFor(k)) {
        const xa = k * this.lengthM + o.x_m;
        if (xa + o.length_m >= lo && xa <= hi) out.push({ ...o, x_abs: xa });
      }
    }
    return out;
  }

  get dynamic() {
    return { run_number: this.runNumber, seed: this.seed, obstacles: this.activeObstacles(), rules: this.gp.spawner };
  }

  reset() {
    this.x = 0;               // total distance (m)
    this.lane = 0;            // logical lane (collision lane)
    this.laneFrom = 0;        // visual tween origin
    this.tweenT = 1;          // 0..1, 1 = done
    this.action = 'none';     // 'none' | 'jumping' | 'sliding'
    this.actionT = 0;         // seconds into the action
    this.loops = 0;
    this.tokens = 0;
    this.collected = new Set();
    this.queued = null;
    this.crashed = null;      // object that took health to 0, or null
    this.health = this.rg.health_max;
    this.hits = 0;
    this.invulnUntil = this.rg.start_grace_s; // elapsed-time (s) until which hits are ignored (start grace)
    this.hitKeys = new Set(); // `${objId}:${loop}` already damaged this pass
    this.lastHit = null;
    this.elapsed = 0;
    this.seed = null;         // dynamic obstacles dropped until startRun()
    this._loopObs = new Map();
  }

  get speed() { return Math.min(this.rg.speed_max_mps, this.rg.speed_start_mps + this.rg.speed_per_loop_mps * this.loops); }
  get score() { return Math.round(this.x) + this.gp.tokens.value * this.tokens; }
  get xWrapped() { return this.x - this.loops * this.lengthM; }
  get invulnerable() { return this.elapsed < this.invulnUntil; }
  get airborne() { return this.action === 'jumping'; }
  get sliding() { return this.action === 'sliding'; }

  // Visual lateral offset (lat_m) interpolating the lane tween.
  visualLatM(easing = (t) => t) {
    // Lane centres follow the traced strips: lane_lat[] interpolated at the runner's station (v1: constants).
    const sw = this.xWrapped;
    const from = this.track.laneLat(sw, this.laneFrom);
    const to = this.track.laneLat(sw, this.lane);
    return from + (to - from) * easing(Math.min(1, this.tweenT));
  }

  // Jump height (m) along a parabola over JUMP_MS.
  jumpY() {
    if (!this.airborne) return 0;
    const t = Math.min(1, this.actionT / (this.rg.jump_ms / 1000));
    return 4 * this.rg.jump_height_m * t * (1 - t);
  }

  canAccept(cmd) {
    if (cmd in LANE_CMDS) return this.tweenT >= 1;
    return this.action === 'none';
  }

  input(cmd) {
    if (this.crashed || !(cmd in LANE_CMDS || cmd === 'jump' || cmd === 'slide')) return;
    if (this.canAccept(cmd)) this._apply(cmd);
    else this.queued = cmd; // one deep; a newer input replaces the older queued one
  }

  _apply(cmd) {
    if (cmd in LANE_CMDS) {
      const target = Math.max(0, Math.min(2, this.lane + LANE_CMDS[cmd]));
      if (target === this.lane) return;
      this.laneFrom = this.lane;
      this.lane = target;
      this.tweenT = 0;
    } else {
      this.action = cmd === 'jump' ? 'jumping' : 'sliding';
      this.actionT = 0;
    }
  }

  update(dt) {
    const events = [];
    if (this.crashed) return events;
    this.elapsed += dt;
    if (this.tweenT < 1) this.tweenT = Math.min(1, this.tweenT + dt / (this.rg.lane_tween_ms / 1000));
    if (this.action !== 'none') {
      this.actionT += dt;
      const limit = (this.action === 'jumping' ? this.rg.jump_ms : this.rg.slide_ms) / 1000;
      if (this.actionT >= limit) { this.action = 'none'; this.actionT = 0; }
    }
    if (this.queued && this.canAccept(this.queued)) { this._apply(this.queued); this.queued = null; }

    const prevX = this.x;
    this.x += this.speed * dt;
    // Tokens: swept test over [prevX, x] so a low frame rate (step > 2 * TOKEN_RADIUS_M) cannot skip
    // a coin. Jumping/sliding still collects. The old loop is swept first, then the new one after the
    // wrap, so a coin near the end of the loop is not confused with its twin at the start of the next.
    this._sweepTokens(prevX, this.x, this.loops, events);
    if (this.x >= (this.loops + 1) * this.lengthM) {
      this.loops += 1;
      this.collected.clear();
      events.push({ type: 'loop', loops: this.loops });
      this._sweepTokens(prevX, this.x, this.loops, events);
    }
    const xw = this.xWrapped;

    const dyn = this.seed === null ? [] : this._obstaclesFor(this.loops);
    for (const obj of this.hazards.concat(dyn)) {
      if (!obj.lanes.includes(this.lane)) continue;
      if (!this._overlaps(obj, xw) && (obj.dynamic || !this._overlaps(obj, xw + this.lengthM))) continue;
      if (obj.action === 'jump' && this.airborne) continue;
      if (obj.action === 'slide' && this.sliding) continue;
      const key = `${obj.id}:${this.loops}`;
      if (this.invulnerable || this.hitKeys.has(key)) continue;
      this.hitKeys.add(key);
      this.health = Math.max(0, this.health - this.rg.hit_damage);
      this.hits += 1;
      this.lastHit = obj;
      this.invulnUntil = this.elapsed + this.rg.invuln_s;
      events.push({ type: 'hit', object: obj, health: this.health });
      if (this.health <= 0) {
        this.crashed = obj;
        events.push({ type: 'crash', object: obj });
      }
      break;
    }
    return events;
  }

  _sweepTokens(x0, x1, loop, events) {
    const lo = x0 - this.gp.tokens.radius_m, hi = x1 + this.gp.tokens.radius_m;
    for (const tok of this.tokenDefs) {
      if (tok.lane !== this.lane || this.collected.has(tok.id)) continue;
      const tx = tok.x_m + loop * this.lengthM;
      if (tx >= lo && tx <= hi) {
        this.collected.add(tok.id);
        this.tokens += 1;
        events.push({ type: 'token', id: tok.id });
      }
    }
  }

  _overlaps(obj, x) {
    return x + this.rg.runner_half_m > obj.x_m && x - this.rg.runner_half_m < obj.x_m + obj.length_m;
  }

  snapshot() {
    return {
      distance_m: Math.round(this.x * 10) / 10,
      score: this.score,
      tokens: this.tokens,
      lane: this.lane,
      lane_name: (this.lanes[this.lane] && this.lanes[this.lane].name) || this.gp.lanes.names[this.lane],
      action: this.action,
      loops: this.loops,
      speed: Math.round(this.speed * 100) / 100,
      health: this.health,
      health_max: this.rg.health_max,
      hits: this.hits,
      invulnerable: this.invulnerable,
      last_hit: this.lastHit ? { id: this.lastHit.id, type: this.lastHit.type, label: this.lastHit.label, ...(this.lastHit.dynamic ? { dynamic: true } : {}) } : null,
      crashed_into: this.crashed ? { id: this.crashed.id, type: this.crashed.type, ...(this.crashed.dynamic ? { label: this.crashed.label, dynamic: true } : {}) } : null,
      elapsed_s: Math.round(this.elapsed * 10) / 10,
      run_number: this.runNumber,
      seed: this.seed,
    };
  }
}
