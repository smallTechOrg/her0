"""Admin dashboard / Phase-1 run view e2e (headless Chromium via Playwright against ``live_server``).

The real static frontend is served by ``live_server``. Wrong-password and the address-form create flow hit the REAL admin
API (``$0``: no generation, only the geocoder response is intercepted). The dashboard table and the Phase-1-shaped run
view use ``admin_api_mock.MockApi`` (envelope JSON shaped per ``spec/api.md``) so they do not need a generated street.
"""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from admin_api_mock import PASSWORD as MOCK_PASSWORD
from admin_api_mock import MockApi
from hero.config import get_settings

ADMIN_HEADER = "X-Hero-Admin-Password"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser) -> Page:
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    pg = ctx.new_page()
    yield pg
    ctx.close()


def _password() -> str:
    pw = get_settings().admin_password.get_secret_value()
    assert pw, "HERO_ADMIN_PASSWORD must be set in .env"
    return pw


def _login(page: Page, base_url: str, path: str, password: str) -> None:
    page.goto(base_url + path)
    page.get_by_label("Admin password").fill(password)
    page.get_by_role("button", name="Enter").click()


CANDIDATE = {"id": "google-0", "label": "100 Feet Road, Indiranagar, Bengaluru (e2e)", "lat": 12.975179, "lon": 77.641074,
             "provider": "google", "heading_deg": 2.0, "heading_source": "nearest OSM road", "road_name": "100 Feet Road",
             "travel_compass": "north", "left_side": "west", "right_side": "east"}


def _mock_resolve(page: Page, candidates=None, status=200, body=None):
    payload = body or {"ok": True, "data": {"candidates": candidates or [CANDIDATE], "half_length_m": {"min": 50, "max": 100}}}
    page.route("**/api/admin/segments/resolve", lambda route: route.fulfill(
        status=status, content_type="application/json", body=json.dumps(payload)))


def test_wrong_password_shows_unauthorized_state_not_blank_page(live_server: str, page: Page):
    _login(page, live_server, "/admin/", "definitely-not-the-password")
    expect(page.locator('[data-hero-page="admin"]')).to_be_visible()
    expect(page.locator("[data-state='unauthorized']")).to_be_visible()
    expect(page.get_by_text("Wrong password")).to_be_visible()
    # The dashboard must not have been revealed.
    expect(page.locator("#dashboard")).to_be_hidden()


P1_RUN_ID = "11111111-0000-4000-8000-000000000001"


