// budget.js: render-budget probe behind window.__hero.perf.
//   draw_calls / triangles : renderer.info.render of the last frame (the whole scene incl. dynamic layer, runner, AO)
//   textures               : textures alive on the GPU (renderer.info.memory.textures)
//   texture_mb             : estimated GPU memory of the unique textures referenced by the scene (RGBA8, +1/3 for mips)
//   fps_avg                : mean frame rate over the last 60 frames
const MB = 1024 * 1024;

function textureBytes(tex) {
  const img = tex.image;
  const w = (img && (img.naturalWidth || img.width || (img.data && img.width))) || 0;
  const h = (img && (img.naturalHeight || img.height)) || 0;
  const mip = tex.generateMipmaps && tex.minFilter !== 1003 && tex.minFilter !== 1006 ? 4 / 3 : 1;   // Nearest / Linear = no mips
  return w * h * 4 * mip;
}

export function sceneTextureMb(scene) {
  const seen = new Set();
  let bytes = 0;
  const add = (t) => { if (t && !seen.has(t.uuid)) { seen.add(t.uuid); bytes += textureBytes(t); } };
  if (scene.background && scene.background.isTexture) add(scene.background);
  scene.traverse((o) => {
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    for (const m of mats) for (const k of ['map', 'alphaMap', 'emissiveMap', 'normalMap']) add(m[k]);
  });
  return { count: seen.size, mb: bytes / MB };
}

export function createBudget(renderer, scene) {
  const perf = { draw_calls: 0, triangles: 0, textures: 0, texture_mb: 0, fps_avg: 0, glb_bytes: 0 };
  const window_ = [];
  let tick = 0;
  return {
    perf,
    /** Call right after renderer.render(). dtMs = wall time since the previous frame. */
    sample(dtMs) {
      if (dtMs > 0 && dtMs < 1000) {
        window_.push(dtMs);
        if (window_.length > 60) window_.shift();
        perf.fps_avg = Math.round(1000 / (window_.reduce((a, b) => a + b, 0) / window_.length) * 10) / 10;
      }
      if (!renderer) return;
      perf.draw_calls = renderer.info.render.calls;
      perf.triangles = renderer.info.render.triangles;
      perf.textures = renderer.info.memory.textures;
      if (tick++ % 30 === 0) perf.texture_mb = Math.round(sceneTextureMb(scene).mb * 100) / 100;
    },
  };
}
