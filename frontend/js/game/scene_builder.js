// Builds the Three.js world from a Scene JSON: x_m -> -z, lat_m -> +x (spec/data.md).
// Low-poly, Lambert/Basic materials, no post-processing. One segment group is built and cloned
// for the next loop so geometry/materials are shared.
import * as THREE from '/vendor/three.module.js';

export const ACTION_COLORS = { jump: 0x4ea1ff, slide: 0xc07bff, dodge: 0xff4d4f, none: 0xd9d9d9 };
const TYPE_COLORS = {
  parked_vehicle: 0xb8c0cc, pothole: 0x2b2b2b, open_drain: 0x1d3a4a, debris: 0x8a6d3b, pole: 0x9aa0a6,
  tree: 0x2f7d3a, signboard: 0xffd166, low_branch: 0x4c8a3a, footpath_gap: 0x5a4a3a, crossing: 0xf2f2f2, ramp: 0xaaaaaa,
};
const MAX_TILE_PX = 1024;
const MAX_CROP_PX = 256;
const GROUND_LAT_MIN = -40;
const GROUND_LAT_MAX = 40;

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

function textSprite(text, { color = '#ffffff', bg = 'rgba(0,0,0,0.6)', px = 48, scale = 1 } = {}) {
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
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  const aspect = canvas.width / canvas.height;
  sprite.scale.set(0.5 * aspect * scale, 0.5 * scale, 1);
  return sprite;
}

function laneCenterLat(scene, lanes) {
  const lats = lanes.map((i) => scene.cross_section.lanes[i].lat_m);
  return lats.reduce((a, b) => a + b, 0) / lats.length;
}

function buildGround(scene, tileTex) {
  const { frame_to_px: f, size_px } = scene.ground;
  const L = scene.segment.length_m;
  const corners = [[0, GROUND_LAT_MIN], [L, GROUND_LAT_MIN], [L, GROUND_LAT_MAX], [0, GROUND_LAT_MAX]];
  const pos = [];
  const uv = [];
  for (const [xm, lat] of corners) {
    pos.push(latToX(lat), 0, xmToZ(xm));
    const px = f.origin_px[0] + xm * f.along_px_per_m[0] + lat * f.across_px_per_m[0];
    const py = f.origin_px[1] + xm * f.along_px_per_m[1] + lat * f.across_px_per_m[1];
    uv.push(px / size_px[0], 1 - py / size_px[1]);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  geo.setIndex([0, 2, 1, 0, 3, 2]);
  geo.computeVertexNormals();
  const mat = tileTex ? new THREE.MeshBasicMaterial({ map: tileTex }) : new THREE.MeshBasicMaterial({ color: 0x3a3f47 });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.name = 'ground';
  return mesh;
}

function buildCoverageStrips(scene) {
  const group = new THREE.Group();
  const lane0 = scene.cross_section.lanes[0];
  const colors = { intact: 0x8d8d8d, broken: 0x5e5248, missing: null };
  for (const range of scene.cross_section.footpath_coverage) {
    const color = colors[range.state];
    if (color === null || color === undefined) continue;
    const len = range.to_m - range.from_m;
    const geo = new THREE.PlaneGeometry(lane0.width_m, len);
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color }));
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(latToX(lane0.lat_m), 0.03, xmToZ(range.from_m + len / 2));
    group.add(mesh);
  }
  return group;
}

function buildBuilding(b, facadeTex) {
  const xs = b.footprint.map((p) => p[0]);
  const lats = b.footprint.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const depth = Math.max(0.5, maxX - minX), width = Math.max(0.5, maxLat - minLat);
  const geo = new THREE.BoxGeometry(width, b.height_m, depth);
  const base = new THREE.MeshLambertMaterial({ color: new THREE.Color(b.color_hex), flatShading: true });
  let material = base;
  if (facadeTex) {
    const facade = new THREE.MeshLambertMaterial({ map: facadeTex });
    // BoxGeometry faces: +x, -x, +y, -y, +z, -z. Street-facing = toward the road centreline (lat 0).
    const streetFaceIndex = (minLat + maxLat) / 2 >= 0 ? 1 : 0;
    material = [base, base, base, base, base, base];
    material[streetFaceIndex] = facade;
  }
  const mesh = new THREE.Mesh(geo, material);
  mesh.position.set(latToX((minLat + maxLat) / 2), b.height_m / 2, xmToZ((minX + maxX) / 2));
  mesh.name = `building:${b.id}`;
  return mesh;
}

