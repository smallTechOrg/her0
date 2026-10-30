// Dynamic obstacle layer: instanced models for the seeded obstacles of runner.activeObstacles(). One InstancedMesh per
// obstacle type, never baked into the street model. Sizes come from gameplay.json obstacle_types[...].size_m (read via
// runner.gp); every model is built to that bounding box.
//
// Realism, within a phone budget (~1-2.5k triangles per model, 3 draw calls per visible type):
//   * modelled after what stands on a Bengaluru street: hatchback, scooter, banded traffic cone, police barricade,
//     pothole with standing water, vendor push-cart with produce and an umbrella, a leafy low branch;
//   * three material groups per model: matte (Lambert), gloss (Phong: paint, glass, chrome, water) and a soft contact
//     shadow (transparent, vertex alpha) that grounds the object;
//   * colours are sRGB hex converted to linear (THREE.Color), so they are not washed out;
//   * only "paint" vertices (aPaint = 1) take the per-instance colour from PALETTES[type][variant], so a red car keeps
//     black tyres and clear glass;
//   * a cheap baked ambient occlusion darkens vertices near the ground; some types get a per-variant yaw (a scooter is
//     parked at an angle, a pothole has no direction).

const CAPACITY = 16;
const AO_HEIGHT_M = 0.55;   // vertices below this are darkened towards AO_FLOOR
const AO_FLOOR = 0.62;
const SHADOW_RGB = [0.035, 0.03, 0.025];

// Per-type paint colours (sRGB), indexed by obstacle variant modulo length. Multiplied with the paint parts' base colour.
const PALETTES = {
  parked_car: [0xf1f1ee, 0xb9bdc1, 0x8e1c1c, 0x55595e, 0x24406e, 0x1b1c1e, 0xe8e2d4],
  two_wheeler: [0x9d1f1f, 0xd9d9d6, 0x2b4f8a, 0x1c1c1e, 0x6c6f72, 0x3e5c3a],
  cone: [0xffffff, 0xf6e4da, 0xffffff, 0xead8c8],
  barricade: [0xf2c218, 0xf0f0ec, 0xd8461b, 0xf2c218, 0xf0f0ec, 0xe8b512],
  pothole: [0xffffff],
  stall: [0x2a5fa8, 0xc4322c, 0x2f7d4a, 0xe08a1e, 0x6a3d8f, 0xd9c04a],
  low_branch: [0xffffff, 0xe3eed6, 0xd2e2bf, 0xf4f1dc, 0xdfe8c9, 0xc9dab4],
};
export const OBSTACLE_PALETTES = PALETTES;
// Per-variant extra yaw (radians) on top of the path direction.
const YAWS = {
  two_wheeler: [0.0, 0.32, -0.26, 0.16, -0.36, 0.22],
  pothole: [0.0, 1.3, 2.4, 3.6, 4.5, 5.6],
  cone: [0.0, 0.4, 0.8, 0.2, 0.6, 1.0],
  stall: [0.0, Math.PI, 0.08, Math.PI - 0.1, -0.06, Math.PI + 0.05],
  parked_car: [0.0, 0.03, -0.02, Math.PI, Math.PI + 0.02, -0.03],
};

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------------------------------------------------
// Model kit: every helper adds one part { geo, c: [r,g,b] linear, a: alpha, mat: 'm'|'g'|'s', paint: 0|1, ao: bool }.
// Local space: x lateral, y up, z along travel, origin on the ground at the model's centre.

