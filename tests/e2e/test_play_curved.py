"""Scene v2 on a CURVED corridor, in a real (phone-viewport) browser.

The scene is served through the real public API: a segment row is inserted into the test DB and finalised
with a Scene v2. Preferred source: the S1d fixture ``fixtures/scene_curved_v2.json``; until that exists this
module generates its own tiny curved scene (``build_curved_scene``) and writes it to
``fixtures/s4_scene_curved_v2.json`` (deterministic; regenerated on every run, validated against
``hero.model.scene_v2.SceneV2``).

Asserted: the runner stays on the traced footpath strip along the bend (``__hero.state.on_strip`` /
``footpath_offset_m``), follows the path in the world frame (checked against the scene's own samples), the
draw-call / triangle budget (``__hero.perf``), the About panel (text, focus trap, Esc, HUD pause), the
descriptions on the results card, and the gallery fidelity chips.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from hero.config import get_settings
from hero.db import repo
from hero.model.scene_v2 import SceneV2
from hero.scene.schema import LatLon, Quality
from tests.fixtures.street_fixtures import minimal_scene_v2_dict

FIXTURES = Path(__file__).parent / "fixtures"
S1D_PATH = FIXTURES / "scene_curved_v2.json"
OWN_PATH = FIXTURES / "s4_scene_curved_v2.json"
TILE_PATH = FIXTURES / "tile_1280.png"
SLUG = "s4-curved-fixture-w"
NAME = "Curved Fixture Road, Indiranagar (S4 e2e)"
PHONE_VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
TILE_URL = "/media/imagery/s4-curved-tile.png"

# ----------------------------------------------------------------- the tiny curved scene (script)

LENGTH = 60.0
RADIUS = 80.0
STEP = 2.5
PX_PER_M = 6.87


def _footpath_centre(s: float) -> float:
    """Footpath centre (game lateral) - wobbles by +-0.6 m with a period of the segment so the loop seam is smooth."""
    return 5.8 + 0.6 * math.sin(2 * math.pi * s / LENGTH)


def _state_at(s: float) -> str | None:
    """Footpath state of the 2.5 m cell starting at station ``s`` (``None`` = missing)."""
    if 15.0 <= s < 20.0:
        return "blocked"
    if 20.0 <= s < 30.0:
        return "broken"
    if 30.0 <= s < 37.5:
        return None
    return "intact"


def build_curved_scene() -> dict:
    """A right-hand bend (R = 80 m, 60 m long, footpath on the left => theta(s) = -s/R), per spec/data.md."""
    base = minimal_scene_v2_dict(LENGTH, slug=SLUG, with_object=False)
    s_vals = [i * STEP for i in range(int(LENGTH // STEP) + 1)]
    samples, stations = [], []
    x = z = 0.0
    prev_theta = 0.0
    for i, s in enumerate(s_vals):
        theta = -s / RADIUS  # turn_sign = -1 for a left footpath
        if i:
            x += math.sin(prev_theta) * STEP  # P(s+ds) = P(s) + f(s) ds, f = (sin t, -cos t)
            z += -math.cos(prev_theta) * STEP
        prev_theta = theta
        n = (math.cos(theta), math.sin(theta))

        def px(lat: float) -> tuple[float, float]:
            return (640.0 + PX_PER_M * (x + lat * n[0]), 900.0 + PX_PER_M * (z + lat * n[1]))

        centre = _footpath_centre(s)
        state = _state_at(s)
        lo, hi = centre - 0.9, centre + 0.9
        samples.append({"s": s, "x": round(x, 6), "z": round(z, 6), "theta": round(theta, 8),
                        "tile_px": [round(v, 2) for v in px(0.0)]})
        stations.append({
            "s": s, "lane_lat": [round(centre, 4), round(lo - 1.1, 4), round(lo - 2.9, 4)],
            "footpath": None if state is None else {"lo": round(lo, 4), "hi": round(hi, 4), "state": state,
                                                    "material": "paver", "height_m": 0.15, "confidence": 0.85},
            "strips": [{"kind": "carriageway", "lo": round(lo - 0.2 - 9.0, 4), "hi": round(lo - 0.2, 4),
                        "material": "asphalt", "height_m": 0.0, "confidence": 0.7}],
            "ground_uv": [[round(px(-30.0)[0] / 1280, 5), round(px(-30.0)[1] / 1280, 5)],
                          [round(px(30.0)[0] / 1280, 5), round(px(30.0)[1] / 1280, 5)]],
        })
    ranges, start, cur = [], 0.0, _state_at(0.0)
    for s in s_vals[1:] + [LENGTH]:
        nxt = _state_at(s) if s < LENGTH else "end"
        if nxt != cur:
            ranges.append({"from_m": start, "to_m": s, "state": cur or "missing", "confidence": 0.8})
            start, cur = s, nxt
    lane_mean = [sum(st["lane_lat"][k] for st in stations) / len(stations) for k in range(3)]

    def obj(oid, typ, label, x_m, length_m, lanes, action, lateral, w, h, d, conf, *, scenery=False, desc="", ev=""):
        return {
            "id": oid, "entity_id": oid, "type": typ, "label": label, "x_m": x_m, "length_m": length_m, "lanes": lanes,
            "action": action, "lateral_m": lateral, "yaw_deg": 3.0, "size_m": {"w": w, "h": h, "d": d},
            "confidence": conf, "scenery": scenery, "description": desc,
            "evidence": {"observation_count": 3, "sources": {"street_view": 3, "satellite": 0, "upload": 0},
                         "summary": ev, "observation_ids": ["obs-0001"], "state": "present"},
            "billboard_url": None, "source": None, "latlon": {"lat": 12.9707, "lon": 77.6414}, "tile_px": [640.0, 700.0]}

    objects = [
        obj("ent-0000000001", "parked_vehicle", "auto-rickshaw parked on footpath", 8.0, 2.6, [0, 1], "dodge",
            _footpath_centre(9.3), 1.4, 1.7, 2.6, 0.86, desc="Yellow auto-rickshaw parked half on the footpath.",
            ev="Seen in 3 Street View frames (2 from <= 10 m)."),
        obj("ent-0000000002", "pole", "street-light pole", 22.0, 0.4, [0], "dodge", _footpath_centre(22.2), 0.4, 6.0, 0.4,
            0.9, desc="Concrete street-light pole in the middle of the walkway.", ev="Seen in 2 Street View frames."),
        obj("ent-0000000003", "signboard", "shop signboard", 40.0, 0.6, [0], "slide", _footpath_centre(40.3), 1.2, 1.1, 0.4,
            0.45, desc="Hanging shop sign, partly hidden.", ev="Seen in 1 Street View frame."),
        obj("ent-0000000004", "pothole", "pothole on the carriageway", 40.0, 1.0, [2], "jump", _footpath_centre(40.5) - 3.8, 0.8, 0.1, 1.0, 0.7,
            desc="Pothole in the carriageway.", ev="Seen in 2 Street View frames."),
        obj("ent-0000000005", "debris", "rubble pile", 50.0, 1.4, [0], "dodge", _footpath_centre(50.7), 1.2, 0.6, 1.4,
            0.8, desc="Rubble pile on the walkway.", ev="Seen in 2 Street View frames."),
        obj("ent-0000000006", "tree", "rain tree beyond the lanes", 12.0, 3.0, [], "none", 14.0, 3.0, 8.0, 3.0, 0.9,
            scenery=True, desc="Large rain tree behind the footpath.", ev="Seen in 3 Street View frames."),
    ]
    tokens = [{"id": f"t{i}", "x_m": float(x_m), "lane": 0, "reason": "intact_footpath",
               "latlon": {"lat": 12.9707, "lon": 77.6414}, "tile_px": [640.0, 700.0 - 6.0 * i]}
              for i, x_m in enumerate((4, 14, 33, 56), start=1)]
    buildings = [
        {"id": "bld-osm-1", "source": "osm", "footprint": [[2.0, 10.0], [22.0, 10.0], [22.0, 18.0], [2.0, 18.0]],
         "height_m": 9.6, "color_hex": "#c9b79c", "facade_url": None, "confidence": 0.9, "side": "modelled",
         "description": "Three-storey cream shopfront block."},
        {"id": "bld-osm-2", "source": "mixed", "footprint": [[26.0, 9.5], [52.0, 9.5], [52.0, 16.0], [26.0, 16.0]],
         "height_m": 7.0, "color_hex": "#a98f7a", "facade_url": TILE_URL, "confidence": 0.7, "side": "modelled",
         "description": "Two-storey block with a photographed facade."},
        {"id": "bld-osm-3", "source": "osm", "footprint": [[5.0, -14.0], [30.0, -14.0], [30.0, -20.0], [5.0, -20.0]],
         "height_m": 12.0, "color_hex": "#8b97a6", "facade_url": None, "confidence": 0.8, "side": "opposite",
         "description": "Apartment block across the road."},
    ]
    scene = dict(base)
    scene.update({
        "segment": {**base["segment"], "name": NAME, "length_m": LENGTH, "heading_deg": 0.0},
        "ground": {**base["ground"], "tile_url": TILE_URL},
        "path": {"length_m": LENGTH, "samples": samples},
        "corridor": {"step_m": STEP, "ground_half_m": 30.0, "stations": stations},
        "cross_section": {"road_width_m": 9.0, "lanes": [
            {"index": 0, "name": "footpath", "kind": "footpath", "lat_m": round(lane_mean[0], 4), "width_m": 1.8},
            {"index": 1, "name": "road_edge", "kind": "road_edge", "lat_m": round(lane_mean[1], 4), "width_m": 1.6},
            {"index": 2, "name": "road", "kind": "road", "lat_m": round(lane_mean[2], 4), "width_m": 2.0}],
            "footpath_coverage": ranges},
        "buildings": buildings, "objects": objects, "tokens": tokens,
        "walkability": {"footpath_coverage_pct": 71.0, "hazard_count": 5, "crossing_present": False, "score": 55},
    })
    scene["about"] = {
        **base["about"], "model_version": 2,
        "fidelity": {"level": "L2", "score": 2.0, "label": "Multi-view",
                     "histogram_m": {"L0": 0.0, "L1": 20.0, "L2": 40.0, "L3": 0.0}},
        "photos": {"total": 48, "street_view": 44, "satellite": 4, "user": 0},
        "low_confidence_parts": [{"s_from": 40.0, "s_to": 55.0,
                                  "reason": "2 objects below 60 % confidence; footpath state seen from one side only"}],
        "summary": "This street is modelled at fidelity L2 (multi-view) from 48 photos (model v2) - 60 m of Curved Fixture Road.",
        "improve_hint": "A close-up photo around 40-55 m would raise it to L3.",
    }
    scene["model"] = {"version": 2, "model_hash": "b" * 64, "compiled_by": "hero.model.compile/1"}
    return scene


def _load_scene_dict() -> dict:
    if S1D_PATH.exists():
        scene = json.loads(S1D_PATH.read_text())
    else:
        scene = build_curved_scene()
        OWN_PATH.write_text(json.dumps(scene, indent=1) + "\n")
    scene["segment"]["slug"] = SLUG
    scene["segment"]["name"] = NAME
    SceneV2.model_validate(scene)  # the fixture itself must be a valid Scene v2
    return scene


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def curved_segment(test_db: str) -> dict:
    scene_dict = _load_scene_dict()
    scene = SceneV2.model_validate(scene_dict)
    data_dir = get_settings().data_dir_abs
    (data_dir / "imagery").mkdir(parents=True, exist_ok=True)
    (data_dir / "previews" / "s4-curved").mkdir(parents=True, exist_ok=True)
    tile_url = scene.ground.tile_url
    if tile_url and tile_url.startswith("/media/"):
        dest = data_dir / tile_url.removeprefix("/media/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TILE_PATH, dest)
    shutil.copyfile(TILE_PATH, data_dir / "previews" / "s4-curved" / "thumb.png")
    seg = scene.segment
    existing = repo.get_segment(seg.slug)
    if existing is None:
        existing = repo.create_segment(repo.SegmentCreate(
            name=seg.name, city=seg.city, slug=seg.slug, start=LatLon(lat=seg.start.lat, lon=seg.start.lon),
            end=LatLon(lat=seg.end.lat, lon=seg.end.lon), footpath_side=seg.footpath_side))
    repo.finalize_segment_scene(existing.id, run_id=scene.generation.run_id, scene=scene, quality=Quality.full,
                                thumbnail_path="previews/s4-curved/thumb.png")
    return {"slug": seg.slug, "scene": scene_dict}


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
    p.set_default_timeout(20_000)
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.on("console", lambda m: errors.append(m.text)
         if m.type == "error" and "Failed to load resource" not in m.text else None)
    p.hero_errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


def _state(page: Page) -> dict:
    return page.evaluate("() => window.__hero && window.__hero.state")


def _perf(page: Page) -> dict:
    return page.evaluate("() => window.__hero.perf")


def _wait_state(page: Page, predicate_js: str, timeout_ms: int = 20_000) -> dict:
    page.wait_for_function(f"() => {{ const s = window.__hero && window.__hero.state; return !!s && ({predicate_js}); }}",
                           timeout=timeout_ms)
    return _state(page)


def _open_ready(page: Page, live_server: str, slug: str) -> None:
    page.goto(f"{live_server}/play/?segment={slug}")
    page.wait_for_selector('[data-hero-page="play"]')
    page.wait_for_selector("#start:not([disabled])")
    _wait_state(page, "s.phase === 'ready'")


def _start_run(page: Page, live_server: str, slug: str) -> None:
    _open_ready(page, live_server, slug)
    page.click("#start")
    _wait_state(page, "s.phase === 'running'")


def _m(n: float) -> str:
    return f"{round(n, 1):g}"


def _gap_ranges(scene: dict) -> list[tuple[float, float]]:
    return [(r["from_m"], r["to_m"]) for r in scene["cross_section"]["footpath_coverage"] if r["state"] == "missing"]


def _theta_at(scene: dict, s: float) -> float:
    samples = scene["path"]["samples"]
    i = max(j for j, p in enumerate(samples[:-1]) if p["s"] <= s)
    a, b = samples[i], samples[i + 1]
    return a["theta"] + (b["theta"] - a["theta"]) * (s - a["s"]) / (b["s"] - a["s"])


def _expected_world(scene: dict, s: float, lat: float) -> tuple[float, float]:
    """P(s) + lat * n(s) from the scene's own path samples (linear interpolation)."""
    samples = scene["path"]["samples"]
    i = max(j for j, p in enumerate(samples[:-1]) if p["s"] <= s)
    a, b = samples[i], samples[i + 1]
    t = (s - a["s"]) / (b["s"] - a["s"])
    x, z, th = (a[k] + (b[k] - a[k]) * t for k in ("x", "z", "theta"))
    return x + lat * math.cos(th), z + lat * math.sin(th)


