"""Phase-2 live gate, UI part: headless Chromium (Playwright) drives the REAL frontend on the really generated v2 street
(``gate`` fixture; free port from ``live_server``): admin segment page (coverage / entities / diff), run report pages (run 2 and
the no-op run 3), gallery card, and the game's About panel. No extra spend (the generations already happened)."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect, sync_playwright

from hero.config import get_settings

pytestmark = pytest.mark.live

CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        yield b
        b.close()


def _new_page(browser: Browser, **ctx_kw) -> Page:
    ctx = browser.new_context(**ctx_kw)
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.hero_errors = errors  # type: ignore[attr-defined]
    return page


@pytest.fixture()
def admin_page(browser: Browser):
    page = _new_page(browser, viewport={"width": 1280, "height": 1000})
    yield page
    page.context.close()


@pytest.fixture()
def phone_page(browser: Browser):
    page = _new_page(browser, viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    yield page
    page.context.close()


def _login(page: Page, url: str) -> None:
    page.goto(url)
    page.get_by_label("Admin password").fill(get_settings().admin_password.get_secret_value())
    page.get_by_role("button", name="Enter").click()


def test_admin_segment_page_shows_coverage_entities_and_diff(live_server: str, admin_page: Page, gate: dict[str, Any]):
    slug, page = gate["slug"], admin_page
    _login(page, f"{live_server}/admin/segment.html?slug={slug}")
    expect(page.locator('[data-hero-page="admin-segment"]')).to_be_visible()
    expect(page.locator("[data-testid=model-badge]")).to_contain_text("v2")
    expect(page.locator("[data-testid=model-badge]")).to_contain_text("L2")

    rows = page.locator("[data-testid=versions] tbody tr")
    expect(rows).to_have_count(3)  # v0 prior, v1, v2

    # coverage map: one cell per 5 m bin, clicking one lists the covering photos
    n_bins = len(gate["run2"]["version"]["fidelity"]["bins"])
    cells = page.locator("[data-testid=coverage] .cov-cell")
    expect(cells).to_have_count(n_bins)
    assert cells.evaluate_all("els => els.filter(e => e.dataset.level === 'L2').length") >= 0.8 * n_bins
    cells.nth(n_bins // 2).click()
    expect(page.locator("[data-testid=coverage-popover]")).to_be_visible()

    # entities: >= 5 rows; a row opens the evidence drawer with a description and photo thumbnails
    ents = page.locator("[data-testid=entities] tbody tr")
    assert ents.count() >= 5
    ents.first.click()
    drawer = page.locator("[data-testid=evidence-drawer]")
    expect(drawer).to_be_visible()
    assert page.locator("[data-testid=evidence-description]").inner_text().strip()
    assert page.locator("[data-testid=evidence-thumb]").count() >= 1

    # diff v1 -> v2 (preselected and compared on load)
    expect(page.locator("[data-testid=diff-title]")).to_contain_text("v1")
    expect(page.locator("[data-testid=diff-title]")).to_contain_text("v2")
    assert page.locator("[data-testid=diff-summary] li").count() >= 1
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_run_pages_show_the_report_and_the_noop(live_server: str, admin_page: Page, gate: dict[str, Any]):
    page = admin_page
    _login(page, f"{live_server}/admin/run.html?id={gate['run2']['run_id']}")
    expect(page.locator("[data-testid=run-report]")).to_be_visible()
    summary = page.locator("[data-testid=report-summary]")
    expect(summary).to_be_visible()
    assert summary.inner_text().strip()
    expect(page.locator("[data-testid=report-images]")).to_be_visible()
    expect(page.locator("[data-testid=report-cost]")).to_be_visible()
    expect(page.locator("[data-testid=report-fidelity]")).to_be_visible()
    expect(page.locator("[data-testid=model-line]")).to_contain_text("v1")
    expect(page.locator("[data-testid=model-line]")).to_contain_text("v2")

    page.goto(f"{live_server}/admin/run.html?id={gate['run3']['run_id']}")
    expect(page.locator("[data-testid=model-line]")).to_contain_text("No change")
    expect(page.locator("[data-testid=model-line]")).to_contain_text("v2")
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]


def test_gallery_card_and_game_about_panel(live_server: str, phone_page: Page, gate: dict[str, Any]):
    slug, page = gate["slug"], phone_page
    page.goto(live_server + "/")
    page.wait_for_selector('[data-hero-page="gallery"]')
    card = page.wait_for_selector(f'article.street-card[data-slug="{slug}"]')
    chip = card.query_selector(".badge-fidelity")
    assert chip is not None and chip.get_attribute("data-fidelity") == "L2", chip and chip.inner_text()
    n_photos = gate["run2"]["head"]["counts"]["fused_observations"]
    assert f"{n_photos} photos" in chip.inner_text()
    card.query_selector("a").click()

    page.wait_for_selector('[data-hero-page="play"]')
    page.wait_for_selector("#start:not([disabled])", timeout=60_000)
    assert "L2" in page.inner_text("[data-testid=model-badge]")
    page.click("[data-testid=about-open]")
    panel = page.locator("[data-testid=about-panel]")
    expect(panel).to_be_visible()
    assert panel.locator("[data-testid=about-summary]").inner_text().strip()
    expect(panel.locator("[data-testid=about-fidelity-bar]")).to_be_visible()
    expect(panel.locator("[data-testid=about-photos]")).to_contain_text("Street View")
    expect(panel.locator("[data-testid=about-least-certain]")).to_be_visible()
    page.click("[data-testid=about-close]")
    expect(panel).to_be_hidden()
    assert not page.hero_errors, page.hero_errors  # type: ignore[attr-defined]
