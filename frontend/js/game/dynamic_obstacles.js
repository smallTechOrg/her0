// Dynamic obstacle layer: generic low-poly instanced models for the seeded obstacles of runner.activeObstacles().
// One InstancedMesh per obstacle type (7 draw calls max, each model a few dozen triangles); never baked into the
// street model. Sizes come from gameplay.json obstacle_types[...].size_m (read via runner.gp).

const CAPACITY = 16;
// Per-variant body tints (multiplied with the vertex colours); variant indexes into it modulo length.
const TINTS = [[1, 1, 1], [0.85, 0.55, 0.5], [0.55, 0.7, 0.95], [0.95, 0.9, 0.5], [0.6, 0.85, 0.6], [0.75, 0.75, 0.8]];

function col(hex) { return [((hex >> 16) & 255) / 255, ((hex >> 8) & 255) / 255, (hex & 255) / 255]; }

// Model parts: boxes / cylinders / cones / blobs in local space (x lateral, y up, z along travel), origin on the ground.
function buildModels(size) {
  const m = {};
  const box = (w, h, d, x, y, z, c) => ({ k: 'box', w, h, d, x, y, z, c: col(c) });
  const cyl = (r, h, x, y, z, c, rot = null, seg = 8) => ({ k: 'cyl', r, h, x, y, z, c: col(c), rot, seg });
  const cone = (r, h, x, y, z, c, seg = 8) => ({ k: 'cone', r, h, x, y, z, c: col(c), seg });
  const blob = (r, x, y, z, c) => ({ k: 'blob', r, x, y, z, c: col(c) });

  let s = size.parked_car;
  m.parked_car = [
    box(s.w, s.h * 0.4, s.d, 0, s.h * 0.42, 0, 0xd9d9de),
    box(s.w * 0.9, s.h * 0.34, s.d * 0.52, 0, s.h * 0.78, -s.d * 0.04, 0xb9c4d0),
    ...[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([a, b]) => cyl(0.3, 0.22, a * (s.w / 2 - 0.05), 0.3, b * s.d * 0.32, 0x222222, 'z')),
  ];
  s = size.two_wheeler;
  m.two_wheeler = [
    cyl(0.3, 0.1, 0, 0.3, -s.d * 0.36, 0x222222, 'x'), cyl(0.3, 0.1, 0, 0.3, s.d * 0.36, 0x222222, 'x'),
    box(0.16, 0.22, s.d * 0.6, 0, 0.55, 0, 0xc0392b), box(0.24, 0.1, s.d * 0.3, 0, 0.78, -s.d * 0.1, 0x2b2b2b),
    box(s.w, 0.06, 0.06, 0, 1.0, s.d * 0.3, 0x333333), box(0.1, 0.5, 0.1, 0, 0.75, s.d * 0.32, 0x444444),
  ];
  s = size.cone;
  m.cone = [box(s.w, 0.04, s.d, 0, 0.02, 0, 0x2a2a2a), cone(s.w * 0.42, s.h, 0, 0.04 + s.h / 2, 0, 0xff6a00), cyl(s.w * 0.27, 0.1, 0, s.h * 0.55, 0, 0xf4f4f4)];
  s = size.barricade;
  m.barricade = [
    box(0.08, s.h, 0.1, -s.w * 0.45, s.h / 2, 0, 0x555555), box(0.08, s.h, 0.1, s.w * 0.45, s.h / 2, 0, 0x555555),
    box(s.w, 0.2, 0.06, 0, s.h * 0.85, 0, 0xf2f2f2), box(s.w * 0.7, 0.2, 0.06, 0, s.h * 0.55, 0, 0xe0561c),
  ];
  s = size.pothole;
  m.pothole = [cyl(s.w * 0.58, 0.015, 0, 0.012, 0, 0x6b6762, null, 10), cyl(s.w * 0.42, 0.02, 0, 0.026, 0, 0x0c0c0c, null, 10)];
  s = size.stall;
  m.stall = [
    ...[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([a, b]) => box(0.08, s.h, 0.08, a * s.w * 0.45, s.h / 2, b * s.d * 0.45, 0x6b5a45)),
    box(s.w * 0.95, 0.12, s.d * 1.05, 0, s.h * 0.97, 0, 0xd9822b), box(s.w * 0.9, 0.5, 0.5, 0, 0.8, 0, 0x8a6b4a),
    box(0.4, 0.25, 0.3, -s.w * 0.2, 1.17, 0, 0x7bb661), box(0.4, 0.25, 0.3, s.w * 0.2, 1.17, 0.05, 0xd94f4f),
  ];
  s = size.low_branch;
  m.low_branch = [
    box(s.w, 0.12, 0.14, 0, 1.32, 0, 0x5b4636),
    blob(0.45, -s.w * 0.28, 1.2, 0.05, 0x3f7d3a), blob(0.4, 0, 1.12, -0.1, 0x4c9142), blob(0.45, s.w * 0.28, 1.2, 0.1, 0x3f7d3a),
    box(0.1, 0.55, 0.1, s.w * 0.5, 1.58, 0, 0x5b4636),
  ];
  return m;
}