# ----------------------------------------------------------------- fixture sanity


def test_the_curved_scene_really_bends_and_has_a_missing_gap(curved_segment: dict):
    scene = curved_segment["scene"]
    thetas = [p["theta"] for p in scene["path"]["samples"]]
    assert thetas[0] == 0.0 and thetas[-1] < -0.3, "the corridor must turn by > 17 degrees"
    assert any(st["footpath"] is None for st in scene["corridor"]["stations"]), "needs a missing-footpath gap"
    assert {r["state"] for r in scene["cross_section"]["footpath_coverage"]} >= {"intact", "broken", "missing"}
    centres = [st["lane_lat"][0] for st in scene["corridor"]["stations"]]
    assert max(centres) - min(centres) > 0.3, "the footpath centre must wander so lane-following is observable"


# ----------------------------------------------------------------- gallery chips


def test_gallery_fidelity_chips_for_l2_l0_and_legacy_streets(page: Page, live_server: str, curved_segment: dict):
    """Chips are rendered from the `model` summary field (spec/api.md); the listing is served per case so the
    assertions do not depend on which sibling slice has landed the API change."""
    listing = [
        {"slug": "chip-l2", "name": "Chip L2", "city": "Bengaluru", "quality": "full", "length_m": 100.2,
         "thumbnail_url": None, "best_score": None, "best_distance_m": None, "run_count": 0,
         "walkability": {"footpath_coverage_pct": 80, "hazard_count": 3, "crossing_present": True, "score": 71},
         "model": {"version": 2, "fidelity_level": "L2", "fidelity_label": "Multi-view", "photos": 48,
                   "updated_at": "2026-10-30", "legacy": False}},
        {"slug": "chip-l0", "name": "Chip L0", "city": "Bengaluru", "quality": "satellite_only", "length_m": 50,
         "thumbnail_url": None, "best_score": 1240, "best_distance_m": 300.0, "run_count": 2,
         "walkability": {"footpath_coverage_pct": 10, "hazard_count": 0, "crossing_present": False, "score": 30},
         "model": {"version": 1, "fidelity_level": "L0", "fidelity_label": "Map only", "photos": 0,
                   "updated_at": "2026-10-30", "legacy": False}},
        {"slug": "chip-legacy", "name": "Chip legacy", "city": "Bengaluru", "quality": "full", "length_m": 75,
         "thumbnail_url": None, "best_score": None, "best_distance_m": None, "run_count": 0,
         "walkability": {"footpath_coverage_pct": 29, "hazard_count": 4, "crossing_present": True, "score": 50},
         "model": {"version": 1, "fidelity_level": "L1", "fidelity_label": "Single view", "photos": 3,
                   "updated_at": "2026-10-29", "legacy": True}},
        {"slug": "chip-null", "name": "Chip no model", "city": "Bengaluru", "quality": "full", "length_m": 75,
         "thumbnail_url": None, "best_score": None, "best_distance_m": None, "run_count": 0,
         "walkability": {"footpath_coverage_pct": 29, "hazard_count": 4, "crossing_present": True, "score": 50},
         "model": None},
    ]
    page.route("**/api/segments", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"ok": True, "data": listing})))
    page.goto(live_server + "/")
    page.wait_for_selector("article.street-card")

    def chip(slug: str):
        return page.query_selector(f'article.street-card[data-slug="{slug}"] .badge-fidelity')

    l2 = chip("chip-l2")
    assert l2.get_attribute("data-fidelity") == "L2" and l2.inner_text() == "L2 · 48 photos"
    l0 = chip("chip-l0")
    assert l0.get_attribute("data-fidelity") == "L0" and l0.inner_text() == "map-only sketch"
    legacy = chip("chip-legacy")
    assert legacy.inner_text() == "legacy · placement unverified" and legacy.get_attribute("data-legacy") == "true"
    assert chip("chip-null").inner_text() == "legacy · placement unverified"
    assert "satellite-only" in page.inner_text('article.street-card[data-slug="chip-l0"]')
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


