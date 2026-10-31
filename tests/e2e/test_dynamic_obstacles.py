"""Dynamic obstacle layer: instanced detailed models in a real WebGL render, draw-call / triangle budget, placement,
reset / dispose, and the hit object shape (``dynamic: true``)."""

from __future__ import annotations

from gameplay_harness import gameplay, harness_browser, harness_url, hpage  # noqa: F401

SETUP = """
async ({seed, loop, ahead}) => {
  const THREE = await import('/vendor/three.module.js');
  const { Runner, prepareLegacy } = await import('/js/game/runner.js');
  await prepareLegacy();
  const { createDynamicLayer } = await import('/js/game/dynamic_obstacles.js');
  const scene3 = new THREE.Scene();
  const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('c'), antialias: false });
  renderer.setSize(390, 844, false);
  const cam = new THREE.PerspectiveCamera(60, 390 / 844, 0.1, 200);
  const scene = await (await fetch('/fx/scene_example.json')).json();
  let runner, s = seed;
  for (; s < seed + 200; s++) {   // first seed that has obstacles in the window (deterministic search)
    runner = new Runner(scene, { seed: s, runNumber: 1 });
    runner.startRun();
    runner.loops = loop; runner.x = loop * runner.lengthM + ahead;
    if (loop > 0 ? runner.activeObstacles().length >= 3 : runner._obstaclesFor(0).length >= 1) break;
  }
  const layer = createDynamicLayer(THREE, { scene: scene3, track: runner.track, runner });
  window.__t = { THREE, scene3, renderer, cam, runner, layer, scene };
  return true;
}
"""


def _setup(hpage, seed=3, loop=4, ahead=10.0):  # noqa: F811
    return hpage.evaluate(SETUP, {"seed": seed, "loop": loop, "ahead": ahead})


def test_layer_budget_draw_calls_and_triangles_in_a_real_render(hpage):  # noqa: F811
    _setup(hpage)
    res = hpage.evaluate("""() => {
      const { THREE, scene3, renderer, cam, runner, layer } = window.__t;
      let maxCalls = 0, maxTris = 0, maxVisible = 0, kinds = new Set(), instanced = 0;
      for (const seed of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) {
        runner.seedOpt = seed; runner.startRun();
        for (const loop of [0, 3, 6]) for (let a = 0; a < 70; a += 10) {
          runner.loops = loop; runner.x = loop * runner.lengthM + a;
          layer.update();
          const p = runner.track.world(runner.x, runner.track.laneLat(a, 1));
          cam.position.set(p.x, 2.5, p.z + 5); cam.lookAt(p.x, 0, p.z - 20);
          renderer.info.reset(); renderer.render(scene3, cam);
          maxCalls = Math.max(maxCalls, renderer.info.render.calls); maxTris = Math.max(maxTris, renderer.info.render.triangles);
          maxVisible = Math.max(maxVisible, layer.visibleIds.length);
          for (const o of runner.activeObstacles()) kinds.add(o.type);
        }
      }
      instanced = layer.group.children.filter((c) => c.isInstancedMesh).length;
      return { maxCalls, maxTris, maxVisible, kinds: [...kinds].sort(), instanced, children: layer.group.children.length, isGroup: layer.group.isGroup };
    }""")
    assert res["isGroup"] and res["children"] <= 16 and res["instanced"] == res["children"]
    # one InstancedMesh per type, up to 3 material groups each (matte, gloss, contact shadow)
    assert 1 <= res["maxCalls"] <= 3 * res["children"], res
    # detailed models (~0.4-4.6k triangles each); the whole frame stays inside test_render_budget's 60k
    assert 0 < res["maxTris"] <= 20000, res
    assert res["maxVisible"] >= 3 and len(res["kinds"]) >= 5, f"render must really show a mix of types: {res}"
    assert not hpage.hero_errors


def test_instances_match_active_obstacles_and_sit_in_their_lane(hpage):  # noqa: F811
    _setup(hpage)
    res = hpage.evaluate("""() => {
      const { THREE, runner, layer } = window.__t;
      layer.update();
      const obs = runner.activeObstacles();
      const counts = {};
      for (const m of layer.group.children) if (m.name.startsWith('dyn_')) counts[m.name.slice(4)] = m.visible ? m.count : 0;
      const want = {};
      for (const o of obs) want[o.type] = (want[o.type] || 0) + 1;
      // first instance of the first visible type: compare against track.world of the obstacle centre
      const o = obs.find((q) => q.type === layer.group.children.find((m) => m.visible && m.name.startsWith('dyn_')).name.slice(4));
      const mesh = layer.group.children.find((m) => m.name === 'dyn_' + o.type);
      const mat = new THREE.Matrix4(); mesh.getMatrixAt(0, mat);
      const p = new THREE.Vector3().setFromMatrixPosition(mat);
      const w = runner.track.world(o.x_abs + o.length_m / 2, runner.track.laneLat(o.x_m + o.length_m / 2, o.lane));
      return { counts, want, n: obs.length, dx: Math.abs(p.x - w.x), dz: Math.abs(p.z - w.z), ids: layer.visibleIds, obsIds: obs.map((q) => q.id) };
    }""")
    assert res["n"] >= 2
    assert {k: v for k, v in res["counts"].items() if v} == res["want"]
    assert res["dx"] < 1e-4 and res["dz"] < 1e-4
    assert sorted(res["ids"]) == sorted(res["obsIds"])