function kit(THREE) {
  const parts = [];
  const lin = (hex) => { const c = new THREE.Color(hex); return [c.r, c.g, c.b]; };
  const flat = (g) => (g.index ? g.toNonIndexed() : g);
  function add(geo, hex, { gloss = false, paint = false, ao = true } = {}) {
    parts.push({ geo: flat(geo), c: lin(hex), a: 1, mat: gloss ? 'g' : 'm', paint: paint ? 1 : 0, ao });
    return geo;
  }
  const at = (g, x, y, z) => { g.translate(x, y, z); return g; };

  const k = {
    THREE, parts, add, lin,
    box(w, h, d, x, y, z, hex, o) { return add(at(new THREE.BoxGeometry(w, h, d), x, y, z), hex, o); },
    // rounded box: w x h rounded rectangle (radius r) extruded with a bevel of r along z
    rbox(w, h, d, r, x, y, z, hex, o) {
      r = Math.min(r, w / 2 - 0.005, h / 2 - 0.005, d / 2 - 0.005);
      const iw = w - 2 * r, ih = h - 2 * r, cr = Math.min(r, iw / 2, ih / 2) * 0.6;
      const s = new THREE.Shape();
      s.moveTo(-iw / 2 + cr, -ih / 2);
      s.lineTo(iw / 2 - cr, -ih / 2); s.quadraticCurveTo(iw / 2, -ih / 2, iw / 2, -ih / 2 + cr);
      s.lineTo(iw / 2, ih / 2 - cr); s.quadraticCurveTo(iw / 2, ih / 2, iw / 2 - cr, ih / 2);
      s.lineTo(-iw / 2 + cr, ih / 2); s.quadraticCurveTo(-iw / 2, ih / 2, -iw / 2, ih / 2 - cr);
      s.lineTo(-iw / 2, -ih / 2 + cr); s.quadraticCurveTo(-iw / 2, -ih / 2, -iw / 2 + cr, -ih / 2);
      const g = new THREE.ExtrudeGeometry(s, { depth: Math.max(0.001, d - 2 * r), bevelEnabled: true, bevelThickness: r,
        bevelSize: r, bevelSegments: 2, curveSegments: 2 });
      g.translate(0, 0, -(d - 2 * r) / 2);
      return add(at(g, x, y, z), hex, o);
    },
    // side profile [(z, y), ...] extruded across x (width), centred on x; bevel rounds the long edges
    profile(pts, width, bevel, x, hex, o) {
      const s = new THREE.Shape();
      pts.forEach(([u, v], i) => (i ? s.lineTo(u, v) : s.moveTo(u, v)));
      return k.profileShape(s, width, bevel, x, hex, o);
    },
    profileShape(s, width, bevel, x, hex, o) {
      const depth = Math.max(0.001, width - 2 * bevel);
      const g = new THREE.ExtrudeGeometry(s, { depth, bevelEnabled: bevel > 0, bevelThickness: bevel, bevelSize: bevel * 0.7,
        bevelSegments: 2, curveSegments: 6 });
      g.rotateY(-Math.PI / 2);         // shape x -> world z, extrusion -> world -x
      g.translate(depth / 2 + x, 0, 0);
      return add(g, hex, o);
    },
    // cylinder with its axis along 'x' | 'y' | 'z'
    cyl(r1, r2, h, x, y, z, hex, axis = 'y', seg = 12, o) {
      const g = new THREE.CylinderGeometry(r1, r2, h, seg);
      if (axis === 'x') g.rotateZ(Math.PI / 2);
      if (axis === 'z') g.rotateX(Math.PI / 2);
      return add(at(g, x, y, z), hex, o);
    },
    sphere(r, x, y, z, hex, sx = 1, sy = 1, sz = 1, o, ws = 7, hs = 5) {
      const g = new THREE.SphereGeometry(r, ws, hs);
      g.scale(sx, sy, sz);
      return add(at(g, x, y, z), hex, o);
    },
    blob(r, x, y, z, hex, sx = 1, sy = 1, sz = 1, o) {
      const g = new THREE.IcosahedronGeometry(r, 1);
      g.scale(sx, sy, sz);
      return add(at(g, x, y, z), hex, o);
    },
    // a beam (box) from point a to point b, square section t
    beam(a, b, t, hex, o) {
      const va = new THREE.Vector3(...a), vb = new THREE.Vector3(...b);
      const len = va.distanceTo(vb);
      const g = new THREE.BoxGeometry(t, len, t);
      const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), vb.clone().sub(va).normalize());
      g.applyQuaternion(q);
      const m = va.clone().add(vb).multiplyScalar(0.5);
      return add(at(g, m.x, m.y, m.z), hex, o);
    },
    tube(points, r, hex, seg = 16, rs = 6, o) {
      const curve = new THREE.CatmullRomCurve3(points.map((p) => new THREE.Vector3(...p)));
      return add(new THREE.TubeGeometry(curve, seg, r, rs, false), hex, o);
    },
    // wheel: tyre (dark, slightly rounded) + rim + hub, axis along x
    wheel(r, width, x, y, z, rimHex = 0xa9adb2, seg = 16) {
      k.cyl(r, r, width, x, y, z, 0x1b1b1c, 'x', seg);
      k.cyl(r * 0.93, r * 0.93, width + 0.02, x, y, z, 0x232324, 'x', seg);
      const sx = Math.sign(x) || 1;
      k.cyl(r * 0.62, r * 0.62, 0.02, x + sx * (width / 2 + 0.006), y, z, rimHex, 'x', seg, { gloss: true });
      k.cyl(r * 0.18, r * 0.18, 0.03, x + sx * (width / 2 + 0.012), y, z, 0x5c6066, 'x', 8, { gloss: true });
    },
    // contact shadow: a rounded-rectangle core of alpha `a` that fades to 0 over `soft` metres; flat at y
    shadow(w, d, a = 0.55, soft = 0.35, y = 0.018, dx = 0, dz = 0) {
      const n = 28, pos = [], col = [];
      const ring = (hw, hd) => Array.from({ length: n }, (_, i) => {
        const t = (i / n) * Math.PI * 2, c = Math.cos(t), s = Math.sin(t);
        const e = 0.35;   // superellipse exponent -> rounded rectangle
        return [dx + hw * Math.sign(c) * Math.abs(c) ** e, dz + hd * Math.sign(s) * Math.abs(s) ** e];
      });
      const inner = ring(w / 2, d / 2), outer = ring(w / 2 + soft, d / 2 + soft);
      const push = (p, al) => { pos.push(p[0], y, p[1]); col.push(...SHADOW_RGB, al); };
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        push([dx, dz], a); push(inner[j], a * 0.9); push(inner[i], a * 0.9);
        push(inner[i], a * 0.9); push(inner[j], a * 0.9); push(outer[j], 0);
        push(inner[i], a * 0.9); push(outer[j], 0); push(outer[i], 0);
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      g.setAttribute('normal', new THREE.Float32BufferAttribute(new Array(pos.length).fill(0).map((_, i) => (i % 3 === 1 ? 1 : 0)), 3));
      g.setAttribute('color', new THREE.Float32BufferAttribute(col, 4));
      parts.push({ geo: g, c: null, a: 1, mat: 's', paint: 0, ao: false });
    },
    // a flat, irregular disc / ring in the ground plane with a colour per ring (inner -> outer); radius(theta) given
    groundRing(radius, r0, r1, y0, y1, hexIn, hexOut, o, n = 36) {
      const pos = [], cols = [], ci = lin(hexIn), co = lin(hexOut);
      for (let i = 0; i < n; i++) {
        const t0 = (i / n) * Math.PI * 2, t1 = ((i + 1) / n) * Math.PI * 2;
        const p = (t, f, y) => [Math.cos(t) * radius(t) * f, y, Math.sin(t) * radius(t) * f];
        const a0 = p(t0, r0, y0), a1 = p(t1, r0, y0), b0 = p(t0, r1, y1), b1 = p(t1, r1, y1);
        const quad = r0 > 0 ? [a0, b1, b0, a0, a1, b1] : [a0, b1, b0];
        const cl = r0 > 0 ? [ci, co, co, ci, ci, co] : [ci, co, co];
        quad.forEach((q, j) => { pos.push(...q); cols.push(...cl[j]); });
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      g.computeVertexNormals();
      parts.push({ geo: g, c: null, cols, a: 1, mat: o && o.gloss ? 'g' : 'm', paint: 0, ao: false });
    },
  };
  return k;
}