# ----------------------------------------------------------------- ready overlay + About panel


def test_ready_overlay_shows_the_model_badge_and_about_panel_text(page: Page, live_server: str, curved_segment: dict):
    _open_ready(page, live_server, curved_segment["slug"])
    scene = curved_segment["scene"]
    badge = page.get_by_test_id("model-badge")
    assert badge.inner_text() == "Fidelity L2 · 48 photos · updated 30 Oct 2026"
    assert badge.get_attribute("data-fidelity") == "L2"
    assert _state(page)["model"] == {"version": 2, "fidelity": "L2", "photos": 48, "legacy": False}

    assert page.get_by_test_id("about-panel").is_hidden()
    page.get_by_test_id("about-open").click()
    panel = page.get_by_test_id("about-panel")
    panel.wait_for(state="visible")
    assert _state(page)["phase"] == "ready", "About must not start the run"
    txt = panel.inner_text()
    assert scene["about"]["summary"] in txt
    assert "Photos: 44 Street View, 4 satellite, 0 uploaded" in txt
    assert "captured 2023-04 to 2024-02" in txt
    assert "last updated 30 Oct 2026" in txt
    assert "Least certain" in txt
    for part in scene["about"]["low_confidence_parts"]:
        assert f"{_m(part['s_from'])}–{_m(part['s_to'])} m: {part['reason']}" in txt
    assert scene["about"]["improve_hint"] in txt
    hist = {k: v for k, v in scene["about"]["fidelity"]["histogram_m"].items() if v > 0}
    levels = page.eval_on_selector_all("[data-testid=about-fidelity-bar] span", "els => els.map(e => [e.dataset.level, e.style.width])")
    assert [lv for lv, _ in levels] == list(hist)
    for lv, width in levels:
        assert abs(float(width.rstrip("%")) - 100 * hist[lv] / sum(hist.values())) < 0.1
    assert f"L2 Multi-view · {_m(hist['L2'])} m".lower() in panel.inner_text().lower()
    assert panel.get_attribute("role") == "dialog" and panel.get_attribute("aria-modal") == "true"
    assert page.get_by_test_id("about-warning").count() == 0  # verified placement, not legacy/degraded

    # focus starts on Close and is trapped inside the dialog
    assert page.evaluate("() => document.activeElement.id") == "about-close"
    for _ in range(4):
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.getElementById('about-panel').contains(document.activeElement)")
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("() => document.getElementById('about-panel').contains(document.activeElement)")

    # Esc closes and focus returns to the opener; the run still has not started
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden")
    assert page.evaluate("() => document.activeElement.dataset.testid") == "about-open"
    page.get_by_test_id("about-open").click()
    page.get_by_test_id("about-close").click()
    panel.wait_for(state="hidden")
    assert _state(page)["phase"] == "ready"
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_hud_pause_icon_opens_about_and_pauses_until_closed(page: Page, live_server: str, curved_segment: dict):
    _start_run(page, live_server, curved_segment["slug"])
    page.get_by_test_id("hud-about").click()
    page.get_by_test_id("about-panel").wait_for(state="visible")
    s = _wait_state(page, "s.phase === 'paused'")
    d = s["distance_m"]
    page.wait_for_timeout(600)
    assert _state(page)["distance_m"] == d, "the run must be paused while About is open"
    page.keyboard.press("ArrowRight")  # never reaches the game
    assert _state(page)["lane"] == 0
    page.keyboard.press("Escape")
    s = _wait_state(page, "s.phase === 'running'")
    s = _wait_state(page, f"s.distance_m > {d} + 0.5")
    assert s["lane"] == 0


