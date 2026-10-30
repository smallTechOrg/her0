// Play page entry: loads the scene via the public API, builds the world, runs the loop, posts the run.
// Phases: loading → ready → running → crashed → results | error. window.__hero.state is read-only.
import * as THREE from '/vendor/three.module.js';
import { api } from '/js/api.js?v=3';
import { buildWorld, makeRunnerMesh } from '/js/game/scene_builder.js?v=4';
import { Runner, C } from '/js/game/runner.js?v=4';
import { attachControls } from '/js/game/controls.js?v=4';
import { createHud } from '/js/game/hud.js?v=4';
import { renderResults } from '/js/game/results.js?v=4';
import { createAbout } from '/js/game/about.js?v=4';

const MAX_DT_S = 0.1;
const CRASH_STUMBLE_MS = 400;
const MAX_DPR = 2;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const easeOut = reducedMotion ? (t) => t : (t) => 1 - (1 - t) * (1 - t);

const $ = (id) => document.getElementById(id);
const panels = { loading: $('panel-loading'), error: $('panel-error'), ready: $('panel-ready'), results: $('results') };

const state = {
  phase: 'loading', slug: null, name: null, error: null, progress: { loaded: 0, total: 0 },
  distance_m: 0, score: 0, tokens: 0, lane: 0, lane_name: 'footpath', action: 'none', loops: 0, speed: C.SPEED_START,
  crashed_into: null, elapsed_s: 0, run_post: null, results: null,
};
// Read-only debug handle (drives the e2e tests). `perf` is sampled from renderer.info after each render.
const perf = { draw_calls: 0, triangles: 0, fps_avg: 0 };
window.__hero = { page: 'play', get state() { return structuredClone(state); }, get perf() { return { ...perf }; } };

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
state.slug = slug;

// ----------------------------------------------------------------- renderer

const canvas = $('game');
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'low-power' });
} catch (err) {
  renderer = null;
}
const camera = new THREE.PerspectiveCamera(62, 1, 0.2, 140);
const three = new THREE.Scene();
three.background = new THREE.Color(0x0f1115);
three.fog = new THREE.Fog(0x0f1115, 60, 120);
three.add(new THREE.HemisphereLight(0xdfe8ff, 0x3a3328, 1.1));
const sun = new THREE.DirectionalLight(0xffffff, 0.9);
sun.position.set(-6, 14, 4);
three.add(sun);

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

let scene = null, world = null, runner = null, runnerMesh = null, hud = null, controls = null;
let lastTs = 0, rafId = 0, crashTimer = 0, camLat = 0, about = null, resumePhase = null;
const fpsWindow = [];

function applySnapshot() {
  const snap = runner.snapshot();
  Object.assign(state, snap);
  hud.update(snap);
  return snap;
}

const CAM_BACK_M = 5.6, CAM_LOOK_AHEAD_M = 9;

// Runner and camera follow the path: position = P(s) + lat * n(s), heading = theta(s).
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
  runnerMesh.userData.shadow.position.y = 0.02 - y;
  camLat += (lat - camLat) * 0.2;
  const bob = reducedMotion || !runner || runner.crashed ? 0 : Math.sin(runner.elapsed * 9) * 0.04;
  const back = track.world(sw - CAM_BACK_M, camLat);
  const ahead = track.world(sw + CAM_LOOK_AHEAD_M, camLat);
  camera.position.set(back.x, 3.1 + bob, back.z);
  camera.lookAt(ahead.x, 0.9, ahead.z);
  if (scene.schema_version === 2) updateCorridorState(sw, lat, w);
}

const r3 = (v) => Math.round(v * 1000) / 1000;
function updateCorridorState(sw, lat, w) {
  const track = runner.track;
  const fp = track.footpathAt(sw);
  state.s = r3(sw);
  state.lat_m = r3(lat);
  state.footpath_centre_m = fp ? r3(fp.centre) : null;
  state.footpath_offset_m = fp ? r3(Math.abs(track.laneLat(sw, 0) - fp.centre)) : null;
  state.on_strip = track.stripAt(sw, lat);
  state.world = { x: r3(w.x), z: r3(w.z) };
  state.heading_deg = r3(track.headingDeg(sw));
}

function syncWorld() {
  world.tick(runner.elapsed);
}

function samplePerf(dtMs) {
  if (dtMs > 0 && dtMs < 1000) {
    fpsWindow.push(dtMs);
    if (fpsWindow.length > 60) fpsWindow.shift();
    perf.fps_avg = Math.round(1000 / (fpsWindow.reduce((a, b) => a + b, 0) / fpsWindow.length) * 10) / 10;
  }
  if (renderer) {
    perf.draw_calls = renderer.info.render.calls;
    perf.triangles = renderer.info.render.triangles;
  }
}

