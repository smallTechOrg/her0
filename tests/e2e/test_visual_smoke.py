"""Visual smoke of the glb-loaded game at 390x844 on the stand-in street: screenshots at 0 / 20 / 40 / 70 m are
analysed as pixels (not just "a canvas exists"): a lit sky gradient that is not black, a textured (high-variance)
ground, a changing picture while running, nothing in the middle of the screen that is one flat colour, no text
sprites in the scene graph. Plus a real render test of the camera-clearance shader clip.
"""

# ruff: noqa: F811  (pytest fixtures imported from glb_harness)
from __future__ import annotations

import io

from glb_harness import browser, install_street_routes, open_ready, page, wait_state  # noqa: F401
from PIL import Image, ImageStat
from playwright.sync_api import Page

DISTANCES = (0, 20, 40, 70)


def _shot(page: Page) -> Image.Image:
    return Image.open(io.BytesIO(page.screenshot())).convert("RGB")


def _region(img: Image.Image, x0: float, y0: float, x1: float, y1: float) -> Image.Image:
    w, h = img.size
    return img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def _lum(img: Image.Image) -> float:
    r, g, b = ImageStat.Stat(img).mean
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_screenshots_show_sky_textured_ground_and_no_flat_slab(page: Page, live_server: str):
    page.emulate_media(reduced_motion="reduce")                              # no red hit-flash over the shots
    install_street_routes(page)
    open_ready(page, live_server)
    shots: dict[int, Image.Image] = {}
    page.wait_for_timeout(400)
    shots[0] = _shot(page)
    page.click("#start")
    wait_state(page, "s.phase === 'running'")
    for d in DISTANCES[1:]:
        s = wait_state(page, f"s.distance_m >= {d} || s.phase !== 'running'", timeout_ms=120_000)
        assert s["phase"] == "running", s
        shots[d] = _shot(page)
    h = page.evaluate("() => ({source: window.__hero.source, legacy: window.__hero.legacy_geometry, sprites: window.__hero.graph().sprites})")
    assert h == {"source": "glb", "legacy": False, "sprites": 0}

    for d, img in shots.items():
        assert img.size == (780, 1688)                                     # 390x844 @ DPR 2
        # the (ready overlay darkens shot 0): look at the sky only once the HUD has replaced the overlay
        if d == 0:
            continue
        # sky = bluish, bright pixels in the upper part of the screen
        top = _region(img, 0.0, 0.0, 1.0, 0.40).resize((78, 68))
        px = list(top.getdata())
        sky = sum(1 for r, g, b in px if b > 120 and b > r + 10) / len(px)
        assert sky > 0.08, f"{d} m: no sky gradient in the upper screen ({sky:.2f} sky-coloured)"
        ground = _region(img, 0.05, 0.62, 0.95, 0.98)
        sd = sum(ImageStat.Stat(ground).stddev) / 3
        assert sd > 14, f"{d} m: ground looks like a flat slab (stddev {sd:.1f})"
        assert len(set(ground.resize((24, 24)).getdata())) > 120, f"{d} m: ground has too few distinct colours"
    # the world moves: shots differ
    a, b2 = shots[20].resize((78, 169)), shots[40].resize((78, 169))
    mad = sum(abs(x - y) for p, q in zip(a.getdata(), b2.getdata()) for x, y in zip(p, q)) / (78 * 169 * 3)
    assert mad > 4, f"20 m and 40 m shots are nearly identical (mad {mad:.1f})"
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_no_giant_billboards_big_labels_or_tag_boxes_in_the_screenshot(page: Page, live_server: str):
    page.emulate_media(reduced_motion="reduce")
    """The old failure modes: a '?' box / big text label covering the lower screen and a billboard filling it. The glb world
    has no sprites; the lower-middle of the screen is the path (not one flat grey rectangle)."""
    install_street_routes(page)
    open_ready(page, live_server)
    page.click("#start")
    for d in (10, 30, 50):
        wait_state(page, f"s.distance_m >= {d} || s.phase !== 'running'", timeout_ms=120_000)
        img = _shot(page)
        centre = _region(img, 0.3, 0.45, 0.7, 0.85)
        tiles = [centre.crop((i * 62, j * 135, i * 62 + 62, j * 135 + 135)) for i in range(5) for j in range(3)]
        flat = sum(1 for t in tiles if sum(ImageStat.Stat(t).stddev) / 3 < 1.5)
        assert flat <= 3, f"{d} m: {flat}/15 tiles in the centre are flat colour (a big slab / label?)"
    g = page.evaluate("() => window.__hero.graph()")
    assert g["sprites"] == 0
    assert all(x["material"] != "SpriteMaterial" for x in g["meshes"])


def test_camera_clearance_shader_clips_furniture_near_the_camera(page: Page, live_server: str):
    """Real render with the patched material: a box 0.5 m from the camera is clipped away, one at 3 m is drawn, one at
    ~1.15 m is dithered (partially drawn)."""
    install_street_routes(page)
    open_ready(page, live_server)
    res = page.evaluate("""async () => {
      const THREE = await import('three');
      const { attachClearance, clearanceUniforms } = await import('/js/game/camera_rig.js?v=1');
      const r = new THREE.WebGLRenderer({ antialias: false });
      r.setSize(64, 64, false);
      r.setClearColor(0x000000, 1);
      const rt = new THREE.WebGLRenderTarget(64, 64);
      const out = {};
      for (const [name, dist] of [['inside', 0.5], ['dither', 1.15], ['outside', 3.0]]) {
        const scene = new THREE.Scene();
        const mat = attachClearance(new THREE.MeshBasicMaterial({ color: 0xffffff }));
        const box = new THREE.Mesh(new THREE.BoxGeometry(40, 40, 0.2), mat);   // a wall filling the view
        box.position.set(0, 3, -dist - 0.1);
        scene.add(box);
        const cam = new THREE.PerspectiveCamera(60, 1, 0.05, 50);
        cam.position.set(0, 3, 0);
        // The clip is a cylinder around the camera in xz: measure fragments by their xz distance to the camera.
        clearanceUniforms.uHeroCam.value.set(0, 3, 0);
        r.setRenderTarget(rt);
        r.render(scene, cam);
        const px = new Uint8Array(64 * 64 * 4);
        r.readRenderTargetPixels(rt, 0, 0, 64, 64, px);
        let lit = 0;
        for (let i = 0; i < px.length; i += 4) if (px[i] > 128) lit += 1;
        out[name] = lit / (64 * 64);
      }
      r.dispose();
      return out;
    }""")
    # fragments of the wall at distance d from the camera in xz: the centre of the view is at ~0.5/1.15/3.0 m, the edges further
    assert res["outside"] > 0.97, res
    assert res["inside"] < 0.35, res                      # the middle of the wall is within 0.8 m -> clipped; far corners remain
    assert 0.05 < res["dither"] < 0.97, res
    assert res["inside"] < res["dither"] < res["outside"]