# ----------------------------------------------------------------- the run: footpath following + budget


def test_runner_stays_on_the_footpath_strip_along_the_bend_and_the_budget_holds(page: Page, live_server: str,
                                                                                curved_segment: dict):
    scene = curved_segment["scene"]
    _start_run(page, live_server, curved_segment["slug"])
    seen_gap = False
    samples: list[dict] = []
    deadline_s = max(e for _, e in _gap_ranges(scene)) + 3.0  # run past the missing-footpath gap
    while True:
        s = _state(page)
        assert s["phase"] == "running", f"run ended early: {s}"
        samples.append(s)
        if s["s"] > deadline_s or s["loops"] > 0:
            break
        page.wait_for_timeout(120)
    assert len({round(x["s"]) for x in samples}) > 15, "need samples all along the corridor"

    for s in samples:
        assert s["lane"] == 0
        off = s["footpath_offset_m"]
        if off is None:  # inside the `missing` gap: no strip to be on
            assert s["on_strip"] == "gap" and s["footpath_centre_m"] is None
            seen_gap = True
            continue
        assert s["on_strip"] == "footpath", f"left the footpath at s={s['s']}: {s}"
        assert off <= 0.9, f"offset {off} m is outside the traced strip half-width at s={s['s']}"
        assert abs(s["lat_m"] - s["footpath_centre_m"]) <= 0.9
    assert seen_gap, "the missing-footpath interval was never sampled"

    # The lane follows the traced strip: lateral position tracks the wandering footpath centre.
    lats = [s["lat_m"] for s in samples if s["footpath_offset_m"] is not None]
    assert max(lats) - min(lats) > 0.2

    # World pose follows the path: heading = theta(s), position = P(s) + lat * n(s) (scene's own samples).
    bend = [s for s in samples if s["s"] > 30]
    assert bend
    for s in bend:
        assert abs(s["heading_deg"] - math.degrees(_theta_at(scene, s["s"]))) < 2.0, s
        ex, ez = _expected_world(scene, s["s"], s["lat_m"])
        assert math.hypot(s["world"]["x"] - ex, s["world"]["z"] - ez) < 0.2, (s, ex, ez)
    assert min(s["world"]["x"] for s in bend) < -6, "the road bends toward -x (mirrored footpath convention)"

    perf = _perf(page)
    assert 0 < perf["draw_calls"] <= 90, perf
    assert 0 < perf["triangles"] <= 60_000, perf
    assert perf["fps_avg"] > 0
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_lane_change_tweens_in_lateral_and_returns_to_the_footpath(page: Page, live_server: str, curved_segment: dict):
    _start_run(page, live_server, curved_segment["slug"])
    page.keyboard.press("ArrowRight")
    s = _wait_state(page, "s.lane === 1 && s.on_strip !== 'footpath'", timeout_ms=10_000)
    assert s["on_strip"] in {"kerb", "carriageway"}, s
    page.keyboard.press("ArrowLeft")
    s = _wait_state(page, "s.lane === 0 && (s.on_strip === 'footpath' || s.on_strip === 'gap')", timeout_ms=10_000)
    assert s["phase"] == "running"