// ---------------------------------------------------------------------------------------------------------------------
// The models.

function parkedCar(k, s) {
  const L = s.d, H = s.h, W = s.w;
  const wr = Math.min(0.31, H * 0.21);           // wheel radius
  const wz = L * 0.31;                           // axle distance from centre
  const bot = wr;                                // sill line = axle height
  const ar = wr + 0.05;                          // wheel-arch radius
  const { THREE } = k;
  // lower body: rear -> roof line of the bonnet -> nose -> bottom with two wheel arches
  const body = new THREE.Shape();
  body.moveTo(-L / 2, bot);
  body.lineTo(-L / 2, H * 0.52);
  body.quadraticCurveTo(-L / 2, H * 0.62, -L * 0.47, H * 0.63);
  body.lineTo(L * 0.2, H * 0.64);
  body.quadraticCurveTo(L * 0.42, H * 0.6, L * 0.48, H * 0.52);
  body.quadraticCurveTo(L / 2, H * 0.46, L / 2, H * 0.36);
  body.lineTo(L / 2, bot);
  body.lineTo(wz + ar, bot);
  body.absarc(wz, bot, ar, 0, Math.PI, false);
  body.lineTo(-wz + ar, bot);
  body.absarc(-wz, bot, ar, 0, Math.PI, false);
  body.lineTo(-L / 2, bot);
  k.profileShape(body, W, 0.09, 0, 0xffffff, { gloss: true, paint: true });
  // glasshouse: hatchback rear, long roof, raked windscreen (dark tinted glass)
  const gw = W * 0.82;
  k.profile([[-L * 0.47, H * 0.62], [-L * 0.42, H * 0.93], [-L * 0.36, H * 0.985], [L * 0.0, H * 0.99], [L * 0.05, H * 0.97],
    [L * 0.22, H * 0.63]], gw, 0.06, 0, 0x18222c, { gloss: true });
  // roof skin + pillars in body colour (the glass is what shows between them)
  k.rbox(gw + 0.1, 0.06, L * 0.42, 0.025, 0, H * 0.99 + 0.035, -L * 0.18, 0xffffff, { gloss: true, paint: true });
  for (const sx of [-1, 1]) {
    const x = sx * (gw / 2 + 0.005);
    k.beam([x, H * 0.64, L * 0.21], [x, H * 0.975, L * 0.03], 0.07, 0xffffff, { gloss: true, paint: true });   // A
    k.beam([x, H * 0.63, -L * 0.1], [x, H * 0.985, -L * 0.1], 0.08, 0xffffff, { gloss: true, paint: true });   // B
    k.beam([x, H * 0.63, -L * 0.42], [x, H * 0.97, -L * 0.37], 0.14, 0xffffff, { gloss: true, paint: true }); // C
    k.box(0.05, 0.11, 0.16, sx * (W / 2 + 0.06), H * 0.66, L * 0.18, 0xffffff, { gloss: true, paint: true });   // mirror
    k.box(0.02, 0.025, 0.14, sx * (W / 2 + 0.005), H * 0.5, -L * 0.03, 0x2a2b2d);                            // door handle
  }
  // bumpers, lights, grille, plates
  k.rbox(W * 0.98, H * 0.12, 0.16, 0.05, 0, bot + H * 0.04, L / 2 - 0.02, 0x2b2c2e);
  k.rbox(W * 0.98, H * 0.12, 0.16, 0.05, 0, bot + H * 0.06, -L / 2 + 0.02, 0x2b2c2e);
  k.box(W * 0.42, H * 0.07, 0.04, 0, H * 0.42, L / 2 + 0.005, 0x141516, { gloss: true });
  k.box(0.5, 0.12, 0.02, 0, bot + H * 0.04, L / 2 + 0.07, 0xf4f4f0);
  k.box(0.5, 0.12, 0.02, 0, H * 0.43, -L / 2 - 0.01, 0xf4f4f0);
  for (const sx of [-1, 1]) {
    k.box(0.34, 0.11, 0.06, sx * W * 0.33, H * 0.5, L / 2 - 0.03, 0xdfe7ec, { gloss: true });
    k.box(0.08, 0.05, 0.05, sx * W * 0.43, H * 0.33, L / 2 + 0.01, 0xf0a020, { gloss: true });
    k.box(0.24, 0.17, 0.06, sx * W * 0.37, H * 0.55, -L / 2 + 0.01, 0xa3141a, { gloss: true });
  }
  // underbody (no see-through) and wheels
  k.box(W * 0.86, 0.14, L * 0.86, 0, bot * 0.75, 0, 0x161617);
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) k.wheel(wr, 0.2, sx * (W / 2 - 0.13), wr, sz * wz);
  k.shadow(W * 0.98, L * 0.96, 0.62, 0.4);
}

