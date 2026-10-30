// Builds the Three.js world from a Scene JSON (v1 or v2) along a Track (path.js).
// Low-poly, Lambert/Basic materials, no post-processing. Draw-call budget (spec/ui.md): ground, footpath/kerb/
// carriageway strips, buildings and object boxes are each MERGED into one vertex-coloured mesh per copy;
// tokens are one InstancedMesh per copy; geometry/materials are shared by the three chained copies.
import * as THREE from '/vendor/three.module.js';
import { makeTrack } from '/js/game/path.js?v=4';

export const ACTION_COLORS = { jump: 0x4ea1ff, slide: 0xc07bff, dodge: 0xff4d4f, none: 0xd9d9d9 };
export const TYPE_COLORS = {
  parked_vehicle: 0xb8c0cc, pothole: 0x2b2b2b, open_drain: 0x1d3a4a, drain: 0x1d3a4a, debris: 0x8a6d3b, pole: 0x9aa0a6,
  tree: 0x2f7d3a, signboard: 0xffd166, low_branch: 0x4c8a3a, footpath_gap: 0x5a4a3a, crossing: 0xf2f2f2, ramp: 0xaaaaaa,
  bollard: 0xe8e8e8, wall: 0x8c8479, gate: 0x6a6f78, kerb_cut: 0xaaaaaa, bus_stop: 0x3d7cc9, shopfront: 0xc99a5b,
};
const FOOTPATH_COLORS = { intact: 0x8d8d8d, broken: 0x5e5248, blocked: 0xd9a23a, encroached: 0xd9a23a, unknown: 0x6d6d73 };
const STRIP_COLORS = {
  carriageway: 0x34373d, shoulder: 0x44474d, parking: 0x3f4248, cycle_lane: 0x3f6b5a, verge: 0x4f6b3a, planting: 0x3f6b30,
  median: 0x707070, kerb: 0x9a9a9a, forecourt: 0x7a6f62, unknown: 0x55585d, footpath: 0x8d8d8d,
};
const KERB_COLOR = 0xb9b9b9;
const EARTH_COLOR = 0x6b4f35;
const SCENERY_DIM = 0.45;
const MAX_TILE_PX = 1024;
const MAX_CROP_PX = 256;
const MAX_LABELS_PER_COPY = 6;
const COPIES = 3;
// ground_uv is tile (px / size), y down, same convention as tile_px; Three's flipped CanvasTexture wants 1 - v.
const uvY = (v) => 1 - v;

export function latToX(latM) { return latM; }
export function xmToZ(xM) { return -xM; }

