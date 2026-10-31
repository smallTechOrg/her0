// pano_backdrop.js: the "walk the real street" look. The stitched Street View panorama nearest to the camera is
// projected onto the street geometry (road, footpaths, kerb, ground, frontage walls) from the point where Google's
// camera stood, and the sky dome shows the same sphere around the camera; as the runner moves, the picture
// cross-fades to the next panorama - the same experience as stepping along Street View on a map, with the same
// smears where the photo and the geometry disagree (parked cars painted on the road, trees on walls).
//
// Data: GET /api/segments/<slug>/panoramas.json (hero-panoramas/1: panoramas with s / t on the reference line,
// stitched by the export step). Textures load lazily: the panorama ahead and the one after it are prefetched, a
// small LRU keeps the rest. Without panoramas (404, no Street View yet) nothing here is applied and the baked
// textures stay.
//
// Frame: everything is sampled in WORLD space, so one material serves every chained copy of the street. A panorama
// that belongs to street copy k sits at applyCopy(world(s, t), k) and its compass frame is rotated by that copy's
// yaw; the fragment shader turns the direction from the projection centre into (bearing, elevation) and reads the
// equirect (column 0 = north, row 0 = up, see hero.imagery.panorama).
import * as THREE from 'three';

export const PANO_NODES = ['Road', 'Footpath', 'Footpath_Opposite', 'Kerb', 'Ground_L', 'Ground_R', 'Frontage_L', 'Frontage_R', 'Frontage_Caps'];
// Ground nodes carry the glb ground UVs (u = (14 - t) / 28, v = (L - s) / L): with `manifest.ground` they get the
// FROZEN ground baked from the panoramas (one static photo, never swims) instead of the live projection.
export const GROUND_NODES = ['Road', 'Footpath', 'Footpath_Opposite', 'Ground_L', 'Ground_R'];
// Decorative furniture the photo already shows (painted crossings, railings): hidden in photo mode. Poles, trees
// and boxes stay: they carry the colliders, and a hit must be visible.
export const PHOTO_HIDDEN_NODES = ['Furniture_Decals', 'Furniture_Railings'];
export const PANO_SCHEMA = 'hero-panoramas/1';
const PREFETCH_AHEAD = 2;
const CACHE_MAX = 6;
const DOME_RADIUS = 120;        // < camera far plane (140)
const MIX_LO = 0.3, MIX_HI = 0.7;   // cross-fade window between two panoramas (fraction of the gap)
const ANISOTROPY = 8;

const GLSL_COMMON = `
const float PI = 3.141592653589793;
const float TAU = 6.283185307179586;
uniform sampler2D uPanoA;
uniform sampler2D uPanoB;
uniform float uRotA;       // bearing offset: compass bearing = atan(d.x, -d.z) + uRot
uniform float uRotB;
uniform float uMix;
uniform float uExposure;
vec2 panoUv(vec3 d, float rot) {
  float b = atan(d.x, -d.z) + rot;
  return vec2(b / TAU, 0.5 - asin(clamp(d.y, -1.0, 1.0)) / PI);
}
// equirect lookup with seam-free mip derivatives (the wrap at u = 0/1 must not pick the smallest mip)
vec3 panoSample(sampler2D t, vec3 d, float rot) {
  vec2 uv = panoUv(d, rot);
  vec2 uvA = vec2(fract(uv.x), uv.y), uvB = vec2(fract(uv.x + 0.5), uv.y);
  vec2 dxA = dFdx(uvA), dyA = dFdy(uvA), dxB = dFdx(uvB), dyB = dFdy(uvB);
  bool useA = dot(dxA, dxA) + dot(dyA, dyA) <= dot(dxB, dxB) + dot(dyB, dyB);
  return textureGrad(t, uvA, useA ? dxA : dxB, useA ? dyA : dyB).rgb;
}
vec3 toSrgb(vec3 c) {
  return mix(c * 12.92, 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}
`;

