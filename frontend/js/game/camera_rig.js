// camera_rig.js: chase camera + the "nothing may fill the screen" clearance.
//   * 3.8 m high, 6.6 m behind the runner (along the path), looking 12 m ahead; near plane 0.3 m.
//   * Furniture is merged per category in the glb, so per-entity fading is impossible. Instead the furniture
//     material gets a dithered discard inside a vertical cylinder around the camera: fully clipped within
//     CLEAR_NEAR_M (0.8 m), fully drawn beyond CLEAR_FAR_M (1.5 m), a screen-door dither in between. One shared
//     uniform (camera world position) updated per frame by the rig.
import * as THREE from 'three';

export const CAM_HEIGHT_M = 3.8;
export const CAM_BACK_M = 6.6;
export const CAM_LOOK_AHEAD_M = 12;
export const CAM_NEAR_M = 0.3;
export const CAM_FAR_M = 140;
export const CAM_FOV_DEG = 62;
export const CLEAR_NEAR_M = 0.8;
export const CLEAR_FAR_M = 1.5;
const MIN_CLIP_Y_M = 0.25;    // flat decals on the ground are never clipped

/** Shared uniforms of the clearance clip (one object, referenced by every patched material). */
export const clearanceUniforms = {
  uHeroCam: { value: new THREE.Vector3(1e6, 0, 1e6) },
  uHeroClear: { value: new THREE.Vector2(CLEAR_NEAR_M, CLEAR_FAR_M) },
};

/** Patch a Lambert/Standard material so fragments inside the camera cylinder are dithered away. */
export function attachClearance(material) {
  material.userData.heroClearance = true;
  material.onBeforeCompile = (shader) => {
    shader.uniforms.uHeroCam = clearanceUniforms.uHeroCam;
    shader.uniforms.uHeroClear = clearanceUniforms.uHeroClear;
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vHeroWorld;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvHeroWorld = (modelMatrix * vec4(transformed, 1.0)).xyz;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>
varying vec3 vHeroWorld;
uniform vec3 uHeroCam;
uniform vec2 uHeroClear;`)
      .replace('#include <clipping_planes_fragment>', `#include <clipping_planes_fragment>
{
  float heroD = distance(vHeroWorld.xz, uHeroCam.xz);
  if (vHeroWorld.y > ${MIN_CLIP_Y_M.toFixed(2)} && heroD < uHeroClear.y) {
    float heroKeep = smoothstep(uHeroClear.x, uHeroClear.y, heroD);
    float heroN = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
    if (heroKeep <= heroN) discard;
  }
}`);
  };
  material.customProgramCacheKey = () => 'hero-clearance';
  material.needsUpdate = true;
  return material;
}

const _look = new THREE.Vector3();

export function createCameraRig(camera, track) {
  camera.fov = CAM_FOV_DEG;
  camera.near = CAM_NEAR_M;
  camera.far = CAM_FAR_M;
  camera.updateProjectionMatrix();
  let camLat = null;
  return {
    reset(lat) { camLat = lat; },
    /** @param sw runner position along the (wrapped or unrolled) path; lat lateral coordinate; bob metres */
    update(sw, lat, bob = 0) {
      if (camLat === null) camLat = lat;
      camLat += (lat - camLat) * 0.2;
      const back = track.world(sw - CAM_BACK_M, camLat);
      const ahead = track.world(sw + CAM_LOOK_AHEAD_M, camLat);
      camera.position.set(back.x, CAM_HEIGHT_M + bob, back.z);
      _look.set(ahead.x, 0.5, ahead.z);
      camera.lookAt(_look);
      clearanceUniforms.uHeroCam.value.copy(camera.position);
    },
  };
}
