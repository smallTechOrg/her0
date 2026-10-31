"""Screenshot every dynamic obstacle model under the game's sky / fog / lights: a close-up sheet (one tile per type and
palette variant) and a game-camera view (3.8 m up, 6.6 m behind, looking 12 m ahead) with obstacles in all lanes.

    uv run python -m tests.e2e.visual.obstacles --out docs/evidence/obstacles

Serves ``frontend/`` with the gameplay harness (no hero server, no data/ needed) and renders with software GL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
from gameplay_harness import CHROMIUM_ARGS, _start

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<script type="importmap">{"imports": {"three": "/vendor/three.module.js"}}</script></head>
<body style="margin:0;background:#000"><canvas id=c></canvas></body></html>"""

RENDER_JS = """
async ({ mode, w, h, variant }) => {
  const THREE = await import('/vendor/three.module.js');
  const { createAtmosphere } = await import('/js/game/atmosphere.js');
  const { buildObstacleGeometry, createObstacleMaterials } = await import('/js/game/dynamic_obstacles.js');
  const gp = await (await fetch('/api/gameplay.json')).json();
  const canvas = document.getElementById('c');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1); renderer.setSize(w, h, false);
  const scene = new THREE.Scene();
  createAtmosphere(scene);
  const mats = createObstacleMaterials(THREE);
  // ground: asphalt road, a raised paver footpath with a kerb (plain colours, roughly the street's)
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), new THREE.MeshLambertMaterial({ color: 0x5b5a57 }));
  ground.rotation.x = -Math.PI / 2; scene.add(ground);
  const fp = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.15, 400), new THREE.MeshLambertMaterial({ color: 0x9c958a }));
  fp.position.set(-2.4, 0.075, 0); scene.add(fp);
  const kerb = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.18, 400), new THREE.MeshLambertMaterial({ color: 0xc9c4b8 }));
  kerb.position.set(-1.15, 0.09, 0); scene.add(kerb);
  const types = Object.keys(gp.obstacle_types);
  const meshes = {};
  for (const t of types) {
    const m = new THREE.InstancedMesh(buildObstacleGeometry(THREE, t, gp.obstacle_types[t].size_m), mats, 8);
    m.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(24).fill(1), 3);
    m.count = 0; m.frustumCulled = false; scene.add(m); meshes[t] = m;
  }
  const { PALETTES } = await import('/js/game/dynamic_obstacles.js').then((mod) => ({ PALETTES: mod.OBSTACLE_PALETTES }));
  const place = (t, x, y, z, yaw, v) => {
    const m = meshes[t], i = m.count++;
    m.setMatrixAt(i, new THREE.Matrix4().compose(new THREE.Vector3(x, y, z), new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), yaw), new THREE.Vector3(1, 1, 1)));
    const c = new THREE.Color(PALETTES[t][v % PALETTES[t].length]);
    m.instanceColor.setXYZ(i, c.r, c.g, c.b);
    m.instanceMatrix.needsUpdate = true; m.instanceColor.needsUpdate = true;
  };
  const cam = new THREE.PerspectiveCamera(62, w / h, 0.1, 140);
  const shots = [];
  const info = {};
  for (const t of types) {
    const g = meshes[t].geometry;
    info[t] = { triangles: g.getAttribute('position').count / 3, groups: g.groups.length };
  }
  if (mode === 'game') {
    // runner lane 1 at x=0; footpath lane 0 at x=-2.4 (on the raised path), road lane 2 at x=+2.2; travel is -z
    const lay = [['two_wheeler', -2.4, 0.15, -8, 1], ['cone', 0, 0, -12, 0], ['parked_car', 2.3, 0, -16, 2], ['stall', -2.4, 0.15, -22, 0],
      ['barricade', 0, 0, -27, 0], ['pothole', 2.2, 0, -31, 0], ['low_branch', -2.4, 0.15, -36, 0], ['parked_car', 0.1, 0, -43, 0],
      ['two_wheeler', 2.0, 0, -48, 3], ['cone', -1.6, 0.15, -52, 1], ['stall', -2.4, 0.15, -58, 2]];
    for (const [t, x, y, z, v] of lay) place(t, x, y, z, v === 1 && t === 'two_wheeler' ? 0.3 : 0, v);
    cam.position.set(0, 3.8, 6.6); cam.lookAt(0, 0, -12);
    renderer.render(scene, cam);
    shots.push(canvas.toDataURL('image/png'));
  } else {
    for (const t of types) {
      meshes[t].count = 0;
    }
    for (const t of types) {
      const s = gp.obstacle_types[t].size_m;
      for (const tt of types) meshes[tt].count = 0;
      place(t, 0, 0, 0, 0.7, variant);
      const dist = Math.max(2.4, Math.max(s.w, s.d, s.h) * 1.35);
      cam.position.set(dist * 0.45, Math.max(1.4, s.h * 0.9 + dist * 0.35), dist);
      cam.lookAt(0, s.h * 0.42, 0);
      cam.aspect = w / h; cam.updateProjectionMatrix();
      renderer.render(scene, cam);
      shots.push(canvas.toDataURL('image/png'));
    }
  }
  return { shots, info, types };
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/evidence/obstacles")
    args = ap.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    srv, url = _start()
    import base64
    with sync_playwright() as p:
        b = p.chromium.launch(args=CHROMIUM_ARGS)
        page = b.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/__obstacles.html", lambda r: r.fulfill(body=PAGE, content_type="text/html"))
        page.goto(f"{url}/__obstacles.html")
        game = page.evaluate(RENDER_JS, {"mode": "game", "w": 390, "h": 844, "variant": 0})
        (out / "game_view.png").write_bytes(base64.b64decode(game["shots"][0].split(",")[1]))
        page.goto(f"{url}/__obstacles.html")
        sheet = page.evaluate(RENDER_JS, {"mode": "sheet", "w": 640, "h": 400, "variant": 0})
        for t, shot in zip(sheet["types"], sheet["shots"]):
            (out / f"{t}.png").write_bytes(base64.b64decode(shot.split(",")[1]))
        (out / "obstacles_meta.json").write_text(json.dumps({"models": sheet["info"], "console_errors": errors}, indent=1) + "\n")
        b.close()
    srv.shutdown()
    print(json.dumps(sheet["info"]), errors or "no console errors")


if __name__ == "__main__":
    main()
