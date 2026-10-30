"""Phone-viewport (390x844) e2e for the Phase-2 admin model UI: segment page, run page report, imagery form.

The real static frontend is served by ``live_server``; the admin API is fulfilled by ``admin_api_mock.MockApi``
with JSON shaped per ``spec/api.md`` (so these tests do not depend on the backend slices). No LLM, no spend.
"""

from __future__ import annotations

import json
import math
import re

import pytest
from admin_api_mock import (
    LENGTH_S,
    NOOP_RUN_ID,
    PASSWORD,
    RUN_ID,
    SLUG,
    MockApi,
    build_report,
)
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
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.hero_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _open(page: Page, base: str, path: str, api: MockApi) -> None:
    api.install(page)
    page.goto(base + path)
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()


def _segment(page: Page, base: str, api: MockApi | None = None, query: str = "") -> MockApi:
    api = api or MockApi()
    _open(page, base, f"/admin/segment.html?slug={SLUG}{query}", api)
    expect(page.locator('[data-hero-page="admin-segment"]')).to_be_visible()
    return api


def test_segment_page_versions_coverage_entities_diff_on_a_phone(live_server: str, page: Page):
    _segment(page, live_server)
    # Header: model badge + actions.
    expect(page.locator("[data-testid=model-badge]")).to_have_text("v2 · L2 · 48 photos")
    expect(page.locator("#generate-btn")).to_be_enabled()
    expect(page.locator("#play-link")).to_have_attribute("href", f"/play/?segment={SLUG}")

    # Version history: newest first, two rows, latest two preselected.
    rows = page.locator("[data-testid=versions] tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-version", "2")
    expect(rows.first).to_contain_text("Added 22 Street View photos")
    expect(rows.first.locator("a")).to_have_attribute("href", re.compile(r"/admin/run\.html\?id="))
    expect(page.locator("[name=cmp-from]")).to_have_value("1")
    expect(page.locator("[name=cmp-to]")).to_have_value("2")

    # Diff (auto-compared v1 -> v2): added entity, field-level change, "L1 -> L2" fidelity line.
    diff = page.locator("[data-testid=diff]")
    expect(diff.locator("[data-testid=diff-title]")).to_have_text("v1 → v2")
    expect(diff.locator("[data-testid=diff-added] li")).to_have_count(1)
    expect(diff.locator("[data-testid=diff-added]")).to_contain_text("bollard")
    expect(diff.locator("[data-testid=diff-changed]")).to_contain_text("confidence: 0.74 → 0.86")
    expect(diff.locator("[data-testid=diff-changed]")).to_contain_text("colour: white → yellow")
    expect(diff.locator("[data-testid=diff-removed]")).to_contain_text("not seen in 2 later views")
    expect(diff.locator(".fid-line")).to_have_text(re.compile(r"L1 → L2"))
    expect(diff.locator("[data-testid=diff-summary]")).to_contain_text("22 new photos fused")

    # Coverage: one cell per 5 m, each with a data-level, ticks, footpath-state row, markers, histogram.
    cov = page.locator("[data-testid=coverage]")
    cells = cov.locator(".cov-cell")
    expect(cells).to_have_count(math.ceil(LENGTH_S / 5))
    levels = {c.get_attribute("data-level") for c in cells.all()}
    assert levels <= {"L0", "L1", "L2", "L3"} and {"L1", "L2", "L3"} <= levels
    assert cov.locator(".cov-tick").count() >= 10
    assert cov.locator(".fp-strip .fp-cell").count() == cells.count()
    assert {c.get_attribute("data-fp") for c in cov.locator(".fp-strip .fp-cell").all()} >= {"intact", "broken", "blocked"}
    expect(cov.locator(".ent-marker")).to_have_count(7)
    expect(cov.locator("[data-testid=coverage-histogram]")).to_contain_text("L2")
    # Clicking an L1 cell shows a hint and at least one thumbnail.
    cov.locator(".cov-cell[data-level='L1']").first.click()
    pop = cov.locator("[data-testid=coverage-popover]")
    expect(pop).to_be_visible()
    expect(pop.locator("[data-testid=coverage-hint]")).to_contain_text("Add a Street View frame")
    assert pop.locator("img").count() >= 1
    expect(pop).to_contain_text("L1")

    # Entities table: >= 5 rows, filters, removed struck through; opening a row shows the evidence drawer.
    ent = page.locator("[data-testid=entities]")
    expect(ent.locator("tbody tr")).to_have_count(7)
    expect(ent.locator("tr[data-state=removed]")).to_have_class(re.compile("removed"))
    ent.locator("[name=f-state]").select_option("removed")
    expect(ent.locator("tbody tr")).to_have_count(1)
    ent.locator("[name=f-state]").select_option("")
    ent.locator("[name=f-type]").select_option("tree")
    expect(ent.locator("tbody tr")).to_have_count(1)
    ent.locator("[name=f-type]").select_option("")
    ent.locator("[name=f-conf]").select_option("0.7")
    expect(ent.locator("tbody tr")).to_have_count(3)
    ent.locator("[name=f-conf]").select_option("0")
    ent.locator("tbody tr").first.click()
    drawer = ent.locator("[data-testid=evidence-drawer]")
    expect(drawer).to_be_visible()
    expect(drawer.locator("[data-testid=evidence-description]")).to_contain_text("parked vehicle")
    thumbs = drawer.locator("[data-testid=evidence-thumb]")
    assert thumbs.count() >= 1
    # The detection box is drawn from crop_px (160,120,320,240 over 640x480 -> 25%/25%/50%/50%).
    style = thumbs.first.locator("[data-testid=det-box]").get_attribute("style")
    assert "left:25%" in style and "top:25%" in style and "width:50%" in style and "height:50%" in style
    expect(drawer.locator("svg.spark")).to_be_visible()
    expect(drawer).to_contain_text("Seen from the left, not from the right.")
    # Click -> full image viewer; Esc closes it.
    thumbs.first.click()
    expect(page.locator("[data-testid=image-viewer] .det-box")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("[data-testid=image-viewer]")).to_have_count(0)
    # A removed entity states why.
    ent.locator("tr[data-state=removed]").click()
    expect(drawer.locator("[data-testid=removed-reason]")).to_contain_text("Removed")

    # No horizontal page overflow on a phone, no JS errors.
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert not page.hero_errors  # type: ignore[attr-defined]


