"""Render the game at 390x844 (software GL) on the Bellandur recorded scene and the curved fixture street and
screenshot at run distances 0/20/40/70 m.

    uv run python -m tests.e2e.visual.capture --label before|after --scene bellandur|curved|all --out DIR

Deterministic driving: an init script replaces requestAnimationFrame so every frame advances a fixed 1/30 s, and
freezes the frame loop the moment ``__hero.state.distance_m`` reaches the target (so the shot is at that distance,
not "roughly later"). No frontend file is touched. A sidecar JSON (``--meta``, default ``<out>/../<label>_meta.json``)
records ``window.__hero.source`` (if the game exposes it), actual distances, perf and console errors.

Server: by default starts its OWN ``hero-serve`` on --port (8735) against a scratch COPY of data/ (db via sqlite
``.backup``); data/ and port 8711 are never touched. ``--base-url`` uses an already running server instead.
Scene source for bellandur: ``recorded`` (the fixture JSON, intercepted; default for label=before) or ``server``
(whatever the copied-db server compiles; default otherwise).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
BELLANDUR_JSON = ROOT / "tests/fixtures/recorded/bellandur_scene_v2.json"
CURVED_JSON = ROOT / "tests/e2e/fixtures/s4_scene_curved_v2.json"
CURVED_TILE = ROOT / "tests/e2e/fixtures/tile_1280.png"
BELLANDUR_SLUG = "bellandur-main-road-n-2"
DISTANCES = (0, 20, 40, 70)
VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]

INIT_JS = """
(() => {
  const cap = window.__cap = { freeze: false, t: 0, target: null, hit: false };
  const realRaf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => realRaf(() => {
    if (cap.freeze) { window.requestAnimationFrame(cb); return; }   // keep polling, run no game frame
    cap.t += 1000 / 30;
    cb(cap.t);
    const h = window.__hero, s = h && h.state;
    if (cap.target !== null && s && s.phase === 'running' && s.distance_m >= cap.target) { cap.freeze = true; cap.hit = true; }
  });
})();
"""


def start_server(port: int, scratch: Path) -> subprocess.Popen:
    import os
    data = scratch / "data"
    if scratch.exists():
        shutil.rmtree(scratch)
    data.mkdir(parents=True)
    src = ROOT / "data"
    for sub in ("crops", "geocode", "imagery", "osm", "previews", "uploads"):
        if (src / sub).exists():
            shutil.copytree(src / sub, data / sub)
    import sqlite3   # consistent online copy without needing the sqlite3 CLI
    with sqlite3.connect(src / "hero.db") as a, sqlite3.connect(data / "hero.db") as b:
        a.backup(b)
    env = {**os.environ, "HERO_DATA_DIR": str(data), "HERO_DATABASE_URL": f"sqlite:///{data / 'hero.db'}",
           "HERO_PORT": str(port), "HERO_HOST": "127.0.0.1"}
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["uv", "run", "python", "-m", "hero.tools.recompile", "--slug", BELLANDUR_SLUG], cwd=ROOT, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen(["uv", "run", "hero-serve"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/segments", timeout=2).read()
            return proc
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("scratch hero server did not come up")


def capture_scene(browser, base: str, name: str, slug: str, scene_json: Path | None, out: Path, meta: dict, street: dict | None = None) -> None:
    page: Page = browser.new_page(viewport=VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.add_init_script(INIT_JS)
    if scene_json is not None:
        raw = json.loads(scene_json.read_text())
        body = json.dumps({"ok": True, "data": raw.get("data", raw) if raw.get("ok") else raw})
        page.route(f"**/api/segments/{slug}/scene", lambda r: r.fulfill(status=200, content_type="application/json", body=body))
    if street is not None:                    # v3 stand-in: scene + glb + manifest from the real pipeline, intercepted
        from tests.e2e.glb_harness import install_real_street
        install_real_street(page, street)
    elif name == "curved":
        tile = CURVED_TILE.read_bytes()
        page.route("**/media/imagery/s4-curved-tile.png", lambda r: r.fulfill(status=200, content_type="image/png", body=tile))
    page.goto(f"{base}/play/?segment={slug}")
    page.wait_for_selector("#start:not([disabled])", timeout=90_000)
    page.add_style_tag(content="#flash { display: none !important; }")   # the hit flash would tint the evidence shots
    page.wait_for_timeout(500)
    info = {"slug": slug, "scene_source": "intercepted-v3-pipeline-build" if street else ("recorded-json" if scene_json else "server"), "shots": {}}
    for d in DISTANCES:
        if d == 0:
            page.wait_for_timeout(300)
        else:
            if page.evaluate("() => window.__hero.state.phase") == "ready":
                page.click("#start")
            page.evaluate("(t) => { window.__cap.target = t; window.__cap.hit = false; window.__cap.freeze = false; }", d)
            page.wait_for_function("() => window.__cap.hit || ['crashed','results'].includes(window.__hero.state.phase)", timeout=240_000, polling=100)
        st = page.evaluate("() => ({d: window.__hero.state.distance_m, phase: window.__hero.state.phase, perf: window.__hero.perf})")
        if d == 0:        # the start scrim would hide the scene: shoot the 0 m frame with it transparent (it stays clickable)
            page.evaluate("() => { document.getElementById('panel-ready').style.opacity = '0'; }")
        page.screenshot(path=str(out / f"{name}_{d}m.png"))
        if d == 0:
            page.evaluate("() => { document.getElementById('panel-ready').style.opacity = ''; }")
        info["shots"][f"{d}m"] = {"distance_m": round(st["d"], 2), "phase": st["phase"], "perf": st["perf"]}
    info["hero_source"] = page.evaluate("() => (window.__hero && window.__hero.source) || null")
    info["console_errors"] = errors[:20]
    meta[name] = info
    page.close()


def build_curved_street() -> dict:
    """Compile the committed curved StreetPlan fixture with the real exporter (procedural textures)."""
    import tempfile
    from hero.export.pipeline import build_and_store_exports
    from hero.model.street_plan_types import StreetPlan
    plan = StreetPlan.model_validate_json((ROOT / "tests/fixtures/plan/curved.plan.json").read_text())
    data_dir = Path(tempfile.mkdtemp(prefix="curved_"))
    rec = build_and_store_exports(slug=plan.slug, version=1, model=None, scene=None, plan=plan, observations={}, data_dir=data_dir)
    scene = json.loads((ROOT / "tests/fixtures/scene_v3/curved.json").read_text())
    slug = scene["segment"]["slug"]
    scene["assets"] = {"mesh_sha256": rec.mesh_sha256, "sha8": rec.sha8, "glb_url": f"/api/segments/{slug}/model.glb?v={rec.sha8}",
                       "manifest_url": f"/api/segments/{slug}/manifest.json?v={rec.sha8}", "xodr_url": f"/api/segments/{slug}/street.xodr?v={rec.sha8}",
                       "gameplay_url": "/api/gameplay.json", "glb_bytes": rec.glb_bytes, "triangles": rec.triangles, "primitives": rec.primitives}
    return {"slug": slug, "scene": scene, "glb": (data_dir / rec.files["glb"]).read_bytes(), "manifest": (data_dir / rec.files["manifest"]).read_bytes()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=["before", "after"])
    ap.add_argument("--scene", default="all", choices=["bellandur", "curved", "all"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--meta", type=Path, help="sidecar JSON path (default: <out>/../<label>_meta.json)")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--base-url", help="use a running server instead of starting a scratch one")
    ap.add_argument("--bellandur-source", choices=["recorded", "server"])
    ap.add_argument("--scratch", type=Path, default=Path("/private/tmp/claude-501/-Users-sai/ac0adbe2-83fb-4f31-bc20-64ea4822e608/scratchpad/s5/serve"))
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    meta_path = a.meta or a.out.parent / f"{a.label}_meta.json"
    bsrc = a.bellandur_source or ("recorded" if a.label == "before" else "server")
    proc = None
    meta: dict = {"label": a.label, "viewport": VIEWPORT, "bellandur_source": bsrc}
    try:
        base = a.base_url
        if not base:
            proc = start_server(a.port, a.scratch)
            base = f"http://127.0.0.1:{a.port}"
        with sync_playwright() as p:
            browser = p.chromium.launch(args=CHROMIUM_ARGS)
            if a.scene in ("bellandur", "all"):
                capture_scene(browser, base, "bellandur", BELLANDUR_SLUG, BELLANDUR_JSON if bsrc == "recorded" else None, a.out, meta)
            if a.scene in ("curved", "all"):
                if a.label == "after":
                    street = build_curved_street()
                    capture_scene(browser, base, "curved", street["slug"], None, a.out, meta, street)
                else:
                    capture_scene(browser, base, "curved", "s4-curved-fixture-w", CURVED_JSON, a.out, meta)
            browser.close()
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    meta_path.write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
