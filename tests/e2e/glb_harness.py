"""Shared support for the glb-renderer e2e tests (S2): the stand-in bundle served through Playwright route
interception, the phone browser/page fixtures and small helpers.

The real endpoints (``/api/segments/{slug}/model.glb|manifest.json``, S1c/S10) are not needed: the contract
stand-in bundle ``tests/fixtures/export/contract_curved/`` is the executable form of ``spec/portability.md`` and
the v3-lite scene fixture ``tests/fixtures/scene_v3/contract_curved.json`` points at it via ``assets.*_url``.
The static frontend, ``/api/gameplay.json`` and the Three.js vendor files come from the real ``live_server``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tests" / "fixtures" / "export" / "contract_curved"
SCENE_V3 = ROOT / "tests" / "fixtures" / "scene_v3" / "contract_curved.json"
TILE = ROOT / "tests" / "e2e" / "fixtures" / "tile_1280.png"
SLUG = "contract-curved-street"
PHONE_VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def scene_v3() -> dict:
    return json.loads(SCENE_V3.read_text())


def manifest() -> dict:
    return json.loads((BUNDLE / "street.manifest.json").read_text())


def glb_bytes() -> bytes:
    return (BUNDLE / "street.glb").read_bytes()


def envelope(data: dict) -> str:
    return json.dumps({"ok": True, "data": data})


def install_street_routes(page: Page, *, scene: dict | None = None, glb: bytes | None = None,
                          manifest_body: str | bytes | None = None, glb_status: int = 200, slug: str = SLUG) -> dict:
    """Serve scene / glb / manifest / tile / run-post for the stand-in street. Returns a dict of request counters."""
    scene = scene if scene is not None else scene_v3()
    glb = glb if glb is not None else glb_bytes()
    manifest_body = manifest_body if manifest_body is not None else (BUNDLE / "street.manifest.json").read_bytes()
    seen = {"scene": 0, "glb": 0, "manifest": 0, "runs": []}

    def on_scene(route):
        seen["scene"] += 1
        route.fulfill(status=200, content_type="application/json", body=envelope(scene))

    def on_glb(route):
        seen["glb"] += 1
        if glb_status != 200:
            route.fulfill(status=glb_status, content_type="text/plain", body="nope")
        else:
            route.fulfill(status=200, content_type="model/gltf-binary", body=glb)

    def on_manifest(route):
        seen["manifest"] += 1
        route.fulfill(status=200, content_type="application/json", body=manifest_body)

    def on_runs(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            seen["runs"].append(body)
            route.fulfill(status=200, content_type="application/json", body=envelope({
                "run_id": f"run-{len(seen['runs'])}", "is_new_best": True, "best_score": body.get("score", 0), "rank": 1}))
        else:
            route.continue_()

    page.route(f"**/api/segments/{slug}/scene", on_scene)
    page.route(f"**/api/segments/{slug}/model.glb*", on_glb)
    page.route(f"**/api/segments/{slug}/manifest.json*", on_manifest)
    page.route(f"**/api/segments/{slug}/runs", on_runs)
    page.route("**/media/imagery/s4-curved-tile.png", lambda r: r.fulfill(status=200, content_type="image/png", body=TILE.read_bytes()))
    return seen


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        yield b
        b.close()


@pytest.fixture()
def page(browser: Browser) -> Page:
    ctx = browser.new_context(viewport=PHONE_VIEWPORT, device_scale_factor=2, is_mobile=True, has_touch=True)
    p = ctx.new_page()
    p.set_default_timeout(30_000)
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.on("console", lambda m: errors.append(m.text)
         if m.type == "error" and "Failed to load resource" not in m.text else None)
    p.hero_errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


def hero_state(page: Page) -> dict:
    return page.evaluate("() => window.__hero && window.__hero.state")


def wait_state(page: Page, predicate_js: str, timeout_ms: int = 30_000) -> dict:
    page.wait_for_function(f"() => {{ const s = window.__hero && window.__hero.state; return !!s && ({predicate_js}); }}",
                           timeout=timeout_ms)
    return hero_state(page)


def open_ready(page: Page, base: str, slug: str = SLUG) -> None:
    page.goto(f"{base}/play/?segment={slug}")
    page.wait_for_selector('[data-hero-page="play"]')
    page.wait_for_selector("#start:not([disabled])", timeout=60_000)
    wait_state(page, "s.phase === 'ready'")


def start_run(page: Page, base: str, slug: str = SLUG) -> None:
    open_ready(page, base, slug)
    page.click("#start")
    wait_state(page, "s.phase === 'running'")


# ---------------------------------------------------------------- bundles built by the REAL pipeline (S6/S7/S8/S10)
PLAN_DIR = ROOT / "tests" / "fixtures" / "plan"
SCENE_DIR = ROOT / "tests" / "fixtures" / "scene_v3"
REAL_STREETS = ("curved", "bellandur")      # tests/fixtures/plan/<name>.plan.json + tests/fixtures/scene_v3/<name>.json


@pytest.fixture(scope="session")
def real_streets(tmp_path_factory) -> dict[str, dict]:
    """``{name: {slug, scene, glb, manifest}}`` built once per session by ``build_and_store_exports`` from the committed
    StreetPlan fixtures (procedural textures: no observations), scene = the committed v3-lite scene with ``assets`` filled in."""
    from hero.export.pipeline import build_and_store_exports
    from hero.model.street_plan_types import StreetPlan

    out: dict[str, dict] = {}
    data_dir = tmp_path_factory.mktemp("real_streets")
    for name in REAL_STREETS:
        plan = StreetPlan.model_validate_json((PLAN_DIR / f"{name}.plan.json").read_text())
        rec = build_and_store_exports(slug=plan.slug, version=1, model=None, scene=None, plan=plan, observations={}, data_dir=data_dir)
        scene = json.loads((SCENE_DIR / f"{name}.json").read_text())
        slug = scene["segment"]["slug"]
        scene["assets"] = {"mesh_sha256": rec.mesh_sha256, "sha8": rec.sha8,
                           "glb_url": f"/api/segments/{slug}/model.glb?v={rec.sha8}",
                           "manifest_url": f"/api/segments/{slug}/manifest.json?v={rec.sha8}",
                           "xodr_url": f"/api/segments/{slug}/street.xodr?v={rec.sha8}", "gameplay_url": "/api/gameplay.json",
                           "glb_bytes": rec.glb_bytes, "triangles": rec.triangles, "primitives": rec.primitives}
        out[name] = {"slug": slug, "scene": scene, "glb": (data_dir / rec.files["glb"]).read_bytes(),
                     "manifest": (data_dir / rec.files["manifest"]).read_bytes()}
    return out


def install_real_street(page: Page, street: dict) -> dict:
    return install_street_routes(page, scene=street["scene"], glb=street["glb"], manifest_body=street["manifest"], slug=street["slug"])