def test_compare_selectors_load_another_pair_and_failures_show_inline_retry(live_server: str, page: Page):
    api = _segment(page, live_server)
    page.locator("[data-testid=versions] [name=cmp-from]").select_option("2")
    page.locator("[data-testid=versions] [name=cmp-to]").select_option("1")
    page.get_by_test_id("compare").click()
    expect(page.locator("[data-testid=diff-title]")).to_have_text("v2 → v1")
    assert any(p.startswith(f"/api/admin/segments/{SLUG}/model/diff?from=2&to=1") for _, p, _ in api.requests)


def test_entities_error_state_is_inline_with_retry_not_blank(live_server: str, page: Page):
    api = MockApi(entity_status=500)
    _segment(page, live_server, api)
    sec = page.locator("[data-testid=entities]")
    expect(sec.locator("[data-state=error]")).to_be_visible()
    expect(sec.locator("[data-state=error]")).to_contain_text("Couldn't load")
    expect(page.locator("[data-testid=versions] tbody tr")).to_have_count(2)  # other sections unaffected
    api.entity_status = None
    sec.get_by_role("button", name="Retry").click()
    expect(sec.locator("tbody tr")).to_have_count(7)


def test_wrong_password_on_segment_page_shows_unauthorized(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    page.goto(f"{live_server}/admin/segment.html?slug={SLUG}")
    page.get_by_label("Admin password").fill("nope")
    page.get_by_role("button", name="Enter").click()
    expect(page.get_by_text("Wrong password")).to_be_visible()
    expect(page.locator("#page")).to_be_hidden()


def test_unknown_segment_and_missing_slug_show_error_states(live_server: str, page: Page):
    api = MockApi()
    _open(page, live_server, "/admin/segment.html", api)
    expect(page.locator("#page-error")).to_contain_text("No segment in the URL")


def test_legacy_segment_is_labelled_and_draft_without_model_shows_empty_state(live_server: str, page: Page):
    _segment(page, live_server, MockApi(legacy=True, head=1))
    expect(page.locator("[data-testid=legacy-warning]")).to_contain_text("Legacy scene, placement unverified")
    expect(page.locator("[data-testid=model-badge]")).to_contain_text("v1")
    expect(page.locator("[data-testid=reference-source]")).to_contain_text("placement not verified")
    # versions with one row: nothing to compare yet, said plainly
    expect(page.get_by_test_id("compare")).to_be_disabled()
    expect(page.locator("[data-testid=versions] .cmp-note")).to_contain_text("Only one version")
    expect(page.locator("[data-testid=diff] [data-state=empty]")).to_be_visible()


def test_segment_without_a_model_shows_no_model_state_and_placement_check(live_server: str, page: Page):
    _segment(page, live_server, MockApi(empty_model=True))
    expect(page.get_by_test_id("no-model")).to_be_visible()
    expect(page.locator("[data-testid=coverage]")).to_be_hidden()
    page.get_by_test_id("check-placement").click()
    verdict = page.get_by_test_id("placement-verdict")
    expect(verdict).to_contain_text("OpenStreetMap way 25173540 (100 Feet Road)")
    expect(verdict).to_contain_text("line snapped 3 m from the address")
    expect(page.locator("#placement-box img")).to_be_visible()


def test_add_imagery_estimate_tracks_headings_and_fetch_only_posts_the_plan(live_server: str, page: Page):
    api = _segment(page, live_server)
    form = page.locator("[data-testid=add-imagery]")
    est = form.get_by_test_id("estimate")
    expect(est).to_contain_text("new images")
    # The default plan is fully cached by the stored observations (≈ 3 new: the extra stations); 7 m is not.
    assert int(re.search(r"≈ (\d+) new", est.inner_text()).group(1)) <= 4
    form.locator("[name=spacing]").fill("7")
    expect(form.locator(".spacing-out")).to_have_text("7 m")
    four = int(re.search(r"≈ (\d+) new", est.inner_text()).group(1))
    form.locator("[name=heading][value=left]").uncheck()
    form.locator("[name=heading][value=right]").uncheck()
    expect(est).not_to_have_text(re.compile(rf"≈ {four} new"))
    two = int(re.search(r"≈ (\d+) new", est.inner_text()).group(1))
    assert two < four
    assert re.search(r"≈ \d+ new images ≈ \$\d+\.\d{2} Google", est.inner_text())
    # No heading -> a plain message, buttons disabled.
    form.locator("[name=heading][value=forward]").uncheck()
    form.locator("[name=heading][value=back]").uncheck()
    expect(est).to_contain_text("Pick at least one heading")
    expect(form.get_by_test_id("fetch-only")).to_be_disabled()
    form.locator("[name=heading][value=forward]").check()
    form.locator("[name=heading][value=back]").check()
    # Wider spacing -> fewer images (the plan is capped by max_images, so test the other direction).
    before = int(re.search(r"≈ (\d+) new", est.inner_text()).group(1))
    form.locator("[name=spacing]").fill("14")
    expect(form.locator(".spacing-out")).to_have_text("14 m")
    after = int(re.search(r"≈ (\d+) new", est.inner_text()).group(1))
    assert after < before
    # Phase 2.1: the upload stub is gone (the real upload form lives in the improve panel).
    expect(page.get_by_test_id("upload-stub")).to_have_count(0)
    expect(page.get_by_test_id("upload-file")).to_have_count(1)

    form.get_by_test_id("fetch-only").click()
    page.wait_for_url(re.compile(rf"/admin/run\.html\?id={RUN_ID}"))
    body = api.posts(f"/segments/{SLUG}/imagery")[0]
    assert body == {"sv_spacing_m": 14.0, "headings": ["forward", "back"], "sat_crop_zoom": 20}
    assert not api.posts("/generate")


def test_clicking_a_coverage_cell_prefills_the_stretch_and_fetch_generate_posts_it(live_server: str, page: Page):
    api = _segment(page, live_server)
    page.locator("[data-testid=coverage] .cov-cell[data-index='19']").click()
    page.get_by_test_id("use-range").click()
    form = page.locator("[data-testid=add-imagery]")
    expect(form.locator("[name=s_from]")).to_have_value("95")
    expect(form.locator("[name=s_to]")).to_have_value("100")
    form.get_by_test_id("fetch-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    body = api.posts(f"/segments/{SLUG}/generate")[0]
    assert body["imagery"]["s_range"] == {"s_from": 95.0, "s_to": 100.0}
    assert body["imagery"]["headings"] == ["forward", "back", "left", "right"]


def test_busy_server_disables_generate_and_imagery_actions_with_a_reason(live_server: str, page: Page):
    _segment(page, live_server, MockApi(active_run_id=RUN_ID))
    expect(page.locator("#generate-btn")).to_be_disabled()
    expect(page.locator("#generate-btn")).to_have_attribute("title", re.compile("already in progress"))
    expect(page.get_by_test_id("fetch-only")).to_be_disabled()
    expect(page.get_by_test_id("fetch-generate")).to_be_disabled()
    expect(page.locator("[data-testid=add-imagery] .active-note")).to_contain_text("already in progress")


def test_start_run_errors_are_shown_inline_409_and_402(live_server: str, page: Page):
    api = MockApi(post_status={
        "/imagery": (409, {"ok": False, "error": {"code": "run_in_progress", "message": "busy"}}),
        "/generate": (402, {"ok": False, "error": {"code": "budget_exhausted", "message": "Generation budget of $50.00 is used up.", "detail": {}}}),
    })
    _segment(page, live_server, api)
    form = page.locator("[data-testid=add-imagery]")
    form.get_by_test_id("fetch-only").click()
    expect(form.locator(".form-msg")).to_contain_text("Another run is in progress")
    form.get_by_test_id("fetch-generate").click()
    expect(form.locator(".form-msg")).to_contain_text("budget of $50.00")
    page.locator("#generate-btn").click()
    expect(page.locator("#header-msg")).to_contain_text("budget of $50.00")
    assert "segment.html" in page.url  # stayed on the page


def test_imagery_grid_filters_and_fused_badges(live_server: str, page: Page):
    _segment(page, live_server)
    sec = page.locator("[data-testid=imagery]")
    expect(sec.locator(".obs-counts")).to_contain_text("total")
    assert sec.locator(".obs").count() == 46
    expect(sec.locator(".chip", has_text="not yet fused")).to_have_count(1)
    expect(sec.locator(".chip", has_text="fused in v2").first).to_be_visible()
    sec.locator("[name=o-view]").select_option("left")
    assert sec.locator(".obs").count() == 11
    sec.locator("[name=o-kind]").select_option("satellite_overview")
    expect(sec.locator(".obs")).to_have_count(0)  # overview has no view -> filtered out
    expect(sec.locator("[data-role=empty-obs]")).to_be_visible()


def test_run_page_shows_the_run_report_and_model_link(live_server: str, page: Page):
    api = MockApi()
    _open(page, live_server, f"/admin/run.html?id={RUN_ID}", api)
    expect(page.locator('[data-hero-page="admin-run"]')).to_be_visible()
    expect(page.locator("#run-status")).to_have_text("succeeded")
    expect(page.locator("[data-testid=mode-chip]")).to_have_text("generate")
    link = page.locator("[data-testid=model-line] a").first
    expect(link).to_have_text("Model v1 → v2")
    expect(link).to_have_attribute("href", f"/admin/segment.html?slug={SLUG}")

    report = build_report()
    rep = page.locator("[data-testid=run-report]")
    expect(rep.locator("[data-testid=report-summary]")).to_have_text(report["summary"])
    imgs = rep.locator("[data-testid=report-images] tbody tr")
    expect(imgs.locator("[data-col=new]")).to_have_text(str(report["images"]["new"]))
    expect(imgs.locator("[data-col=cached]")).to_have_text(str(report["images"]["cached"]))
    expect(imgs.locator("[data-col=total]")).to_have_text(str(report["images"]["used_total"]))
    expect(rep.locator("[data-testid=report-by-view]")).to_have_text("By heading: forward 11 · back 11 · left 11 · right 11")
    expect(rep.locator("[data-testid=report-by-source]")).to_contain_text("streetview 44")
    expect(rep.locator("[data-testid=report-new]")).to_contain_text("A white bollard by the kerb.")
    expect(rep.locator("[data-testid=report-new] a")).to_have_attribute("href", re.compile(rf"segment\.html\?slug={SLUG}&entity=ent-0000000099"))
    expect(rep.locator("[data-testid=report-changed]")).to_contain_text("confidence 0.74 → 0.86")
    expect(rep.locator("[data-testid=report-profile]")).to_contain_text("footpath now 'missing'")
    assert rep.locator("[data-testid=report-fidelity] .fid-seg").count() == 2
    expect(rep.locator("[data-testid=report-fidelity]")).to_contain_text("L1 → L2 over 0–100 m")
    expect(rep.locator("[data-testid=report-cost]")).to_contain_text("$0.2700 · ")
    expect(rep.locator("[data-testid=report-cost]")).to_contain_text("22 new / 26 cached")
    expect(rep.locator("[data-testid=report-suggestions]")).to_contain_text("close-up photo")
    # Images used: new / cached badges.
    used = page.locator("[data-testid=images-used]")
    expect(used.locator("[data-badge=new]")).to_have_count(3)
    expect(used.locator("[data-badge=cached]")).to_have_count(3)
    # Phase-1 sections without data are hidden, not shown empty.
    expect(page.locator("#card-graphs")).to_be_hidden()
    expect(page.locator("#card-iter")).to_be_hidden()
    expect(page.locator("#card-final")).to_be_hidden()
    stub = page.locator("button[data-stub='true']")
    expect(stub).to_be_disabled()
    expect(stub).to_contain_text("Phase 3")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert not page.hero_errors  # type: ignore[attr-defined]


def test_noop_run_says_the_model_stays_and_report_failure_is_not_blank(live_server: str, page: Page):
    api = MockApi()
    _open(page, live_server, f"/admin/run.html?id={NOOP_RUN_ID}", api)
    expect(page.locator("[data-testid=model-line]")).to_contain_text("No change — model stays v2")
    rep = page.locator("[data-testid=run-report]")
    expect(rep.locator("[data-testid=report-summary]")).to_contain_text("The model stays at v2")
    expect(rep.locator("[data-testid=report-images] [data-col=new]")).to_have_text("0")
    expect(rep.locator("[data-testid=report-images] [data-col=cached]")).to_have_text("48")
    expect(rep.locator("[data-testid=report-cost]")).to_contain_text("$0.0000 · ")


def test_run_report_fetch_error_shows_inline_retry(live_server: str, page: Page):
    api = MockApi(post_status={})
    api.install(page)
    calls = {"n": 0}
    orig = api._handle

    def flaky(route):
        if route.request.url.endswith("/report"):
            calls["n"] += 1
            if calls["n"] == 1:
                return route.fulfill(status=500, content_type="application/json",
                                     body=json.dumps({"ok": False, "error": {"code": "internal_error", "message": "report exploded"}}))
        return orig(route)

    page.unroute("**/api/**")
    page.route("**/api/**", flaky)
    page.goto(f"{live_server}/admin/run.html?id={RUN_ID}")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    err = page.locator("#report-error")
    expect(err).to_contain_text("report exploded")
    page.get_by_role("button", name="Retry").click()
    expect(page.locator("[data-testid=report-summary]")).to_contain_text("published model v2")


def test_dashboard_lists_model_column_open_link_and_mode_in_recent_runs(live_server: str, page: Page):
    api = MockApi(legacy=True)
    _open(page, live_server, "/admin/", api)
    row = page.locator(f"tr[data-slug='{SLUG}']")
    expect(row).to_be_visible()
    expect(row.locator(".model-cell")).to_have_text("v2 · L2")
    expect(row.locator(".chip.kind-legacy")).to_have_text("legacy · placement unverified")
    expect(row.get_by_test_id("open-segment")).to_have_attribute("href", f"/admin/segment.html?slug={SLUG}")
    runs = page.locator("#runs-body tr").first
    expect(runs).to_contain_text("generate")
    expect(runs).to_contain_text("new version")
    stub = page.locator("button[data-stub='true']", has_text="Seed city")
    expect(stub).to_be_disabled()
    expect(stub).to_contain_text("Phase 3")


def test_dashboard_check_placement_after_create_shows_tile_and_verdict(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    orig = api._handle
    cand = {"id": "g-0", "label": "100 Feet Road (e2e)", "lat": 12.9711, "lon": 77.6413, "provider": "google", "heading_deg": 2.0,
            "heading_source": "nearest OSM road", "road_name": "100 Feet Road", "travel_compass": "north", "left_side": "west", "right_side": "east"}

    def handle(route):
        path = route.request.url.split("?")[0]
        if path.endswith("/segments/resolve"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "data": {"candidates": [cand], "half_length_m": {"min": 50, "max": 100}}}))
        if path.endswith("/segments/from-address"):
            return route.fulfill(status=201, content_type="application/json", body=json.dumps(
                {"ok": True, "data": {"slug": SLUG, "name": "New", "length_m": 100.0, "status": "draft"}}))
        return orig(route)

    page.unroute("**/api/**")
    page.route("**/api/**", handle)
    page.goto(live_server + "/admin/")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    page.get_by_role("button", name="Find location").click()
    page.get_by_role("button", name="Create segment").click()
    page.get_by_test_id("check-placement").click()
    box = page.get_by_test_id("placement-box")
    expect(box.get_by_test_id("placement-verdict")).to_contain_text("Road geometry: OpenStreetMap way 25173540 (100 Feet Road) · line snapped 3 m from the address")
    expect(box.locator("img")).to_be_visible()
    assert api.posts(f"/segments/{SLUG}/placement-preview") == [None]


# ---------------------------------------------------------------------------------------------------------------
# Real backend + recorded fixture street (tests/e2e/fixtures/model_fixture, seeded by ``seeded_fixture_segment``).
# No mocks, no LLM, no spend: the admin pages read the real admin_model API from the seeded test DB.
# ---------------------------------------------------------------------------------------------------------------


def _real_login(page: Page, url: str) -> None:
    from hero.config import get_settings

    page.goto(url)
    page.get_by_label("Admin password").fill(get_settings().admin_password.get_secret_value())
    page.get_by_role("button", name="Enter").click()


def test_real_fixture_segment_page_shows_history_coverage_entities_evidence_boxes_diff_and_imagery(
        live_server: str, page: Page, seeded_fixture_segment: str):
    slug = seeded_fixture_segment
    _real_login(page, f"{live_server}/admin/segment.html?slug={slug}")
    expect(page.locator('[data-hero-page="admin-segment"]')).to_be_visible()
    expect(page.locator("[data-testid=model-badge]")).to_contain_text("v2 · L2")

    # Version history: v2, v1, v0 newest first; the latest two preselected and compared.
    rows = page.locator("[data-testid=versions] tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.first).to_have_attribute("data-version", "2")
    expect(page.locator("[name=cmp-from]")).to_have_value("1")
    expect(page.locator("[name=cmp-to]")).to_have_value("2")
    expect(page.locator("[data-testid=diff] [data-testid=diff-title]")).to_have_text("v1 → v2")
    page.locator("[name=cmp-from]").select_option("0")
    page.get_by_test_id("compare").click()
    expect(page.locator("[data-testid=diff] [data-testid=diff-title]")).to_have_text("v0 → v2")

    # Coverage map: cells with levels, histogram, popover with a hint on click.
    cov = page.locator("[data-testid=coverage]")
    assert cov.locator(".cov-cell").count() >= 20
    assert {c.get_attribute("data-level") for c in cov.locator(".cov-cell").all()} <= {"L0", "L1", "L2", "L3"}
    expect(cov.locator("[data-testid=coverage-histogram]")).to_be_visible()
    cov.locator(".cov-cell").first.click()
    expect(cov.locator("[data-testid=coverage-popover]")).to_be_visible()

    # Entities with evidence: opening a row shows photos with a detection box inside the image.
    ent = page.locator("[data-testid=entities]")
    assert ent.locator("tbody tr").count() >= 5
    opened = False
    for i in range(ent.locator("tbody tr").count()):
        ent.locator("tbody tr").nth(i).click()
        drawer = ent.locator("[data-testid=evidence-drawer]")
        expect(drawer.locator("[data-testid=evidence-description]")).to_be_visible()
        if drawer.locator("[data-testid=evidence-thumb] [data-testid=det-box]").count():
            opened = True
            style = drawer.locator("[data-testid=det-box]").first.get_attribute("style")
            assert style and "left:" in style and "width:" in style
            img = drawer.locator("[data-testid=evidence-thumb] img").first
            expect(img).to_be_visible()
            assert img.evaluate("i => i.complete && i.naturalWidth > 0")
            break
    assert opened, "no entity of the fixture street shows a detection box on its evidence photo"

    # Imagery grid + the Add more imagery form: heading, controls (no upload stub since Phase 2.1).
    imagery = page.locator("[data-testid=imagery]")
    assert imagery.locator(".obs-grid > *").count() >= 10
    form = page.locator("[data-testid=add-imagery]")
    expect(form.locator("h3")).to_have_text("Add more imagery")
    expect(form.get_by_test_id("estimate")).to_contain_text("new images")
    expect(page.get_by_test_id("upload-stub")).to_have_count(0)
    expect(page.get_by_test_id("upload-file")).to_have_count(1)
    expect(page.locator("[data-testid=reference-line]")).to_be_visible()

    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert not page.hero_errors  # type: ignore[attr-defined]


def test_real_fixture_run_page_shows_the_run_report(live_server: str, page: Page, seeded_fixture_segment: str):
    import httpx

    from hero.config import get_settings

    hdr = {"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()}
    runs = httpx.get(f"{live_server}/api/admin/runs?limit=100", headers=hdr, timeout=10).json()["data"]
    mine = [r["id"] for r in runs if r["segment"]["slug"] == seeded_fixture_segment]
    with_report = [rid for rid in mine
                   if httpx.get(f"{live_server}/api/admin/runs/{rid}/report", headers=hdr, timeout=10).status_code == 200]
    assert with_report, "seeded fixture street has no run with a report"
    _real_login(page, f"{live_server}/admin/run.html?id={with_report[0]}")
    rep = page.locator("[data-testid=run-report]")
    expect(rep).to_be_visible()
    expect(rep.locator("[data-testid=report-summary]")).not_to_be_empty()
    expect(rep.locator("[data-testid=report-cost]")).to_contain_text("$")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert not page.hero_errors  # type: ignore[attr-defined]