function twoWheeler(k, s) {
  // a step-through scooter on its side stand (the commonest thing parked on a Bengaluru footpath)
  const L = s.d, H = s.h, sc = L / 1.9, hs = H / 1.2;
  const wr = 0.26 * sc, wz = 0.63 * sc;
  k.wheel(wr, 0.1, 0, wr, wz, 0x6d7177, 14);
  k.wheel(wr, 0.1, 0, wr, -wz, 0x6d7177, 14);
  // rear body panel (paint)
  k.profile([[-0.95 * sc, 0.38 * hs], [-0.93 * sc, 0.6 * hs], [-0.8 * sc, 0.76 * hs], [-0.12 * sc, 0.76 * hs], [-0.02 * sc, 0.56 * hs],
    [-0.1 * sc, 0.34 * hs], [-0.7 * sc, 0.3 * hs]], 0.36, 0.06, 0, 0xffffff, { gloss: true, paint: true });
  // leg shield + front column (paint)
  k.profile([[0.26 * sc, 0.3 * hs], [0.45 * sc, 0.3 * hs], [0.58 * sc, 0.62 * hs], [0.62 * sc, 0.98 * hs], [0.5 * sc, 1.02 * hs],
    [0.4 * sc, 0.64 * hs], [0.27 * sc, 0.38 * hs]], 0.42, 0.05, 0, 0xffffff, { gloss: true, paint: true });
  k.rbox(0.17, 0.08, 0.46 * sc, 0.03, 0, wr * 2 + 0.05, wz + 0.02, 0xffffff, { gloss: true, paint: true });   // front mudguard
  k.box(0.3, 0.06, 0.6 * sc, 0, 0.31 * hs, 0.08 * sc, 0x2d2d2e);                                           // floorboard
  k.rbox(0.31, 0.12, 0.74 * sc, 0.05, 0, 0.84 * hs, -0.46 * sc, 0x1a1a1b);                                 // seat
  k.box(0.2, 0.025, 0.08, 0, 0.79 * hs, -0.88 * sc, 0x3a3a3c, { gloss: true });                            // grab rail
  k.box(0.13, 0.06, 0.04, 0, 0.68 * hs, -0.955 * sc, 0xa3141a, { gloss: true });                           // tail lamp
  k.box(0.2, 0.1, 0.01, 0, 0.5 * hs, -0.955 * sc, 0xf4f4f0);                                               // plate
  for (const sx of [-1, 1]) k.box(0.035, 0.5 * hs, 0.05, sx * 0.07, 0.56 * hs, wz - 0.02, 0x8a8e93, { gloss: true }); // fork
  // handlebar, headlamp, mirrors
  k.box(0.66, 0.045, 0.05, 0, 1.06 * hs, 0.56 * sc, 0x2a2a2b);
  for (const sx of [-1, 1]) {
    k.cyl(0.022, 0.022, 0.12, sx * 0.36, 1.06 * hs, 0.56 * sc, 0x111111, 'x', 6);
    k.beam([sx * 0.2, 1.06 * hs, 0.55 * sc], [sx * 0.26, 1.3 * hs, 0.5 * sc], 0.015, 0x2a2a2b);
    k.rbox(0.11, 0.07, 0.025, 0.01, sx * 0.27, 1.32 * hs, 0.5 * sc, 0x1e1f20, { gloss: true });
  }
  k.rbox(0.26, 0.17, 0.2, 0.06, 0, 1.06 * hs, 0.62 * sc, 0xffffff, { gloss: true, paint: true });
  k.box(0.15, 0.08, 0.02, 0, 1.06 * hs, 0.72 * sc, 0xe9eef0, { gloss: true });
  k.beam([-0.1, 0.3 * hs, -0.05], [-0.28, 0.0, -0.1], 0.025, 0x2a2a2b);                                   // side stand
  // lean onto the side stand
  const lean = 0.11;
  for (const p of k.parts) if (p.mat !== 's') p.geo.rotateZ(lean);
  k.shadow(0.55, L * 0.95, 0.55, 0.3, 0.018, -0.08, 0);
}

