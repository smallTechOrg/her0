// atmosphere.js: sky dome with a horizon band, distance fog matched to the horizon colour, soft light, the
// ground-AO blob under the runner (the avatar is runner_avatar.js). No textures except the 64 px AO gradient.
import * as THREE from 'three';

export const FOG_NEAR_M = 30;
export const FOG_FAR_M = 90;
export const PHOTO_FOG_NEAR_M = 110;   // with a Street View backdrop the photo carries its own haze: fog nearly off
export const PHOTO_FOG_FAR_M = 300;
export const SKY_ZENITH = 0x5d93cf;
export const SKY_MID = 0x9cc3e6;
export const SKY_HORIZON = 0xdde8ef;
const DOME_RADIUS = 110;   // < camera far plane (140) so the dome is never clipped

function skyGeometry() {
  const geo = new THREE.SphereGeometry(DOME_RADIUS, 24, 14);
  const pos = geo.getAttribute('position');
  const cols = new Float32Array(pos.count * 3);
  const zen = new THREE.Color(SKY_ZENITH), mid = new THREE.Color(SKY_MID), hor = new THREE.Color(SKY_HORIZON);
  const c = new THREE.Color();
  for (let i = 0; i < pos.count; i++) {
    const e = pos.getY(i) / DOME_RADIUS;            // elevation -1 .. 1
    if (e <= 0.02) c.copy(hor);                       // at and below the horizon: haze, same colour as the fog
    else if (e < 0.25) c.copy(hor).lerp(mid, e / 0.25);
    else c.copy(mid).lerp(zen, Math.min(1, (e - 0.25) / 0.6));
    cols[i * 3] = c.r; cols[i * 3 + 1] = c.g; cols[i * 3 + 2] = c.b;
  }
  geo.setAttribute('color', new THREE.BufferAttribute(cols, 3));
  return geo;
}

function aoTexture() {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 64;
  const g = cv.getContext('2d');
  const grad = g.createRadialGradient(32, 32, 2, 32, 32, 31);
  grad.addColorStop(0, 'rgba(0,0,0,0.55)');
  grad.addColorStop(0.6, 'rgba(0,0,0,0.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.generateMipmaps = false;
  tex.minFilter = THREE.LinearFilter;
  return tex;
}

/**
 * Sky + fog + lights on `scene`. `sky.follow(camera)` re-centres the dome on the camera each frame.
 * Fog colour === the dome's horizon colour so the far ground melts into the sky with no visible edge.
 */
export function createAtmosphere(scene) {
  const horizon = new THREE.Color(SKY_HORIZON);
  scene.background = horizon.clone();
  scene.fog = new THREE.Fog(horizon.getHex(), FOG_NEAR_M, FOG_FAR_M);
  const sky = new THREE.Mesh(skyGeometry(), new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide, fog: false, depthWrite: false }));
  sky.name = 'Sky';
  sky.renderOrder = -10;
  sky.frustumCulled = false;
  scene.add(sky);
  const hemi = new THREE.HemisphereLight(0xe6f0fb, 0x8d8576, 1.55);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xfff0d8, 0.85);
  sun.position.set(-8, 16, 6);
  scene.add(sun);
  return {
    sky, hemi, sun,
    follow(camera) { sky.position.copy(camera.position); },
    /** Photo mode: the Street View dome replaces the gradient sky and the fog backs off (the photo has its own haze). */
    setPhoto(on) {
      sky.visible = !on;
      scene.fog.near = on ? PHOTO_FOG_NEAR_M : FOG_NEAR_M;
      scene.fog.far = on ? PHOTO_FOG_FAR_M : FOG_FAR_M;
    },
  };
}

/** Soft dark blob on the ground under the runner (stays on the ground while jumping). */
export function createAoBlob() {
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 1.5),
    new THREE.MeshBasicMaterial({ map: aoTexture(), transparent: true, depthWrite: false, fog: false }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = 0.04;
  mesh.renderOrder = 2;
  mesh.name = 'AoBlob';
  return mesh;
}