const PROJECT_VERT = `
uniform vec3 uCentreA;
uniform vec3 uCentreB;
out vec3 vDirA;
out vec3 vDirB;
out float vY;
void main() {
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vDirA = wp.xyz - uCentreA;
  vDirB = wp.xyz - uCentreB;
  vY = wp.y;
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;

const PROJECT_FRAG = `
precision highp float;
${GLSL_COMMON}
in vec3 vDirA;
in vec3 vDirB;
in float vY;
out vec4 outColor;
void main() {
  vec3 a = panoSample(uPanoA, normalize(vDirA), uRotA);
  vec3 b = panoSample(uPanoB, normalize(vDirB), uRotB);
  vec3 c = mix(a, b, uMix) * uExposure;
  outColor = vec4(toSrgb(c), 1.0);
}`;

const DOME_VERT = `
out vec3 vDir;
void main() {
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vDir = wp.xyz - cameraPosition;
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;

const DOME_FRAG = `
precision highp float;
${GLSL_COMMON}
in vec3 vDir;
out vec4 outColor;
void main() {
  vec3 d = normalize(vDir);
  vec3 c = mix(panoSample(uPanoA, d, uRotA), panoSample(uPanoB, d, uRotB), uMix) * uExposure;
  outColor = vec4(toSrgb(c), 1.0);
}`;

function blankTexture() {
  const data = new Uint8Array([128, 128, 132, 255]);
  const tex = new THREE.DataTexture(data, 1, 1);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/** Fetch the panorama manifest; null when the street has none (404) or the response is unusable. */
export async function fetchPanoramas(slug, fetchImpl = fetch) {
  let resp;
  try { resp = await fetchImpl(`/api/segments/${encodeURIComponent(slug)}/panoramas.json`); } catch { return null; }
  if (!resp.ok) return null;
  let m;
  try { m = await resp.json(); } catch { return null; }
  if (!m || m.schema !== PANO_SCHEMA || !Array.isArray(m.panoramas) || !m.panoramas.length) return null;
  return m;
}

function loadTexture(url) {
  return new Promise((resolve) => {
    new THREE.TextureLoader().setCrossOrigin('anonymous').load(url, (tex) => {
      tex.flipY = false;                     // row 0 = up, as stitched
      tex.wrapS = THREE.RepeatWrapping;
      tex.wrapT = THREE.ClampToEdgeWrapping;
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = ANISOTROPY;
      tex.generateMipmaps = true;
      tex.minFilter = THREE.LinearMipmapLinearFilter;
      resolve(tex);
    }, undefined, () => resolve(null));
  });
}

/**
 * @param manifest  hero-panoramas/1
 * @param track     ManifestTrack (world(s, t), applyCopy(p, k), length)
 * @param street    { root, copies } from createStreetView (copies are traversed for PANO_NODES)
 * @param camBackM  how far behind the runner the camera sits (the projection source follows the camera)
 */
export function createPanoBackdrop(manifest, track, street, { camBackM = 0, exposure = 1.0 } = {}) {
  const L = track.length;
  const b0 = THREE.MathUtils.degToRad(manifest.bearing0_deg || 0);
  const camH = manifest.camera_height_m || 2.5;
  const panos = manifest.panoramas.slice().sort((p, q) => p.s - q.s);
  // one entry per (panorama, copy) around the loop: stations along the unrolled path
  const entries = [];
  for (const k of [-1, 0, 1, 2]) {
    const p = track.applyCopy({ x: 0, z: 0, theta: 0 }, k);
    for (let i = 0; i < panos.length; i++) {
      const w = track.world(panos[i].s, panos[i].t);
      const c = Math.cos(p.theta), sn = Math.sin(p.theta);
      entries.push({
        i, k, s: panos[i].s + k * L, url: panos[i].url,
        centre: new THREE.Vector3(p.x + w.x * c - w.z * sn, camH, p.z + w.x * sn + w.z * c),
        rot: b0 - p.theta,
      });
    }
  }
  entries.sort((a, b) => a.s - b.s);

  const blank = blankTexture();
  const uniforms = {
    uPanoA: { value: blank }, uPanoB: { value: blank },
    uCentreA: { value: new THREE.Vector3() }, uCentreB: { value: new THREE.Vector3() },
    uRotA: { value: 0 }, uRotB: { value: 0 }, uMix: { value: 0 }, uExposure: { value: exposure },
  };
  const project = new THREE.ShaderMaterial({ uniforms, vertexShader: PROJECT_VERT, fragmentShader: PROJECT_FRAG, glslVersion: THREE.GLSL3, name: 'mat_pano' });
  const domeMat = new THREE.ShaderMaterial({ uniforms, vertexShader: DOME_VERT, fragmentShader: DOME_FRAG, glslVersion: THREE.GLSL3, side: THREE.BackSide, depthWrite: false, name: 'mat_pano_sky' });
  const dome = new THREE.Mesh(new THREE.SphereGeometry(DOME_RADIUS, 32, 20), domeMat);
  dome.name = 'PanoSky';
  dome.renderOrder = -10;
  dome.frustumCulled = false;

  const previous = new Map();   // mesh -> original material (restored by dispose)
  const hidden = [];
  const groundMat = manifest.ground ? new THREE.MeshBasicMaterial({ name: 'mat_pano_ground', color: 0xffffff }) : null;
  for (const g of street.copies) {
    g.traverse((o) => {
      if (!o.isMesh) return;
      if (groundMat && GROUND_NODES.includes(o.name)) { previous.set(o, o.material); o.material = groundMat; }
      else if (PANO_NODES.includes(o.name)) { previous.set(o, o.material); o.material = project; }
      else if (PHOTO_HIDDEN_NODES.includes(o.name) && o.visible) { o.visible = false; hidden.push(o); }
    });
  }

  // ---- texture cache (LRU by url)
  const cache = new Map();      // url -> { tex: Texture|null, promise }
  function get(url) {
    let e = cache.get(url);
    if (e) { cache.delete(url); cache.set(url, e); return e; }
    e = { tex: null, promise: null };
    e.promise = loadTexture(url).then((tex) => { e.tex = tex; return tex; });
    cache.set(url, e);
    while (cache.size > CACHE_MAX) {
      const [oldUrl, old] = cache.entries().next().value;
      if (old.tex && old.tex !== uniforms.uPanoA.value && old.tex !== uniforms.uPanoB.value) {
        old.tex.dispose(); cache.delete(oldUrl);
      } else break;
    }
    return e;
  }

  let cur = { a: null, b: null, mix: 0 };
  const stats = { loaded: 0 };

  function pick(camS) {
    let ia = -1;
    for (let i = 0; i < entries.length; i++) { if (entries[i].s <= camS) ia = i; else break; }
    if (ia < 0) ia = 0;
    const ib = Math.min(entries.length - 1, ia + 1);
    const a = entries[ia], b = entries[ib];
    const gap = b.s - a.s;
    const f = gap > 1e-6 ? (camS - a.s) / gap : 0;
    const mix = THREE.MathUtils.smoothstep(f, MIX_LO, MIX_HI);
    return { a, b, mix, ahead: entries.slice(ib + 1, ib + 1 + PREFETCH_AHEAD) };
  }

  function apply(a, b, mix) {
    const ea = get(a.url), eb = get(b.url);
    const ta = ea.tex, tb = eb.tex;
    if (!ta && !tb) return;
    const useA = ta || tb, useB = tb || ta;
    uniforms.uPanoA.value = useA; uniforms.uPanoB.value = useB;
    uniforms.uCentreA.value.copy(ta ? a.centre : b.centre); uniforms.uCentreB.value.copy(tb ? b.centre : a.centre);
    uniforms.uRotA.value = ta ? a.rot : b.rot; uniforms.uRotB.value = tb ? b.rot : a.rot;
    uniforms.uMix.value = ta && tb ? mix : 0;
    cur = { a, b, mix };
  }

  return {
    dome, material: project, entries, panos,
    /** Load the panoramas the start needs and the frozen ground (call before showing the street). */
    async prime(runnerS = 0) {
      const { a, b } = pick(runnerS - camBackM);
      const [ta, tb, tg] = await Promise.all([get(a.url).promise, get(b.url).promise,
        groundMat ? loadTexture(manifest.ground.url) : Promise.resolve(null)]);
      if (groundMat) {
        if (tg) { tg.wrapS = tg.wrapT = THREE.ClampToEdgeWrapping; tg.anisotropy = 16; groundMat.map = tg; groundMat.needsUpdate = true; }
        else { for (const [mesh, mat] of previous) if (mesh.material === groundMat) mesh.material = project; }   // fall back to the projection
      }
      stats.loaded = cache.size;
      apply(a, b, 0);
      return !!(ta || tb);
    },
    /** @param runnerS the runner's wrapped position; the projection follows the camera behind it */
    update(runnerS, camera) {
      const { a, b, mix, ahead } = pick(runnerS - camBackM);
      for (const e of ahead) get(e.url);
      apply(a, b, mix);
      if (camera) dome.position.copy(camera.position);
    },
    /** Debug summary (e2e tests). */
    get state() {
      return { count: panos.length, current: cur.a ? cur.a.i : null, next: cur.b ? cur.b.i : null, mix: Math.round(cur.mix * 1000) / 1000,
        cached: [...cache.values()].filter((e) => e.tex).length, ground: !!(groundMat && groundMat.map), hidden: hidden.length };
    },
    dispose() {
      for (const [mesh, mat] of previous) mesh.material = mat;
      for (const o of hidden) o.visible = true;
      if (groundMat) { if (groundMat.map) groundMat.map.dispose(); groundMat.dispose(); }
      for (const e of cache.values()) if (e.tex) e.tex.dispose();
      cache.clear();
      project.dispose(); domeMat.dispose(); dome.geometry.dispose(); blank.dispose();
    },
  };
}
