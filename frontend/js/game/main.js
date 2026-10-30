// Play page entry (the S2 <-> S3 integration point). Loads the scene via the public API and builds one of two worlds:
//   * v3 scene  -> street.glb + street.manifest.json (GLTFLoader, ManifestTrack)      window.__hero.source === 'glb'
//   * v1/v2     -> the legacy JS geometry (scene_builder.js, path.js), imported lazily  window.__hero.source === 'legacy'
// then wires Runner (S3), the dynamic obstacle layer (S3), atmosphere, camera, HUD, results.
// Phases: loading -> ready -> running -> crashed -> results | error. window.__hero.state is read-only.
import * as THREE from 'three';
import { api } from '/js/api.js?v=3';
import { Runner } from '/js/game/runner.js?v=4';
import { loadGameplay } from '/js/game/gameplay.js';
import { createDynamicLayer } from '/js/game/dynamic_obstacles.js';
import { attachControls } from '/js/game/controls.js?v=4';
import { createHud } from '/js/game/hud.js?v=4';
import { renderResults } from '/js/game/results.js?v=4';
import { createAbout } from '/js/game/about.js?v=4';
import { fromManifest, fromLegacyScene, resultsScene } from '/js/game/play_data.js?v=1';
import { createAtmosphere, createAoBlob, createRunnerAvatar } from '/js/game/atmosphere.js?v=1';
import { createCameraRig } from '/js/game/camera_rig.js?v=1';
import { createBudget } from '/js/game/budget.js?v=1';

const MAX_DT_S = 0.1;
const CRASH_STUMBLE_MS = 400;
const MAX_DPR = 2;
const TOAST_MS = 1300;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const easeOut = reducedMotion ? (t) => t : (t) => 1 - (1 - t) * (1 - t);

const $ = (id) => document.getElementById(id);
const panels = { loading: $('panel-loading'), error: $('panel-error'), ready: $('panel-ready'), results: $('results') };

const state = {
  phase: 'loading', slug: null, name: null, error: null, progress: { loaded: 0, total: 0 },
  distance_m: 0, score: 0, tokens: 0, lane: 0, lane_name: 'footpath', action: 'none', loops: 0, speed: 0,
  crashed_into: null, elapsed_s: 0, run_post: null, results: null,
};
let scene = null, playData = null, manifest = null, runner = null, view = null, layer = null, gameplay = null;
let source = null;   // 'glb' | 'legacy'

// ----------------------------------------------------------------- renderer + atmosphere

const canvas = $('game');
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'low-power' });
} catch (err) {
  renderer = null;
}
const camera = new THREE.PerspectiveCamera(62, 1, 0.3, 140);
const three = new THREE.Scene();
const atmosphere = createAtmosphere(three);
const budget = createBudget(renderer, three);

// Read-only debug handle (drives the e2e tests).
window.__hero = {
  page: 'play',
  get state() { return structuredClone(state); },
  get perf() { return { ...budget.perf }; },
  get source() { return source; },
  get legacy_geometry() { return source === null ? null : source === 'legacy'; },
  get gameplay() { return gameplay; },
  /** Compact, serialisable summary of the Three.js scene graph (e2e tests: no sprites / labels, node names, camera). */
  graph() {
    const meshes = [];
    let sprites = 0, lights = 0;
    three.traverse((o) => {
      if (o.isSprite) sprites += 1;
      if (o.isLight) lights += 1;
      if (o.isMesh) {
        const g = o.geometry;
        const tris = g.index ? g.index.count / 3 : g.getAttribute('position').count / 3;
        meshes.push({ name: o.name, type: o.type, instanced: !!o.isInstancedMesh, tris: Math.round(tris), visible: o.visible,
          material: o.material && o.material.type, map: !!(o.material && o.material.map) });
      }
    });
    const p = camera.position;
    return { sprites, lights, meshes, fog: three.fog ? { near: three.fog.near, far: three.fog.far, color: three.fog.color.getHex() } : null,
      camera: { x: p.x, y: p.y, z: p.z, near: camera.near, far: camera.far, fov: camera.fov } };
  },
  get dynamic() {
    if (!runner) return null;
    return { run_number: runner.runNumber ?? null, seed: runner.seed ?? null, obstacles: runner.activeObstacles(), rules: gameplay ? gameplay.spawner : null };
  },
};

