// Play page entry: loads the scene via the public API, builds the world, runs the loop, posts the run.
// Phases: loading → ready → running → crashed → results | error. window.__hero.state is read-only.
import * as THREE from '/vendor/three.module.js';
import { api } from '/js/api.js?v=3';
import { buildWorld, makeRunnerMesh, latToX } from '/js/game/scene_builder.js?v=3';
import { Runner, C } from '/js/game/runner.js?v=3';
import { attachControls } from '/js/game/controls.js?v=3';
import { createHud } from '/js/game/hud.js?v=3';
import { renderResults } from '/js/game/results.js?v=3';

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
window.__hero = { page: 'play', get state() { return structuredClone(state); } };

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
const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 140);
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
let lastTs = 0, rafId = 0, crashTimer = 0, camX = 0;

function applySnapshot() {
  const snap = runner.snapshot();
  Object.assign(state, snap);
  hud.update(snap);
  return snap;
}

function placeRunner() {
  const lat = runner.visualLatM(easeOut);
  const x = latToX(lat);
  const y = runner.jumpY();
  runnerMesh.position.set(x, y, 0);
  const body = runnerMesh.userData.body;
  body.scale.y = runner.sliding ? 0.5 : 1;
  body.position.y = runner.sliding ? 0.4 : 0.67;
  runnerMesh.userData.shadow.position.y = 0.02 - y;
  camX += (x - camX) * 0.2;
  const bob = reducedMotion || !runner || runner.crashed ? 0 : Math.sin(runner.elapsed * 9) * 0.04;
  camera.position.set(camX, 3.1 + bob, 5.6);
  camera.lookAt(camX, 0.9, -9);
}

function syncWorld() {
  world.world.position.z = runner.xWrapped;
  const t = runner.elapsed;
  for (const m of world.allTokens) m.rotation.y = t * 3;
}

function frame(ts) {
  rafId = requestAnimationFrame(frame);
  const dt = Math.min(MAX_DT_S, lastTs ? (ts - lastTs) / 1000 : 0);
  lastTs = ts;
  if (state.phase === 'running') {
    const events = runner.update(dt);
    for (const ev of events) {
      if (ev.type === 'token') { const m = world.tokensByCopy[0].get(ev.id); if (m) m.visible = false; }
      if (ev.type === 'loop') { for (const m of world.tokensByCopy[0].values()) m.visible = true; }
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
  const { crashMarker } = renderResults(panels.results, { scene, snap, post, onRunAgain: runAgain });
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
  for (const map of world.tokensByCopy) for (const m of map.values()) m.visible = true;
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
  $('loading-name').textContent = scene.segment.name;
  $('ready-name').textContent = scene.segment.name;
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
  camX = latToX(scene.cross_section.lanes[0].lat_m);
  resize();
  applySnapshot();
  controls = attachControls(canvas, onCommand, () => state.phase === 'running');
  $('start').addEventListener('click', startRun);
  $('start').disabled = false;
  setPhase('ready');
  showPanel('ready');
  rafId = requestAnimationFrame(frame);
}

boot();