function loadImage(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

// Downscale to <= maxPx on the longest side; returns a CanvasTexture or null when the image failed.
async function loadTexture(url, maxPx, tracker) {
  const img = url ? await loadImage(url) : null;
  tracker.done += 1;
  tracker.onProgress(tracker.done, tracker.total);
  if (!img) return null;
  const scale = Math.min(1, maxPx / Math.max(img.width, img.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(img.width * scale));
  canvas.height = Math.max(1, Math.round(img.height * scale));
  canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.minFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;
  return tex;
}

function textTexture(text, { color = '#ffffff', bg = 'rgba(0,0,0,0.6)', px = 48 } = {}) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  ctx.font = `bold ${px}px system-ui, sans-serif`;
  const w = Math.ceil(ctx.measureText(text).width) + 24;
  canvas.width = Math.max(32, w);
  canvas.height = px + 20;
  ctx.font = `bold ${px}px system-ui, sans-serif`;
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = color;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 12, canvas.height / 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return { tex, aspect: canvas.width / canvas.height };
}

function textSprite(text, opts = {}) {
  const { scale = 1, ...rest } = opts;
  const { tex, aspect } = textTexture(text, rest);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  sprite.scale.set(0.5 * aspect * scale, 0.5 * scale, 1);
  return sprite;
}

// ------------------------------------------------------------------ merged triangle-soup buffer

class Buf {
  constructor(withUv = false) { this.p = []; this.n = []; this.c = []; this.u = withUv ? [] : null; this.tris = 0; }

  tri(a, b, c, col, uv) {
    const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
    const vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    for (const v of [a, b, c]) { this.p.push(v[0], v[1], v[2]); this.n.push(nx, ny, nz); this.c.push(col.r, col.g, col.b); }
    if (this.u) this.u.push(...(uv || [0, 0, 0, 0, 0, 0]));
    this.tris += 1;
  }

  quad(a, b, c, d, col, uv) {
    this.tri(a, b, c, col, uv && [uv[0], uv[1], uv[2], uv[3], uv[4], uv[5]]);
    this.tri(a, c, d, col, uv && [uv[0], uv[1], uv[4], uv[5], uv[6], uv[7]]);
  }

  /** Append a (transformed) three geometry; colour is flat per call. */
  geometry(geo, matrix, col) {
    const g = geo.index ? geo.toNonIndexed() : geo.clone();
    g.applyMatrix4(matrix);
    const pos = g.getAttribute('position');
    const uv = g.getAttribute('uv');
    for (let i = 0; i < pos.count; i += 3) {
      const v = [0, 1, 2].map((k) => [pos.getX(i + k), pos.getY(i + k), pos.getZ(i + k)]);
      this.tri(v[0], v[1], v[2], col, this.u && uv ? [uv.getX(i), uv.getY(i), uv.getX(i + 1), uv.getY(i + 1), uv.getX(i + 2), uv.getY(i + 2)] : null);
    }
    g.dispose();
  }

  get empty() { return this.tris === 0; }

  mesh(material, name) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(this.p, 3));
    geo.setAttribute('normal', new THREE.Float32BufferAttribute(this.n, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(this.c, 3));
    if (this.u) geo.setAttribute('uv', new THREE.Float32BufferAttribute(this.u, 2));
    geo.computeBoundingSphere();
    const m = new THREE.Mesh(geo, material);
    m.name = name;
    return m;
  }
}

const colour = (hex, k = 1) => { const c = new THREE.Color(hex); if (k !== 1) c.multiplyScalar(k); return c; };

// ------------------------------------------------------------------ ground

