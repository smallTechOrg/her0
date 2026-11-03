// traffic.js: MOVING obstacles on the road tracks - cars, auto-rickshaws, two-wheelers and the odd cow - the
// third kind of obstacle (spec/ui.md "Three kinds of obstacle"): stock game models, never from imagery.
//
// Deterministic: a mulberry32 stream keyed by `${slug}|${seed}|traffic` decides every vehicle (type, lane,
// direction, speed, spawn time), so a seed replays the same traffic. Vehicles live in the runner's unrolled
// x (metres since the start): oncoming ones appear TRAFFIC.spawn_ahead_m ahead and drive toward the runner,
// same-direction ones appear behind and overtake. Collision is the runner's usual one (lane + x overlap) through
// runner.movingHazards; a vehicle only hurts while it is drawn (runner.isDrawn), like every other obstacle.
//
// Models come from dynamic_obstacles.js' kit (buildObstacleGeometry: parked_car, two_wheeler, auto, cow); one
// InstancedMesh per type. Wheels do not turn and cows do not bob: phone budget.
import { buildObstacleGeometry, createObstacleMaterials, OBSTACLE_PALETTES, STATIC_BASE_SIZES } from '/js/game/dynamic_obstacles.js';
import { fnv1a32, mulberry32 } from '/js/game/spawner.js';

export const TRAFFIC = {
  start_after_s: 4.0,          // no traffic during the start grace
  interval_s: [3.0, 7.0],      // seconds between spawns (uniform), shrinking a little per loop
  interval_per_loop_s: 0.4,
  interval_min_s: 1.6,
  spawn_ahead_m: 85,           // oncoming vehicles appear here ahead of the runner
  spawn_behind_m: 18,          // same-direction vehicles appear here behind the runner (they overtake)
  despawn_behind_m: 30,
  despawn_ahead_m: 100,
  oncoming_prob: 0.6,
  // type: weight, lanes it may use (2 = road, 1 = road edge), speed range m/s, label
  types: {
    car: { weight: 0.35, lanes: [2], speed: [8, 11], label: 'car', model: 'parked_car' },
    auto: { weight: 0.3, lanes: [2], speed: [6, 8.5], label: 'auto-rickshaw', model: 'auto' },
    bike: { weight: 0.25, lanes: [1, 2], speed: [7, 10], label: 'two-wheeler', model: 'two_wheeler' },
    cow: { weight: 0.1, lanes: [1, 2], speed: [0.8, 1.6], label: 'cow', model: 'cow' },
  },
  max_active: 8,
  min_gap_m: 12,               // a new vehicle never spawns on top of another in its lane
};
const CAPACITY = 12;
const SIZES = { car: { w: 1.8, h: 1.5, d: 4.2 }, bike: { w: 0.7, h: 1.2, d: 1.9 }, auto: STATIC_BASE_SIZES.auto, cow: STATIC_BASE_SIZES.cow };

function pickType(u) {
  const items = Object.entries(TRAFFIC.types);
  const total = items.reduce((a, [, t]) => a + t.weight, 0);
  let x = u * total;
  for (const [name, t] of items) { x -= t.weight; if (x < 0) return name; }
  return items[items.length - 1][0];
}

/**
 * @param THREE  three module
 * @param o      { track, runner, slug, seed }  runner supplies x, elapsed, loops, lengthM, gp
 */