function cone(k, s) {
  const H = s.h, W = s.w, base = 0.04;
  const r0 = W * 0.42, r1 = W * 0.08;
  const rAt = (y) => r0 + (r1 - r0) * ((y - base) / (H - base));
  const bands = [[base, 0.28, 'o'], [0.28, 0.42, 'w'], [0.42, 0.58, 'o'], [0.58, 0.69, 'w'], [0.69, 1.0, 'o']];
  for (const [a, b, kind] of bands) {
    const y0 = base + (H - base) * a, y1 = base + (H - base) * b;
    const g = new k.THREE.CylinderGeometry(rAt(y1), rAt(y0), y1 - y0, 16, 1, b < 1);
    g.translate(0, (y0 + y1) / 2, 0);
    if (kind === 'o') k.add(g, 0xf0560a, { paint: true });
    else k.add(g, 0xf2f2ee, { gloss: true });
  }
  k.rbox(W, base, W, 0.015, 0, base / 2, 0, 0x1e1e1f);
  k.cyl(rAt(base) + 0.012, rAt(base) + 0.02, 0.025, 0, base + 0.012, 0, 0x1e1e1f, 'y', 16);
  k.shadow(W * 0.95, W * 0.95, 0.5, 0.18);
}

function barricade(k, s) {
  // Bengaluru traffic-police barricade: steel tube frame on two feet, a striped sign band, a grille below
  const W = s.w, H = s.h, D = s.d, px = W / 2 - 0.05, tr = 0.028;
  for (const sx of [-1, 1]) {
    k.cyl(tr, tr, H, sx * px, H / 2, 0, 0x3c3f42, 'y', 8, { gloss: true });
    k.rbox(0.07, 0.05, D, 0.02, sx * px, 0.025, 0, 0x2c2e30);
    k.beam([sx * px, 0.05, D * 0.45], [sx * px, 0.42, 0], 0.025, 0x3c3f42);
    k.beam([sx * px, 0.05, -D * 0.45], [sx * px, 0.42, 0], 0.025, 0x3c3f42);
  }
  k.cyl(tr, tr, W - 0.1, 0, H - 0.02, 0, 0x3c3f42, 'x', 8, { gloss: true });
  k.cyl(tr, tr, W - 0.1, 0, H * 0.22, 0, 0x3c3f42, 'x', 8, { gloss: true });
  // sign band: alternating paint / black blocks, slanted by shifting the top edge (reads as diagonal hazard stripes)
  const n = 9, x0 = -px + 0.03, x1 = px - 0.03, bw = (x1 - x0) / n, yb = H * 0.55, yt = H * 0.93, slant = bw * 0.6;
  for (let i = 0; i < n; i++) {
    const a = x0 + i * bw, b = a + bw;
    const pts = [[a, yb], [b, yb], [Math.min(b + slant, x1), yt], [Math.min(a + slant, x1), yt]];
    if (i === 0) pts.splice(3, 0, [a, yt]);
    const sh = new k.THREE.Shape();
    pts.forEach(([u, v], j) => (j ? sh.lineTo(u, v) : sh.moveTo(u, v)));
    const g = new k.THREE.ExtrudeGeometry(sh, { depth: 0.03, bevelEnabled: false });
    g.translate(0, 0, -0.015);
    if (i % 2 === 0) k.add(g, 0xffffff, { paint: true, gloss: true });
    else k.add(g, 0x141414, { gloss: true });
  }
  // grille below the sign
  for (let x = -px + 0.12; x < px - 0.05; x += 0.13) k.box(0.018, yb - H * 0.22, 0.018, x, (yb + H * 0.22) / 2, 0, 0x4a4d50);
  k.shadow(W * 0.9, D * 0.85, 0.42, 0.25);
}

