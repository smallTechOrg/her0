// HUD: distance, score, tokens, lane indicator. Updates only when values change (cheap on mobile).
const LANE_LABELS = { footpath: 'footpath', road_edge: 'road edge', road: 'road' };

export function createHud() {
  const root = document.getElementById('hud');
  const distance = document.getElementById('hud-distance');
  const score = document.getElementById('hud-score');
  const tokens = document.getElementById('hud-tokens');
  const lane = document.getElementById('hud-lane');
  const health = document.getElementById('hud-health');
  const healthBar = document.getElementById('hud-health-bar');
  let last = {};
  return {
    show() { root.hidden = false; },
    hide() { root.hidden = true; },
    update(snap) {
      const d = Math.floor(snap.distance_m);
      if (last.d !== d) { distance.textContent = String(d); last.d = d; }
      if (last.score !== snap.score) { score.textContent = String(snap.score); last.score = snap.score; }
      if (last.tokens !== snap.tokens) { tokens.textContent = String(snap.tokens); last.tokens = snap.tokens; }
      // health scale = gameplay.runner.health_max, carried on the runner snapshot (no hard-coded 100)
      const h = snap.health, hm = snap.health_max;
      if (h !== undefined && (last.health !== h || last.healthMax !== hm)) {
        health.textContent = String(h);
        if (!(hm > 0)) throw new Error('hud: snapshot lacks health_max (gameplay.runner.health_max)');
        const pct = (100 * h) / hm;
        healthBar.style.width = `${pct}%`;
        healthBar.dataset.level = pct <= 25 ? 'low' : pct <= 60 ? 'mid' : 'ok';
        last.health = h; last.healthMax = hm;
      }
      if (last.lane !== snap.lane) {
        lane.textContent = LANE_LABELS[snap.lane_name] || snap.lane_name;
        lane.dataset.lane = String(snap.lane);
        last.lane = snap.lane;
      }
    },
    reset() { last = {}; this.update({ distance_m: 0, score: 0, tokens: 0, lane: 0, lane_name: 'footpath' }); },
  };
}