function showPanel(which) {
  for (const [k, p] of Object.entries(panels)) p.hidden = k !== which;
}
function setPhase(phase) { state.phase = phase; document.body.dataset.phase = phase; }
function fail(reason) {
  state.error = reason;
  $('error-reason').textContent = reason;
  setPhase('error');
  showPanel('error');
}

const params = new URLSearchParams(location.search);
const slug = params.get('segment') || params.get('slug');
const seedParam = params.get('seed');
const seed = seedParam !== null && /^\d+$/.test(seedParam) ? Number(seedParam) : null;
state.slug = slug;

function resize() {
  if (!renderer) return;
  const w = canvas.clientWidth || window.innerWidth, h = canvas.clientHeight || window.innerHeight;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_DPR));
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);

// ----------------------------------------------------------------- game

let runnerMesh = null, aoBlob = null, rig = null, hud = null, controls = null, about = null;
let lastTs = 0, rafId = 0, crashTimer = 0, toastTimer = 0, resumePhase = null, booted = false;

function applySnapshot() {
  const snap = runner.snapshot();
  Object.assign(state, snap);
  hud.update(snap);
  return snap;
}

// Runner and camera follow the path: position = world(s, lat), heading = theta(s).
function placeRunner() {
  const track = runner.track;
  const sw = runner.xWrapped;
  const lat = runner.visualLatM(easeOut);
  const w = track.world(sw, lat);
  const y = runner.jumpY();
  runnerMesh.position.set(w.x, y, w.z);
  runnerMesh.rotation.y = -w.theta;
  const body = runnerMesh.userData.body;
  body.scale.y = runner.sliding ? 0.5 : 1;
  body.position.y = runner.sliding ? 0.4 : 0.67;
  aoBlob.position.set(w.x, 0.04, w.z);
  aoBlob.rotation.set(-Math.PI / 2, 0, w.theta);
  const bob = reducedMotion || !runner || runner.crashed ? 0 : Math.sin(runner.elapsed * 9) * 0.04;
  rig.update(sw, lat, bob);
  atmosphere.follow(camera);
  if (scene.schema_version !== 1) updateCorridorState(sw, lat, w);   // v1 scenes have no corridor debug fields
}

const r3 = (v) => Math.round(v * 1000) / 1000;
function updateCorridorState(sw, lat, w) {
  const track = runner.track;
  const fp = track.footpathAt(sw);
  state.s = r3(sw);
  state.lat_m = r3(lat);
  if (source === 'glb') state.t_m = r3(lat);
  state.footpath_centre_m = fp ? r3(fp.centre) : null;
  state.footpath_offset_m = fp ? r3(Math.abs(track.laneLat(sw, 0) - fp.centre)) : null;
  state.on_strip = track.stripAt(sw, lat);
  state.world = { x: r3(w.x), z: r3(w.z) };
  state.heading_deg = r3(track.headingDeg(sw));
}

function frame(ts) {
  rafId = requestAnimationFrame(frame);
  const rawDt = lastTs ? (ts - lastTs) / 1000 : 0;
  const dt = Math.min(MAX_DT_S, rawDt);
  lastTs = ts;
  if (state.phase === 'running') {
    const events = runner.update(dt);
    for (const ev of events) {
      if (ev.type === 'token') view.hideToken(ev.id);
      if (ev.type === 'loop') view.showAllTokens();
      if (ev.type === 'hit') onHit(ev.object);
      if (ev.type === 'crash') onCrash(ev.object);
    }
    applySnapshot();
  } else if (state.phase === 'crashed') {
    runner.elapsed += dt;
    runnerMesh.rotation.x = Math.min(Math.PI / 2, runnerMesh.rotation.x + dt * 6);
  }
  if (runner) {
    view.update(runner.xWrapped, runner.elapsed);
    layer.update();
    placeRunner();
    if (runnerMesh && state.phase === 'running') runnerMesh.visible = !runner.invulnerable || Math.floor(runner.elapsed * 12) % 2 === 0;
  }
  if (renderer) renderer.render(three, camera);
  budget.sample(rawDt * 1000);
}

function toast(text) {
  const el = $('toast');
  if (!text) return;
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), TOAST_MS);
}

function flash() {
  if (reducedMotion) return;
  const f = $('flash');
  f.classList.remove('on');
  void f.offsetWidth; // restart the animation
  f.classList.add('on');
}