function buildGround(track, tileTex) {
  const st = track.stations, sm = track.samples, h = track.groundHalf;
  const pos = [], uv = [], idx = [];
  const useUv = !!tileTex;
  for (let i = 0; i < st.length; i++) {
    const p = sm[i], c = Math.cos(p.theta), s = Math.sin(p.theta);
    pos.push(p.x - h * c, 0, p.z - h * s, p.x + h * c, 0, p.z + h * s);
    const g = st[i].ground_uv;
    if (useUv && g) uv.push(g[0][0], uvY(g[0][1]), g[1][0], uvY(g[1][1])); else uv.push(0, 0, 0, 0);
    if (i > 0) { const a = 2 * (i - 1); idx.push(a, a + 1, a + 3, a, a + 3, a + 2); }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  geo.computeBoundingSphere();
  const mat = tileTex ? new THREE.MeshBasicMaterial({ map: tileTex }) : new THREE.MeshBasicMaterial({ color: 0x3a3f47 });
  mat.polygonOffset = true; mat.polygonOffsetFactor = 1; mat.polygonOffsetUnits = 1;
  const mesh = new THREE.Mesh(geo, mat);
  mesh.name = 'ground';
  return mesh;
}

// ------------------------------------------------------------------ strips (footpath, kerb, carriageway ...)

function buildStrips(track) {
  const buf = new Buf();
  const st = track.stations, sm = track.samples;
  const pt = (i, lat, y) => {
    const p = sm[i];
    return [p.x + lat * Math.cos(p.theta), y, p.z + lat * Math.sin(p.theta)];
  };
  // A, B (station i, lo/hi) -> C, D (station i+1): up-facing winding.
  const ribbon = (i, lo0, hi0, lo1, hi1, y, col) =>
    buf.quad(pt(i, lo0, y), pt(i, hi0, y), pt(i + 1, hi1, y), pt(i + 1, lo1, y), col);
  for (let i = 0; i < st.length - 1; i++) {
    const a = st[i], b = st[i + 1];
    // 1. non-footpath strips (carriageway darker, verges ...), matched by kind
    for (const sa of a.strips) {
      const sb = b.strips.find((x) => x.kind === sa.kind) || sa;
      ribbon(i, sa.lo, sa.hi, sb.lo, sb.hi, 0.02, colour(STRIP_COLORS[sa.kind] ?? STRIP_COLORS.unknown));
    }
    // 2. footpath: raised top coloured by state + a kerb rib along its road-side edge; missing = a gap showing earth
    const fa = a.footpath, fb = b.footpath;
    if (fa && fb) {
      const ha = Math.max(0.1, fa.height_m || 0), hb = Math.max(0.1, fb.height_m || 0);
      ribbon(i, fa.lo, fa.hi, fb.lo, fb.hi, (ha + hb) / 2, colour(FOOTPATH_COLORS[fa.state] ?? FOOTPATH_COLORS.unknown));
      const k = colour(KERB_COLOR);
      // rib on the lo (road-side) edge, facing the road (-lat)
      buf.quad(pt(i + 1, fb.lo, 0), pt(i, fa.lo, 0), pt(i, fa.lo, ha), pt(i + 1, fb.lo, hb), k);
    } else {
      const c0 = a.lane_lat[0], c1 = b.lane_lat[0];
      ribbon(i, c0 - 0.8, c0 + 0.8, c1 - 0.8, c1 + 0.8, 0.03, colour(EARTH_COLOR));
    }
  }
  const mat = new THREE.MeshLambertMaterial({ vertexColors: true });
  return buf.mesh(mat, 'strips');
}

// ------------------------------------------------------------------ buildings

function subEdges(points, maxStep) {
  const out = [];
  for (let i = 0; i < points.length; i++) {
    const p = points[i], q = points[(i + 1) % points.length];
    const n = Math.max(1, Math.ceil(Math.hypot(q[0] - p[0], q[1] - p[1]) / maxStep));
    for (let k = 0; k < n; k++) {
      out.push({ edge: i, a: [p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n],
        b: [p[0] + (q[0] - p[0]) * (k + 1) / n, p[1] + (q[1] - p[1]) * (k + 1) / n] });
    }
  }
  return out;
}

// Footprints are [s, lat]; each edge is cut every 2.5 m of path so walls follow the bend.
function buildBuildings(track, scene, facadeTexs) {
  const walls = new Buf();
  const facades = [];
  scene.buildings.forEach((b, bi) => {
    const col = colour(b.color_hex);
    const roofCol = colour(b.color_hex, 0.8);
    const fp = b.footprint;
    // street-facing edge = nearest to the road centreline among edges running along s (only used with a facade)
    let facadeEdge = -1;
    if (facadeTexs[bi]) {
      let best = Infinity;
      for (let i = 0; i < fp.length; i++) {
        const p = fp[i], q = fp[(i + 1) % fp.length];
        if (Math.abs(q[0] - p[0]) < Math.abs(q[1] - p[1]) || Math.abs(q[0] - p[0]) < 1) continue;
        const d = Math.abs((p[1] + q[1]) / 2);
        if (d < best) { best = d; facadeEdge = i; }
      }
    }
    let subs = subEdges(fp, 2.5).map((e) => ({ ...e, wa: track.world(e.a[0], e.a[1]), wb: track.world(e.b[0], e.b[1]) }));
    // outward-facing walls need negative shoelace area in (x, z)
    let area = 0;
    for (const e of subs) area += e.wa.x * e.wb.z - e.wb.x * e.wa.z;
    if (area > 0) subs = subs.reverse().map((e) => ({ edge: e.edge, a: e.b, b: e.a, wa: e.wb, wb: e.wa }));
    const H = b.height_m;
    const fac = [];
    for (const e of subs) {
      const q = [[e.wa.x, 0, e.wa.z], [e.wb.x, 0, e.wb.z], [e.wb.x, H, e.wb.z], [e.wa.x, H, e.wa.z]];
      if (e.edge === facadeEdge) fac.push({ q, sa: e.a[0], sb: e.b[0] }); else walls.quad(q[0], q[1], q[2], q[3], col);
    }
    if (fac.length) {
      const fb = new Buf(true);
      const lo = Math.min(...fac.map((f) => Math.min(f.sa, f.sb))), hi = Math.max(...fac.map((f) => Math.max(f.sa, f.sb)));
      const u = (s) => (hi > lo ? (s - lo) / (hi - lo) : 0);
      for (const f of fac) fb.quad(f.q[0], f.q[1], f.q[2], f.q[3], new THREE.Color(0xffffff), [u(f.sa), 0, u(f.sb), 0, u(f.sb), 1, u(f.sa), 1]);
      facades.push({ buf: fb, tex: facadeTexs[bi], id: b.id });
    }
    // roof: triangulate the (x, z) ring
    const contour = subs.map((e) => new THREE.Vector2(e.wa.x, e.wa.z));
    const tris = THREE.ShapeUtils.triangulateShape(contour, []);
    for (const [i0, i1, i2] of tris) {
      const A = [contour[i0].x, H, contour[i0].y], B = [contour[i1].x, H, contour[i1].y], C = [contour[i2].x, H, contour[i2].y];
      const crossY = (B[2] - A[2]) * (C[0] - A[0]) - (B[0] - A[0]) * (C[2] - A[2]);
      if (crossY >= 0) walls.tri(A, B, C, roofCol); else walls.tri(A, C, B, roofCol);
    }
  });
  const out = [];
  if (!walls.empty) out.push(walls.mesh(new THREE.MeshLambertMaterial({ vertexColors: true }), 'buildings'));
  for (const f of facades) out.push(f.buf.mesh(new THREE.MeshLambertMaterial({ map: f.tex }), `facade:${f.id}`));
  return out;
}

// ------------------------------------------------------------------ objects

const FLAT_TYPES = new Set(['crossing', 'ramp', 'kerb_cut']);
const UNIT_BOX = new THREE.BoxGeometry(1, 1, 1);

function laneCenterLat(track, scene, obj, sC) {
  if (typeof obj.lateral_m === 'number') return obj.lateral_m;
  const lats = obj.lanes.map((i) => track.laneLat(sC, i));
  return lats.reduce((a, b) => a + b, 0) / lats.length;
}

function buildObjects(track, scene, billboardTexs, questionTex) {
  const v2 = scene.schema_version === 2;
  const boxes = new Buf();
  const outlines = new Buf();
  const badges = new Buf(true);
  const extras = [];
  let labels = 0;
  scene.objects.forEach((obj, i) => {
    const sC = Math.min(track.length - 0.01, obj.x_m + obj.length_m / 2);
    const lat = laneCenterLat(track, scene, obj, sC);
    const w = track.world(sC, lat);
    const yaw = -w.theta + (v2 ? THREE.MathUtils.degToRad(obj.yaw_deg || 0) : 0);
    const dim = obj.scenery ? SCENERY_DIM : 1;
    const bw = Math.max(obj.size_m.w, 0.4), bh = Math.max(obj.size_m.h, 0.05), bd = Math.max(obj.length_m, 0.4);
    const rot = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, yaw, 0));
    const col = colour(TYPE_COLORS[obj.type] ?? ACTION_COLORS[obj.action] ?? 0xffffff, dim);

    if (v2 ? FLAT_TYPES.has(obj.type) : obj.action === 'none') {
      // crossing / ramp: flat strip across its lanes.
      const m = new THREE.Matrix4().compose(new THREE.Vector3(w.x, 0.05, w.z), rot, new THREE.Vector3(bw, 0.02, bd));
      boxes.geometry(UNIT_BOX, m, col);
      return;
    }
    if (v2 && obj.type === 'footpath_gap') return; // the corridor already draws the gap (earth)

    const tex = billboardTexs[i];
    let top = Math.max(bh, 0.15);
    if (tex) {
      const aspect = tex.image.width / tex.image.height;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: obj.scenery, opacity: obj.scenery ? 0.55 : 1 }));
      const sh = Math.max(bh, 0.5);
      sprite.scale.set(Math.max(bw, sh * aspect), sh, 1);
      sprite.position.set(w.x, sh / 2, w.z);
      sprite.name = `billboard:${obj.id}`;
      extras.push(sprite);
      top = sh;
    } else {
      const hh = Math.max(bh, 0.15);
      const m = new THREE.Matrix4().compose(new THREE.Vector3(w.x, hh / 2, w.z), rot, new THREE.Vector3(bw, hh, bd));
      boxes.geometry(UNIT_BOX, m, col);
      if (!obj.scenery && labels < MAX_LABELS_PER_COPY) {
        const label = textSprite(obj.label, { bg: 'rgba(0,0,0,0.65)' });
        label.position.set(w.x, hh + 0.5, w.z);
        extras.push(label);
        labels += 1;
      }
    }
    if (obj.confidence < 0.6) {
      const oh = top + 0.2;
      const m = new THREE.Matrix4().compose(new THREE.Vector3(w.x, oh / 2, w.z), rot, new THREE.Vector3(bw + 0.2, oh, bd + 0.2));
      outlines.geometry(UNIT_BOX, m, colour(0x9aa0a6));
      // "?" badge: a quad facing back along the path (towards the camera)
      const bm = new THREE.Matrix4().compose(
        new THREE.Vector3(w.x + Math.cos(w.theta) * bw / 2, Math.max(top, 0.5) + 0.9, w.z + Math.sin(w.theta) * bw / 2),
        new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -w.theta, 0)), new THREE.Vector3(0.7, 0.7, 1));
      badges.geometry(new THREE.PlaneGeometry(1, 1), bm, new THREE.Color(0xffffff));
    }
  });
  const out = [];
  if (!boxes.empty) out.push(boxes.mesh(new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true }), 'objects'));
  if (!outlines.empty) {
    out.push(outlines.mesh(new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.25, depthWrite: false }), 'outlines'));
  }
  if (!badges.empty) {
    out.push(badges.mesh(new THREE.MeshBasicMaterial({ map: questionTex, transparent: true, side: THREE.DoubleSide }), 'badges'));
  }
  return [...out, ...extras];
}

