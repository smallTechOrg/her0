// runner_avatar.js: the player is a school kid with a backpack, running. One SkinnedMesh (one draw call): every body
// part is a small mesh rigidly bound to one bone, and a procedural run / jump / slide pose drives the bones.
// Local space: -z is forward (path.js forward at theta = 0), +z is the back the chase camera sees; feet at y = 0.
import * as THREE from 'three';

const SKIN = 0x8a5a3c, HAIR = 0x17130f, SHIRT = 0xf2f0ea, COLLAR = 0x2f5d9e, SHORTS = 0x2b3f6e, SOCK = 0xe8e6df,
  SHOE = 0x1c1c1e, SOLE = 0x4a4a4a, BAG = 0xb3352b, BAG_DARK = 0x6e1f19, STRAP = 0x2a2a2a;

// bone name -> [parent, rest position relative to the parent]
const BONES = {
  hips: [null, [0, 0.86, 0]],
  spine: ['hips', [0, 0.08, 0]],
  chest: ['spine', [0, 0.24, 0]],
  head: ['chest', [0, 0.2, 0]],
  armL: ['chest', [-0.19, 0.12, 0]], foreL: ['armL', [0, -0.27, 0]],
  armR: ['chest', [0.19, 0.12, 0]], foreR: ['armR', [0, -0.27, 0]],
  legL: ['hips', [-0.09, -0.04, 0]], shinL: ['legL', [0, -0.4, 0]],
  legR: ['hips', [0.09, -0.04, 0]], shinR: ['legR', [0, -0.4, 0]],
};

function restWorld(name) {
  let [parent, p] = BONES[name];
  const out = [...p];
  while (parent) { const [pp, q] = BONES[parent]; out[0] += q[0]; out[1] += q[1]; out[2] += q[2]; parent = pp; }
  return out;
}

function buildGeometry(boneIndex) {
  const parts = [];
  const lin = (hex) => { const c = new THREE.Color(hex); return [c.r, c.g, c.b]; };
  // add a geometry given in the bone's local rest frame (origin at the joint)
  const add = (bone, g, hex, x = 0, y = 0, z = 0) => {
    g.translate(x, y, z);
    const w = restWorld(bone);
    g.translate(w[0], w[1], w[2]);
    parts.push({ g: g.index ? g.toNonIndexed() : g, c: lin(hex), b: boneIndex[bone] });
  };
  const capsule = (r, len, sx = 1, sz = 1) => { const g = new THREE.CapsuleGeometry(r, len, 3, 10); g.scale(sx, 1, sz); return g; };
  const box = (w, h, d) => new THREE.BoxGeometry(w, h, d);
  const sphere = (r, sx = 1, sy = 1, sz = 1) => { const g = new THREE.SphereGeometry(r, 12, 9); g.scale(sx, sy, sz); return g; };

  // pelvis / shorts
  add('hips', capsule(0.13, 0.08, 1.25, 0.8).rotateZ(Math.PI / 2).scale(0.75, 1, 1), SHORTS, 0, -0.03, 0);
  // torso: shirt, collar, belt line
  add('spine', capsule(0.135, 0.2, 1.2, 0.78), SHIRT, 0, 0.14, 0);
  add('chest', box(0.17, 0.04, 0.12), COLLAR, 0, 0.12, -0.02);
  add('spine', box(0.31, 0.04, 0.2), 0x1f2a44, 0, -0.03, 0);
  // head: neck, face, hair cap, ears
  add('head', capsule(0.045, 0.06), SKIN, 0, -0.02, 0);
  add('head', sphere(0.105, 0.92, 1.1, 1), SKIN, 0, 0.11, 0);
  add('head', sphere(0.11, 0.95, 0.8, 1.02), HAIR, 0, 0.16, 0.012);
  add('head', sphere(0.022, 0.6, 1, 1), SKIN, -0.098, 0.11, 0.01);
  add('head', sphere(0.022, 0.6, 1, 1), SKIN, 0.098, 0.11, 0.01);
  // backpack with flap, pocket and straps
  add('chest', box(0.3, 0.36, 0.14), BAG, 0, -0.08, 0.16);
  add('chest', box(0.3, 0.1, 0.15), BAG_DARK, 0, 0.06, 0.165);
  add('chest', box(0.2, 0.12, 0.05), BAG_DARK, 0, -0.17, 0.25);
  for (const sx of [-1, 1]) add('chest', box(0.04, 0.34, 0.03), STRAP, sx * 0.1, -0.04, -0.1);
  // arms: sleeve + upper arm, forearm + hand
  for (const [arm, fore] of [['armL', 'foreL'], ['armR', 'foreR']]) {
    add(arm, capsule(0.058, 0.08), SHIRT, 0, -0.05, 0);
    add(arm, capsule(0.044, 0.16), SKIN, 0, -0.15, 0);
    add(fore, capsule(0.04, 0.16), SKIN, 0, -0.11, 0);
    add(fore, sphere(0.048, 0.8, 1.1, 1), SKIN, 0, -0.25, 0);
  }
  // legs: shorts, knee/shin, sock, shoe
  for (const [leg, shin] of [['legL', 'shinL'], ['legR', 'shinR']]) {
    add(leg, capsule(0.075, 0.16), SHORTS, 0, -0.1, 0);
    add(leg, capsule(0.06, 0.2), SKIN, 0, -0.28, 0);
    add(shin, capsule(0.052, 0.24), SKIN, 0, -0.17, 0);
    add(shin, capsule(0.048, 0.06), SOCK, 0, -0.33, 0);
    add(shin, box(0.1, 0.07, 0.24), SHOE, 0, -0.39, -0.045);
    add(shin, box(0.105, 0.025, 0.25), SOLE, 0, -0.425, -0.045);
  }

  let n = 0;
  for (const p of parts) n += p.g.getAttribute('position').count;
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), col = new Float32Array(n * 3);
  const si = new Uint16Array(n * 4), sw = new Float32Array(n * 4);
  let o = 0;
  for (const p of parts) {
    const pa = p.g.getAttribute('position'), na = p.g.getAttribute('normal');
    pos.set(pa.array, o * 3); nor.set(na.array, o * 3);
    for (let v = 0; v < pa.count; v++) {
      col.set(p.c, (o + v) * 3);
      si[(o + v) * 4] = p.b; sw[(o + v) * 4] = 1;
    }
    o += pa.count;
    p.g.dispose();
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  g.setAttribute('skinIndex', new THREE.Uint16BufferAttribute(si, 4));
  g.setAttribute('skinWeight', new THREE.Float32BufferAttribute(sw, 4));
  return g;
}