function onHit(obj) {
  flash();
  if (obj && obj.label) toast(obj.label);   // the only in-world naming: a small toast when the runner is hit
}

function onCrash(obj) {
  setPhase('crashed');
  flash();
  if (obj && obj.label) toast(obj.label);
  crashTimer = setTimeout(showResults, CRASH_STUMBLE_MS);
}

async function showResults() {
  const snap = applySnapshot();
  hud.hide();
  state.run_post = 'pending';
  const body = {
    score: snap.score, distance_m: Math.round(snap.distance_m * 10) / 10, tokens_collected: snap.tokens, loops: snap.loops,
    crashed_into_type: snap.crashed_into ? snap.crashed_into.type : null,
    crashed_into_id: snap.crashed_into ? snap.crashed_into.id : null,
    duration_s: Math.round(runner.elapsed * 10) / 10,
  };
  let post;
  try {
    const data = await api.post(`/api/segments/${encodeURIComponent(slug)}/runs`, body);
    post = { status: 'saved', data };
    state.run_post = 'saved';
  } catch (err) {
    post = { status: 'failed', error: err.message };
    state.run_post = 'failed';
  }
  let crashMarker = null;
  try {
    ({ crashMarker } = renderResults(panels.results, {
      scene: resultsScene(scene, manifest), manifest, playData, snap, post, onRunAgain: runAgain, onAbout: (opener) => about.open(opener),
    }));
  } catch (err) {
    state.run_post = state.run_post || 'failed';
    fail(`Could not show the results: ${err.message}`);
    return;
  }
  state.results = {
    posted: body, response: post.status === 'saved' ? post.data : null, error: post.error || null,
    crash_marker_xy: crashMarker ? { x: crashMarker.x, y: crashMarker.y } : null,
  };
  setPhase('results');
  showPanel('results');
}

function startRun() {
  if (state.phase !== 'ready') return;
  showPanel(null);
  runner.startRun();      // fixes seed / run number and generates the first dynamic obstacles
  layer.reset();
  hud.reset();
  hud.show();
  lastTs = 0;
  setPhase('running');
}

function runAgain() {
  clearTimeout(crashTimer);
  runner.reset();
  layer.reset();
  view.showAllTokens();
  runnerMesh.rotation.x = 0;
  runnerMesh.visible = true;
  state.run_post = null;
  state.results = null;
  rig.reset(runner.track.laneLat(0, 0));
  applySnapshot();
  setPhase('ready');
  showPanel('ready');
  $('start').focus();
}

function onCommand(cmd) {
  if (state.phase === 'ready' && cmd === 'tap') { startRun(); return; }
  if (state.phase !== 'running') return;
  runner.input(cmd);
}

// ----------------------------------------------------------------- about panel + model badge

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function fmtDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : (iso || '');
}

function modelOf(sc) {
  if (sc.schema_version === 2 || sc.schema_version === 3) {
    const a = sc.about;
    return { version: sc.model ? sc.model.version : a.model_version ?? null, fidelity: a.fidelity.level, photos: a.photos.total, legacy: !!a.legacy };
  }
  return { version: null, fidelity: 'legacy', photos: null, legacy: true };
}

function modelBadge(sc) {
  if (sc.schema_version !== 2 && sc.schema_version !== 3) return 'Legacy scene · placement unverified';
  const a = sc.about;
  if (a.legacy) return 'Legacy scene · placement unverified';
  if (a.degraded || a.fidelity.level === 'L0') return `Map-only sketch · updated ${fmtDate(a.last_updated)}`;
  return `Fidelity ${a.fidelity.level} · ${a.photos.total} photos · updated ${fmtDate(a.last_updated)}`;
}

// Opening About from the HUD pauses the run; closing resumes it.
function pauseForAbout() {
  if (state.phase === 'running') { resumePhase = 'running'; setPhase('paused'); }
}
function resumeFromAbout() {
  if (resumePhase === 'running' && state.phase === 'paused') { lastTs = 0; setPhase('running'); }
  resumePhase = null;
}

// ----------------------------------------------------------------- world builders