// ------------------------------------------------------------------ tokens (InstancedMesh per copy)

const _m = new THREE.Matrix4(), _q = new THREE.Quaternion(), _e = new THREE.Euler(), _v = new THREE.Vector3(), _s = new THREE.Vector3(1, 1, 1);
const ZERO = new THREE.Matrix4().makeScale(0, 0, 0);

function buildTokenSet(track, scene, coinGeo, coinMat) {
  const defs = scene.tokens.map((tok) => {
    const w = track.world(tok.x_m, track.laneLat(tok.x_m, tok.lane));
    return { id: tok.id, x: w.x, z: w.z, theta: w.theta };
  });
  const mesh = new THREE.InstancedMesh(coinGeo, coinMat, Math.max(1, defs.length));
  mesh.count = defs.length;
  mesh.frustumCulled = false;
  mesh.name = 'tokens';
  return { mesh, defs, hidden: new Set(), index: new Map(defs.map((d, i) => [d.id, i])) };
}

function updateTokenSet(set, t) {
  for (let i = 0; i < set.defs.length; i++) {
    const d = set.defs[i];
    if (set.hidden.has(d.id)) { set.mesh.setMatrixAt(i, ZERO); continue; }
    _e.set(Math.PI / 2, t * 3 - d.theta, 0, 'YXZ');
    _q.setFromEuler(_e);
    _v.set(d.x, 0.9, d.z);
    set.mesh.setMatrixAt(i, _m.compose(_v, _q, _s));
  }
  set.mesh.instanceMatrix.needsUpdate = true;
}

