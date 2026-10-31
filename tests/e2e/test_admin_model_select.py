"""e2e for the model dropdown wiring on every spend button (segment page), against the mocked ``GET /api/admin/models``.

The dropdown element comes from ``model_select.js`` (slice SM, ``renderModelSelect``); the shared state across the page's
dropdowns lives in ``improve.js`` (``createModels``). These tests assert behaviour only (data-testid=model-select)."""

from __future__ import annotations

import re

import pytest
from admin_api_mock import MODELS, PASSWORD, RUN_ID, SLUG, MockApi
from playwright.sync_api import Page, expect, sync_playwright

PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser) -> Page:
    ctx = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.set_default_timeout(15_000)
    yield pg
    ctx.close()


def _open(page: Page, base: str, api: MockApi) -> MockApi:
    api.install(page)
    page.goto(f"{base}/admin/segment.html?slug={SLUG}")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    expect(page.get_by_test_id("improve-badge")).to_have_text("v2 · L2 · 48 photos")
    return api


def test_default_model_is_preselected_next_to_every_spend_button(live_server: str, page: Page):
    _open(page, live_server, MockApi())
    for scope in (page.locator(".header-actions"), page.get_by_test_id("improve-action-fill_gaps"), page.get_by_test_id("improve-action-add_imagery")):
        sel = scope.get_by_test_id("model-select")
        expect(sel).to_have_value("claude-sonnet-5-5")
        expect(sel.locator("option:checked")).to_have_text("Sonnet 5.5")


def test_options_show_price_and_a_disabled_option_explains_why(live_server: str, page: Page):
    _open(page, live_server, MockApi())
    sel = page.locator(".header-actions").get_by_test_id("model-select")
    opts = sel.locator("option")
    expect(opts).to_have_count(2)
    sonnet, opus = opts.nth(0), opts.nth(1)
    expect(sonnet).to_have_attribute("title", re.compile(r"\$3/Mtok in · \$15/Mtok out"))
    expect(sonnet).not_to_be_disabled()
    expect(opus).to_have_attribute("disabled", "")
    expect(opus).to_have_attribute("title", MODELS["options"][1]["disabled_reason"])
    expect(opus).to_contain_text("unavailable")
    expect(page.locator(".header-actions .model-note")).to_contain_text("Structured-output check failed")


def test_header_generate_sends_the_chosen_model(live_server: str, page: Page):
    api = _open(page, live_server, MockApi())
    page.locator("#generate-btn").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    assert api.posts(f"/segments/{SLUG}/generate")[0] == {"model": "claude-sonnet-5-5"}


def test_fetch_generate_sends_model_and_imagery(live_server: str, page: Page):
    api = _open(page, live_server, MockApi())
    page.get_by_test_id("fetch-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    body = api.posts(f"/segments/{SLUG}/generate")[0]
    assert body["model"] == "claude-sonnet-5-5" and body["imagery"]["headings"] == ["forward", "back", "left", "right"]


def test_fill_gaps_estimate_names_the_model_and_the_choice_is_shared(live_server: str, page: Page):
    api = _open(page, live_server, MockApi())
    card = page.get_by_test_id("improve-action-fill_gaps")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Sonnet 5.5")
    # The disabled option cannot be selected.
    sel = card.get_by_test_id("model-select")
    sel.evaluate("s => { s.value = 'claude-opus-5-5'; s.dispatchEvent(new Event('change', {bubbles: true})); }")  # forced past the UI
    expect(sel).to_have_value("claude-sonnet-5-5")
    card.get_by_test_id("fill-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    assert api.posts(f"/segments/{SLUG}/fill-gaps")[0]["model"] == "claude-sonnet-5-5"


def test_choosing_another_enabled_model_updates_all_dropdowns_and_is_sent(live_server: str, page: Page):
    api = MockApi()
    api.models = {"default": "claude-sonnet-5-5", "options": [dict(o, enabled=True, disabled_reason=None) for o in MODELS["options"]]}
    _open(page, live_server, api)
    header = page.locator(".header-actions").get_by_test_id("model-select")
    fill = page.get_by_test_id("improve-action-fill_gaps").get_by_test_id("model-select")
    header.select_option("claude-opus-5-5")
    expect(fill).to_have_value("claude-opus-5-5")
    expect(page.get_by_test_id("improve-action-add_imagery").get_by_test_id("model-select")).to_have_value("claude-opus-5-5")
    card = page.get_by_test_id("improve-action-fill_gaps")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Opus 5.5")
    card.get_by_test_id("fill-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    assert api.posts(f"/segments/{SLUG}/fill-gaps")[0]["model"] == "claude-opus-5-5"


def test_a_non_default_choice_is_not_remembered_across_reloads(live_server: str, page: Page):
    api = MockApi()
    api.models = {"default": "claude-sonnet-5-5", "options": [dict(o, enabled=True, disabled_reason=None) for o in MODELS["options"]]}
    _open(page, live_server, api)
    sel = page.locator(".header-actions").get_by_test_id("model-select")
    sel.select_option("claude-opus-5-5")
    page.reload()
    expect(page.get_by_test_id("improve-badge")).to_have_text("v2 · L2 · 48 photos")
    expect(page.locator(".header-actions").get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")


def test_models_endpoint_failure_hides_the_dropdown_and_runs_still_start(live_server: str, page: Page):
    api = _open(page, live_server, MockApi(models_status=500))
    expect(page.locator(".header-actions .model-note")).to_contain_text("Model list unavailable")
    expect(page.locator(".header-actions").get_by_test_id("model-select")).to_be_hidden()
    page.locator("#generate-btn").click()
    page.wait_for_url(re.compile(rf"/admin/run\.html\?id={RUN_ID}"))
    body = api.posts(f"/segments/{SLUG}/generate")[0]
    assert body is None or "model" not in body