async function buildGlbWorld(sc) {
  const assets = sc.assets || {};
  if (!assets.glb_url || !assets.manifest_url) throw new Error('The scene has no street model (assets missing).');
  const { loadStreet } = await import('/js/game/glb_loader.js?v=1');
  const { createStreetView } = await import('/js/game/street_view.js?v=1');
  const loaded = await loadStreet({
    glbUrl: assets.glb_url, manifestUrl: assets.manifest_url,
    onProgress: (got, total) => {
      state.progress = { loaded: got, total };
      $('loading-progress').textContent = `Downloading the street model ${Math.round((got / total) * 100)}%`;
    },
  });
  const pd = fromManifest(loaded.manifest);
  const sv = createStreetView(loaded, pd.track, pd.tokens);
  budget.perf.glb_bytes = loaded.bytes;
  return { playData: pd, manifest: loaded.manifest, view: sv, gp: loaded.manifest.gameplay };
}

async function buildLegacyWorld(sc) {
  const [{ buildWorld }, { makeTrack }, gp] = await Promise.all([
    import('/js/game/scene_builder.js?v=4'), import('/js/game/path.js?v=4'), loadGameplay()]);
  const world = await buildWorld(sc, (loaded, total) => {
    state.progress = { loaded, total };
    $('loading-progress').textContent = total ? `Loading textures ${loaded}/${total}` : 'Building the street…';
  });
  const pd = fromLegacyScene(sc, world.track || makeTrack(sc), gp);
  const sv = {
    root: world.world,
    hideToken: (id) => world.hideToken(id), showAllTokens: () => world.showAllTokens(),
    update: (_sw, t) => world.tick(t),
  };
  return { playData: pd, manifest: null, view: sv, gp };
}

// ----------------------------------------------------------------- boot

async function boot() {
  setPhase('loading');
  state.error = null;
  if (!slug) { fail('No street selected — pick one from the gallery.'); return; }
  if (!renderer) { fail('WebGL is not available in this browser.'); return; }
  showPanel('loading');
  $('loading-progress').textContent = 'Fetching scene…';
  let sc;
  try {
    sc = await api.get(`/api/segments/${encodeURIComponent(slug)}/scene`);
  } catch (err) {
    fail(err.message || 'The scene could not be fetched.');
    return;
  }
  state.name = sc.segment.name;
  state.model = modelOf(sc);
  $('loading-name').textContent = sc.segment.name;
  $('ready-name').textContent = sc.segment.name;
  $('model-badge').textContent = modelBadge(sc);
  $('model-badge').dataset.fidelity = state.model.fidelity;
  document.title = `#hero — ${sc.segment.name}`;

  const glb = sc.schema_version === 3;
  let built;
  try {
    built = glb ? await buildGlbWorld(sc) : await buildLegacyWorld(sc);
  } catch (err) {
    fail(glb ? `Could not load the street model: ${err.message}` : `Could not build the street: ${err.message}`);
    return;
  }
  // Commit: nothing below can fail for data reasons, and nothing above touched the page's scene graph.
  if (view) three.remove(view.root);
  scene = sc; playData = built.playData; manifest = built.manifest; view = built.view; gameplay = built.gp;
  source = glb ? 'glb' : 'legacy';
  three.add(view.root);

  if (!runnerMesh) {
    runnerMesh = createRunnerAvatar();
    aoBlob = createAoBlob();
    three.add(runnerMesh);
    three.add(aoBlob);
  }
  runner = new Runner(playData, { gameplay, seed });
  if (layer) { three.remove(layer.group); layer.dispose(); }
  layer = createDynamicLayer(THREE, { scene: sc, track: runner.track, runner });
  three.add(layer.group);
  rig = createCameraRig(camera, runner.track);
  rig.reset(runner.track.laneLat(0, 0));
  if (!hud) hud = createHud();
  resize();
  applySnapshot();
  about = createAbout($('about-panel'), sc, { manifest, onOpen: () => pauseForAbout(), onClose: () => resumeFromAbout() });
  if (!booted) {
    $('about-open').addEventListener('click', () => about.open($('about-open')));
    $('hud-about').addEventListener('click', () => about.open($('hud-about')));
    controls = attachControls(canvas, onCommand, () => state.phase === 'running');
    $('start').addEventListener('click', startRun);
    rafId = requestAnimationFrame(frame);
    booted = true;
  }
  $('start').disabled = false;
  setPhase('ready');
  showPanel('ready');
}

$('error-retry').addEventListener('click', () => { boot(); });

boot();