def test_hit_object_is_dynamic_and_carries_the_obstacle_shape(hpage, gameplay):  # noqa: F811
    _setup(hpage, seed=3, loop=0, ahead=0.0)
    res = hpage.evaluate("""() => {
      const { runner } = window.__t;
      runner.reset(); runner.startRun();
      const targets = runner._obstaclesFor(0);
      const first = targets[0];
      // place the runner in the obstacle's lane just before it, past the start grace, and step through it
      runner.lane = first.lane; runner.laneFrom = first.lane; runner.elapsed = runner.invulnUntil + 1;
      runner.x = first.x_m - 0.5;
      let hit = null;
      for (let i = 0; i < 400 && !hit; i++) for (const ev of runner.update(1 / 60)) if (ev.type === 'hit' && ev.object.dynamic) hit = ev;
      return { hit, first, health: runner.health, snap: runner.snapshot() };
    }""")
    hit = res["hit"]
    assert hit is not None, "walking into a spawned obstacle must hit it"
    obj = hit["object"]
    assert obj["dynamic"] is True and obj["id"].startswith("dyn-0-") and obj["lane"] == res["first"]["lane"]
    for key in ("type", "x_m", "length_m", "action", "size_m", "label", "loop"):
        assert key in obj
    assert hit["health"] == res["health"] == gameplay["runner"]["health_max"] - gameplay["runner"]["hit_damage"]
    assert res["snap"]["last_hit"]["dynamic"] is True


def test_reset_hides_everything_and_dispose_removes_the_group(hpage):  # noqa: F811
    _setup(hpage)
    res = hpage.evaluate("""() => {
      const { scene3, runner, layer } = window.__t;
      layer.update();
      const before = layer.visibleIds.length;
      const inScene = scene3.children.includes(layer.group);
      layer.reset();
      const afterReset = layer.group.children.filter((m) => m.visible).length + layer.visibleIds.length;
      runner.reset();               // dynamic obstacles dropped until startRun()
      layer.update();
      const noRun = layer.visibleIds.length;
      layer.dispose();
      return { before, inScene, afterReset, noRun, removed: !scene3.children.includes(layer.group) };
    }""")
    assert res["before"] >= 2 and res["inScene"]
    assert res["afterReset"] == 0 and res["noRun"] == 0 and res["removed"]


def test_error_path_layer_needs_a_runner_with_gameplay(hpage):  # noqa: F811
    err = hpage.evaluate("""async () => {
      const THREE = await import('/vendor/three.module.js');
      const { createDynamicLayer } = await import('/js/game/dynamic_obstacles.js');
      try { createDynamicLayer(THREE, { scene: null, track: null, runner: {} }); return null; } catch (e) { return String(e.message); }
    }""")
    assert err and "runner.gp" in err and "required" in err


def test_every_static_hazard_without_glb_geometry_gets_a_visible_model(hpage):  # noqa: F811
    """A fixed hazard the glb does not draw where it collides (low branch, debris, pothole, ...) is drawn by the layer,
    so the runner never takes damage from something invisible."""
    _setup(hpage, seed=3, loop=0, ahead=0.0)
    res = hpage.evaluate("""async () => {
      const { THREE, runner, scene3 } = window.__t;
      const { createDynamicLayer, STATIC_HAZARD_MODELS } = await import('/js/game/dynamic_obstacles.js');
      runner.hazards = [
        { id: 'h-lb', type: 'low_branch', x_m: 20, length_m: 1, lanes: [0, 1], action: 'slide', size_m: { w: 1.8, h: 0.6, d: 1 } },
        { id: 'h-db', type: 'debris', x_m: 30, length_m: 1, lanes: [0], action: 'jump', size_m: { w: 1, h: 0.4, d: 1 } },
        { id: 'h-pl', type: 'pole', x_m: 25, length_m: 0.3, lanes: [0], action: 'dodge', size_m: { w: 0.3, h: 7, d: 0.3 } },
      ];
      const layer = createDynamicLayer(THREE, { scene: scene3, track: runner.track, runner });
      runner.reset(); runner.startRun(); runner.x = 5;
      layer.update();
      const names = layer.group.children.filter((m) => m.name.startsWith('static_') && m.visible).map((m) => m.name).sort();
      return { names, ids: layer.visibleStaticIds.sort(), mapped: Object.keys(STATIC_HAZARD_MODELS) };
    }""")
    assert res["names"] == ["static_debris", "static_low_branch"], res
    assert res["ids"] == ["h-db", "h-lb"], res  # the pole is drawn by the glb itself