function partGeometry(THREE, p) {
  let g;
  if (p.k === 'box') g = new THREE.BoxGeometry(p.w, p.h, p.d);
  else if (p.k === 'cyl') g = new THREE.CylinderGeometry(p.r, p.r, p.h, p.seg);
  else if (p.k === 'cone') g = new THREE.ConeGeometry(p.r, p.h, p.seg);
  else g = new THREE.IcosahedronGeometry(p.r, 0);
  if (p.rot === 'z') g.rotateZ(Math.PI / 2);   // wheel axis along x
  if (p.rot === 'x') g.rotateZ(Math.PI / 2);
  g.translate(p.x, p.y, p.z);
  return g.index ? g.toNonIndexed() : g;
}

function mergeParts(THREE, parts) {
  const geos = parts.map((p) => partGeometry(THREE, p));
  let n = 0;
  for (const g of geos) n += g.getAttribute('position').count;
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), colr = new Float32Array(n * 3);
  let o = 0;
  geos.forEach((g, i) => {
    const c = parts[i].c, pa = g.getAttribute('position'), na = g.getAttribute('normal');
    pos.set(pa.array, o * 3); nor.set(na.array, o * 3);
    for (let v = 0; v < pa.count; v++) { colr[(o + v) * 3] = c[0]; colr[(o + v) * 3 + 1] = c[1]; colr[(o + v) * 3 + 2] = c[2]; }
    o += pa.count;
    g.dispose();
  });
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  out.setAttribute('color', new THREE.BufferAttribute(colr, 3));
  return out;
}

/** createDynamicLayer(THREE, { scene, track, runner }) -> { group, update, reset, dispose } */
export function createDynamicLayer(THREE, { scene, track, runner }) {
  const gp = runner && runner.gp;
  if (!gp || !gp.obstacle_types) throw new Error('createDynamicLayer: runner.gp (gameplay.json) is required');
  const types = Object.keys(gp.obstacle_types);
  const sizes = Object.fromEntries(types.map((t) => [t, gp.obstacle_types[t].size_m]));
  const models = buildModels(sizes);
  const group = new THREE.Group();
  group.name = 'DynamicObstacles';
  const material = new THREE.MeshLambertMaterial({ vertexColors: true });
  const meshes = {};
  const geometries = [];
  for (const t of types) {
    const geo = mergeParts(THREE, models[t]);
    geometries.push(geo);
    const mesh = new THREE.InstancedMesh(geo, material, CAPACITY);
    mesh.name = `dyn_${t}`;
    mesh.count = 0;
    mesh.visible = false;
    mesh.frustumCulled = false; // bounding sphere is the model's, not the instances'
    mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(CAPACITY * 3).fill(1), 3);
    meshes[t] = mesh;
    group.add(mesh);
  }
  if (scene && scene.add) scene.add(group);

  const mat = new THREE.Matrix4();
  const quat = new THREE.Quaternion();
  const pos = new THREE.Vector3();
  const one = new THREE.Vector3(1, 1, 1);
  const up = new THREE.Vector3(0, 1, 0);
  const counts = {};
  let lastIds = [];

  function groundY(o) {
    if (o.lane !== 0 || !track.footpathAt) return 0;
    try {
      const f = track.footpathAt(o.x_m + o.length_m / 2);
      return f && f.height_m ? f.height_m : 0;
    } catch (_e) { return 0; }
  }

  function update() {
    for (const t of types) counts[t] = 0;
    const list = runner.activeObstacles();
    lastIds = [];
    for (const o of list) {
      const mesh = meshes[o.type];
      if (!mesh) continue;
      const i = counts[o.type];
      if (i >= CAPACITY) continue;
      const mid = o.x_abs + o.length_m / 2;
      const lat = track.laneLat(o.x_m + o.length_m / 2, o.lane);
      const w = track.world(mid, lat);
      pos.set(w.x, groundY(o), w.z);
      quat.setFromAxisAngle(up, -w.theta);
      mat.compose(pos, quat, one);
      mesh.setMatrixAt(i, mat);
      const tint = TINTS[(o.variant || 0) % TINTS.length];
      mesh.instanceColor.setXYZ(i, tint[0], tint[1], tint[2]);
      counts[o.type] = i + 1;
      lastIds.push(o.id);
    }
    for (const t of types) {
      const mesh = meshes[t];
      mesh.count = counts[t];
      mesh.visible = counts[t] > 0;
      mesh.instanceMatrix.needsUpdate = true;
      mesh.instanceColor.needsUpdate = true;
    }
  }

  function reset() {
    for (const t of types) { counts[t] = 0; meshes[t].count = 0; meshes[t].visible = false; }
    lastIds = [];
  }

  function dispose() {
    if (scene && scene.remove) scene.remove(group);
    for (const g of geometries) g.dispose();
    material.dispose();
    for (const t of types) meshes[t].dispose();
  }

  return {
    group, update, reset, dispose,
    get visibleIds() { return lastIds.slice(); },
    get drawCalls() { return types.filter((t) => meshes[t].visible).length; },
  };
}