# ----------------------------------------------------------------- results card (descriptions) + About from results


def test_results_card_has_descriptions_evidence_footpath_states_and_model_line(page: Page, live_server: str,
                                                                               curved_segment: dict):
    scene = curved_segment["scene"]
    _start_run(page, live_server, curved_segment["slug"])
    s = _wait_state(page, "s.phase === 'results'", timeout_ms=240_000)
    assert s["health"] == 0
    results = page.wait_for_selector("#results:not([hidden])")
    txt = results.inner_text()
    # per-object description + evidence line
    objs = scene["objects"]
    hazard = next(o for o in objs if o["action"] != "none" and o["description"] and o["evidence"]["summary"])
    assert hazard["description"] in txt and hazard["evidence"]["summary"] in txt
    row = page.query_selector(f'#detected li[data-id="{hazard["id"]}"]')
    assert row is not None and hazard["label"] in row.inner_text()
    scenery = page.query_selector(f'#detected li[data-id="{next(o for o in objs if o["scenery"])["id"]}"]')
    assert "scenery" in scenery.inner_text() and scenery.get_attribute("data-hazard") == "false"
    low = next(o for o in objs if o["confidence"] < 0.6)
    assert "low confidence" in page.query_selector(f'#detected li[data-id="{low["id"]}"]').inner_text()
    # footpath states (those present in the scene's coverage)
    states = page.get_by_test_id("footpath-states").inner_text()
    for word in {r["state"] for r in scene["cross_section"]["footpath_coverage"]}:
        assert word in states
    # model line + least certain + About
    assert page.inner_text("#results-model-line") == "Model v2 · fidelity L2 · 48 photos · updated 30 Oct 2026"
    first = scene["about"]["low_confidence_parts"][0]
    assert f"{_m(first['s_from'])}–{_m(first['s_to'])} m" in page.get_by_test_id("results-uncertain").inner_text()
    page.get_by_test_id("about-open-results").click()
    panel = page.get_by_test_id("about-panel")
    panel.wait_for(state="visible")
    assert "Least certain" in panel.inner_text()
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden")
    assert page.evaluate("() => document.activeElement.id") == "results-about-open"
    assert page.query_selector("#hazard-map canvas") is not None
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