export function createTraffic(THREE, { track, runner, slug, seed }) {
  const group = new THREE.Group();
  group.name = 'Traffic';
  const materials = createObstacleMaterials(THREE);
  const meshes = {}, geometries = [], palettes = {};
  for (const [name, t] of Object.entries(TRAFFIC.types)) {
    const geo = buildObstacleGeometry(THREE, t.model, SIZES[name]);
    geometries.push(geo);
    const mesh = new THREE.InstancedMesh(geo, materials, CAPACITY);
    mesh.name = `traffic_${name}`;
    mesh.count = 0; mesh.visible = false; mesh.frustumCulled = false;
    mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(CAPACITY * 3).fill(1), 3);
    meshes[name] = mesh;
    palettes[name] = (OBSTACLE_PALETTES[t.model] || [0xffffff]).map((h) => new THREE.Color(h));
    group.add(mesh);
  }

  let rng = mulberry32(0), vehicles = [], nextAt = 0, serial = 0, drawnIds = new Set();
  const mat = new THREE.Matrix4(), quat = new THREE.Quaternion(), pos = new THREE.Vector3(), one = new THREE.Vector3(1, 1, 1), up = new THREE.Vector3(0, 1, 0);

  function reset() {
    rng = mulberry32(fnv1a32(`${slug}|${seed ?? runner.seed ?? 0}|traffic`));
    vehicles = []; serial = 0; drawnIds = new Set();
    nextAt = TRAFFIC.start_after_s + rng() * TRAFFIC.interval_s[0];
    for (const m of Object.values(meshes)) { m.count = 0; m.visible = false; }
  }

  function spawn(t) {
    const type = pickType(rng());
    const rule = TRAFFIC.types[type];
    const lane = rule.lanes[Math.floor(rng() * rule.lanes.length)];
    const oncoming = type !== 'cow' && rng() < TRAFFIC.oncoming_prob;
    const speed = rule.speed[0] + rng() * (rule.speed[1] - rule.speed[0]);
    const x0 = oncoming ? runner.x + TRAFFIC.spawn_ahead_m : runner.x - TRAFFIC.spawn_behind_m;
    const len = SIZES[type].d;
    for (const v of vehicles) if (v.lane === lane && Math.abs(v.x - x0) < TRAFFIC.min_gap_m) return;   // keep the gap
    vehicles.push({ id: `trf-${serial++}`, type, lane, oncoming, speed, x: x0, t0: t, len, variant: Math.floor(rng() * 6),
      label: rule.label });
  }

  function step(t) {
    if (runner.seed === null || runner.seed === undefined) return;
    while (t >= nextAt && vehicles.length < TRAFFIC.max_active) {
      spawn(t);
      const loop = runner.loops || 0;
      const lo = Math.max(TRAFFIC.interval_min_s, TRAFFIC.interval_s[0] - TRAFFIC.interval_per_loop_s * loop);
      const hi = Math.max(lo + 0.5, TRAFFIC.interval_s[1] - TRAFFIC.interval_per_loop_s * loop);
      nextAt = t + lo + rng() * (hi - lo);
    }
    if (t < nextAt - 10) nextAt = t;    // (reset / rewind)
  }

  let lastT = null;
  function update() {
    const t = runner.elapsed || 0;
    const dt = lastT === null ? 0 : Math.max(0, t - lastT);
    lastT = t;
    step(t);
    for (const v of vehicles) v.x += (v.oncoming ? -v.speed : v.speed) * dt;
    const x = runner.x;
    vehicles = vehicles.filter((v) => v.x > x - TRAFFIC.despawn_behind_m && v.x < x + TRAFFIC.despawn_ahead_m);
    const counts = {};
    drawnIds = new Set();
    for (const v of vehicles) {
      const mesh = meshes[v.type];
      const i = counts[v.type] || 0;
      if (i >= CAPACITY) continue;
      const lat = track.laneLat(((v.x % runner.lengthM) + runner.lengthM) % runner.lengthM, v.lane);
      const w = track.world(v.x, lat);
      pos.set(w.x, 0, w.z);
      quat.setFromAxisAngle(up, -w.theta + (v.oncoming ? Math.PI : 0));
      mat.compose(pos, quat, one);
      mesh.setMatrixAt(i, mat);
      const pal = palettes[v.type], c = pal[v.variant % pal.length];
      mesh.instanceColor.setXYZ(i, c.r, c.g, c.b);
      counts[v.type] = i + 1;
      drawnIds.add(v.id);
    }
    for (const [name, mesh] of Object.entries(meshes)) {
      mesh.count = counts[name] || 0;
      mesh.visible = mesh.count > 0;
      mesh.instanceMatrix.needsUpdate = true;
      mesh.instanceColor.needsUpdate = true;
    }
  }

  /** Hazards for the runner this frame (runner.movingHazards): x_m in the runner's current loop frame. A vehicle
   *  overtaking from behind never hurts (it would be off screen: the camera looks forward) - it becomes a hazard
   *  once it is ahead of the runner, like a parked car the runner can run into. Oncoming traffic always counts. */
  function hazards() {
    const L = runner.lengthM, loop = runner.loops || 0;
    return vehicles.filter((v) => v.oncoming || v.x - v.len / 2 > runner.x).map((v) => ({
      id: v.id, type: v.type, label: v.label, dynamic: true, moving: true, action: 'dodge', lanes: [v.lane],
      x_m: v.x - v.len / 2 - loop * L, length_m: v.len,
    }));
  }

  return {
    group, update, reset, hazards,
    isDrawn: (id) => drawnIds.has(id),
    get state() { return vehicles.map((v) => ({ id: v.id, type: v.type, lane: v.lane, oncoming: v.oncoming, x: Math.round(v.x * 10) / 10, speed: Math.round(v.speed * 10) / 10 })); },
    dispose() { for (const g of geometries) g.dispose(); for (const m of materials) m.dispose(); for (const m of Object.values(meshes)) m.dispose(); },
  };
}
