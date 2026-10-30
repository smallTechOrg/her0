// Pure run simulation (no Three.js): lanes, actions, loops, tokens, collisions. Constants per spec/ui.md.
export const C = Object.freeze({
  LANE_TWEEN_MS: 150, JUMP_MS: 500, SLIDE_MS: 600,
  SPEED_START: 6, SPEED_PER_LOOP: 0.1, SPEED_MAX: 12,
  TOKEN_VALUE: 50, HEALTH_MAX: 100, HIT_DAMAGE: 20, INVULN_S: 1.2, RUNNER_HALF_M: 0.3, TOKEN_RADIUS_M: 0.6, JUMP_HEIGHT_M: 1.3,
});

const LANE_CMDS = { left: -1, right: +1 };

export class Runner {
  constructor(scene) {
    this.lengthM = scene.segment.length_m;
    this.lanes = scene.cross_section.lanes;
    this.hazards = scene.objects.filter((o) => o.action !== 'none');
    this.tokenDefs = scene.tokens;
    this.reset();
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
    this.crashed = null;      // scene object that took health to 0, or null
    this.health = C.HEALTH_MAX;
    this.hits = 0;
    this.invulnUntil = 0;     // elapsed-time (s) until which hits are ignored
    this.hitKeys = new Set(); // `${objId}:${loop}` already damaged this pass
    this.lastHit = null;
    this.elapsed = 0;
  }

  get speed() { return Math.min(C.SPEED_MAX, C.SPEED_START + C.SPEED_PER_LOOP * this.loops); }
  get score() { return Math.round(this.x) + C.TOKEN_VALUE * this.tokens; }
  get xWrapped() { return this.x - this.loops * this.lengthM; }
  get invulnerable() { return this.elapsed < this.invulnUntil; }
  get airborne() { return this.action === 'jumping'; }
  get sliding() { return this.action === 'sliding'; }

  // Visual lateral offset (lat_m) interpolating the lane tween.
  visualLatM(easing = (t) => t) {
    const from = this.lanes[this.laneFrom].lat_m;
    const to = this.lanes[this.lane].lat_m;
    return from + (to - from) * easing(Math.min(1, this.tweenT));
  }

  // Jump height (m) along a parabola over JUMP_MS.
  jumpY() {
    if (!this.airborne) return 0;
    const t = Math.min(1, this.actionT / (C.JUMP_MS / 1000));
    return 4 * C.JUMP_HEIGHT_M * t * (1 - t);
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
    if (this.tweenT < 1) this.tweenT = Math.min(1, this.tweenT + dt / (C.LANE_TWEEN_MS / 1000));
    if (this.action !== 'none') {
      this.actionT += dt;
      const limit = (this.action === 'jumping' ? C.JUMP_MS : C.SLIDE_MS) / 1000;
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

    for (const obj of this.hazards) {
      if (!obj.lanes.includes(this.lane)) continue;
      if (!this._overlaps(obj, xw) && !this._overlaps(obj, xw + this.lengthM)) continue;
      if (obj.action === 'jump' && this.airborne) continue;
      if (obj.action === 'slide' && this.sliding) continue;
      const key = `${obj.id}:${this.loops}`;
      if (this.invulnerable || this.hitKeys.has(key)) continue;
      this.hitKeys.add(key);
      this.health = Math.max(0, this.health - C.HIT_DAMAGE);
      this.hits += 1;
      this.lastHit = obj;
      this.invulnUntil = this.elapsed + C.INVULN_S;
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
    const lo = x0 - C.TOKEN_RADIUS_M, hi = x1 + C.TOKEN_RADIUS_M;
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
    return x + C.RUNNER_HALF_M > obj.x_m && x - C.RUNNER_HALF_M < obj.x_m + obj.length_m;
  }

  snapshot() {
    return {
      distance_m: Math.round(this.x * 10) / 10,
      score: this.score,
      tokens: this.tokens,
      lane: this.lane,
      lane_name: this.lanes[this.lane].name,
      action: this.action,
      loops: this.loops,
      speed: Math.round(this.speed * 100) / 100,
      health: this.health,
      hits: this.hits,
      invulnerable: this.invulnerable,
      last_hit: this.lastHit ? { id: this.lastHit.id, type: this.lastHit.type, label: this.lastHit.label } : null,
      crashed_into: this.crashed ? { id: this.crashed.id, type: this.crashed.type } : null,
      elapsed_s: Math.round(this.elapsed * 10) / 10,
    };
  }
}