function pothole(k, s) {
  const R = s.w / 2, rnd = mulberry32(7);
  const ph = [rnd() * 6, rnd() * 6, rnd() * 6];
  const radius = (t) => R * (0.82 + 0.1 * Math.sin(3 * t + ph[0]) + 0.06 * Math.sin(5 * t + ph[1]) + 0.04 * Math.sin(11 * t + ph[2]));
  // wet, dirty halo around the hole (transparent), broken edge, sunken floor, standing water
  k.groundRing(radius, 0.9, 1.0, 0.03, 0.026, 0x3a362f, 0x5d574d);
  k.groundRing(radius, 0.55, 0.9, 0.02, 0.03, 0x24211d, 0x3a362f);
  k.groundRing(radius, 0.0, 0.55, 0.02, 0.02, 0x1d1b18, 0x24211d);
  const water = (t) => radius(t) * (0.95 + 0.05 * Math.sin(4 * t));
  k.groundRing(water, 0.0, 0.5, 0.032, 0.032, 0x47515a, 0x3a434b, { gloss: true });
  // loose asphalt chunks around the rim
  for (let i = 0; i < 9; i++) {
    const t = rnd() * Math.PI * 2, f = 0.95 + rnd() * 0.35, sz = 0.05 + rnd() * 0.08;
    const g = new k.THREE.BoxGeometry(sz, 0.025 + rnd() * 0.02, sz * (0.6 + rnd() * 0.6));
    g.rotateY(rnd() * 3); g.rotateX((rnd() - 0.5) * 0.4);
    g.translate(Math.cos(t) * radius(t) * f, 0.035, Math.sin(t) * radius(t) * f);
    k.add(g, rnd() > 0.5 ? 0x3e3c39 : 0x55524c, { ao: false });
  }
  // a halo of darker, cracked road (soft, transparent) so the hole sits in the asphalt rather than on it
  const pos = [], col = [], n = 36;
  for (let i = 0; i < n; i++) {
    const t0 = (i / n) * Math.PI * 2, t1 = ((i + 1) / n) * Math.PI * 2;
    const p = (t, f) => [Math.cos(t) * radius(t) * f, 0.024, Math.sin(t) * radius(t) * f];
    const a0 = p(t0, 0.98), a1 = p(t1, 0.98), b0 = p(t0, 1.45), b1 = p(t1, 1.45);
    for (const [q, al] of [[a0, 0.5], [b1, 0], [b0, 0], [a0, 0.5], [a1, 0.5], [b1, 0]]) { pos.push(...q); col.push(0.06, 0.055, 0.045, al); }
  }
  const g = new k.THREE.BufferGeometry();
  g.setAttribute('position', new k.THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('normal', new k.THREE.Float32BufferAttribute(pos.map((_, i) => (i % 3 === 1 ? 1 : 0)), 3));
  g.setAttribute('color', new k.THREE.Float32BufferAttribute(col, 4));
  k.parts.push({ geo: g, c: null, a: 1, mat: 's', paint: 0, ao: false });
}

function stall(k, s) {
  // push-cart (thallu gaadi): wooden bed on two bicycle wheels, heaped produce, a big umbrella on a pole
  const W = s.w, D = s.d, H = s.h, bedY = 0.8, rnd = mulberry32(11);
  const bw = W * 0.82, bd = D * 0.95;
  k.box(bw, 0.07, bd, 0, bedY, 0, 0x8b5e34);
  for (const sx of [-1, 1]) k.box(0.04, 0.12, bd, sx * (bw / 2 - 0.02), bedY + 0.09, 0, 0x6e4626);
  for (const sz of [-1, 1]) k.box(bw, 0.12, 0.04, 0, bedY + 0.09, sz * (bd / 2 - 0.02), 0x6e4626);
  k.box(bw * 0.9, 0.05, 0.06, 0, bedY - 0.08, -bd * 0.2, 0x3b3b3b);   // axle beam
  for (const sx of [-1, 1]) {
    const x = sx * (bw / 2 + 0.04), wy = 0.36, wz = -bd * 0.2;
    const tyre = new k.THREE.TorusGeometry(0.34, 0.025, 6, 22);
    tyre.rotateY(Math.PI / 2); tyre.translate(x, wy, wz);
    k.add(tyre, 0x1c1c1c);
    for (let i = 0; i < 6; i++) {
      const t = (i / 6) * Math.PI;
      k.beam([x, wy + Math.sin(t) * 0.33, wz + Math.cos(t) * 0.33], [x, wy - Math.sin(t) * 0.33, wz - Math.cos(t) * 0.33], 0.012, 0x9a9da1);
    }
    k.box(0.05, bedY - 0.04, 0.05, sx * (bw / 2 - 0.05), (bedY - 0.04) / 2, bd / 2 - 0.08, 0x5a3b22);   // front legs
    k.beam([sx * (bw / 2 - 0.1), bedY + 0.02, bd / 2], [sx * (bw / 2 - 0.12), bedY + 0.12, bd / 2 + 0.38], 0.045, 0x6e4626); // handles
  }
  // produce in four heaps (oranges, tomatoes, onions, greens / bananas), a weighing scale pan
  const heaps = [[-bw * 0.24, -bd * 0.22, 0xe8891c, 0.075], [bw * 0.24, -bd * 0.22, 0xc0281f, 0.07],
    [-bw * 0.24, bd * 0.2, 0x8e3a4f, 0.065], [bw * 0.22, bd * 0.2, 0x4f8a2b, 0.08]];
  for (const [hx, hz, hex, r] of heaps) {
    for (let layer = 0; layer < 3; layer++) {
      const n = [9, 5, 2][layer], spread = [0.17, 0.11, 0.05][layer];
      for (let i = 0; i < n; i++) {
        const t = (i / n) * Math.PI * 2 + layer, f = i === 0 && layer ? 0 : spread;
        k.sphere(r * (0.9 + rnd() * 0.2), hx + Math.cos(t) * f, bedY + 0.1 + r + layer * r * 1.4, hz + Math.sin(t) * f, hex, 1, 0.9, 1, { ao: false }, 7, 5);
      }
    }
  }
  k.cyl(0.13, 0.11, 0.03, bw * 0.02, bedY + 0.1, 0, 0xb8bcc0, 'y', 10, { gloss: true });
  // umbrella on a pole at the back corner
  const poleX = -bw * 0.38, poleZ = -bd * 0.4, top = H - 0.02;
  k.cyl(0.022, 0.022, top - bedY, poleX, (top + bedY) / 2, poleZ, 0x8f9397, 'y', 6, { gloss: true });
  const ur = Math.max(W, D) * 0.62, uh = 0.38;
  const canopy = new k.THREE.ConeGeometry(ur, uh, 10, 1, false);
  canopy.translate(poleX * 0.5, top - uh / 2, poleZ * 0.5);
  k.add(canopy, 0xffffff, { paint: true, ao: false });
  const rim = new k.THREE.CylinderGeometry(ur, ur, 0.1, 10, 1, true);
  rim.translate(poleX * 0.5, top - uh - 0.05, poleZ * 0.5);
  k.add(rim, 0xe8e8e2, { paint: true, ao: false });
  k.shadow(bw * 1.1, bd * 1.15, 0.5, 0.35);
  k.shadow(ur * 1.6, ur * 1.6, 0.16, 0.4, 0.016, poleX * 0.5 + 0.2, poleZ * 0.5 + 0.1);
}

function lowBranch(k, s) {
  // an overhanging branch: a tapering limb that comes down from a tree beside the footpath, twigs, leaf clusters
  const W = s.w, rnd = mulberry32(5), yLow = 1.05;
  const limb = [[W * 0.75, 2.9, 0.15], [W * 0.5, 2.05, 0.08], [W * 0.15, 1.62, 0.0], [-W * 0.2, 1.48, -0.04], [-W * 0.55, 1.42, 0.02]];
  k.tube(limb.slice(0, 3), 0.085, 0x4e3a2b, 12, 7);
  k.tube(limb.slice(2), 0.05, 0x5b4636, 12, 6);
  const twigs = [[[W * 0.32, 1.8, 0.05], [W * 0.2, 1.55, 0.3], [W * 0.05, 1.35, 0.42]], [[-W * 0.05, 1.53, -0.02], [-W * 0.15, 1.3, -0.3], [-W * 0.3, 1.22, -0.4]],
    [[-W * 0.3, 1.45, 0], [-W * 0.45, 1.3, 0.28], [-W * 0.6, 1.2, 0.32]], [[W * 0.1, 1.6, 0], [W * 0.02, 1.4, -0.32]]];
  for (const t of twigs) k.tube(t, 0.022, 0x5b4636, 6, 5);
  const greens = [0x3d6b2a, 0x4f7f33, 0x5a8c3a, 0x355f26, 0x6a9442];
  for (let i = 0; i < 20; i++) {
    const f = rnd(), x = W * (0.55 - f * 1.15), r = 0.16 + rnd() * 0.14;
    const y = Math.max(yLow + r * 0.55, 1.55 - f * 0.25 + (rnd() - 0.4) * 0.3);
    k.blob(r, x, y, (rnd() - 0.5) * 0.85, greens[i % greens.length], 1.15, 0.62, 1.1, { paint: true, ao: false });
  }
  k.shadow(W * 1.1, 1.1, 0.22, 0.5);
}

const MODELS = { parked_car: parkedCar, two_wheeler: twoWheeler, cone, barricade, pothole, stall, low_branch: lowBranch };

function genericModel(k, s) { k.rbox(s.w, s.h, s.d, 0.05, 0, s.h / 2, 0, 0x8a8f96); k.shadow(s.w, s.d); }

/** Build one type's merged geometry: groups [matte, gloss, shadow]; attributes position, normal, color (rgba), aPaint. */
export function buildObstacleGeometry(THREE, type, size) {
  const k = kit(THREE);
  (MODELS[type] || genericModel)(k, size);
  const order = { m: 0, g: 1, s: 2 };
  const parts = k.parts.slice().sort((a, b) => order[a.mat] - order[b.mat]);
  let n = 0;
  for (const p of parts) n += p.geo.getAttribute('position').count;
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), col = new Float32Array(n * 4), paint = new Float32Array(n);
  const out = new THREE.BufferGeometry();
  let o = 0, groupStart = 0, cur = null;
  for (const p of parts) {
    if (cur !== null && p.mat !== cur) { out.addGroup(groupStart, o - groupStart, order[cur]); groupStart = o; }
    cur = p.mat;
    const g = p.geo;
    if (!g.getAttribute('normal')) g.computeVertexNormals();
    const pa = g.getAttribute('position'), na = g.getAttribute('normal'), ca = g.getAttribute('color');
    for (let v = 0; v < pa.count; v++) {
      const y = pa.getY(v), i = o + v;
      pos[i * 3] = pa.getX(v); pos[i * 3 + 1] = y; pos[i * 3 + 2] = pa.getZ(v);
      nor[i * 3] = na.getX(v); nor[i * 3 + 1] = na.getY(v); nor[i * 3 + 2] = na.getZ(v);
      let c;
      if (p.cols) c = [p.cols[v * 3], p.cols[v * 3 + 1], p.cols[v * 3 + 2], 1];
      else if (ca) c = [ca.getX(v), ca.getY(v), ca.getZ(v), ca.itemSize > 3 ? ca.getW(v) : 1];
      else c = [...p.c, p.a];
      const ao = p.ao ? AO_FLOOR + (1 - AO_FLOOR) * Math.min(1, Math.max(0, y / AO_HEIGHT_M)) : 1;
      col[i * 4] = c[0] * ao; col[i * 4 + 1] = c[1] * ao; col[i * 4 + 2] = c[2] * ao; col[i * 4 + 3] = c[3];
      paint[i] = p.paint;
    }
    o += pa.count;
    g.dispose();
  }
  if (cur !== null) out.addGroup(groupStart, o - groupStart, order[cur]);
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  out.setAttribute('color', new THREE.BufferAttribute(col, 4));
  out.setAttribute('aPaint', new THREE.BufferAttribute(paint, 1));
  return out;
}

