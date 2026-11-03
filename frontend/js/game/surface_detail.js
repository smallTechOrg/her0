// surface_detail.js: render-time realism for the ground nodes of street.glb (the glb itself stays engine-neutral).
// The satellite bake is ~10 cm/px and magnified, so up close it is a soft smear with dark tree-shadow blotches. Per
// surface kind this patch, in the fragment shader:
//   1. flattens the satellite's luminance towards a typical value for the material (keeps its colour and large-scale
//      variation, drops most of the blotchy contrast) and mixes in a real base colour;
//   2. multiplies procedural, metric detail laid out along the street direction:
//        road     - asphalt grain, speckle, darker repair patches, oil stains
//        footpath - staggered concrete slabs with grout lines, per-slab tone, stains
//        ground   - earth: clumps, gravel, dry/wet variation
//        kerb     - Bengaluru black-and-yellow painted kerb stones (1 m), weathered
//   Grout lines and kerb joints fade out by screen-space derivative so they do not shimmer in the distance.
// No textures, one extra varying; materials are cloned per kind so the shared mat_ground keeps working elsewhere.

const KINDS = {
  Road: 'ROAD', Footpath: 'FOOTPATH', Footpath_Opposite: 'FOOTPATH', Ground_L: 'GROUND', Ground_R: 'GROUND', Kerb: 'KERB',
};

const NOISE = `
varying vec3 vSurfW;
uniform vec2 uSurfDir;
float sdHash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float sdNoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(sdHash(i), sdHash(i + vec2(1.0, 0.0)), f.x), mix(sdHash(i + vec2(0.0, 1.0)), sdHash(i + vec2(1.0, 1.0)), f.x), f.y);
}
float sdFbm(vec2 p) { return 0.5 * sdNoise(p) + 0.3 * sdNoise(p * 2.03 + 7.1) + 0.2 * sdNoise(p * 4.01 + 3.7); }
// thin line mask around value 0 of a periodic coordinate, faded by its screen-space footprint
float sdLine(float x, float width) {
  float d = abs(fract(x + 0.5) - 0.5);
  float fw = max(fwidth(x), 1e-4);
  float m = 1.0 - smoothstep(width, width + fw * 1.5, d);
  return m * (1.0 - smoothstep(0.08, 0.25, fw));
}
`;

const DETAIL = {
  ROAD: `
    c = sdFlatten(c, 0.115, 0.6);
    c = mix(c, vec3(0.085, 0.085, 0.09), 0.35);
    float grain = 0.88 + 0.16 * sdNoise(w * 23.0) + 0.08 * (sdHash(floor(w * 70.0)) - 0.5);
    float patchy = smoothstep(0.58, 0.66, sdFbm(w * vec2(0.22, 0.5) + 11.0));
    float oil = smoothstep(0.62, 0.75, sdFbm(w * vec2(0.6, 1.6) + 23.0));
    c *= grain * (1.0 - 0.22 * patchy) * (1.0 - 0.18 * oil);
  `,
  FOOTPATH: `
    c = sdFlatten(c, 0.2, 0.55);
    c = mix(c, vec3(0.30, 0.27, 0.235), 0.3);
    vec2 slab = vec2(0.6, 0.45);
    vec2 q = w / slab;
    q.x += 0.5 * mod(floor(q.y), 2.0);
    vec2 id = floor(q);
    float grout = max(sdLine(q.x, 0.015), sdLine(q.y, 0.02));
    float tone = 0.86 + 0.22 * sdHash(id) + 0.1 * sdNoise(w * 9.0);
    float stain = 1.0 - 0.25 * smoothstep(0.5, 0.75, sdFbm(w * 0.7 + 2.0));
    float chip = 1.0 - 0.25 * step(0.965, sdHash(id + 17.0)) * smoothstep(0.3, 0.7, sdNoise(w * 6.0));
    c *= tone * stain * chip * (1.0 - 0.28 * grout);
  `,
  GROUND: `
    c = sdFlatten(c, 0.12, 0.45);
    c = mix(c, vec3(0.17, 0.12, 0.075), 0.3);
    float clump = 0.78 + 0.32 * sdFbm(w * 1.4 + 9.0);
    float gravel = 0.9 + 0.2 * (sdHash(floor(w * 45.0)) - 0.5) + 0.1 * sdNoise(w * 14.0);
    c *= clump * gravel;
  `,
  KERB: `
    float seg = step(0.5, fract(w.x / 2.0));
    vec3 paint = mix(vec3(0.62, 0.42, 0.02), vec3(0.025, 0.025, 0.025), seg);
    float wear = smoothstep(0.45, 0.8, sdFbm(w * vec2(3.0, 9.0) + 1.0));
    c = mix(paint, vec3(0.32, 0.31, 0.29), 0.2 + 0.55 * wear);
    c *= 0.9 + 0.15 * sdNoise(w * 30.0);
    c *= 1.0 - 0.6 * sdLine(w.x, 0.01);
  `,
};