function frame(ts) {
  rafId = requestAnimationFrame(frame);
  const rawDt = lastTs ? (ts - lastTs) / 1000 : 0;
  const dt = Math.min(MAX_DT_S, rawDt);
  lastTs = ts;
  if (state.phase === 'running') {
    const events = runner.update(dt);
    for (const ev of events) {
      if (ev.type === 'token') world.hideToken(ev.id);
      if (ev.type === 'loop') world.showAllTokens();
      if (ev.type === 'hit') onHit();
      if (ev.type === 'crash') onCrash();
    }
    applySnapshot();
  } else if (state.phase === 'crashed') {
    runner.elapsed += dt;
    runnerMesh.rotation.x = Math.min(Math.PI / 2, runnerMesh.rotation.x + dt * 6);
  }
  if (runner) { syncWorld(); placeRunner(); if (runnerMesh && state.phase === 'running') runnerMesh.visible = !runner.invulnerable || Math.floor(runner.elapsed * 12) % 2 === 0; }
  if (renderer) renderer.render(three, camera);
  samplePerf(rawDt * 1000);
}

function onHit() {
  if (reducedMotion) return;
  const flash = $('flash');
  flash.classList.remove('on');
  void flash.offsetWidth;
  flash.classList.add('on');
}

function onCrash() {
  setPhase('crashed');
  if (!reducedMotion) {
    const flash = $('flash');
    flash.classList.remove('on');
    void flash.offsetWidth; // restart the animation
    flash.classList.add('on');
  }
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
  const { crashMarker } = renderResults(panels.results, {
    scene, snap, post, onRunAgain: runAgain, onAbout: (opener) => about.open(opener),
  });
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
  hud.reset();
  hud.show();
  lastTs = 0;
  setPhase('running');
}

function runAgain() {
  clearTimeout(crashTimer);
  runner.reset();
  world.showAllTokens();
  runnerMesh.rotation.x = 0;
  runnerMesh.visible = true;
  state.run_post = null;
  state.results = null;
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
  if (sc.schema_version === 2) {
    const a = sc.about;
    return { version: sc.model.version, fidelity: a.fidelity.level, photos: a.photos.total, legacy: !!a.legacy };
  }
  return { version: null, fidelity: 'legacy', photos: null, legacy: true };
}

function modelBadge(sc) {
  if (sc.schema_version !== 2) return 'Legacy scene · placement unverified';
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

// ----------------------------------------------------------------- boot

async function boot() {
  if (!slug) { fail('No street selected — pick one from the gallery.'); return; }
  if (!renderer) { fail('WebGL is not available in this browser.'); return; }
  showPanel('loading');
  $('loading-progress').textContent = 'Fetching scene…';
  try {
    scene = await api.get(`/api/segments/${encodeURIComponent(slug)}/scene`);
  } catch (err) {
    fail(err.message || 'The scene could not be fetched.');
    return;
  }
  state.name = scene.segment.name;
  state.model = modelOf(scene);
  $('loading-name').textContent = scene.segment.name;
  $('ready-name').textContent = scene.segment.name;
  $('model-badge').textContent = modelBadge(scene);
  $('model-badge').dataset.fidelity = state.model.fidelity;
  document.title = `#hero — ${scene.segment.name}`;
  try {
    world = await buildWorld(scene, (loaded, total) => {
      state.progress = { loaded, total };
      $('loading-progress').textContent = total ? `Loading textures ${loaded}/${total}` : 'Building the street…';
    });
  } catch (err) {
    fail(`Could not build the street: ${err.message}`);
    return;
  }
  three.add(world.world);
  runnerMesh = makeRunnerMesh();
  three.add(runnerMesh);
  runner = new Runner(scene);
  hud = createHud();
  camLat = runner.track.laneLat(0, 0);
  resize();
  applySnapshot();
  about = createAbout($('about-panel'), scene, { onOpen: () => pauseForAbout(), onClose: () => resumeFromAbout() });
  $('about-open').addEventListener('click', () => about.open($('about-open')));
  $('hud-about').addEventListener('click', () => about.open($('hud-about')));
  controls = attachControls(canvas, onCommand, () => state.phase === 'running');
  $('start').addEventListener('click', startRun);
  $('start').disabled = false;
  setPhase('ready');
  showPanel('ready');
  rafId = requestAnimationFrame(frame);
}

boot();
