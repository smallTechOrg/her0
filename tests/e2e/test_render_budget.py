"""Render budget of the glb-loaded game (spec/architecture.md "Render budget r2", roadmap r2 criterion "Browser loads
the glb"): at 0 / 20 / 40 / 70 m of a run, ``__hero.perf`` = ``draw_calls <= 60``, ``triangles <= 60 000``,
``textures <= 8``, ``texture_mb <= 24`` with three chained copies alive, and ``source === 'glb'`` with no legacy
geometry. 70 m is past the 60 m loop seam of the stand-in street, so the budget is also asserted after the wrap.
"""

# ruff: noqa: F811  (pytest fixtures imported from glb_harness)
from __future__ import annotations

import pytest
from glb_harness import (SLUG, browser, install_real_street, install_street_routes, manifest, open_ready, page,  # noqa: F401
                         real_streets, wait_state)
from playwright.sync_api import Page

DISTANCES = (0, 20, 40, 70)
BUDGET = {"draw_calls": 60, "triangles": 60_000, "textures": 8, "texture_mb": 24}


def _assert_budget(perf: dict, where: str) -> None:
    assert perf["draw_calls"] > 0 and perf["triangles"] > 0, (where, perf)
    for key, limit in BUDGET.items():
        assert perf[key] <= limit, f"{where}: {key}={perf[key]} > {limit}: {perf}"
    assert perf["textures"] >= 2, (where, perf)          # ground + facade atlas are really alive
    assert perf["texture_mb"] > 0.01, (where, perf)


def test_glb_game_stays_inside_the_render_budget_at_0_20_40_70_m(page: Page, live_server: str):
    install_street_routes(page)
    open_ready(page, live_server)
    h = page.evaluate("() => ({source: window.__hero.source, legacy: window.__hero.legacy_geometry})")
    assert h == {"source": "glb", "legacy": False}
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    _assert_budget(page.evaluate("() => window.__hero.perf"), "0 m")
    page.click("#start")
    wait_state(page, "s.phase === 'running'")
    seen = {}
    for d in DISTANCES[1:]:
        s = wait_state(page, f"s.distance_m >= {d} || s.phase !== 'running'", timeout_ms=120_000)
        assert s["phase"] == "running", f"run ended before {d} m: {s}"
        perf = page.evaluate("() => window.__hero.perf")
        _assert_budget(perf, f"{d} m")
        seen[d] = perf
    assert page.evaluate("() => window.__hero.state.loops") >= 1          # the 70 m shot is after the seam
    assert page.evaluate("() => window.__hero.source") == "glb"
    assert seen[70]["fps_avg"] > 0
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_budget_per_glb_matches_the_manifest_mesh_stats(page: Page, live_server: str):
    """One copy of the glb costs exactly the manifest's mesh_stats; the page shows at most 3 copies + extras."""
    m = manifest()["mesh_stats"]
    install_street_routes(page)
    open_ready(page, live_server)
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    g = page.evaluate("() => window.__hero.graph()")
    street = [x for x in g["meshes"] if x["name"] in {n["name"] for n in m["nodes"]}]
    assert street, "the contract node names are present in the scene graph"
    names = {n["name"]: n["triangles"] for n in m["nodes"]}
    per_copy = {}
    for x in street:
        per_copy.setdefault(x["name"], []).append(x["tris"])
    for name, tris in per_copy.items():
        assert all(t == names[name] for t in tris), (name, tris, names[name])
    copies = {len(v) for v in per_copy.values()}
    assert len(copies) == 1 and 1 <= copies.pop() <= 5
    assert m["primitives"] <= 14 and m["triangles"] <= 12_000 and m["materials"] <= 4 and m["textures"] <= 2


@pytest.mark.parametrize("viewport", [{"width": 390, "height": 844}, {"width": 360, "height": 640}])
def test_budget_holds_on_small_phone_viewports(browser, live_server: str, viewport: dict):  # noqa: F811
    ctx = browser.new_context(viewport=viewport, device_scale_factor=3, is_mobile=True, has_touch=True)
    p = ctx.new_page()
    p.set_default_timeout(30_000)
    try:
        install_street_routes(p)
        open_ready(p, live_server)
        p.click("#start")
        wait_state(p, "s.distance_m >= 10")
        _assert_budget(p.evaluate("() => window.__hero.perf"), f"{viewport}")
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["curved", "bellandur"])
def test_real_pipeline_bundles_stay_inside_the_budget_at_0_20_40_70_m(page: Page, live_server: str, real_streets: dict, name: str):
    """The same budget on the bundles built by the real mesh builder (the recorded Bellandur plan and the curved fixture
    plan, 100 m each): textures are the baked/procedural atlases, geometry is the real ribbon/frontage/furniture."""
    st = real_streets[name]
    install_real_street(page, st)
    open_ready(page, live_server, st["slug"])
    assert page.evaluate("() => [window.__hero.source, window.__hero.legacy_geometry]") == ["glb", False]
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    _assert_budget(page.evaluate("() => window.__hero.perf"), f"{name} 0 m")
    page.click("#start")
    wait_state(page, "s.phase === 'running'")
    for d in DISTANCES[1:]:
        s = wait_state(page, f"s.distance_m >= {d} || s.phase !== 'running'", timeout_ms=180_000)
        assert s["phase"] == "running", f"{name}: run ended before {d} m: {s}"
        _assert_budget(page.evaluate("() => window.__hero.perf"), f"{name} {d} m")
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]
