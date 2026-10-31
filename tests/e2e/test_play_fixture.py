"""Deterministic play journey on the canonical example scene (tests/e2e/fixtures/scene_example.json).

The fixture is served through the REAL public API: a segment row is inserted into the test
DB via ``hero.db.repo`` and finalised with the example scene; the tile PNG is copied into the
data dir so ``/media/imagery/...`` serves it. The browser then drives the gallery and the
game with real keyboard input only (no debug shortcuts) - ``window.__hero.state`` is read-only.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from hero.config import get_settings
from hero.db import repo
from hero.scene.schema import LatLon, Quality, Scene

FIXTURES = Path(__file__).parent / "fixtures"
SCENE_PATH = FIXTURES / "scene_example.json"
TILE_PATH = FIXTURES / "tile_1280.png"
# The fixture is inserted under its OWN slug so it never overwrites the really generated seed
# street (``100-feet-road-indiranagar-w``) that test_admin_live / test_play_live share in the
# session DB.
FIXTURE_SLUG = "example-fixture-w"
FIXTURE_NAME = "Example Street, Indiranagar (fixture, west footpath)"
PHONE_VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def _read_scene() -> dict:
    scene = json.loads(SCENE_PATH.read_text())
    scene["segment"]["slug"] = FIXTURE_SLUG
    scene["segment"]["name"] = FIXTURE_NAME
    return scene


def test_gallery_shows_the_empty_state_copy_when_there_are_no_ready_streets(page: Page, live_server: str):
    """The empty state is a real browser render of the gallery's own JS against the real
    envelope shape (``{ok, data: []}``); the shared session DB may already hold streets from
    other test modules, so the empty list is served to this page only."""
    page.route("**/api/segments", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"ok": True, "data": []})))
    page.goto(live_server + "/")
    page.wait_for_selector("#gallery-empty:not([hidden])")
    txt = page.inner_text("#gallery-empty")
    assert "No streets yet" in txt and "/admin/" in txt
    assert page.query_selector('#gallery-empty a[href="/admin/"]') is not None
    assert page.is_hidden("#street-list") and page.is_hidden("#gallery-error")
    assert _state(page)["phase"] == "empty"
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def fixture_segment(test_db: str) -> dict:
    """Insert the example scene as a ready segment on the test DB under FIXTURE_SLUG; returns {slug, scene}."""
    scene_dict = _read_scene()
    scene = Scene.model_validate(scene_dict)
    assert scene.segment.slug == FIXTURE_SLUG != "100-feet-road-indiranagar-w"
    data_dir = get_settings().data_dir_abs
    tile_name = Path(scene.ground.tile_url).name
    (data_dir / "imagery").mkdir(parents=True, exist_ok=True)
    (data_dir / "previews" / "fixture-example").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TILE_PATH, data_dir / "imagery" / tile_name)
    shutil.copyfile(TILE_PATH, data_dir / "previews" / "fixture-example" / "thumb.png")

    seg = scene.segment
    existing = repo.get_segment(seg.slug)
    if existing is None:
        existing = repo.create_segment(repo.SegmentCreate(
            name=seg.name, city=seg.city, slug=seg.slug,
            start=LatLon(lat=seg.start.lat, lon=seg.start.lon), end=LatLon(lat=seg.end.lat, lon=seg.end.lon),
            footpath_side=seg.footpath_side,
        ))
    repo.finalize_segment_scene(existing.id, run_id=scene.generation.run_id, scene=scene, quality=Quality.full,
                                thumbnail_path="previews/fixture-example/thumb.png")
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
    # The fixture's billboard/source crops intentionally do not exist (exercises the labelled-box
    # fallback); the browser logs those 404s as console errors, which are not JS errors.
    p.on("console", lambda m: errors.append(m.text)
         if m.type == "error" and "Failed to load resource" not in m.text else None)
    p.hero_errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


def _state(page: Page) -> dict:
    return page.evaluate("() => window.__hero && window.__hero.state")


def _wait_state(page: Page, predicate_js: str, timeout_ms: int = 20_000) -> dict:
    page.wait_for_function(f"() => {{ const s = window.__hero && window.__hero.state; return !!s && ({predicate_js}); }}",
                           timeout=timeout_ms)
    return _state(page)


def _start_run(page: Page, live_server: str, slug: str) -> None:
    page.goto(f"{live_server}/play/?segment={slug}")
    page.wait_for_selector('[data-hero-page="play"]')
    page.wait_for_selector("#start:not([disabled])")
    _wait_state(page, "s.phase === 'ready'")
    page.click("#start")
    _wait_state(page, "s.phase === 'running'")


# ----------------------------------------------------------------- gallery


def test_gallery_lists_the_fixture_street_with_no_runs_yet_and_a_labelled_stub(page: Page, live_server: str,
                                                                               fixture_segment: dict):
    page.goto(live_server + "/")
    page.wait_for_selector('[data-hero-page="gallery"]')
    card = page.wait_for_selector(f'article.street-card[data-slug="{fixture_segment["slug"]}"]')
    text = card.inner_text()
    assert FIXTURE_NAME in text
    assert "Bengaluru" in text
    assert "No runs yet" in text
    assert "50/100" in text
    # Only the fixture card is asserted on: the gallery may also list a really generated street.
    assert len(page.query_selector_all(f'article.street-card[data-slug="{FIXTURE_SLUG}"]')) == 1
    assert card.query_selector("img.street-thumb") is not None
    assert card.query_selector("a").get_attribute("href").endswith(f"/play/?segment={fixture_segment['slug']}")
    stub = page.query_selector('button[data-stub="true"]')
    assert stub is not None and stub.is_disabled()
    assert "Use my location" in stub.inner_text()
    assert "Coming in Phase 3" in page.inner_text("main")
    assert "Coming in Phase 2" not in page.inner_text("main")
    # Phase-1 (scene v1) street: labelled legacy chip, never silent
    chip = card.query_selector(".badge-fidelity")
    assert chip is not None and chip.inner_text() == "legacy · placement unverified"
    assert chip.get_attribute("data-legacy") == "true"
    assert page.query_selector('a[href="/admin/"]') is not None
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_v1_scene_ready_overlay_about_panel_and_render_budget(page: Page, live_server: str, fixture_segment: dict):
    """A Phase-1 scene still plays: labelled legacy badge, the v1 About text, a straight track, within the budget."""
    page.goto(f"{live_server}/play/?segment={fixture_segment['slug']}")
    page.wait_for_selector("#start:not([disabled])")
    _wait_state(page, "s.phase === 'ready'")
    assert page.get_by_test_id("model-badge").inner_text() == "Legacy scene · placement unverified"
    page.get_by_test_id("about-open").click()
    panel = page.get_by_test_id("about-panel")
    panel.wait_for(state="visible")
    txt = panel.inner_text()
    assert "This street was built with the first-generation model (2 photos); its position was not verified." in txt
    assert "Legacy scene, placement unverified" in txt
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden")
    assert _state(page)["phase"] == "ready"
    page.click("#start")
    s = _wait_state(page, "s.phase === 'running' && s.distance_m > 3")
    assert "on_strip" not in s  # v2-only debug fields are absent for v1 scenes
    perf = page.evaluate("() => window.__hero.perf")
    assert 0 < perf["draw_calls"] <= 90 and 0 < perf["triangles"] <= 60_000, perf
    # legacy path: labelled as such, and no "?" badge boxes / in-world text labels any more (sprites = billboards only)
    h = page.evaluate("() => ({source: window.__hero.source, legacy: window.__hero.legacy_geometry, g: window.__hero.graph()})")
    assert h["source"] == "legacy" and h["legacy"] is True and h["g"]["sprites"] == 0
    assert not [m for m in h["g"]["meshes"] if m["name"] in {"badges", "outlines"}]
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_gallery_shows_error_state_with_retry_when_api_fails(page: Page, live_server: str):
    page.route("**/api/segments", lambda route: route.abort())
    page.goto(live_server + "/")
    page.wait_for_selector("#gallery-error:not([hidden])")
    assert "Couldn't load streets" in page.inner_text("#gallery-error")
    page.unroute("**/api/segments")
    page.click("#gallery-retry")
    page.wait_for_selector("article.street-card")


# ----------------------------------------------------------------- play: crash + results


def test_no_input_crashes_into_the_parked_vehicle_and_results_show_fixture_walkability(page: Page, live_server: str,
                                                                                        fixture_segment: dict):
    slug = fixture_segment["slug"]
    _start_run(page, live_server, slug)
    assert page.is_visible("#hud")
    s = _wait_state(page, "s.distance_m > 5")
    assert s["lane"] == 0
    assert "footpath" in page.inner_text("#hud-lane").lower()

    # The 3 s start grace (~18 m) skips o1; the first hit is o2 (footpath gap, lane 0, 22 m): -10 health, run continues.
    s = _wait_state(page, "s.hits >= 1", timeout_ms=30_000)
    assert s["phase"] == "running" and s["crashed_into"] is None
    assert s["last_hit"]["id"] == "o2" and s["health"] == 90
    assert 21.0 <= s["distance_m"] <= 25.0
    assert s["tokens"] == 1  # t1 at 8 m in lane 0 is passed during the start grace
    assert page.inner_text("#hud-health") == "90"

    # Keep running without input: health drains to 0 and only then the run ends.
    s = _wait_state(page, "s.phase === 'results'", timeout_ms=180_000)
    assert s["health"] == 0 and s["hits"] == 10
    assert s["crashed_into"] is not None and s["crashed_into"]["id"] in {"o1", "o2", "o4"}
    assert s["score"] == round(s["distance_m"]) + 50 * s["tokens"]
    assert s["loops"] >= 1

    results = page.wait_for_selector("#results:not([hidden])")
    txt = results.inner_text()
    assert "parked car on footpath" in txt
    assert page.inner_text("#result-score") == str(s["score"])
    assert "New best" in page.inner_text("#best-status")
    assert page.inner_text("#walk-coverage") == "29.5%"
    assert page.inner_text("#walk-hazards") == "4"
    assert page.inner_text("#walk-crossing") == "Yes"
    assert page.inner_text("#walk-score") == "50/100"
    assert "low-confidence detections included" in txt
    assert page.query_selector("#hazard-map canvas") is not None
    assert len(page.query_selector_all("#detected li[data-hazard='true']")) == 4
    assert len(page.query_selector_all("#detected li")) == 5
    low_rows = [li.inner_text() for li in page.query_selector_all("#detected li.low-confidence")]
    assert len(low_rows) == 1 and "low branch" in low_rows[0] and "low confidence" in low_rows[0]
    crashed_row = page.query_selector("#detected li[data-crashed='true']")
    assert crashed_row is not None
    assert page.inner_text("#result-hits") == "10"
    assert s["run_post"] == "saved"
    assert "legacy" in page.inner_text("#results-model-line").lower()
    assert page.get_by_test_id("about-open-results").is_visible()
    assert "not yet observed" not in page.get_by_test_id("footpath-states").inner_text()  # v1 fixture has no unknown range
    assert page.query_selector("#run-again") is not None
    assert page.query_selector("#back-gallery").get_attribute("href") == "/"
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]

    # The run was recorded through the real API and the gallery shows the best score.
    listing = httpx.get(f"{live_server}/api/segments", timeout=5).json()["data"]
    entry = next(e for e in listing if e["slug"] == slug)
    assert entry["best_score"] == s["score"]
    page.goto(live_server + "/")
    card = page.wait_for_selector(f'article.street-card[data-slug="{slug}"]')
    assert f"Best {s['score']:,}" in card.inner_text()


def test_marker_tap_on_hazard_map_shows_label_and_confidence(page: Page, live_server: str, fixture_segment: dict):
    _start_run(page, live_server, fixture_segment["slug"])
    s = _wait_state(page, "s.phase === 'results'", timeout_ms=180_000)
    page.wait_for_selector("#hazard-map canvas")
    page.click("#hazard-map canvas", position=page.evaluate("() => window.__hero.state.results.crash_marker_xy"))
    caption = page.inner_text("#map-caption")
    assert "confidence" in caption and "you crashed here" in caption  # the object that took health to 0
    assert s["crashed_into"] is not None


# ----------------------------------------------------------------- play: correct actions survive


def test_lane_change_and_repeated_jumps_clear_the_pothole_and_crossing_never_crashes(page: Page, live_server: str,
                                                                                      fixture_segment: dict):
    _start_run(page, live_server, fixture_segment["slug"])
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    s = _wait_state(page, "s.lane === 2 && s.distance_m > 1")
    assert "road" in page.inner_text("#hud-lane").lower()

    # Lane 2 is clear of o1 (lanes 0-1) and o2 (lane 0); o3 (pothole, 40 m) needs a jump.
    _wait_state(page, "s.distance_m > 36", timeout_ms=30_000)
    saw_jump = False
    deadline = time.time() + 15
    while time.time() < deadline:
        s = _state(page)
        if s["phase"] != "running" or s["distance_m"] > 43:
            break
        page.keyboard.press("ArrowUp")
        if s["action"] == "jumping":
            saw_jump = True
        page.wait_for_timeout(60)
    assert saw_jump
    s = _wait_state(page, "s.distance_m > 45 || s.phase !== 'running'", timeout_ms=20_000)
    assert s["phase"] == "running", f"crashed into {s['crashed_into']} at {s['distance_m']} m"

    # o5 (crossing, action none, all lanes) at 60 m never crashes.
    s = _wait_state(page, "s.distance_m > 64 || s.phase !== 'running'", timeout_ms=20_000)
    assert s["phase"] == "running"
    assert s["tokens"] == 0  # t2 sits in lane 1 at the crossing; we stayed in lane 2

    page.keyboard.press("ArrowDown")
    s = _wait_state(page, "s.action === 'sliding' || s.phase !== 'running'", timeout_ms=5_000)
    assert s["action"] == "sliding"

    # Next loop: no jump at the pothole -> a hit (health reduced), run continues, loops counter advanced.
    s = _wait_state(page, "s.hits >= 1", timeout_ms=60_000)
    assert s["phase"] == "running"
    assert {k: s["last_hit"][k] for k in ("id", "type")} == {"id": "o3", "type": "pothole"}  # (+ label, dynamic: False)
    assert s["health"] > 0 and s["loops"] == 1
    assert 114.0 <= s["distance_m"] <= 116.5  # 74.6 + ~40
    assert s["speed"] > 6.0
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_touch_swipes_change_lane_and_jump(page: Page, live_server: str, fixture_segment: dict):
    _start_run(page, live_server, fixture_segment["slug"])
    canvas = page.query_selector("#game")
    box = canvas.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def swipe(dx: float, dy: float) -> None:
        page.evaluate(
            """([x, y, dx, dy]) => {
                const el = document.getElementById('game');
                const mk = (type, px, py) => new TouchEvent(type, {bubbles: true, cancelable: true,
                    touches: type === 'touchend' ? [] : [new Touch({identifier: 1, target: el, clientX: px, clientY: py})],
                    changedTouches: [new Touch({identifier: 1, target: el, clientX: px, clientY: py})]});
                el.dispatchEvent(mk('touchstart', x, y));
                el.dispatchEvent(mk('touchend', x + dx, y + dy));
            }""",
            [cx, cy, dx, dy],
        )

    swipe(120, 0)
    s = _wait_state(page, "s.lane === 1 || s.phase !== 'running'", timeout_ms=5_000)
    assert s["lane"] == 1
    swipe(0, -120)
    s = _wait_state(page, "s.action === 'jumping' || s.phase !== 'running'", timeout_ms=5_000)
    assert s["action"] == "jumping"
    swipe(-120, 0)
    s = _wait_state(page, "s.lane === 0 || s.phase !== 'running'", timeout_ms=5_000)
    assert s["lane"] == 0


# ----------------------------------------------------------------- play: error + run again


def test_unknown_street_shows_error_state_with_back_link(page: Page, live_server: str):
    page.goto(f"{live_server}/play/?segment=no-such-street")
    page.wait_for_selector("#panel-error:not([hidden])")
    txt = page.inner_text("#panel-error")
    assert "Couldn't load this street" in txt
    reason = page.inner_text("#error-reason").strip()
    assert reason, "the API's not_found message must be shown as the reason"
    st = _state(page)
    assert st["phase"] == "error" and st["error"] == reason
    link = page.query_selector("#panel-error a")
    assert link.get_attribute("href") == "/"
    assert "Back to gallery" in link.inner_text()


def test_run_again_restarts_from_the_ready_overlay_and_posts_a_second_run(page: Page, live_server: str,
                                                                           fixture_segment: dict):
    slug = fixture_segment["slug"]
    _start_run(page, live_server, slug)
    first = _wait_state(page, "s.phase === 'results'", timeout_ms=180_000)
    page.click("#run-again")
    s = _wait_state(page, "s.phase === 'ready'")
    assert s["distance_m"] == 0 and s["crashed_into"] is None and s["health"] == 100 and s["hits"] == 0
    page.click("#start")
    second = _wait_state(page, "s.phase === 'results'", timeout_ms=180_000)
    assert second["crashed_into"] is not None and second["health"] == 0
    assert "Best" in page.inner_text("#best-status")
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]
    assert first["run_post"] == "saved" and second["run_post"] == "saved"
