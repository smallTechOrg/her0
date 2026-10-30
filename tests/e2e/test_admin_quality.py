"""e2e for the "Max quality" preset (Fill gaps + Add imagery cards), the model-aware Fill-gaps preview and the admin UI QA
fixes (card heading order, Regenerate radios at 390 px, upload mock validation parity, no engine-export UI).
Real static frontend from ``live_server``; admin API = ``admin_api_mock.MockApi``. No LLM, no Google, no spend."""

from __future__ import annotations

import copy
import re

from admin_api_mock import MODELS, PASSWORD, RUN_ID, SLUG, MockApi
from playwright.sync_api import Page, expect
from test_admin_improve import _segment, browser, desktop, page, png  # noqa: F401 - fixtures + helpers shared with the improve tests

OPUS = "claude-opus-5-5"


def opus_enabled() -> dict:
    models = copy.deepcopy(MODELS)
    for o in models["options"]:
        if o["id"] == OPUS:
            o.update(enabled=True, disabled_reason=None, verified_on="2026-10-30")
    return models


def api_with_opus() -> MockApi:
    api = MockApi()
    api.models = opus_enabled()
    return api


def money(text: str, label: str) -> float:
    m = re.search(rf"≈ \$([0-9.]+) {label}", text)
    assert m, f"no '{label}' figure in {text!r}"
    return float(m.group(1))


def fill_card(page: Page):
    return page.get_by_test_id("improve-action-fill_gaps")


def imagery_card(page: Page):
    return page.get_by_test_id("improve-action-add_imagery")


# ------------------------------------------------------------------ (a) the Fill-gaps preview is model-aware


def test_fill_gaps_preview_posts_the_model_and_reruns_when_the_dropdown_changes(live_server: str, page: Page):
    api = _segment(page, live_server, api_with_opus())
    card = fill_card(page)
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Sonnet 5.5")
    previews = api.posts(f"/segments/{SLUG}/fill-gaps/preview")
    assert len(previews) == 1 and previews[0]["model"] == "claude-sonnet-5-5"
    sonnet_claude = money(card.get_by_test_id("fill-totals").inner_text(), "Claude")

    card.get_by_test_id("model-select").select_option(OPUS)

    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Opus 5.5")
    previews = api.posts(f"/segments/{SLUG}/fill-gaps/preview")
    assert len(previews) == 2 and previews[-1]["model"] == OPUS, "the preview was asked again, priced for the new model"
    assert money(card.get_by_test_id("fill-totals").inner_text(), "Claude") > sonnet_claude


def test_changing_the_model_before_any_preview_does_not_post_one(live_server: str, page: Page):
    api = _segment(page, live_server, api_with_opus())
    fill_card(page).get_by_test_id("model-select").select_option(OPUS)
    page.wait_for_timeout(300)
    assert not api.posts(f"/segments/{SLUG}/fill-gaps/preview")


# ------------------------------------------------------------------ Max quality: Fill gaps card


def test_max_quality_is_off_by_default_and_unchecked_never_changes_the_model(live_server: str, page: Page):
    api = _segment(page, live_server, api_with_opus())
    card = fill_card(page)
    cb = card.get_by_test_id("max-quality-fill")
    expect(cb).not_to_be_checked()
    expect(card.get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_be_visible()
    assert "quality" not in api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]
    expect(card.get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")


def test_max_quality_fills_fields_switches_to_opus_prices_passes_and_rounds_and_can_be_switched_back(live_server: str, page: Page):
    api = _segment(page, live_server, api_with_opus())
    card = fill_card(page)
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Sonnet 5.5")
    plain = money(card.get_by_test_id("fill-totals").inner_text(), "Claude")

    card.get_by_test_id("max-quality-fill").check()

    expect(card.get_by_test_id("model-select")).to_have_value(OPUS)
    expect(card.get_by_test_id("q-passes-fill")).to_have_value("3")
    expect(card.get_by_test_id("q-rounds-fill")).to_have_value("4")
    expect(card.get_by_test_id("fill-totals")).to_contain_text("up to 3 passes + 4 review rounds")
    body = api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]
    assert body["model"] == OPUS and body["quality"] == {"preset": True, "extraction_passes": 3, "review_rounds": 4}
    maxq = money(card.get_by_test_id("fill-totals").inner_text(), "Claude")
    assert maxq > plain

    card.get_by_test_id("q-passes-fill").fill("5")  # the fields are editable and re-price the estimate
    expect(card.get_by_test_id("fill-totals")).to_contain_text("up to 5 passes")
    assert money(card.get_by_test_id("fill-totals").inner_text(), "Claude") > maxq

    card.get_by_test_id("model-select").select_option("claude-sonnet-5-5")  # the user can switch back
    expect(card.get_by_test_id("fill-totals")).to_contain_text("Model: Sonnet 5.5")
    assert api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]["model"] == "claude-sonnet-5-5"
    assert api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]["quality"]["extraction_passes"] == 5

    card.get_by_test_id("fill-generate").click()
    page.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    run = api.posts(f"/segments/{SLUG}/fill-gaps")[0]
    assert run["fuse"] is True and run["quality"] == {"preset": True, "extraction_passes": 5, "review_rounds": 4}