# ================================================================= v3 street: the glb-loaded game (stand-in bundle)
# The same journey on a v3 scene: street.glb + street.manifest.json served by route interception (the S1a stand-in
# bundle tests/fixtures/export/contract_curved/). The glb/Track frame is the TRUE unmirrored frame (spec/portability.md
# section 2): travel toward -Z at s = 0, +X to the RIGHT, the left footpath at -X, t positive to the LEFT.

import glb_harness as gh  # noqa: E402


def _m_points(m: dict) -> list[dict]:
    return m["reference_line"]["points"]


def _m_pose(m: dict, s: float) -> tuple[float, float, float]:
    """(x, z, phi_rad) of the reference line at s, linearly interpolated on the manifest's 2.5 m grid."""
    pts = _m_points(m)
    b0 = pts[0]["bearing_deg"]
    for a, b in zip(pts, pts[1:]):
        if a["s"] <= s <= b["s"]:
            f = (s - a["s"]) / (b["s"] - a["s"])
            return (a["x"] + (b["x"] - a["x"]) * f, a["z"] + (b["z"] - a["z"]) * f,
                    math.radians(a["bearing_deg"] - b0 + (b["bearing_deg"] - a["bearing_deg"]) * f))
    raise AssertionError(s)


def _m_world(m: dict, s: float, t: float) -> tuple[float, float]:
    x, z, phi = _m_pose(m, s)
    return x - t * math.cos(phi), z - t * math.sin(phi)


def _glb_start(page: Page, live_server: str) -> dict:
    seen = gh.install_street_routes(page)
    gh.start_run(page, live_server)
    return seen


