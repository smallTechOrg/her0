// Deterministic obstacle spawner: JS port of the reference in spec/portability.md section 7.
// Identical (loop, row, lane, type, x_mm, variant) lists for identical inputs. Integers are uint32 (>>> 0 after every
// op); floats are doubles using only + - * /, comparisons, floor, min, max. Exactly 8 draws per slot.

export function fnv1a32(str) {
  const bytes = new TextEncoder().encode(str);
  let h = 0x811C9DC5;
  for (const b of bytes) {
    h = (h ^ b) >>> 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h;
}

export function mulberry32(seed) {
  let a = seed >>> 0;
  return function rng() {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
    t = (t ^ ((t + (Math.imul(t ^ (t >>> 7), t | 61) >>> 0)) >>> 0)) >>> 0;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296.0;
  };
}

/** x in metres -> integer millimetres: floor(x*1000 + 0.5) (never Math.round). */
export const toMm = (x) => Math.floor(x * 1000.0 + 0.5);

export const typeOrder = (gp) => Object.keys(gp.obstacle_types);

/** Weighted pick (rule 7). */
function pick(gp, lane, u, tw) {
  const sp = gp.spawner;
  const table = sp.lane_weights[String(lane)];
  const items = [];
  for (const type of typeOrder(gp)) {
    const base = table[type];
    if (base !== undefined && base > 0) items.push([type, base * (1.0 + sp.type_boost * (tw[type] === undefined ? 0.0 : tw[type]))]);
  }
  let total = 0.0;
  for (const it of items) total = total + it[1];
  let x = u * total;
  for (const [type, w] of items) {
    x = x - w;
    if (x < 0) return type;
  }
  return items[items.length - 1][0];
}

/**
 * Generate the rows of loop `L`.
 * @param gp gameplay object   @param inp SpawnInput {slug, seed, length_m, density, type_weights, footpath_missing, static, keepout}
 * @returns Array<[loop, row, lane, type, x_mm, variant]>
 */
export function generateLoop(gp, inp, L) {
  if (!inp || typeof inp.slug !== 'string' || typeof inp.length_m !== 'number') throw new Error('generateLoop: input needs {slug, seed, length_m}');
  if (!gp || !gp.spawner || !gp.obstacle_types) throw new Error('generateLoop: gameplay object (spawner, obstacle_types) required');
  const sp = gp.spawner;
  const ot = gp.obstacle_types;
  const tw = inp.type_weights || sp.default_spawn_hints.type_weights;
  const density = inp.density === undefined ? sp.default_spawn_hints.density : inp.density;
  const statics = inp.static || [];
  const keepout = inp.keepout || [];
  const missing = inp.footpath_missing || [];
  const rng = mulberry32(fnv1a32(`${inp.slug}|${inp.seed}|${L}`));
  let cursor = L === 0 ? sp.free_start_loop0_m : sp.free_start_later_m;
  const limit = inp.length_m - sp.seam_free_m;
  const gap = Math.max(sp.row_gap_min_m, sp.row_gap_base_m - sp.row_gap_per_loop_m * L);
  const p = Math.min(sp.spawn_p_max, sp.spawn_p_base + sp.spawn_p_per_loop * L) * (sp.density_floor + sp.density_gain * density);
  let maxLen = 0.0;
  for (const t of typeOrder(gp)) maxLen = Math.max(maxLen, ot[t].length_m);
  const margin = sp.footpath_missing_margin_m;
  const placed = []; // {lane, action, x, len} of this loop (all rows)
  const out = [];
  let row = -1;

  const allowed = (type, lane, x, rowSoFar) => {
    const len = ot[type].length_m;
    const action = ot[type].action;
    let zlen = len;
    for (const r of rowSoFar) zlen = Math.max(zlen, r.len); // R8: zone spans the whole row
    const zlo = x - sp.gap_zone_m, zhi = x + zlen + sp.gap_zone_m;
    for (const s of statics) if (s.lanes.includes(lane) && s.s0 < x + len && s.s1 > x) return false;
    const sAll = new Set([lane]);
    const sDodge = new Set();
    if (action === 'dodge') sDodge.add(lane);
    for (const s of statics) {
      if (s.s0 < zhi && s.s1 > zlo) {
        for (const l of s.lanes) { sAll.add(l); if (s.action === 'dodge') sDodge.add(l); }
      }
    }
    for (const r of rowSoFar) { sAll.add(r.lane); if (r.action === 'dodge') sDodge.add(r.lane); }
    if (L <= sp.clear_lane_until_loop) {
      if (sAll.size > 2) return false;
    } else if (action === 'dodge') {
      if (sDodge.size > 2) return false;
    }
    if (action !== 'dodge') {
      const dist = (a0, a1, b0, b1) => Math.max(a0 - b1, b0 - a1);
      for (const s of statics) {
        if (s.action !== 'dodge' && s.lanes.includes(lane) && dist(x, x + len, s.s0, s.s1) <= sp.action_gap_m) return false;
      }
      for (const o of placed) {
        if (o.action !== 'dodge' && o.lane === lane && dist(x, x + len, o.x, o.x + o.len) <= sp.action_gap_m) return false;
      }
    }
    return true;
  };

  for (;;) {
    const d = [];
    for (let i = 0; i < 8; i++) d.push(rng());
    const x = cursor + d[0] * sp.jitter_max_m;
    if (x > limit) break;
    row += 1;
    cursor = x + gap;
    if (!(d[1] < p)) continue;
    let blocked = false;
    for (const [a, b] of keepout) if (a <= x && x <= b) { blocked = true; break; }
    if (blocked) continue;
    let footMissing = false;
    for (const [s0, s1] of missing) if (s0 < x + maxLen + margin && s1 > x - margin) { footMissing = true; break; }
    const cands = footMissing ? [1, 2] : [0, 1, 2];
    const laneA = cands[Math.floor(d[3] * cands.length)];
    const typeA = pick(gp, laneA, d[5], tw);
    const varA = Math.floor(d[7] * sp.variants);
    const two = L >= sp.second_obstacle_from_loop && d[2] < sp.second_obstacle_prob;
    if (!allowed(typeA, laneA, x, [])) continue;
    const A = { lane: laneA, action: ot[typeA].action, x, len: ot[typeA].length_m };
    const rowOut = [[L, row, laneA, typeA, toMm(x), varA]];
    placed.push(A);
    if (two) {
      const cb = cands.filter((l) => l !== laneA);
      if (cb.length > 0) {
        const laneB = cb[Math.floor(d[4] * cb.length)];
        let typeB = pick(gp, laneB, d[6], tw);
        if (ot[typeA].action !== 'dodge' && ot[typeB].action !== 'dodge') {
          const table = sp.lane_weights[String(laneB)];
          typeB = typeOrder(gp).find((t) => table[t] !== undefined && table[t] > 0 && ot[t].action === 'dodge');
        }
        const varB = (varA + Math.floor(sp.variants / 2)) % sp.variants;
        if (allowed(typeB, laneB, x, [A])) rowOut.push([L, row, laneB, typeB, toMm(x), varB]);
      }
    }
    for (const r of rowOut.slice(1)) placed.push({ lane: r[2], action: ot[r[3]].action, x, len: ot[r[3]].length_m });
    for (const r of rowOut) out.push(r);
  }
  return out;
}

/** Row tuple -> Obstacle object (runner / dynamic layer shape). */
export function toObstacle(gp, row) {
  const [loop, rowIdx, lane, type, xMm, variant] = row;
  const def = gp.obstacle_types[type];
  return {
    id: `dyn-${loop}-${rowIdx}-${lane}`, type, dynamic: true, lane, x_m: xMm / 1000, length_m: def.length_m,
    action: def.action, size_m: def.size_m, label: def.label, loop, variant, lanes: [lane],
  };
}

/** SpawnInput helper from a footpath-missing list of stations etc. is the caller's job (play_data.js); defaults here. */
export function defaultSpawnInput(gp, slug, length_m) {
  return {
    slug, seed: 0, length_m, density: gp.spawner.default_spawn_hints.density,
    type_weights: gp.spawner.default_spawn_hints.type_weights, footpath_missing: [], static: [], keepout: [],
  };
}
