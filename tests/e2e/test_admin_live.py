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


def _seed_form_values(page: Page) -> dict:
    return {k: page.locator(f"#f-{k}").input_value() for k in ("name", "city", "start-lat", "start-lon", "end-lat", "end-lon")}


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
    # Create form prefilled with the seed.
    expect(page.locator("#f-start-lat")).to_have_value("12.97400")
    expect(page.locator("#f-end-lon")).to_have_value("77.64058")
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


def test_create_segment_form_adds_a_row_without_spending(live_server: str, page: Page, generated_segment: str):
    """Second segment through the real form ($0 - no generation): same seed coordinates, new name."""
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    before = httpx.get(live_server + "/api/admin/status", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    name = "100 Feet Road, Indiranagar (e2e form copy)"
    page.locator("#f-name").fill(name)
    page.get_by_role("button", name="Create segment").click()
    msg = page.locator("#create-msg")
    expect(msg).to_be_visible(timeout=15000)
    expect(msg).to_contain_text("Created")
    expect(msg).to_contain_text("Press Generate")
    slug = re.search(r"\(([a-z0-9-]+), (\d+) m\)", msg.inner_text())
    assert slug, msg.inner_text()
    assert slug.group(1) != generated_segment and 30 <= int(slug.group(2)) <= 150
    row = page.locator(f"tr[data-slug='{slug.group(1)}']")
    expect(row).to_be_visible()
    expect(row).to_contain_text(name)
    expect(row.locator("td:nth-child(4) .chip")).to_have_text("draft")
    expect(row).to_contain_text("No runs yet")
    expect(row.get_by_role("button", name=re.compile("Generate"))).to_be_visible()
    # Listed by the admin API as draft, nothing spent.
    listing = httpx.get(live_server + "/api/admin/segments", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    created = next(s for s in listing if s["slug"] == slug.group(1))
    assert created["status"] == "draft" and created["latest_run"] is None
    after = httpx.get(live_server + "/api/admin/status", headers={ADMIN_HEADER: _password()}, timeout=10).json()["data"]
    assert after["spent_usd"] == before["spent_usd"] and after["runs"]["total"] == before["runs"]["total"]


def test_create_segment_form_shows_inline_422_message_for_endpoints_too_close(live_server: str, page: Page):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    seed = _seed_form_values(page)
    page.locator("#f-name").fill("Too short (e2e)")
    # ~10 m north of the start: fails the server's 30-150 m rule (client-side only checks ranges).
    page.locator("#f-end-lat").fill(f"{float(seed['start-lat']) + 0.00009:.5f}")
    page.locator("#f-end-lon").fill(seed["start-lon"])
    page.get_by_role("button", name="Create segment").click()
    msg = page.locator("#create-msg")
    expect(msg).to_be_visible(timeout=15000)
    expect(msg).to_have_class(re.compile(r"\berror\b"))
    expect(msg).to_contain_text("Not created")
    expect(msg).to_contain_text(re.compile(r"30|150|apart|length", re.I))
    assert page.locator("tr[data-slug]", has_text="Too short (e2e)").count() == 0
    expect(page.locator("#create-btn")).to_be_enabled()


def test_create_segment_form_rejects_bad_coordinates_client_side(live_server: str, page: Page):
    _login(page, live_server, "/admin/", _password())
    expect(page.locator("#dashboard")).to_be_visible(timeout=15000)
    page.locator("#f-start-lat").fill("abc")
    page.get_by_role("button", name="Create segment").click()
    msg = page.locator("#create-msg")
    expect(msg).to_be_visible()
    expect(msg).to_contain_text("must be a number")
    expect(page.locator("#f-start-lat")).to_have_attribute("aria-invalid", "true")