/**
 * Runner avatar group. `group.userData.pose(runner)` sets the pose for this frame from runner.elapsed, runner.speed,
 * runner.action ('running' | 'jumping' | 'sliding') and runner.crashed. The AO blob stays separate (atmosphere.js).
 */
export function createRunnerAvatar() {
  const group = new THREE.Group();
  group.name = 'Runner';
  const names = Object.keys(BONES);
  const boneIndex = Object.fromEntries(names.map((n, i) => [n, i]));
  const bones = names.map((n) => { const b = new THREE.Bone(); b.name = n; return b; });
  names.forEach((n, i) => {
    const [parent, p] = BONES[n];
    bones[i].position.set(...p);
    if (parent) bones[boneIndex[parent]].add(bones[i]);
  });
  const mesh = new THREE.SkinnedMesh(buildGeometry(boneIndex), new THREE.MeshLambertMaterial({ vertexColors: true }));
  mesh.name = 'RunnerBody';
  mesh.frustumCulled = false;
  const root = bones[boneIndex.hips];
  mesh.add(root);
  mesh.bind(new THREE.Skeleton(bones));
  group.add(mesh);
  const B = Object.fromEntries(names.map((n, i) => [n, bones[i]]));
  group.userData.body = mesh;
  group.userData.bones = B;

  let phase = 0, lastT = null;
  group.userData.pose = (runner) => {
    const t = runner ? runner.elapsed : 0;
    const dt = lastT === null ? 0 : Math.max(0, Math.min(0.1, t - lastT));
    lastT = t;
    const speed = runner ? runner.speed : 6;
    const action = runner && !runner.crashed ? runner.action : 'idle';
    phase += dt * (1.6 + speed * 0.32) * Math.PI * 2 * 0.5;
    const s = Math.sin(phase), c = Math.cos(phase);
    for (const b of bones) b.rotation.set(0, 0, 0);
    root.position.set(0, 0.86, 0);
    mesh.position.set(0, 0, 0);
    mesh.rotation.set(0, 0, 0);
    if (action === 'sliding') {
      // feet first, leaning back: about 0.85 m tall
      mesh.rotation.x = 1.0;
      mesh.position.set(0, 0.05, -0.15);
      B.legL.rotation.x = 0.45; B.legR.rotation.x = 0.25;
      B.shinL.rotation.x = -0.15; B.shinR.rotation.x = -0.5;
      B.armL.rotation.set(0.3, 0, -0.6); B.armR.rotation.set(0.3, 0, 0.6);
      B.foreL.rotation.x = 0.4; B.foreR.rotation.x = 0.4;
      B.head.rotation.x = -0.7;
    } else if (action === 'jumping') {
      B.legL.rotation.x = 1.0; B.shinL.rotation.x = -1.3;
      B.legR.rotation.x = -0.35; B.shinR.rotation.x = -0.7;
      B.armL.rotation.set(-0.6, 0, -0.35); B.armR.rotation.set(1.4, 0, 0.35);
      B.foreL.rotation.x = 0.6; B.foreR.rotation.x = 0.5;
      B.spine.rotation.x = -0.12;
    } else if (action === 'idle') {
      B.armL.rotation.z = -0.08; B.armR.rotation.z = 0.08;
    } else {
      // run cycle: legs swing opposite, knees fold on the back swing, arms counter-swing, bounce twice per cycle
      B.legL.rotation.x = -0.75 * s; B.legR.rotation.x = 0.75 * s;
      B.shinL.rotation.x = -(0.3 + 0.9 * Math.max(0, s * 0.8 - c * 0.6) + 0.2 * (1 + s));
      B.shinR.rotation.x = -(0.3 + 0.9 * Math.max(0, -s * 0.8 + c * 0.6) + 0.2 * (1 - s));
      B.armL.rotation.set(0.8 * s, 0, -0.12); B.armR.rotation.set(-0.8 * s, 0, 0.12);
      B.foreL.rotation.x = 1.2 + 0.25 * s; B.foreR.rotation.x = 1.2 - 0.25 * s;
      B.spine.rotation.x = -0.18; B.spine.rotation.y = 0.12 * s;
      B.chest.rotation.y = -0.1 * s;
      B.head.rotation.x = 0.12;
      root.position.y = 0.86 - 0.03 + 0.04 * Math.abs(c);
    }
  };
  group.userData.pose(null);
  return group;
}