def test_v3_runner_follows_the_manifest_footpath_in_the_true_frame(page: Page, live_server: str):
    m = gh.manifest()
    _glb_start(page, live_server)
    samples: list[dict] = []
    while True:
        s = _state(page)
        assert s["phase"] == "running", f"run ended early: {s}"
        samples.append(s)
        if s["s"] > 41 or s["loops"] > 0:
            break
        page.wait_for_timeout(120)
    assert len({round(x["s"]) for x in samples}) > 15
    seen_gap = False
    for s in samples:
        assert s["lane"] == 0 and "t_m" in s
        if s["footpath_offset_m"] is None:        # inside the missing-footpath gap (30 - 37.5 m)
            assert s["on_strip"] == "gap"
            seen_gap = True
            continue
        assert s["on_strip"] == "footpath", s
        assert s["footpath_offset_m"] <= 0.9
        # world pose == the spec formula on the manifest's own reference line
        ex, ez = _m_world(m, s["s"], s["t_m"])
        assert math.hypot(s["world"]["x"] - ex, s["world"]["z"] - ez) < 0.15, (s, ex, ez)
        assert abs(s["heading_deg"] - math.degrees(_m_pose(m, s["s"])[2])) < 1.0
    assert seen_gap
    # a LEFT footpath lies at -X of the centreline and lane t is positive-left
    early = [s for s in samples if s["s"] < 3 and s["footpath_offset_m"] is not None]
    assert early and all(s["t_m"] > 4.0 and s["world"]["x"] < -4.0 for s in early)
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_v3_glb_agrees_with_the_manifest_track(page: Page, live_server: str):
    """The footpath surface of the glb lies inside the manifest's footpath strip (t_lo..t_hi) - the Track and the mesh
    share one frame."""
    gh.install_street_routes(page)
    gh.open_ready(page, live_server)
    res = page.evaluate("""async () => {
      const { loadStreet } = await import('/js/game/glb_loader.js?v=1');
      const { ManifestTrack } = await import('/js/game/track_manifest.js?v=1');
      const s = await loadStreet({ glbUrl: '/api/segments/contract-curved-street/model.glb', manifestUrl: '/api/segments/contract-curved-street/manifest.json' });
      const tr = new ManifestTrack(s.manifest);
      const pos = s.nodes.Footpath.geometry.getAttribute('position');
      let inside = 0, total = 0, worst = 0;
      for (let i = 0; i < pos.count; i++) {
        const x = pos.getX(i), z = pos.getZ(i);
        let best = 1e9, bs = 0;
        for (const p of tr.samples) { const d = Math.hypot(p.x - x, p.z - z); if (d < best) { best = d; bs = p.s; } }
        const p = tr.pose(bs);
        const t = -((x - p.x) * Math.cos(p.theta) + (z - p.z) * Math.sin(p.theta));
        const fp = tr.footpathAt(Math.min(bs, tr.length - 0.01)) || tr.stations.map((st) => st.footpath).find(Boolean);
        const lo = Math.min(...tr.stations.map((st) => st.footpath ? st.footpath.lo : 99)), hi = Math.max(...tr.stations.map((st) => st.footpath ? st.footpath.hi : -99));
        total += 1;
        const ok = t >= lo - 0.3 && t <= hi + 0.3;
        if (ok) inside += 1; else worst = Math.max(worst, Math.min(Math.abs(t - lo), Math.abs(t - hi)));
      }
      const road = s.nodes.Road.geometry; road.computeBoundingBox();
      const end = tr.pose(tr.length);
      return { inside, total, worst, roadBox: [road.boundingBox.min.toArray(), road.boundingBox.max.toArray()], end: [end.x, end.z] };
    }""")
    assert res["total"] > 10 and res["inside"] == res["total"], res
    (mn, mx), (ex, ez) = res["roadBox"], res["end"]
    assert mn[1] >= -0.01 and mx[1] <= 0.5                      # +Y is up, the carriageway is at y = 0
    assert mn[2] - 6 <= ez <= mx[2] + 6 and mn[0] - 6 <= ex <= mx[0] + 6  # the road reaches P(L)


def test_v3_hit_shows_a_small_toast_with_the_object_name_and_no_labels_in_the_world(page: Page, live_server: str):
    _glb_start(page, live_server)
    g = page.evaluate("() => window.__hero.graph()")
    assert g["sprites"] == 0, "no text sprites / '?' badges in the 3D world"
    assert not [x for x in g["meshes"] if "badge" in x["name"].lower() or "label" in x["name"].lower() or "outline" in x["name"].lower()]
    s = _wait_state(page, "s.hits >= 1", timeout_ms=40_000)
    label = s["last_hit"]["label"]
    page.wait_for_function("() => document.getElementById('toast').classList.contains('on')")
    assert page.inner_text("#toast") == label
    box = page.eval_on_selector("#toast", "(e) => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }")
    assert box[0] < 300 and box[1] < 60, "the toast stays small"
    page.wait_for_function("() => !document.getElementById('toast').classList.contains('on')", timeout=5_000)
    assert page.evaluate("() => window.__hero.graph().sprites") == 0