def test_unchecking_max_quality_restores_the_model_it_switched(live_server: str, page: Page):
    api = _segment(page, live_server, api_with_opus())
    card = fill_card(page)
    card.get_by_test_id("max-quality-fill").check()
    expect(card.get_by_test_id("model-select")).to_have_value(OPUS)
    card.get_by_test_id("max-quality-fill").uncheck()
    expect(card.get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")
    card.get_by_test_id("fill-preview").click()
    expect(card.get_by_test_id("fill-totals")).to_be_visible()
    assert "quality" not in api.posts(f"/segments/{SLUG}/fill-gaps/preview")[-1]


def test_max_quality_keeps_the_model_when_the_preset_model_is_not_enabled(live_server: str, page: Page):
    _segment(page, live_server)  # the default mock has Opus disabled
    card = fill_card(page)
    card.get_by_test_id("max-quality-fill").check()
    expect(card.get_by_test_id("model-select")).to_have_value("claude-sonnet-5-5")
    expect(card.get_by_test_id("q-note-fill")).to_contain_text("not enabled")
    expect(card.get_by_test_id("q-passes-fill")).to_have_value("3")


# ------------------------------------------------------------------ Max quality: Add imagery card


def test_max_quality_fills_the_add_imagery_form_and_the_estimate_scales_with_passes_and_rounds(live_server: str, desktop: Page):
    api = _segment(desktop, live_server, api_with_opus())
    form = desktop.get_by_test_id("add-imagery")
    est = form.get_by_test_id("estimate-claude")
    expect(est).to_contain_text("1 extraction pass, 0 review rounds")
    plain_total = float(est.get_attribute("data-total"))
    expect(form.get_by_test_id("max-fields")).to_be_hidden()

    form.get_by_test_id("max-quality-imagery").check()

    expect(form.locator("[name=spacing]")).to_have_value("12")
    for h in ("forward", "back", "left", "right"):
        expect(form.locator(f"[name=heading][value={h}]")).to_be_checked()
    expect(form.locator("[name=zoom][value='21']")).to_be_checked()
    expect(form.get_by_test_id("extra-zoom")).to_have_value("20")
    expect(form.get_by_test_id("max-images")).to_have_value("120")
    expect(form.get_by_test_id("frames-per-station")).to_have_text("4")
    expect(form.get_by_test_id("model-select")).to_have_value(OPUS)
    expect(est).to_contain_text("3 extraction passes, 4 review rounds")
    expect(est).to_contain_text("Model: claude-opus-5-5")
    maxq_total = float(est.get_attribute("data-total"))
    assert maxq_total > plain_total

    form.get_by_test_id("q-rounds-imagery").fill("8")  # more rounds -> a higher estimate
    expect(est).to_contain_text("8 review rounds")
    assert float(est.get_attribute("data-total")) > maxq_total
    form.get_by_test_id("q-rounds-imagery").fill("4")
    form.locator("[name=spacing]").fill("14")  # fields stay editable
    expect(form.locator(".spacing-out")).to_have_text("14 m")

    form.get_by_test_id("fetch-generate").click()
    desktop.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    body = api.posts(f"/segments/{SLUG}/generate")[0]
    assert body["model"] == OPUS and body["quality"] == {"preset": True, "extraction_passes": 3, "review_rounds": 4}
    assert body["imagery"]["sv_spacing_m"] == 14 and body["imagery"]["sat_crop_zoom"] == 21
    assert body["imagery"]["sat_extra_zoom"] == 20 and body["imagery"]["max_images"] == 120
    assert sorted(body["imagery"]["headings"]) == ["back", "forward", "left", "right"]
    est_req = api.posts(f"/segments/{SLUG}/imagery/estimate")[-1]
    assert est_req["quality"]["extraction_passes"] == 3 and est_req["model"] == OPUS


def test_add_imagery_without_the_preset_sends_exactly_what_it_sent_before(live_server: str, desktop: Page):
    api = _segment(desktop, live_server, api_with_opus())
    form = desktop.get_by_test_id("add-imagery")
    form.get_by_test_id("fetch-generate").click()
    desktop.wait_for_url(re.compile(r"/admin/run\.html\?id="))
    body = api.posts(f"/segments/{SLUG}/generate")[0]
    assert set(body) == {"imagery", "model"} and body["model"] == "claude-sonnet-5-5"
    assert set(body["imagery"]) == {"sv_spacing_m", "headings", "sat_crop_zoom"}


# ------------------------------------------------------------------ (b) heading order, (c) radios, (d) upload parity, (e) no export


def test_add_imagery_card_title_comes_before_its_description(live_server: str, page: Page):
    _segment(page, live_server)
    card = imagery_card(page)
    h3 = card.locator("h3").bounding_box()
    desc = card.locator("p.head-desc").bounding_box()
    assert h3["y"] < desc["y"], "the title must be above the description"
    # and it is the first thing in the card
    first = card.locator("form > *").first
    expect(first).to_have_text("Add more imagery")


def test_regenerate_radios_are_aligned_with_their_labels_at_390px(live_server: str, page: Page):
    _segment(page, live_server)
    card = page.get_by_test_id("improve-action-regenerate")
    radios = card.locator("input[type=radio]")
    assert radios.count() == 2
    for i in range(2):
        r = radios.nth(i).bounding_box()
        lab = radios.nth(i).locator("xpath=ancestor::label").bounding_box()
        assert r["width"] <= 24 and r["height"] <= 24, f"radio {i} stretched to {r['width']}x{r['height']}"
        assert abs((r["y"] + r["height"] / 2) - (lab["y"] + lab["height"] / 2)) <= 4, "radio centred on its label"
        assert r["x"] - lab["x"] <= 6, "radio sits at the left edge of its label"
    labels = card.locator("fieldset label.inline")
    assert labels.nth(0).bounding_box()["y"] < labels.nth(1).bounding_box()["y"], "one option per row"
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")


UPLOAD_JS = """async ({slug, fields, name}) => {  // distinct bytes per file name, identical bytes for a re-upload
  const bytes = [137,80,78,71,13,10,26,10,...[...name].map((c) => c.charCodeAt(0))];
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  fd.append('file', new Blob([new Uint8Array(bytes)], {type: 'image/png'}), name);
  const r = await fetch(`/api/admin/segments/${slug}/observations/upload`, {method: 'POST', body: fd, headers: {'X-Hero-Admin-Password': %r}});
  const j = await r.json();
  return {status: r.status, code: (j.error || {}).code, message: (j.error || {}).message};
}""" % PASSWORD


def post_upload(page: Page, name: str = "a.png", **fields) -> dict:
    fields = {k: str(v) for k, v in fields.items()}
    return page.evaluate(UPLOAD_JS, {"slug": SLUG, "fields": fields, "name": name})


def test_upload_mock_enforces_the_real_endpoints_validation(live_server: str, page: Page):
    _segment(page, live_server)
    ok = dict(s=20, side="left", facing="forward")
    assert post_upload(page, "ok.png", **ok)["status"] == 201
    again = post_upload(page, "ok.png", **ok)
    assert again["status"] == 200, "re-uploading the same bytes is the same observation, not a new one"
    r = post_upload(page, "f.png", s=20, side="left", facing="sideways")
    assert r["status"] == 422 and r["code"] == "validation_error" and "facing" in r["message"]
    r = post_upload(page, "s.png", s=20, side="middle", facing="forward")
    assert r["status"] == 422 and "side" in r["message"]
    r = post_upload(page, "h.png", s=20, side="left")
    assert r["status"] == 422 and "facing" in r["message"], "facing or heading_deg is required"
    assert post_upload(page, "h2.png", s=21, side="left", heading_deg=90)["status"] == 201
    r = post_upload(page, "d.png", s=22, side="left", facing="forward", captured_at="2999-01")
    assert r["status"] == 422 and "future" in r["message"]
    r = post_upload(page, "d2.png", s=23, side="left", facing="forward", captured_at="last year")
    assert r["status"] == 422 and "captured_at" in r["message"]
    r = post_upload(page, "p.png", side="left", facing="forward")
    assert r["status"] == 422, "a position (s or lat/lon) is required"
    r = post_upload(page, "p2.png", s=24, facing="forward")
    assert r["status"] == 422 and "side" in r["message"]
    assert post_upload(page, "ok2.png", lat=12.97, lon=77.64, facing="toward_left", captured_at="2023-04-05", s="")["status"] in (200, 201)


def test_there_is_no_engine_export_ui_on_the_segment_page(live_server: str, page: Page):
    api = _segment(page, live_server)
    body = page.locator("body")
    for text in ("Download for", "Unity", "Unreal", "Godot"):
        expect(body).not_to_contain_text(text)
    expect(page.locator("[data-testid^=export-download]")).to_have_count(0)
    expect(page.locator(".export-slot")).to_have_count(0)
    assert not [r for r in api.requests if "export" in r[1]]
    assert page.request.get(f"{live_server}/js/admin/export_button.js").status == 404


def test_run_report_shows_fidelity_per_5m_bin_and_the_max_quality_section(live_server: str, page: Page):
    api = MockApi()
    api.install(page)
    page.goto(f"{live_server}/admin/run.html?id={RUN_ID}")
    page.get_by_label("Admin password").fill(PASSWORD)
    page.get_by_role("button", name="Enter").click()
    bins = page.get_by_test_id("report-bins").locator(".q-bin")
    expect(bins).to_have_count(20)
    expect(bins.first).to_have_attribute("data-before", "L1")
    expect(bins.first).to_have_attribute("data-after", "L2")
    expect(page.locator(".q-bin.up")).to_have_count(10)
    q = page.get_by_test_id("report-quality")
    expect(q).to_contain_text("Max quality")
    expect(q).to_contain_text("3 per frame batch")
    expect(q).to_contain_text("18 detections merged into 8 (5 unanimous, 3 disputed, none dropped)")
    expect(q).to_contain_text("2 of up to 4")
    expect(q).to_contain_text("early stop after round 2")
    expect(q).to_contain_text("3 answered from storage at $0")