function buildObject(scene, obj, billboardTex) {
  const group = new THREE.Group();
  group.name = `object:${obj.id}`;
  const lat = laneCenterLat(scene, obj.lanes);
  const z = xmToZ(obj.x_m + obj.length_m / 2);
  const w = Math.max(obj.size_m.w, 0.4), h = Math.max(obj.size_m.h, 0.05), d = Math.max(obj.length_m, 0.4);

  if (obj.action === 'none') {
    // crossing / ramp: flat strip across its lanes.
    const strip = new THREE.Mesh(new THREE.PlaneGeometry(w, d),
      new THREE.MeshLambertMaterial({ color: TYPE_COLORS[obj.type] || 0xffffff, transparent: true, opacity: 0.85 }));
    strip.rotation.x = -Math.PI / 2;
    strip.position.set(latToX(lat), 0.05, z);
    group.add(strip);
    return group;
  }

  if (billboardTex) {
    const aspect = billboardTex.image.width / billboardTex.image.height;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: billboardTex, transparent: true }));
    const sh = Math.max(h, 0.5);
    sprite.scale.set(Math.max(w, sh * aspect), sh, 1);
    sprite.position.set(latToX(lat), sh / 2, z);
    group.add(sprite);
  } else {
    const box = new THREE.Mesh(new THREE.BoxGeometry(w, Math.max(h, 0.15), d),
      new THREE.MeshLambertMaterial({ color: TYPE_COLORS[obj.type] || ACTION_COLORS[obj.action], flatShading: true }));
    box.position.set(latToX(lat), Math.max(h, 0.15) / 2, z);
    group.add(box);
    const label = textSprite(obj.label, { bg: 'rgba(0,0,0,0.65)' });
    label.position.set(latToX(lat), Math.max(h, 0.15) + 0.5, z);
    group.add(label);
  }
  if (obj.confidence < 0.6) {
    const outline = new THREE.Mesh(new THREE.BoxGeometry(w + 0.2, Math.max(h, 0.15) + 0.2, d + 0.2),
      new THREE.MeshBasicMaterial({ color: 0x9aa0a6, transparent: true, opacity: 0.25, depthWrite: false }));
    outline.position.set(latToX(lat), (Math.max(h, 0.15) + 0.2) / 2, z);
    group.add(outline);
    const badge = textSprite('?', { color: '#000', bg: '#9aa0a6', scale: 0.8 });
    badge.position.set(latToX(lat) + w / 2, Math.max(h, 0.5) + 0.9, z);
    group.add(badge);
  }
  return group;
}

function buildToken(scene, tok, coinGeo, coinMat) {
  const mesh = new THREE.Mesh(coinGeo, coinMat);
  mesh.name = `token:${tok.id}`;
  mesh.rotation.x = Math.PI / 2;
  mesh.position.set(latToX(scene.cross_section.lanes[tok.lane].lat_m), 0.9, xmToZ(tok.x_m));
  return mesh;
}

// Returns {world, tokensByCopy:[Map, Map, Map], allTokens:[...], tileLoaded}.
// Three copies of the segment are chained along -z (current, next, next+1): the camera sees ~60 m of
// fog range ahead, so on a 30 m segment two copies would show void past the second one.
export async function buildWorld(scene, onProgress) {
  const urls = [scene.ground.tile_url, ...scene.buildings.map((b) => b.facade_url), ...scene.objects.map((o) => o.billboard_url)]
    .filter(Boolean);
  const tracker = { done: 0, total: urls.length, onProgress: onProgress || (() => {}) };
  tracker.onProgress(0, tracker.total);

  const [tileTex, facadeTexs, billboardTexs] = await Promise.all([
    loadTexture(scene.ground.tile_url, MAX_TILE_PX, tracker),
    Promise.all(scene.buildings.map((b) => (b.facade_url ? loadTexture(b.facade_url, MAX_CROP_PX, tracker) : null))),
    Promise.all(scene.objects.map((o) => (o.billboard_url ? loadTexture(o.billboard_url, MAX_CROP_PX, tracker) : null))),
  ]);

  const segment = new THREE.Group();
  segment.name = 'segment';
  segment.add(buildGround(scene, tileTex));
  segment.add(buildCoverageStrips(scene));
  scene.buildings.forEach((b, i) => segment.add(buildBuilding(b, facadeTexs[i])));
  scene.objects.forEach((o, i) => segment.add(buildObject(scene, o, billboardTexs[i])));
  const coinGeo = new THREE.CylinderGeometry(0.35, 0.35, 0.08, 12);
  const coinMat = new THREE.MeshLambertMaterial({ color: 0xffb100, emissive: 0x553a00 });
  const tokenMeshes = [];
  for (const tok of scene.tokens) {
    const m = buildToken(scene, tok, coinGeo, coinMat);
    tokenMeshes.push(m);
    segment.add(m);
  }

  const L = scene.segment.length_m;
  const copyA = segment;
  const copyB = segment.clone(true);
  copyB.position.z = xmToZ(L);
  const copyC = segment.clone(true);
  copyC.position.z = xmToZ(2 * L);
  const world = new THREE.Group();
  world.add(copyA, copyB, copyC);

  const tokensByCopy = [copyA, copyB, copyC].map((g) => {
    const map = new Map();
    g.traverse((n) => { if (n.name.startsWith('token:')) map.set(n.name.slice(6), n); });
    return map;
  });
  const allTokens = tokensByCopy.flatMap((m) => [...m.values()]);
  return { world, tokensByCopy, allTokens, tileLoaded: !!tileTex };
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