def test_v3_camera_rig_numbers_and_atmosphere(page: Page, live_server: str):
    _glb_start(page, live_server)
    s = _wait_state(page, "s.distance_m > 12")
    g = page.evaluate("() => window.__hero.graph()")
    cam = g["camera"]
    assert abs(cam["y"] - 3.8) <= 0.06 and cam["near"] == 0.3
    m = gh.manifest()
    rx, rz = s["world"]["x"], s["world"]["z"]
    behind = math.hypot(cam["x"] - rx, cam["z"] - rz)
    assert 6.0 <= behind <= 7.8, behind                        # ~6.6 m behind (chord of the path)
    assert g["fog"]["near"] == 30 and g["fog"]["far"] == 90
    assert g["lights"] >= 2
    assert any(x["name"] == "Sky" for x in g["meshes"])
    assert any(x["name"] == "AoBlob" for x in g["meshes"])
    assert m["gameplay"] == page.evaluate("() => window.__hero.gameplay"), "gameplay is the manifest's embedded copy, verbatim"


def test_v3_about_panel_lists_interpolated_parts_and_low_fidelity_only_there(page: Page, live_server: str):
    gh.install_street_routes(page)
    gh.open_ready(page, live_server)
    page.get_by_test_id("about-open").click()
    panel = page.get_by_test_id("about-panel")
    panel.wait_for(state="visible")
    txt = panel.inner_text()
    sc = gh.scene_v3()
    assert sc["about"]["summary"] in txt and sc["about"]["gap_summary"] in txt and sc["about"]["dynamic_summary"] in txt
    rows = page.get_by_test_id("about-interpolated").locator("li").all_inner_texts()
    assert len(rows) == len(gh.manifest()["interpolated"]) >= 4
    assert any("frontage" in r and "right side" in r and "35–60 m" in r for r in rows), rows
    assert "Satellite imagery © Google" in page.get_by_test_id("about-attribution").inner_text()
    assert "neutral facade" in txt
    # low-fidelity wording never appears in the in-world UI (ready overlay without About)
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden")
    assert "neutral facade" not in page.inner_text("#panel-ready") and "interpolated" not in page.inner_text("#panel-ready").lower()


def test_v3_loop_seam_is_continuous_and_run_continues_after_the_loop(page: Page, live_server: str):
    gh.install_street_routes(page)
    gh.open_ready(page, live_server)
    jumps = page.evaluate("""async () => {
      const { ManifestTrack } = await import('/js/game/track_manifest.js?v=1');
      const m = await (await fetch('/api/segments/contract-curved-street/manifest.json')).json();
      const tr = new ManifestTrack(m), L = tr.length, out = [];
      for (const lane of [0, 1, 2]) {
        const a = tr.world(L - 0.01, tr.laneLat(L - 0.01, lane)), b = tr.world(L + 0.01, tr.laneLat(0.01, lane));
        out.push(Math.hypot(a.x - b.x, a.z - b.z));
      }
      const h0 = tr.headingDeg(L - 0.01), h1 = tr.headingDeg(L + 0.01);
      return { out, dh: Math.abs(h1 - h0), pk: tr.pose(L), end: m.loop.end_pose };
    }""")
    assert all(j < 1.5 for j in jumps["out"]), jumps          # <= 1.5 m lateral steps per spec (blend baked into the data)
    assert jumps["dh"] < 3.0
    assert abs(jumps["pk"]["x"] - jumps["end"]["x"]) < 0.01 and abs(jumps["pk"]["z"] - jumps["end"]["z"]) < 0.01
    page.click("#start")
    s = _wait_state(page, "s.loops >= 1 || s.phase !== 'running'", timeout_ms=120_000)
    assert s["loops"] >= 1, s
    page.wait_for_timeout(500)
    assert hero_state_ok(page)


def hero_state_ok(page: Page) -> bool:
    st = _state(page)
    return st["phase"] in {"running", "crashed", "results"} and st["distance_m"] > 60


def test_v3_run_results_use_the_manifest_and_post_a_run(page: Page, live_server: str):
    seen = _glb_start(page, live_server)
    s = _wait_state(page, "s.phase === 'results'", timeout_ms=280_000)
    assert s["health"] == 0
    page.wait_for_selector("#results:not([hidden])")
    assert seen["runs"] and seen["runs"][0]["score"] == s["score"]
    cols = gh.manifest()["colliders"]
    assert len(page.query_selector_all("#detected li")) == len(cols)               # detected list comes from the manifest
    assert len(page.query_selector_all("#detected li[data-hazard='true']")) == sum(1 for c in cols if c["action"] != "none")
    assert page.query_selector("#hazard-map canvas") is not None
    assert page.query_selector("#run-again") is not None
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]
