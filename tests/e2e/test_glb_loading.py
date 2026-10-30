"""The browser loads the street from street.glb + street.manifest.json (stand-in bundle, served by route interception).

Covers: source === 'glb' with no legacy geometry, the parsed node set, required-node / corrupt-file / HTTP-error /
manifest-error screens with a working Retry, progress text, and the scene's ``assets`` urls being the ones fetched.
"""

# ruff: noqa: F811  (pytest fixtures imported from glb_harness)
from __future__ import annotations

import json

from glb_harness import (SLUG, browser, glb_bytes, hero_state, install_street_routes, manifest, open_ready, page,  # noqa: F401
                         scene_v3, wait_state)
from playwright.sync_api import Page


def test_v3_street_loads_from_the_glb_and_is_not_legacy(page: Page, live_server: str):
    seen = install_street_routes(page)
    open_ready(page, live_server)
    h = page.evaluate("() => ({source: window.__hero.source, legacy: window.__hero.legacy_geometry, perf: window.__hero.perf})")
    assert h["source"] == "glb" and h["legacy"] is False
    assert seen["scene"] == 1 and seen["glb"] == 1 and seen["manifest"] == 1
    assert h["perf"]["glb_bytes"] == len(glb_bytes())
    assert page.get_by_test_id("model-badge").inner_text().startswith("Fidelity L2 · 48 photos")
    # the legacy geometry module is never fetched for a v3 street
    assert not [u for u in page.evaluate("() => performance.getEntriesByType('resource').map((e) => e.name)")
                if u.endswith("scene_builder.js") or "/scene_builder.js?" in u]
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_glb_node_set_is_the_contract_set_and_materials_are_swapped(page: Page, live_server: str):
    install_street_routes(page)
    open_ready(page, live_server)
    info = page.evaluate("""async () => {
      const { loadStreet } = await import('/js/game/glb_loader.js?v=1');
      const s = await loadStreet({ glbUrl: '/api/segments/contract-curved-street/model.glb', manifestUrl: '/api/segments/contract-curved-street/manifest.json' });
      return { nodes: Object.keys(s.nodes), missing: s.missing,
               mats: Object.fromEntries(s.materials.map((m) => [m.name, { type: m.type, map: !!m.map, vc: m.vertexColors, clr: m.userData.heroClearance === true }])),
               furnMats: Object.fromEntries(Object.entries(s.nodes).filter(([k]) => k.startsWith('Furniture_')).map(([k, n]) => [k, [n.material.name, n.material.userData.heroClearance === true]])) };
    }""")
    expected = {"Footpath", "Footpath_Opposite", "Road", "Kerb", "Ground_L", "Ground_R", "Frontage_L", "Frontage_R",
                "Frontage_Caps", "Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals"}
    assert set(info["nodes"]) == expected and info["missing"] == []
    assert all(m["type"] == "MeshLambertMaterial" for m in info["mats"].values())
    assert info["mats"]["mat_ground"]["map"] is True and info["mats"]["mat_frontage"]["map"] is True
    assert info["mats"]["mat_ground"]["vc"] is True      # COLOR_0 multiplies the ground atlas
    assert info["mats"]["mat_flat"]["clr"] is True       # mat_flat carries the camera-clearance clip (userData.heroClearance)
    assert [k for k, m in info["mats"].items() if m["clr"]] == ["mat_flat"]
    assert info["furnMats"] and all(name == "mat_flat" and clr for name, clr in info["furnMats"].values()), info["furnMats"]


def _error_screen(page: Page, live_server: str) -> str:
    page.goto(f"{live_server}/play/?segment={SLUG}")
    page.wait_for_selector("#panel-error:not([hidden])", timeout=30_000)
    assert page.is_hidden("#panel-ready") and page.is_hidden("#panel-loading")
    assert hero_state(page)["phase"] == "error"
    assert page.get_by_test_id("error-retry").is_visible()
    assert page.query_selector('#panel-error a[href="/"]') is not None
    return page.inner_text("#error-reason")