function patch(material, kind, dirUniform) {
  const prev = material.onBeforeCompile;
  material.onBeforeCompile = (shader, renderer) => {
    if (prev) prev(shader, renderer);
    shader.uniforms.uSurfDir = dirUniform;
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vSurfW;')
      .replace('#include <worldpos_vertex>', '#include <worldpos_vertex>\nvSurfW = (modelMatrix * vec4(transformed, 1.0)).xyz;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>\n${NOISE}
vec3 sdFlatten(vec3 c, float target, float k) {
  float l = max(dot(c, vec3(0.2126, 0.7152, 0.0722)), 1e-3);
  return c * (mix(l, target, k) / l);
}`)
      .replace('#include <color_fragment>', `#include <color_fragment>
{
  vec2 w = vec2(dot(vSurfW.xz, uSurfDir), dot(vSurfW.xz, vec2(-uSurfDir.y, uSurfDir.x)));
  vec3 c = diffuseColor.rgb;
  ${DETAIL[kind]}
  diffuseColor.rgb = c;
}`);
  };
  const prevKey = material.customProgramCacheKey ? material.customProgramCacheKey.bind(material) : () => '';
  material.customProgramCacheKey = () => `${prevKey()}|surf-${kind}`;
  material.userData.surfaceDetail = kind;
  return material;
}

/** Dominant horizontal direction of a mesh (principal axis of its world-space vertices), as a unit vec2. */
export function dominantDirection(mesh) {
  const pos = mesh.geometry.getAttribute('position');
  const e = mesh.matrixWorld.elements;
  let n = 0, mx = 0, mz = 0, sxx = 0, szz = 0, sxz = 0;
  const step = Math.max(1, Math.floor(pos.count / 4000));
  for (let i = 0; i < pos.count; i += step) {
    const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
    const wx = e[0] * x + e[4] * y + e[8] * z + e[12], wz = e[2] * x + e[6] * y + e[10] * z + e[14];
    n++; mx += wx; mz += wz; sxx += wx * wx; szz += wz * wz; sxz += wx * wz;
  }
  if (!n) return [1, 0];
  mx /= n; mz /= n;
  const cxx = sxx / n - mx * mx, czz = szz / n - mz * mz, cxz = sxz / n - mx * mz;
  const ang = 0.5 * Math.atan2(2 * cxz, cxx - czz);
  return [Math.cos(ang), Math.sin(ang)];
}

/**
 * Give the ground nodes their surface detail. `nodes` maps glb node name -> mesh (loadStreet's `nodes`).
 * Returns the new materials (to be disposed with the street).
 */
export function applySurfaceDetail(THREE, nodes) {
  const ref = nodes.Road || nodes.Footpath;
  if (!ref) return [];
  const dir = dominantDirection(ref);
  const dirUniform = { value: new THREE.Vector2(dir[0], dir[1]) };
  const byKey = new Map();
  const out = [];
  for (const [name, kind] of Object.entries(KINDS)) {
    const mesh = nodes[name];
    if (!mesh || !mesh.material) continue;
    const key = `${mesh.material.uuid}:${kind}`;
    let m = byKey.get(key);
    if (!m) {
      m = patch(mesh.material.clone(), kind, dirUniform);
      byKey.set(key, m);
      out.push(m);
    }
    mesh.material = m;
  }
  return out;
}