// ------------------------------------------------------------------ world

// Returns {world, track, hideToken(id), showAllTokens(), tick(t), tileLoaded}.
// Three rigid copies of the segment are chained along the path (current, next, next+1): the camera sees ~60 m
// of fog range ahead, so on a short segment fewer copies would show void past the last one.
export async function buildWorld(scene, onProgress) {
  const track = makeTrack(scene);
  const urls = [scene.ground.tile_url, ...scene.buildings.map((b) => b.facade_url), ...scene.objects.map((o) => o.billboard_url)]
    .filter(Boolean);
  const tracker = { done: 0, total: urls.length, onProgress: onProgress || (() => {}) };
  tracker.onProgress(0, tracker.total);

  const [tileTex, facadeTexs, billboardTexs] = await Promise.all([
    loadTexture(scene.ground.tile_url, MAX_TILE_PX, tracker),
    Promise.all(scene.buildings.map((b) => (b.facade_url ? loadTexture(b.facade_url, MAX_CROP_PX, tracker) : null))),
    Promise.all(scene.objects.map((o) => (o.billboard_url ? loadTexture(o.billboard_url, MAX_CROP_PX, tracker) : null))),
  ]);

  const q = textTexture('?', { color: '#000', bg: '#9aa0a6', px: 64 });
  const segment = new THREE.Group();
  segment.name = 'segment';
  segment.add(buildGround(track, tileTex));
  segment.add(buildStrips(track));
  for (const m of buildBuildings(track, scene, facadeTexs)) segment.add(m);
  for (const m of buildObjects(track, scene, billboardTexs, q.tex)) segment.add(m);

  const coinGeo = new THREE.CylinderGeometry(0.35, 0.35, 0.08, 12);
  const coinMat = new THREE.MeshLambertMaterial({ color: 0xffb100, emissive: 0x553a00 });

  const world = new THREE.Group();
  const tokenSets = [];
  for (let k = 0; k < COPIES; k++) {
    const copy = k === 0 ? segment : segment.clone(true);
    const p = track.applyCopy({ x: 0, z: 0, theta: 0 }, k);
    copy.position.set(p.x, 0, p.z);
    copy.rotation.y = -p.theta;
    const set = buildTokenSet(track, scene, coinGeo, coinMat);
    copy.add(set.mesh);
    // token positions are computed in the segment's local frame (the copy transform is applied by the group)
    tokenSets.push(set);
    world.add(copy);
  }
  const tick = (t) => { for (const set of tokenSets) updateTokenSet(set, t); };
  tick(0);
  return {
    world, track, tileLoaded: !!tileTex,
    hideToken(id) { const s = tokenSets[0]; if (s.index.has(id)) s.hidden.add(id); },
    showAllTokens() { for (const s of tokenSets) s.hidden.clear(); },
    tick,
  };
}

export function makeRunnerMesh() {
  const group = new THREE.Group();
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.32, 0.7, 2, 8),
    new THREE.MeshLambertMaterial({ color: 0xffb100, flatShading: true }));
  body.position.y = 0.67;
  group.add(body);
  const shadow = new THREE.Mesh(new THREE.CircleGeometry(0.35, 12),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35 }));
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.02;
  group.add(shadow);
  group.userData.body = body;
  group.userData.shadow = shadow;
  return group;
}
