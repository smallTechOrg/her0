"""Phone (390x844) + desktop e2e for the Phase-2.1 "How to improve this model" panel, the real upload form and the
placement map. The real static frontend is served by ``live_server``; the admin API is ``admin_api_mock.MockApi``
(shapes per ``spec/api.md``). No LLM, no Google, no spend."""

from __future__ import annotations

import json
import re

import pytest
from admin_api_mock import PASSWORD, RUN_ID, SLUG, TILE, MockApi
from playwright.sync_api import Page, expect, sync_playwright

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 900}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _new_page(browser, viewport, mobile):
    ctx = browser.new_context(viewport=viewport, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    pg = ctx.new_page()
    pg.set_default_timeout(15_000)
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.hero_errors = errors  # type: ignore[attr-defined]
    return ctx, pg


@pytest.fixture()
def page(browser) -> Page:
    ctx, pg = _new_page(browser, PHONE, True)
    yield pg
    ctx.close()


@pytest.fixture()
def desktop(browser) -> Page:
    ctx, pg = _new_page(browser, DESKTOP, False)
    yield pg
    ctx.close()


def _segment(page: Page, base: str, api: MockApi | None = None, query: str = "") -> MockApi:
    api = api or MockApi()
    api.install(page)
    page.goto(f"{base}/admin/segment.html?slug={SLUG}{query}")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    expect(page.locator('[data-hero-page="admin-segment"]')).to_be_visible()
    expect(page.get_by_test_id("improve-weakest").locator("h3, [data-testid=improve-weakest-empty]").first).to_be_visible()
    return api


def png(name: str, size: int | None = None) -> dict:
    buf = TILE if size is None else TILE + b"\0" * size
    return {"name": name, "mimeType": "image/png", "buffer": buf}


def click_map(page: Page, dx: float, dy: float) -> None:
    """Click the placement canvas at an offset (CSS px) from its centre."""
    canvas = page.get_by_test_id("upload-map-canvas")
    box = canvas.bounding_box()
    canvas.click(position={"x": box["width"] / 2 + dx, "y": box["height"] / 2 + dy})


def no_overflow(page: Page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")


# ---------------------------------------------------------------------------------------------------- the panel


def test_improve_panel_lists_weakest_intervals_with_hints_and_steps(live_server: str, page: Page):
    _segment(page, live_server)
    panel = page.get_by_test_id("improve")
    expect(panel).to_be_visible()
    # It is the first section under the header, above Version history.
    order = page.evaluate("""() => [...document.querySelectorAll('#page > section')].map(s => s.dataset.testid)""")
    assert order[0] == "improve" and order.index("versions") > 0
    expect(panel.get_by_test_id("improve-badge")).to_have_text("v2 · L2 · 48 photos")
    expect(panel.get_by_test_id("improve-scene")).to_have_text("scene v3")
    # Compact coverage bar with data-level cells (one per 5 m).
    cells = panel.get_by_test_id("improve-bar").locator(".cov-cell")
    assert cells.count() == 21 and {c.get_attribute("data-level") for c in cells.all()} >= {"L1", "L2", "L3"}
    # The five weakest (of six intervals), weakest first, each with its hint and per-interval fidelity bar.
    items = panel.get_by_test_id("weakest-item")
    expect(items).to_have_count(5)
    expect(items.nth(0)).to_contain_text("0–10 m · L0")
    expect(items.nth(1)).to_contain_text("10–40 m · L1")
    expect(items.nth(1)).to_contain_text("single viewpoint; no right-facing photo")
    expect(items.nth(1)).to_contain_text("Add a Street View frame facing right")
    expect(items.nth(1).locator(".lvl-bar")).to_have_attribute("data-level", "L1")
    expect(items.nth(1).get_by_test_id("fill-stretch")).to_be_visible()
    expect(items.nth(1).get_by_test_id("upload-here")).to_be_visible()
    # Four action cards.
    for action in ("fill_gaps", "add_imagery", "upload", "regenerate"):
        expect(panel.get_by_test_id(f"improve-action-{action}")).to_be_visible()
    # The how-to steps, numbered.
    steps = panel.get_by_test_id("improve-help")
    expect(steps).to_have_attribute("open", "")
    expect(steps.locator("li")).to_have_count(5)
    expect(steps.locator("li").nth(1)).to_contain_text("Press Fill gaps")
    expect(steps).to_contain_text("L3 plus a close-up photo")
    assert no_overflow(page)
    assert not page.hero_errors  # type: ignore[attr-defined]


def test_the_upload_stub_is_gone_and_a_real_file_input_exists(live_server: str, page: Page):
    _segment(page, live_server)
    expect(page.get_by_test_id("upload-stub")).to_have_count(0)
    assert page.locator("[data-stub='true'][data-testid=upload-stub]").count() == 0
    expect(page.get_by_text("Coming in Phase 3")).to_have_count(0)
    fi = page.get_by_test_id("upload-file")
    expect(fi).to_have_count(1)
    expect(fi).to_have_attribute("type", "file")
    expect(fi).to_have_attribute("multiple", "")
    expect(fi).to_have_attribute("accept", "image/jpeg,image/png")
    assert page.evaluate("() => document.querySelector('[data-testid=upload-file]').matches('input[type=file]')")


def test_weakest_empty_state_and_stale_scene_chip(live_server: str, page: Page):
    _segment(page, live_server, MockApi(weakest_empty=True, scene_schema=2))
    panel = page.get_by_test_id("improve")
    expect(panel.get_by_test_id("improve-weakest-empty")).to_have_text("Every stretch is at the target level.")
    expect(panel.get_by_test_id("weakest-item")).to_have_count(0)
    expect(panel.get_by_test_id("improve-scene")).to_have_text("scene v2 (stale — Regenerate to upgrade)")


def test_improve_plan_failure_shows_an_error_with_retry_and_cards_still_work(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    page.route("**/improve-plan", lambda r: r.fulfill(status=500, content_type="application/json",
                                                       body=json.dumps({"ok": False, "error": {"code": "internal_error", "message": "boom", "detail": None}})))
    page.goto(f"{live_server}/admin/segment.html?slug={SLUG}")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    err = page.get_by_test_id("improve").locator("[data-state=error]")
    expect(err).to_be_visible()
    expect(err).to_contain_text("boom")
    expect(page.get_by_test_id("upload-file")).to_be_visible()
    expect(page.get_by_test_id("add-imagery")).to_be_visible()


def test_busy_run_disables_spend_buttons_and_links_to_it(live_server: str, page: Page):
    _segment(page, live_server, MockApi(active_run_id=RUN_ID))
    busy = page.get_by_test_id("improve-busy")
    expect(busy).to_contain_text("already in progress")
    expect(busy.locator("a")).to_have_attribute("href", re.compile(rf"/admin/run\.html\?id={RUN_ID}"))
    expect(page.get_by_test_id("regenerate")).to_be_disabled()
    expect(page.get_by_test_id("fetch-generate")).to_be_disabled()
    expect(page.locator("#generate-btn")).to_be_disabled()


# ---------------------------------------------------------------------------------------------------- fill gaps


def test_fill_gaps_preview_shows_counts_cost_stretches_then_generates(live_server: str, page: Page):
    api = _segment(page, live_server)
    card = page.get_by_test_id("improve-action-fill_gaps")
    # No preview yet: the spend buttons say why they are off.
    expect(card.get_by_test_id("fill-generate")).to_be_disabled()
    expect(card.get_by_test_id("fill-generate")).to_have_attribute("title", "Press Preview plan first.")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-table")).to_be_visible()
    rows = card.get_by_test_id("fill-table").locator("tbody tr")
    expect(rows).to_have_count(7)
    expect(card.locator("tr[data-cached=true]")).to_have_count(1)
    expect(card.locator("tr[data-cached=false] .chip").first).to_have_text("new")
    expect(rows.first).to_contain_text("right")
    expect(rows.first).to_contain_text("no right-facing photo within 15 m")
    totals = card.get_by_test_id("fill-totals")
    expect(totals).to_contain_text("6 new · 1 cached")
    expect(totals).to_have_text(re.compile(r"≈ \$0\.\d\d Google · ≈ \$0\.\d\d Claude \(estimate\) · ≈ \$0\.\d\d total · Model: Sonnet 5\.5"))
    expect(card.get_by_test_id("fill-stretches")).to_contain_text("L1 → L2")
    # The preview body is the plan's default body; nothing was spent.
    assert api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1] == {"target_level": 2, "max_images": 40, "max_usd": 1.0,
                                                                    "model": "claude-sonnet-5-5"}
    assert not api.posts(f"/segments/{SLUG}/fill-gaps") and not api.posts("/generate")
    # Now the run buttons are live; Fetch + Generate posts the same body with fuse + the model.
    expect(card.get_by_test_id("fill-generate")).to_be_enabled()
    card.get_by_test_id("fill-generate").click()
    page.wait_for_url(re.compile(rf"/admin/run\.html\?id={RUN_ID}"))
    body = api.posts(f"/segments/{SLUG}/fill-gaps")[0]
    assert body == {"target_level": 2, "max_images": 40, "max_usd": 1.0, "fuse": True, "model": "claude-sonnet-5-5"}


def test_fill_gaps_fetch_only_sends_fuse_false_without_a_model(live_server: str, page: Page):
    api = _segment(page, live_server)
    card = page.get_by_test_id("improve-action-fill_gaps")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-fetch")).to_be_enabled()
    card.get_by_test_id("fill-fetch").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    body = api.posts(f"/segments/{SLUG}/fill-gaps")[0]
    assert body["fuse"] is False and "model" not in body


def test_fill_this_stretch_presets_the_range_and_previews_only_it(live_server: str, page: Page):
    api = _segment(page, live_server)
    page.get_by_test_id("weakest-item").nth(2).get_by_test_id("fill-stretch").click()  # 40-60 m
    card = page.get_by_test_id("improve-action-fill_gaps")
    expect(card.get_by_test_id("fill-table")).to_be_visible()
    body = api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]
    assert body["s_range"] == {"s_from": 40.0, "s_to": 60.0}
    expect(card.get_by_test_id("fill-table").locator("tbody tr")).to_have_count(2)  # stations 45 and 55
    expect(card).to_contain_text("Only 40–60 m")
    card.get_by_role("button", name="Whole street").click()
    expect(card.get_by_test_id("fill-table")).to_have_count(0)  # stale preview dropped
    expect(card.get_by_test_id("fill-generate")).to_be_disabled()


def test_fill_gaps_warning_chips_never_block_the_run(live_server: str, page: Page):
    _segment(page, live_server, MockApi(exceed=True, trimmed=True))
    card = page.get_by_test_id("improve-action-fill_gaps")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-exceeds")).to_contain_text("exceeds the remaining global budget")
    expect(card.get_by_test_id("fill-trimmed")).to_be_visible()
    expect(card.get_by_test_id("fill-generate")).to_be_enabled()


def test_fill_gaps_start_error_is_shown_inline(live_server: str, page: Page):
    api = MockApi(post_status={"/fill-gaps": (409, {"ok": False, "error": {"code": "run_in_progress", "message": "busy", "detail": None}})})
    _segment(page, live_server, api)
    card = page.get_by_test_id("improve-action-fill_gaps")
    card.get_by_test_id("fill-preview").click()
    card.get_by_test_id("fill-generate").click()
    expect(card.locator(".form-msg")).to_contain_text("Another run is in progress")


# ---------------------------------------------------------------------------------------------------- regenerate


def test_regenerate_with_what_we_have_reports_zero_spend(live_server: str, page: Page):
    api = _segment(page, live_server)
    card = page.get_by_test_id("improve-action-regenerate")
    expect(card).to_contain_text("Costs $0: no Google, no Claude.")
    expect(card.locator("[name=regen-mode][value=compile]")).to_be_checked()
    expect(card.locator(".regen-hint")).to_contain_text("2 stored photos have no saved detections")
    card.get_by_test_id("regenerate").click()
    res = card.get_by_test_id("regenerate-result")
    expect(res).to_have_text("Done — scene v3, 0 Google requests, 0 Claude calls, $0.00; 25 m interpolated; 41 objects now spawn dynamically")
    assert api.posts(f"/segments/{SLUG}/regenerate")[-1] == {"mode": "compile"}
    # No Google/Claude-spending endpoint was touched.
    assert not api.posts("/generate") and not api.posts("/imagery") and not api.posts("/fill-gaps")
    card.locator("[name=regen-mode][value=remerge]").check()
    card.get_by_test_id("regenerate").click()
    for _ in range(50):
        if len(api.posts(f"/segments/{SLUG}/regenerate")) == 2:
            break
        page.wait_for_timeout(100)
    assert [b["mode"] for b in api.posts(f"/segments/{SLUG}/regenerate")] == ["compile", "remerge"]
    expect(card.get_by_test_id("regenerate")).to_have_text("Regenerate")


def test_regenerate_error_is_visible(live_server: str, page: Page):
    api = MockApi(post_status={"/regenerate": (422, {"ok": False, "error": {"code": "validation_error", "message": "Nothing to regenerate: generate once first.", "detail": None}})})
    _segment(page, live_server, api)
    page.get_by_test_id("regenerate").click()
    expect(page.get_by_test_id("regenerate-result")).to_contain_text("Nothing to regenerate: generate once first.")


# ---------------------------------------------------------------------------------------------------- upload + map


def test_choosing_files_makes_rows_and_upload_waits_for_positions(live_server: str, page: Page):
    _segment(page, live_server)
    form = page.get_by_test_id("upload-form")
    expect(form.get_by_test_id("upload-submit")).to_be_disabled()
    expect(form.get_by_test_id("upload-map")).to_be_visible()
    page.get_by_test_id("upload-file").set_input_files([png("one.png"), png("two.png")])
    rows = form.get_by_test_id("upload-row")
    expect(rows).to_have_count(2)
    expect(rows.first.locator("img.thumb")).to_have_attribute("src", re.compile("^blob:"))
    expect(rows.first).to_contain_text("one.png")
    expect(rows.first).to_contain_text("click the map to set the position")
    expect(form.get_by_test_id("upload-submit")).to_be_disabled()
    expect(form.locator(".upload-summary")).to_contain_text("2 photos still need a position")
    # Facing choices incl. custom heading.
    opts = rows.first.locator("[name=facing] option").all_text_contents()
    assert opts == ["along the street ahead", "back along the street", "the left-hand buildings", "the right-hand buildings", "custom heading…"]
    expect(rows.first.locator("[name=heading_deg]")).to_be_hidden()
    rows.first.locator("[name=facing]").select_option("custom")
    expect(rows.first.locator("[name=heading_deg]")).to_be_visible()
    # Removing a row works.
    rows.nth(1).get_by_role("button", name="Remove").click()
    expect(rows).to_have_count(1)
    assert no_overflow(page)


def test_map_click_snaps_to_the_line_and_fills_s_and_side(live_server: str, page: Page):
    _segment(page, live_server)
    page.get_by_test_id("upload-file").set_input_files([png("a.png")])
    row = page.get_by_test_id("upload-row").first
    expect(page.get_by_test_id("upload-map-canvas")).to_be_visible()
    click_map(page, 0, -26)  # above the line = left of travel, middle of the street
    s = float(row.locator("[name=s]").input_value())
    assert 40 <= s <= 60, s
    expect(row.locator("[name=side]")).to_have_value("left")
    expect(row.locator(".row-hint")).to_be_hidden()
    click_map(page, 0, 26)
    expect(row.locator("[name=side]")).to_have_value("right")
    click_map(page, 0, 0)
    expect(row.locator("[name=side]")).to_have_value("centre")
    # Compass wording for the side (the fixture line runs north, so left of travel is the west side).
    assert "west side (left of travel)" in row.locator("[name=side]").evaluate("s => [...s.options].map(o => o.textContent).join('|')")
    # Keyboard: arrows move the marker by 1 m.
    before = float(row.locator("[name=s]").input_value())
    page.get_by_test_id("upload-map-canvas").focus()
    page.keyboard.press("ArrowRight")
    after = float(row.locator("[name=s]").input_value())
    assert abs((after - before) - 1.0) < 0.11
    # Clicking a coverage cell under the map places the photo in the middle of that 5 m cell.
    page.get_by_test_id("upload-map-cells").locator(".cov-cell").nth(19).click()
    assert float(row.locator("[name=s]").input_value()) == 97.5


def test_upload_posts_s_side_heading_and_date_sequentially(live_server: str, page: Page):
    api = _segment(page, live_server)
    form = page.get_by_test_id("upload-form")
    page.get_by_test_id("upload-file").set_input_files([png("front.png"), png("side.png")])
    rows = form.get_by_test_id("upload-row")
    rows.nth(0).click()
    click_map(page, 0, -26)
    rows.nth(0).locator("[name=captured_at]").fill("2024-03")
    rows.nth(1).click()
    click_map(page, -80, 26)
    rows.nth(1).locator("[name=facing]").select_option("custom")
    rows.nth(1).locator("[name=heading_deg]").fill("92")
    rows.nth(1).locator("[name=captured_at]").fill("2023-11")
    expect(form.get_by_test_id("upload-submit")).to_have_text("Upload 2 photos")
    expect(form.get_by_test_id("upload-submit")).to_be_enabled()
    form.get_by_test_id("upload-submit").click()
    expect(rows.nth(0).get_by_test_id("upload-row-status")).to_contain_text("placed at")
    expect(rows.nth(0).get_by_test_id("upload-row-status")).to_contain_text("can lift")
    expect(rows.nth(1).get_by_test_id("upload-row-status")).to_contain_text("to L3")
    ups = [u for u in api.uploads]
    assert len(ups) == 2
    first, second = ups
    assert first["file"]["filename"] == "front.png" and first["file"]["content_type"] == "image/png" and first["file"]["size"] == len(TILE)
    assert 40 <= float(first["s"]) <= 60 and first["side"] == "left" and first["facing"] == "forward" and first["captured_at"] == "2024-03"
    assert "heading_deg" not in first
    assert second["file"]["filename"] == "side.png" and second["side"] == "right" and second["heading_deg"] == "92" and second["captured_at"] == "2023-11"
    assert float(second["s"]) < float(first["s"]) - 5  # clicked 80 px to the left of centre
    assert "facing" not in second
    # Every upload went to the segment's endpoint, one file per request, in order.
    paths = [p for (m, p, b) in api.requests if m == "POST" and p.endswith("/observations/upload")]
    assert paths == [f"/api/admin/segments/{SLUG}/observations/upload"] * 2
    # After uploading: the grid refreshed and Generate now / Fuse later appear.
    after = form.get_by_test_id("upload-after")
    expect(after).to_contain_text("2 photos uploaded — not yet fused")
    expect(page.get_by_test_id("imagery").locator(".obs-grid figure")).to_have_count(len(api.observations))
    expect(after.get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")
    after.get_by_test_id("upload-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    assert api.posts(f"/segments/{SLUG}/generate")[-1] == {"model": "claude-sonnet-5-5"}


def test_upload_error_is_shown_verbatim_on_its_row_and_others_continue(live_server: str, page: Page):
    api = _segment(page, live_server)
    form = page.get_by_test_id("upload-form")
    page.get_by_test_id("upload-file").set_input_files([png("bad-far.png"), png("good.png")])
    rows = form.get_by_test_id("upload-row")
    for i in range(2):
        rows.nth(i).click()
        click_map(page, 10 * i, -26)
    form.get_by_test_id("upload-submit").click()
    expect(rows.nth(0).get_by_test_id("upload-row-status")).to_have_text("The camera is more than 60 m from the reference line.")
    expect(rows.nth(0)).to_have_attribute("data-status", "error")
    expect(rows.nth(1)).to_have_attribute("data-status", "done")
    assert len(api.uploads) == 2


def test_oversize_and_non_image_files_are_rejected_inline_without_a_request(live_server: str, page: Page):
    api = _segment(page, live_server)
    form = page.get_by_test_id("upload-form")
    page.get_by_test_id("upload-file").set_input_files([
        {"name": "huge.png", "mimeType": "image/png", "buffer": b"\0" * (11 * 1024 * 1024)},
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello"},
    ])
    rows = form.get_by_test_id("upload-row")
    expect(rows.nth(0).get_by_test_id("upload-row-status")).to_contain_text("the limit is 10 MB")
    expect(rows.nth(1).get_by_test_id("upload-row-status")).to_contain_text("not a JPEG or PNG")
    expect(form.get_by_test_id("upload-submit")).to_be_disabled()
    assert not api.uploads


def test_upload_here_presets_the_position_of_the_first_photo(live_server: str, page: Page):
    _segment(page, live_server)
    page.get_by_test_id("weakest-item").nth(1).get_by_test_id("upload-here").click()  # 10-40 m -> 25 m
    expect(page.get_by_test_id("upload-form").locator(".upload-summary")).to_contain_text("near 25 m")
    page.get_by_test_id("upload-file").set_input_files([png("x.png")])
    expect(page.get_by_test_id("upload-row").first.locator("[name=s]")).to_have_value("25")
    # And with a photo already chosen it moves the selected photo.
    page.get_by_test_id("weakest-item").nth(2).get_by_test_id("upload-here").click()  # 40-60 -> 50
    expect(page.get_by_test_id("upload-row").first.locator("[name=s]")).to_have_value("50")


# ---------------------------------------------------------------------------------------------------- desktop


def test_desktop_layout_two_columns_and_full_flow(live_server: str, desktop: Page):
    api = _segment(desktop, live_server)
    cards = desktop.locator("[data-testid^=improve-action-]")
    boxes = [c.bounding_box() for c in cards.all()]
    assert len(boxes) == 4
    assert boxes[0]["y"] == boxes[1]["y"] and boxes[0]["x"] < boxes[1]["x"]  # two columns
    desktop.get_by_test_id("improve-action-fill_gaps").get_by_test_id("fill-preview").click()
    expect(desktop.get_by_test_id("fill-totals")).to_contain_text("Claude (estimate)")
    desktop.get_by_test_id("upload-file").set_input_files([png("d.png")])
    click_map(desktop, 0, -30)
    desktop.get_by_test_id("upload-submit").click()
    expect(desktop.get_by_test_id("upload-row-status")).to_contain_text("placed at")
    assert api.uploads and api.uploads[0]["side"] == "left"
    assert no_overflow(desktop)
    assert not desktop.hero_errors  # type: ignore[attr-defined]