def _phase1_run() -> dict:
    """A Phase-1 shaped run (no ``mode``): every Phase-1 key of ``GET /api/admin/runs/{id}``, unchanged."""
    scene = {"schema_version": 1, "segment": {"slug": "seed-w", "name": "Seed"}, "cross_section": {"lanes": [{}, {}, {}]},
             "objects": [{}], "tokens": [], "buildings": [],
             "walkability": {"score": 60, "footpath_coverage_pct": 70, "hazard_count": 2, "crossing_present": True},
             "generation": {"quality": "full", "confidence_overall": 0.7, "review_iterations": 1, "warnings": []}}
    steps = [{"name": n, "status": "ok", "latency_ms": 120, "detail": ""} for n in
             ("resolve_geometry", "fetch_satellite", "detect_layout", "assemble_scene", "review_1", "finalize")]
    return {
        "id": P1_RUN_ID, "segment": {"slug": "seed-w", "name": "Seed"}, "status": "succeeded", "started_at": "2026-10-01T10:00:00Z",
        "finished_at": "2026-10-01T10:02:00Z", "inputs": {}, "steps": steps,
        "imagery": {"satellite": {"url": "/media/imagery/tile.png", "annotated_url": None, "zoom": 19, "meters_per_px": 0.2},
                    "streetview": [], "streetview_status": "ok", "osm_available": True, "streetview_available": True, "warnings": []},
        "layout": {"a": 1}, "obstacles": {"b": 2},
        "iterations": [{"n": 1, "preview_url": "/media/previews/p1.png", "accepted": True, "critique": "fine", "patch_ops": [], "applied": True, "detail": ""}],
        "scene": scene, "preview_url": "/media/previews/p1.png", "thumbnail_url": None,
        "cost": {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.31,
                 "calls": [{"step": "detect_layout", "model": "m", "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.31, "latency_ms": 900, "ok": True}]},
        "google": {"requests": 4, "cache_hits": 1}, "error": None, "budget": {"spent_total_usd": 0.31, "budget_usd": 50.0},
    }


def _mock_p1_run(page: Page, api: MockApi) -> None:
    api.install(page)
    orig = api._handle

    def handle(route):
        if route.request.url.split("?")[0].endswith(f"/api/admin/runs/{P1_RUN_ID}"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "data": _phase1_run()}))
        return orig(route)

    page.unroute("**/api/**")
    page.route("**/api/**", handle)


def test_admin_dashboard_shows_spend_and_segment_row(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    _login(page, live_server, "/admin/", MOCK_PASSWORD)
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    expect(page.locator("#spend")).to_have_text(re.compile(r"\$\d+\.\d{2} / \$\d+\.\d{2}"), timeout=15000)
    expect(page.locator("#google-totals")).to_contain_text("Google requests")
    row = page.locator("tr[data-slug='fixture-curved-street']")
    expect(row).to_be_visible()
    expect(row.locator("td:nth-child(4) .chip")).to_have_text("ready")
    expect(row.get_by_role("button", name=re.compile("Generate"))).to_be_visible()
    expect(row.get_by_role("link", name="Open")).to_have_attribute("href", "/admin/segment.html?slug=fixture-curved-street")
    # Create form is address-based (no coordinates to type), prefilled with the demo street.
    expect(page.locator("#f-address")).to_have_value("100 Feet Road, Indiranagar")
    expect(page.locator("#f-half")).to_have_value("50")
    expect(page.locator("#f-start-lat")).to_have_count(0)
    expect(page.locator("#create-btn")).to_be_disabled()  # until a location is found and picked
    # Phase 3 stub is clearly labelled and disabled, not a dead button.
    seed = page.locator("button[data-stub='true']", has_text="Seed city")
    expect(seed).to_be_disabled()
    expect(seed).to_contain_text("Phase 3")
    assert page.evaluate("() => window.__hero && window.__hero.state && window.__hero.state.status !== null")


def test_dashboard_generate_navigates_to_the_run_and_reports_a_running_run_reason(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    _login(page, live_server, "/admin/", MOCK_PASSWORD)
    page.locator("tr[data-slug='fixture-curved-street']").get_by_role("button", name=re.compile("Generate")).click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    assert api.posts("/segments/fixture-curved-street/generate")


def test_phase1_run_view_renders_steps_cost_google_counts_and_final_scene(live_server: str, page: Page):
    api = MockApi()
    _mock_p1_run(page, api)
    _login(page, live_server, f"/admin/run.html?id={P1_RUN_ID}", MOCK_PASSWORD)

    expect(page.locator('[data-hero-page="admin-run"]')).to_be_visible()
    expect(page.locator("#run-status")).to_have_text("succeeded", timeout=30000)
    assert page.locator("#steps li[data-step]").count() >= 5
    expect(page.locator("#steps li[data-step='fetch_satellite']")).to_be_visible()
    expect(page.locator("#steps li[data-step='detect_layout'] .latency")).to_have_text(re.compile(r"\d+(\.\d+)?\s*(ms|s)"))
    usd = float(re.search(r"\$(\d+\.\d+)", page.locator("#cost-usd").inner_text()).group(1))
    assert usd > 0
    expect(page.locator("#google-counts")).to_have_text(re.compile(r"\d+ new / \d+ cached"))
    assert page.locator("#calls tbody tr").count() >= 1
    page.locator("#final-scene summary").click()
    scene = json.loads(page.locator("#final-scene pre").inner_text())
    assert scene["schema_version"] == 1 and len(scene["cross_section"]["lanes"]) == 3
    expect(page.locator("#imagery img.satellite")).to_be_visible()
    assert page.locator("#iterations img.preview").count() >= 1
    expect(page.locator("a#play-link")).to_have_attribute("href", "/play/?segment=seed-w")
    # Phase-1 run: no mode chip, no model line, no report.
    expect(page.locator("[data-testid=mode-chip]")).to_be_hidden()
    expect(page.locator("[data-testid=run-report]")).to_be_hidden()
    expect(page.locator("#card-final")).to_be_visible()
    correct = page.locator("button[data-stub='true']", has_text="Correct detected lines")
    expect(correct).to_be_disabled()
    expect(correct).to_contain_text("Phase 3")


def test_run_view_with_unknown_run_id_shows_error_state(live_server: str, page: Page):
    _login(page, live_server, "/admin/run.html?id=00000000-0000-4000-8000-000000000000", _password())
    expect(page.locator("[data-state='error']")).to_be_visible(timeout=15000)
    expect(page.locator("[data-state='error']")).not_to_contain_text("Traceback")


def test_address_form_finds_a_location_and_creates_a_centred_segment_without_spending(live_server: str, page: Page):
    """Real form + real create endpoint ($0 - no generation); only the geocoder response is mocked."""
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    before = httpx.get(live_server + "/api/admin/status", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    _mock_resolve(page)
    name = f"100 Feet Road e2e {int(time.time())}"
    page.locator("#f-address").fill(name)
    page.locator("#f-half").fill("75")
    page.get_by_role("button", name="Find location").click()
    expect(page.locator("#resolve-list li")).to_have_count(1)
    expect(page.locator("#resolve-list")).to_contain_text("road runs north")
    expect(page.locator("#resolve-list a")).to_have_attribute("href", re.compile(r"google\.com/maps.*12\.975179,77\.641074"))
    expect(page.locator("#f-side option")).to_have_text(["west side (left of travel)", "east side (right of travel)"])
    expect(page.locator("#f-heading")).to_have_value("2")
    expect(page.locator("#segment-preview")).to_contain_text("150 m long")
    page.get_by_role("button", name="Create segment").click()
    msg = page.locator("#create-msg")
    expect(msg).to_contain_text("Created", timeout=15000)
    expect(msg).to_contain_text("press Generate")
    slug = re.search(r"\(([a-z0-9-]+), (\d+) m\)", msg.inner_text())
    assert slug, msg.inner_text()
    assert int(slug.group(2)) == 150
    row = page.locator(f"tr[data-slug='{slug.group(1)}']")
    expect(row).to_be_visible()
    expect(row.locator("td:nth-child(4) .chip")).to_have_text("draft")
    expect(row).to_contain_text("No runs yet")
    # After create: Check placement + a link to the segment page are offered.
    expect(page.get_by_test_id("check-placement")).to_be_visible()
    expect(page.get_by_role("link", name="Open segment page")).to_have_attribute("href", f"/admin/segment.html?slug={slug.group(1)}")
    listing = httpx.get(live_server + "/api/admin/segments", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    created = next(x for x in listing if x["slug"] == slug.group(1))
    assert created["status"] == "draft" and created["latest_run"] is None
    assert abs((created["start"]["lat"] + created["end"]["lat"]) / 2 - CANDIDATE["lat"]) < 1e-4  # centred on the address
    after = httpx.get(live_server + "/api/admin/status", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    assert after["spent_usd"] == before["spent_usd"] and after["runs"]["total"] == before["runs"]["total"]


def test_address_form_blocks_creation_when_half_length_is_outside_50_to_100(live_server: str, page: Page):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    _mock_resolve(page)
    page.get_by_role("button", name="Find location").click()
    expect(page.locator("#create-btn")).to_be_enabled()
    page.locator("#f-half").fill("20")
    expect(page.locator("#create-btn")).to_be_disabled()
    page.locator("#f-half").fill("100")
    expect(page.locator("#create-btn")).to_be_enabled()
    expect(page.locator("#segment-preview")).to_contain_text("200 m long")


def test_address_form_asks_for_the_road_direction_when_none_was_found(live_server: str, page: Page):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    no_dir = {**CANDIDATE, "heading_deg": None, "heading_source": None}
    for k in ("travel_compass", "left_side", "right_side"):
        no_dir.pop(k)
    _mock_resolve(page, [no_dir])
    page.get_by_role("button", name="Find location").click()
    expect(page.locator("#resolve-list")).to_contain_text("enter it below")
    expect(page.locator("#create-btn")).to_be_disabled()
    page.locator("#f-heading").fill("35")
    expect(page.locator("#create-btn")).to_be_enabled()


def test_address_form_shows_the_hint_when_nothing_matches(live_server: str, page: Page):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    _mock_resolve(page, status=404, body={"ok": False, "error": {"code": "not_found",
                  "message": "No match for that address. Try adding the area or city, or paste 'lat, lon'."}})
    page.locator("#f-address").fill("zzzqqq")
    page.get_by_role("button", name="Find location").click()
    msg = page.locator("#create-msg")
    expect(msg).to_contain_text("No match")
    expect(msg).to_have_class(re.compile(r"\berror\b"))
    expect(page.locator("#resolve-results")).to_be_hidden()
    expect(page.locator("#create-btn")).to_be_disabled()
