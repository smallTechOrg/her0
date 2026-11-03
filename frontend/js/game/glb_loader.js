// glb_loader.js: fetch street.glb + street.manifest.json and parse the glb with the vendored GLTFLoader.
// Consumer rules (spec/portability.md): look nodes up BY NAME, tolerate missing optional nodes, never read
// geometry from extras, never hard-code a number from gameplay.json.
// The glb's MeshStandardMaterials are swapped for MeshLambertMaterial (same map / colour / vertex colours): the
// game has no PBR need and Lambert is much cheaper on phones. mat_flat (furniture) gets the camera-clearance clip;
// the ground nodes get procedural surface detail (surface_detail.js).
import * as THREE from 'three';
import { GLTFLoader } from '/vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { attachClearance } from '/js/game/camera_rig.js?v=1';
import { applySurfaceDetail } from '/js/game/surface_detail.js';

export const NODE_NAMES = [
  'Footpath', 'Footpath_Opposite', 'Road', 'Kerb', 'Ground_L', 'Ground_R', 'Frontage_L', 'Frontage_R', 'Frontage_Caps',
  'Furniture_Poles', 'Furniture_Trees', 'Furniture_Boxes', 'Furniture_Railings', 'Furniture_Decals',
];
export const REQUIRED_NODES = ['Road'];   // a street with no carriageway is not a street (error screen, not a blank world)
const MAX_ANISOTROPY = 8;

export class LoadError extends Error {
  constructor(message, kind) { super(message); this.name = 'LoadError'; this.kind = kind; }
}

async function fetchBuffer(url, onProgress) {
  let resp;
  try { resp = await fetch(url); } catch (err) { throw new LoadError(`Could not download the street model (${err.message}).`, 'network'); }
  if (!resp.ok) throw new LoadError(`The street model could not be downloaded (HTTP ${resp.status}).`, 'http');
  const total = Number(resp.headers.get('content-length')) || 0;
  if (!resp.body || !resp.body.getReader || !total) {
    const buf = await resp.arrayBuffer();
    if (onProgress) onProgress(buf.byteLength, buf.byteLength);
    return buf;
  }
  const reader = resp.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    if (onProgress) onProgress(got, total);
  }
  const out = new Uint8Array(got);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out.buffer;
}

async function fetchManifest(url) {
  let resp;
  try { resp = await fetch(url); } catch (err) { throw new LoadError(`Could not download the street manifest (${err.message}).`, 'network'); }
  if (!resp.ok) throw new LoadError(`The street manifest could not be downloaded (HTTP ${resp.status}).`, 'http');
  let m;
  try { m = await resp.json(); } catch { throw new LoadError('The street manifest is not valid JSON.', 'manifest'); }
  if (!m || m.schema !== 'hero-street-manifest/1') throw new LoadError(`Unsupported street manifest (${m && m.schema}).`, 'manifest');
  return m;
}

function parseGlb(buffer) {
  const head = new Uint8Array(buffer, 0, Math.min(4, buffer.byteLength));
  if (head.length < 4 || String.fromCharCode(...head) !== 'glTF') throw new LoadError('The street model is not a glb file.', 'glb');
  return new Promise((resolve, reject) => {
    new GLTFLoader().parse(buffer, '', resolve, (err) => reject(new LoadError(`The street model could not be parsed (${err && err.message ? err.message : err}).`, 'glb')));
  });
}

/** Swap glTF PBR materials for Lambert (cached per source material and vertex-colour use). */
function lambertise(root, shared) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    const src = o.material;
    const hasColor = !!o.geometry.getAttribute('color');
    const key = `${src.uuid}:${hasColor}`;
    let m = shared.get(key);
    if (!m) {
      m = new THREE.MeshLambertMaterial({ name: src.name, map: src.map || null, color: src.color ? src.color.clone() : new THREE.Color(0xffffff), vertexColors: hasColor });
      if (m.map) {
        m.map.anisotropy = MAX_ANISOTROPY;
        m.map.colorSpace = THREE.SRGBColorSpace;
      }
      if (src.name === 'mat_flat') attachClearance(m);
      shared.set(key, m);
    }
    o.material = m;
    o.frustumCulled = true;
    o.matrixAutoUpdate = false;
  });
}

/**
 * @returns {Promise<{group: THREE.Group, nodes: Record<string, THREE.Mesh>, manifest: object, materials: THREE.Material[], bytes: number, missing: string[]}>}
 */
export async function loadStreet({ glbUrl, manifestUrl, onProgress }) {
  const [buffer, manifest] = await Promise.all([fetchBuffer(glbUrl, onProgress), fetchManifest(manifestUrl)]);
  const gltf = await parseGlb(buffer);
  const group = new THREE.Group();
  group.name = 'StreetCopy';
  const scene = gltf.scene;
  const nodes = {};
  scene.traverse((o) => { if (o.isMesh) nodes[o.name] = o; });
  const missing = REQUIRED_NODES.filter((n) => !nodes[n]);
  if (missing.length) throw new LoadError(`The street model is missing required parts: ${missing.join(', ')}.`, 'glb');
  const shared = new Map();
  lambertise(scene, shared);
  scene.updateMatrixWorld(true);
  const detailed = applySurfaceDetail(THREE, nodes);   // asphalt / slabs / earth / painted kerb at render time
  group.add(scene);
  return { group, nodes, manifest, materials: [...shared.values(), ...detailed], bytes: buffer.byteLength, missing: NODE_NAMES.filter((n) => !nodes[n]) };
}
