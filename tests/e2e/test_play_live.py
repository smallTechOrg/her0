"""Play journey on a REALLY generated segment (``generated_segment`` session fixture, one pipeline
run against the test DB). Asserts structure, never scene-specific numbers.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

PHONE_VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


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
    p.hero_errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


def _state(page: Page) -> dict:
    return page.evaluate("() => window.__hero && window.__hero.state")


def _wait_state(page: Page, predicate_js: str, timeout_ms: int) -> dict:
    page.wait_for_function(f"() => {{ const s = window.__hero && window.__hero.state; return !!s && ({predicate_js}); }}",
                           timeout=timeout_ms)
    return _state(page)


def test_generated_street_appears_in_the_gallery_and_plays_to_a_crash_with_a_results_card(
        page: Page, live_server: str, generated_segment: str):
    scene = httpx.get(f"{live_server}/api/segments/{generated_segment}/scene", timeout=10).json()["data"]
    hazards = [o for o in scene["objects"] if o["action"] != "none"]
    assert hazards, "a generated scene needs at least one hazard for the run to end in a crash"

    page.goto(live_server + "/")
    page.wait_for_selector('[data-hero-page="gallery"]')
    card = page.wait_for_selector(f'article.street-card[data-slug="{generated_segment}"]')
    assert scene["segment"]["name"] in card.inner_text()
    assert card.query_selector("img.street-thumb") is not None
    card.query_selector("a").click()

    page.wait_for_selector('[data-hero-page="play"]')
    page.wait_for_selector("#start:not([disabled])", timeout=60_000)
    assert _state(page)["phase"] == "ready"
    page.click("#start")
    _wait_state(page, "s.phase === 'running'", 10_000)
    _wait_state(page, "s.distance_m > 20 || s.phase !== 'running'", 15_000)

    # Spec: against the real street the run ends in a crash within 90 s without input.
    s = _wait_state(page, "s.phase === 'results'", 90_000)
    assert s["crashed_into"] is not None
    assert s["crashed_into"]["id"] in {o["id"] for o in hazards}
    assert s["score"] == round(s["distance_m"]) + 50 * s["tokens"]
    assert s["run_post"] == "saved"

    results = page.wait_for_selector("#results:not([hidden])")
    txt = results.inner_text()
    assert "Walkability" in txt
    assert page.inner_text("#result-score") == str(s["score"])
    assert page.inner_text("#result-distance").isdigit()
    w = scene["walkability"]
    assert float(page.inner_text("#walk-coverage").rstrip("%")) == float(w["footpath_coverage_pct"])
    assert page.inner_text("#walk-hazards") == str(w["hazard_count"])
    assert page.inner_text("#walk-crossing") == ("Yes" if w["crossing_present"] else "No")
    assert page.inner_text("#walk-score") == f"{w['score']}/100"
    assert page.query_selector("#hazard-map canvas") is not None
    assert len(page.query_selector_all("#detected li[data-hazard='true']")) == len(hazards)
    low_rows = page.query_selector_all("#detected li.low-confidence")
    assert len(low_rows) == sum(1 for o in scene["objects"] if o["confidence"] < 0.6)
    for row in low_rows:
        assert "low confidence" in row.inner_text()
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]

    listing = httpx.get(f"{live_server}/api/segments", timeout=5).json()["data"]
    entry = next(e for e in listing if e["slug"] == generated_segment)
    assert entry["best_score"] is not None and entry["best_score"] >= s["score"]