def test_corrupted_glb_shows_the_error_screen_and_retry_recovers(page: Page, live_server: str):
    good = glb_bytes()
    bad = good[:12] + b"\xff" * 64 + good[76:2000]            # glTF magic ok, chunk table garbage
    install_street_routes(page, glb=bad)
    reason = _error_screen(page, live_server)
    assert "street model" in reason.lower()
    # fix the server side, press Retry: the same page boots into the Ready overlay without a reload
    page.unroute(f"**/api/segments/{SLUG}/model.glb*")
    page.route(f"**/api/segments/{SLUG}/model.glb*", lambda r: r.fulfill(status=200, content_type="model/gltf-binary", body=good))
    page.get_by_test_id("error-retry").click()
    page.wait_for_selector("#panel-ready:not([hidden])")
    wait_state(page, "s.phase === 'ready'")
    assert page.evaluate("() => window.__hero.source") == "glb"
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_not_a_glb_at_all_shows_the_error_screen(page: Page, live_server: str):
    install_street_routes(page, glb=b"<html>proxy login page</html>")
    assert "not a glb" in _error_screen(page, live_server).lower()


def test_truncated_glb_shows_the_error_screen(page: Page, live_server: str):
    install_street_routes(page, glb=glb_bytes()[:5000])
    assert "street model" in _error_screen(page, live_server).lower()


def test_http_error_on_the_glb_shows_the_error_screen_with_the_status(page: Page, live_server: str):
    install_street_routes(page, glb_status=404)
    assert "HTTP 404" in _error_screen(page, live_server)


def test_manifest_of_an_unknown_schema_shows_the_error_screen(page: Page, live_server: str):
    m = manifest()
    m["schema"] = "hero-street-manifest/9"
    import json
    install_street_routes(page, manifest_body=json.dumps(m))
    assert "manifest" in _error_screen(page, live_server).lower()


def test_scene_without_assets_shows_the_error_screen(page: Page, live_server: str):
    sc = scene_v3()
    del sc["assets"]
    install_street_routes(page, scene=sc)
    assert "no street model" in _error_screen(page, live_server).lower()


def test_glb_without_a_road_node_is_rejected(page: Page, live_server: str):
    import json
    import struct
    raw = glb_bytes()
    jlen = struct.unpack("<I", raw[12:16])[0]
    doc = json.loads(raw[20:20 + jlen])
    for n in doc["nodes"]:
        if n.get("name") == "Road":
            n["name"] = "Tarmac"
    new_json = json.dumps(doc, separators=(",", ":")).encode()
    new_json += b" " * ((4 - len(new_json) % 4) % 4)
    rest = raw[20 + jlen:]
    total = 12 + 8 + len(new_json) + len(rest)
    bad = raw[:8] + struct.pack("<I", total) + struct.pack("<I", len(new_json)) + raw[16:20] + new_json + rest
    install_street_routes(page, glb=bad)
    assert "required parts: Road" in _error_screen(page, live_server)


def test_scene_fetch_failure_has_retry(page: Page, live_server: str):
    page.route(f"**/api/segments/{SLUG}/scene", lambda r: r.abort())
    reason = _error_screen(page, live_server)
    assert reason


def test_vendored_gltf_loader_is_pinned_served_and_in_the_import_map(page: Page, live_server: str):
    import json
    from pathlib import Path

    import httpx
    from hero.tools import vendor_three

    assert vendor_three.verify() == [], "frontend/vendor does not match VENDOR.json"
    rec = json.loads((Path(vendor_three.VENDOR_DIR) / "VENDOR.json").read_text())
    assert rec["three_version"] == "0.170.0" and len(rec["files"]) == 3
    assert all(len(f["sha256"]) == 64 for f in rec["files"].values())
    for rel in ("three/examples/jsm/loaders/GLTFLoader.js", "three/examples/jsm/utils/BufferGeometryUtils.js", "three.module.js"):
        r = httpx.get(f"{live_server}/vendor/{rel}", timeout=10)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"], rel
    html = httpx.get(f"{live_server}/play/", timeout=10).text
    assert '<script type="importmap">' in html and '"three": "/vendor/three.module.js"' in html
    assert html.index("importmap") < html.index("/js/game/main.js")            # the map must precede the first module