// Only paint vertices take the instance colour: vColor = mix(color, color * instanceColor, aPaint).
function paintOnly(material) {
  material.onBeforeCompile = (shader) => {
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nattribute float aPaint;')
      .replace('#include <color_vertex>', `#include <color_vertex>
#ifdef USE_INSTANCING_COLOR
  vColor.xyz = mix(color.xyz, color.xyz * instanceColor.xyz, aPaint);
#endif`);
  };
  material.customProgramCacheKey = () => 'hero-obstacle-paint';
  return material;
}

/** Shared materials: [matte, gloss, shadow]. */
export function createObstacleMaterials(THREE) {
  return [
    paintOnly(new THREE.MeshLambertMaterial({ vertexColors: true })),
    paintOnly(new THREE.MeshPhongMaterial({ vertexColors: true, shininess: 70, specular: 0x5a5a5a })),
    paintOnly(new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, polygonOffset: true,
      polygonOffsetFactor: -2, polygonOffsetUnits: -2 })),
  ];
}

/** createDynamicLayer(THREE, { scene, track, runner }) -> { group, update, reset, dispose } */
export function createDynamicLayer(THREE, { scene, track, runner }) {
  const gp = runner && runner.gp;
  if (!gp || !gp.obstacle_types) throw new Error('createDynamicLayer: runner.gp (gameplay.json) is required');
  const types = Object.keys(gp.obstacle_types);
  const group = new THREE.Group();
  group.name = 'DynamicObstacles';
  const materials = createObstacleMaterials(THREE);
  const meshes = {};
  const geometries = [];
  const palettes = {};
  for (const t of types) {
    const geo = buildObstacleGeometry(THREE, t, gp.obstacle_types[t].size_m);
    geometries.push(geo);
    const mesh = new THREE.InstancedMesh(geo, materials, CAPACITY);
    mesh.name = `dyn_${t}`;
    mesh.count = 0;
    mesh.visible = false;
    mesh.frustumCulled = false; // bounding sphere is the model's, not the instances'
    mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(CAPACITY * 3).fill(1), 3);
    meshes[t] = mesh;
    palettes[t] = (PALETTES[t] || [0xffffff]).map((h) => new THREE.Color(h));
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
      const v = o.variant || 0;
      const yaws = YAWS[o.type];
      pos.set(w.x, groundY(o), w.z);
      quat.setFromAxisAngle(up, -w.theta + (yaws ? yaws[v % yaws.length] : 0));
      mat.compose(pos, quat, one);
      mesh.setMatrixAt(i, mat);
      const pal = palettes[o.type], c = pal[v % pal.length];
      mesh.instanceColor.setXYZ(i, c.r, c.g, c.b);
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
    for (const m of materials) m.dispose();
    for (const t of types) meshes[t].dispose();
  }

  return {
    group, update, reset, dispose,
    get visibleIds() { return lastIds.slice(); },
    get drawCalls() { return types.reduce((n, t) => n + (meshes[t].visible ? meshes[t].geometry.groups.length : 0), 0); },
  };
}
