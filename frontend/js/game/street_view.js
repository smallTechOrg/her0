// street_view.js: the glb world at run time. The parsed street is cloned into a small pool of rigid copies
// chained end to start (spec/portability.md §2 "Loop seam"); each frame only the copies that can be seen are
// visible and positioned (geometry/materials/textures are shared, so copies cost draw calls only, and frustum
// culling drops the one behind the camera). Also draws the collectable tokens (one InstancedMesh).
import * as THREE from 'three';

const VIEW_AHEAD_M = 100;   // beyond the fog far plane (90 m)
const VIEW_BEHIND_M = 10;
const MAX_POOL = 5;

export function createStreetView(loaded, track, tokenDefs) {
  const L = track.length;
  const poolSize = Math.min(MAX_POOL, Math.ceil((VIEW_AHEAD_M + VIEW_BEHIND_M) / L) + 2);
  const root = new THREE.Group();
  root.name = 'Street';
  const copies = [];
  for (let i = 0; i < poolSize; i++) {
    const g = i === 0 ? loaded.group : loaded.group.clone(true);   // clone shares geometry + materials
    g.matrixAutoUpdate = false;
    g.visible = false;
    g.userData.k = null;
    root.add(g);
    copies.push(g);
  }

  const m = new THREE.Matrix4(), rot = new THREE.Matrix4();
  function placeCopy(g, k) {
    const p = track.applyCopy({ x: 0, z: 0, theta: 0 }, k);
    rot.makeRotationY(-p.theta);
    m.makeTranslation(p.x, 0, p.z).multiply(rot);
    g.matrix.copy(m);
    g.matrixWorldNeedsUpdate = true;
    g.userData.k = k;
  }

  // ---- tokens: one InstancedMesh covering the visible copies
  const coinGeo = new THREE.CylinderGeometry(0.35, 0.35, 0.08, 12);
  const coinMat = new THREE.MeshLambertMaterial({ color: 0xffb100, emissive: 0x553a00 });
  const tokenLocal = tokenDefs.map((t) => {
    const w = track.world(t.s, track.laneLat(t.s, t.lane));
    return { id: t.id, x: w.x, z: w.z, theta: w.theta };
  });
  const capacity = Math.max(1, tokenLocal.length * poolSize);
  const coins = new THREE.InstancedMesh(coinGeo, coinMat, capacity);
  coins.count = 0;
  coins.frustumCulled = false;
  coins.name = 'Tokens';
  root.add(coins);
  const hidden = new Set();   // token ids already collected in the CURRENT loop
  const _q = new THREE.Quaternion(), _e = new THREE.Euler(), _v = new THREE.Vector3(), _s = new THREE.Vector3(1, 1, 1), _m = new THREE.Matrix4();
  let visibleKs = [];

  return {
    root, copies, coins,
    hideToken(id) { hidden.add(id); },
    showAllTokens() { hidden.clear(); },
    get visibleCopies() { return visibleKs.slice(); },
    /** @param s the runner's WRAPPED position in the copy-0 frame (as the legacy world: the runner teleports at the
     *  loop seam, copies k+1 are rigid images of copy k so the picture is seamless); t = elapsed seconds (coin spin) */
    update(s, t) {
      const kMin = Math.floor((s - VIEW_BEHIND_M) / L), kMax = Math.floor((s + VIEW_AHEAD_M) / L);
      const ks = [];
      for (let k = kMin; k <= kMax; k++) ks.push(k);
      visibleKs = ks;
      const used = new Set();
      for (const k of ks) {
        const slot = ((k % poolSize) + poolSize) % poolSize;
        const g = copies[slot];
        used.add(slot);
        if (g.userData.k !== k) placeCopy(g, k);
        g.visible = true;
      }
      copies.forEach((g, i) => { if (!used.has(i)) g.visible = false; });

      let n = 0;
      for (const k of ks) {
        const p = track.applyCopy({ x: 0, z: 0, theta: 0 }, k);
        const c = Math.cos(p.theta), sn = Math.sin(p.theta);
        for (const tok of tokenLocal) {
          if (k === 0 && hidden.has(tok.id)) continue;
          if (n >= capacity) break;
          // token local position -> copy frame (same rotation as applyCopy)
          const x = p.x + tok.x * c - tok.z * sn, z = p.z + tok.x * sn + tok.z * c;
          _e.set(Math.PI / 2, t * 3 - (tok.theta + p.theta), 0, 'YXZ');
          _q.setFromEuler(_e);
          _v.set(x, 0.9, z);
          coins.setMatrixAt(n++, _m.compose(_v, _q, _s));
        }
      }
      coins.count = n;
      coins.instanceMatrix.needsUpdate = true;
    },
  };
}