# ------------------------------------------------------------------ the Street View backdrop (pano_backdrop.js)


def _tiny_jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (120, 140, 160)).save(buf, "JPEG")
    return buf.getvalue()


def _install_panoramas(page: Page, count: int = 2) -> dict:
    seen = {"manifest": 0, "images": 0}
    jpeg = _tiny_jpeg()
    panos = [{"pano_id": f"p{i}", "s": 10.0 + 40.0 * i, "t": 3.0, "lat": 12.9, "lon": 77.6, "frames": 4, "capture_date": "2026-04",
              "url": f"/media/panoramas/{SLUG}/p{i}_deadbeef.jpg", "width": 64, "height": 32} for i in range(count)]
    manifest = {"schema": "hero-panoramas/1", "slug": SLUG, "key": "deadbeef", "length_s": 100.0, "camera_height_m": 2.5,
                "bearing0_deg": 90.0, "panoramas": panos}

    def on_manifest(route):
        seen["manifest"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(manifest))

    def on_image(route):
        seen["images"] += 1
        route.fulfill(status=200, content_type="image/jpeg", body=jpeg)

    page.route(f"**/api/segments/{SLUG}/panoramas.json", on_manifest)
    page.route(f"**/media/panoramas/{SLUG}/*", on_image)
    return seen


PANO_PROBE = """() => {
  const names = {};
  for (const m of window.__hero.graph().meshes) { names[m.name] = m.material; }
  const g = window.__hero.graph();
  return { pano: window.__hero.pano, road: names.Road, frontage: names.Frontage_L, poles: names.Furniture_Poles,
           hasDome: g.meshes.some((m) => m.name === 'PanoSky' && m.visible), skyVisible: g.meshes.some((m) => m.name === 'Sky' && m.visible),
           fog: g.fog };
}"""


def test_street_view_panoramas_are_projected_onto_the_street_when_the_export_stitched_them(page: Page, live_server: str):
    install_street_routes(page)
    seen = _install_panoramas(page)
    open_ready(page, live_server)
    info = page.evaluate(PANO_PROBE)
    assert seen["manifest"] == 1 and seen["images"] >= 2           # the two the start needs were primed
    assert info["pano"]["count"] == 2 and info["pano"]["cached"] >= 2
    assert info["road"] == "ShaderMaterial" and info["frontage"] == "ShaderMaterial"
    assert info["poles"] == "MeshLambertMaterial"                   # furniture / obstacles stay lit models
    assert info["hasDome"] and not info["skyVisible"]
    assert info["fog"]["near"] >= 100                               # the photo carries its own haze
    page.click("#start")
    page.wait_for_function("() => window.__hero.state.distance_m > 30", timeout=60_000)
    moved = page.evaluate("() => window.__hero.pano")
    assert moved["current"] is not None and moved["next"] is not None
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_without_panoramas_the_baked_textures_and_gradient_sky_stay(page: Page, live_server: str):
    install_street_routes(page)
    page.route(f"**/api/segments/{SLUG}/panoramas.json",
               lambda route: route.fulfill(status=404, content_type="application/json", body='{"ok":false,"error":{"code":"not_found","message":"no panoramas yet"}}'))
    open_ready(page, live_server)
    info = page.evaluate(PANO_PROBE)
    assert info["pano"] is None and info["road"] == "MeshLambertMaterial"
    assert info["skyVisible"] and not info["hasDome"] and info["fog"]["near"] == 30
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]
