"""Admin UI live smoke (headless Chromium via Playwright against ``live_server``).

Walks the admin journey from spec/roadmap.md "How the user tests it" step 1 without
triggering a second generation: it reuses the ``generated_segment`` session fixture's
latest run and asserts the pipeline view renders steps, timings, cost, Google counts, the
final scene JSON and a loadable preview image. Also asserts the wrong-password path shows
an unauthorized state instead of a blank page.
"""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

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


def _scene_run_id(base_url: str, slug: str, password: str) -> str:
    """The run that PRODUCED the live scene (``segments.latest_run_id``), not the newest run of any
    outcome - another test module may have left a newer ``failed_budget`` run behind."""
    r = httpx.get(base_url + "/api/admin/segments", headers={ADMIN_HEADER: password}, timeout=10)
    assert r.status_code == 200, r.text
    rows = [s for s in r.json()["data"] if s["slug"] == slug]
    assert rows, f"segment {slug} not listed by /api/admin/segments"
    run_id = rows[0].get("latest_run_id")
    assert run_id, f"segment {slug} has no latest_run_id (no successful generation)"
    return run_id


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


def test_admin_dashboard_shows_spend_and_seed_segment(live_server: str, page: Page, generated_segment: str):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    spend = page.locator("#spend")
    expect(spend).to_have_text(re.compile(r"\$\d+\.\d{2} / \$\d+\.\d{2}"), timeout=15000)
    expect(page.locator("#google-totals")).to_contain_text("Google requests")
    # Seed segment listed (status + Generate button per row).
    row = page.locator(f"tr[data-slug='{generated_segment}']")
    expect(row).to_be_visible()
    expect(row.locator("td:nth-child(4) .chip")).to_have_text(re.compile(r"ready|generating|draft|failed"))
    expect(row.get_by_role("button", name=re.compile("Generate"))).to_be_visible()
    # Create form is address-based (no coordinates to type), prefilled with the seed street.
    expect(page.locator("#f-address")).to_have_value("100 Feet Road, Indiranagar")
    expect(page.locator("#f-half")).to_have_value("50")
    expect(page.locator("#f-start-lat")).to_have_count(0)
    expect(page.locator("#create-btn")).to_be_disabled()  # until a location is found and picked
    # Phase 2 stub is clearly labelled and disabled, not a dead button.
    seed = page.locator("button[data-stub='true']", has_text="Seed city")
    expect(seed).to_be_disabled()
    expect(seed).to_contain_text("Phase 2")
    assert page.evaluate("() => window.__hero && window.__hero.state && window.__hero.state.status !== null")


def test_run_view_renders_steps_cost_google_counts_and_final_scene(
    live_server: str, page: Page, generated_segment: str
):
    password = _password()
    run_id = _scene_run_id(live_server, generated_segment, password)
    _login(page, live_server, f"/admin/run.html?id={run_id}", password)

    root = page.locator('[data-hero-page="admin-run"]')
    expect(root).to_be_visible()
    expect(page.locator("#run-status")).to_have_text("succeeded", timeout=30000)

    # Steps rendered with timings.
    steps = page.locator("#steps li[data-step]")
    assert steps.count() >= 5, "expected at least resolve_geometry..assemble_scene + finalize"
    expect(page.locator("#steps li[data-step='fetch_satellite']")).to_be_visible()
    expect(page.locator("#steps li[data-step='detect_layout'] .latency")).to_have_text(re.compile(r"\d+(\.\d+)?\s*(ms|s)"))
    expect(page.locator("#steps li[data-step='finalize']")).to_be_visible()

    # Cost > 0 and Google counts "N new / M cached".
    cost_text = page.locator("#cost-usd").inner_text()
    usd = float(re.search(r"\$(\d+\.\d+)", cost_text).group(1))
    assert usd > 0, cost_text
    expect(page.locator("#google-counts")).to_have_text(re.compile(r"\d+ new / \d+ cached"))
    assert page.locator("#calls tbody tr").count() >= 1

    # Final scene JSON present and parseable.
    page.locator("#final-scene summary").click()
    scene_text = page.locator("#final-scene pre").inner_text()
    scene = json.loads(scene_text)
    assert scene["schema_version"] == 1
    assert scene["segment"]["slug"] == generated_segment
    assert len(scene["cross_section"]["lanes"]) == 3

    # Imagery and review iteration previews.
    sat = page.locator("#imagery img.satellite")
    expect(sat).to_be_visible()
    previews = page.locator("#iterations img.preview")
    assert previews.count() >= 1, "expected at least one review iteration preview"
    preview_url = previews.first.get_attribute("src")
    r = httpx.get(live_server + preview_url, timeout=10)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")

    # Play link + labelled Phase 2 stub.
    expect(page.locator("a#play-link")).to_have_attribute("href", f"/play/?segment={generated_segment}")
    correct = page.locator("button[data-stub='true']", has_text="Correct detected lines")
    expect(correct).to_be_disabled()
    expect(correct).to_contain_text("Phase 2")


def test_run_view_with_unknown_run_id_shows_error_state(live_server: str, page: Page):
    _login(page, live_server, "/admin/run.html?id=00000000-0000-4000-8000-000000000000", _password())
    expect(page.locator("[data-state='error']")).to_be_visible(timeout=15000)
    expect(page.locator("[data-state='error']")).not_to_contain_text("Traceback")


def test_address_form_finds_a_location_and_creates_a_centred_segment_without_spending(live_server: str, page: Page, generated_segment: str):
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
    expect(msg).to_contain_text("Press Generate")
    slug = re.search(r"\(([a-z0-9-]+), (\d+) m\)", msg.inner_text())
    assert slug, msg.inner_text()
    assert slug.group(1) != generated_segment and int(slug.group(2)) == 150
    row = page.locator(f"tr[data-slug='{slug.group(1)}']")
    expect(row).to_be_visible()
    expect(row.locator("td:nth-child(4) .chip")).to_have_text("draft")
    expect(row).to_contain_text("No runs yet")
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
