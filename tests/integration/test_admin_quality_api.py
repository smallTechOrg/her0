"""Max-quality API surface: the preset endpoint, passes/review rounds in the Fill-gaps preview and the Add-imagery estimate,
and the `quality` option reaching the run registry. No spend: the registry is captured."""

from __future__ import annotations

import pytest

from hero.config import get_settings
from hero.model import improve
from tests.integration.test_admin_improve_api import _Run, app_client, auth, new_segment, no_network  # noqa: F401

PRESET_Q = {"preset": True}


def post(client, auth, seg, path, body):
    return client.post(f"/api/admin/segments/{seg.slug}/{path}", headers=auth, json=body)


def test_preset_endpoint_serves_the_values_the_form_fills(app_client, auth):
    r = app_client.get("/api/admin/quality-preset", headers=auth)
    assert r.status_code == 200
    d = r.json()["data"]
    p = d["preset"]
    assert p["model"] == "claude-opus-5-5" and p["extraction_passes"] == 3 and p["review_rounds"] == 4
    assert p["sat_crop_zoom"] == 21 and p["frames_per_station"] == 48 and len(p["headings"]) == 8 and p["sv_spacing_m"] <= 6
    assert d["limits"]["extraction_passes"] == [1, 5]
    assert app_client.get("/api/admin/quality-preset").status_code == 401


def test_fill_gaps_preview_estimate_includes_passes_and_review_rounds(app_client, auth, no_network):
    seg = new_segment("Quality preview")
    plain = post(app_client, auth, seg, "fill-gaps/preview", {}).json()["data"]
    maxq = post(app_client, auth, seg, "fill-gaps/preview", {"quality": PRESET_Q}).json()["data"]
    p3 = post(app_client, auth, seg, "fill-gaps/preview", {"quality": {"extraction_passes": 3}}).json()["data"]

    assert maxq["images"] == plain["images"], "the plan is the same; only the Claude work grows"
    assert maxq["est"]["google_usd"] == plain["est"]["google_usd"]
    assert plain["est"]["claude_usd"] < p3["est"]["claude_usd"] < maxq["est"]["claude_usd"]
    n_sv = sum(1 for i in maxq["images"] if i["kind"] == "streetview" and not i["cached"])
    n_sat = sum(1 for i in maxq["images"] if i["kind"] == "satellite_crop" and not i["cached"])
    want = improve.estimate_cost(n_sv, n_sat, maxq["est"]["model"], passes=3, review_rounds=4)
    assert maxq["est"]["claude_usd"] == want.claude_usd and maxq["est"]["total_usd"] == want.total_usd
    assert maxq["quality"] == {"preset": True, "extraction_passes": 3, "review_rounds": 4}
    assert plain["quality"]["review_rounds"] is None
    assert no_network == []


def test_max_quality_ignores_the_fill_gaps_dollar_ceiling_but_a_plain_run_keeps_it(app_client, auth):
    seg = new_segment("Quality ceiling")
    tight = {"max_usd": 0.05}
    plain = post(app_client, auth, seg, "fill-gaps/preview", tight).json()["data"]
    maxq = post(app_client, auth, seg, "fill-gaps/preview", {**tight, "quality": PRESET_Q}).json()["data"]
    assert plain["trimmed"] is True
    assert maxq["trimmed"] is False and maxq["counts"]["planned"] > plain["counts"]["planned"], "no per-run dollar cap at max quality"


def test_fill_gaps_passes_the_quality_options_to_the_registry(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None, model=None, quality=None):
        seen.update(mode=mode, model=model, quality=quality)
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    seg = new_segment("Quality run")
    r = post(app_client, auth, seg, "fill-gaps", {"quality": {"preset": True, "extraction_passes": 2}})
    assert r.status_code == 202, r.text
    assert seen["quality"] == {"preset": True, "extraction_passes": 2, "review_rounds": 4, "review_stop_changes": 1}
    seen.clear()
    post(app_client, auth, seg, "fill-gaps", {})
    assert seen["quality"] is None, "without the option the call is exactly what it was before"


@pytest.mark.parametrize("quality", [{"extraction_passes": 0}, {"extraction_passes": 6}, {"review_rounds": 9}, {"bogus": 1}])
def test_bad_quality_is_422_everywhere(app_client, auth, no_network, quality):
    seg = new_segment("Quality 422")
    assert post(app_client, auth, seg, "fill-gaps/preview", {"quality": quality}).status_code == 422
    assert post(app_client, auth, seg, "fill-gaps", {"quality": quality}).status_code == 422
    assert post(app_client, auth, seg, "generate", {"quality": quality}).status_code == 422
    assert post(app_client, auth, seg, "imagery/estimate", {"quality": quality}).status_code == 422


def test_generate_forwards_quality_and_the_overlapping_tile_zoom(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None, model=None, quality=None):
        seen.update(overrides=plan_overrides, quality=quality)
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    seg = new_segment("Generate quality")
    body = {"imagery": {"sv_spacing_m": 12, "headings": ["forward", "back", "left", "right"], "sat_crop_zoom": 21,
                        "sat_extra_zoom": 20, "max_images": 120}, "quality": PRESET_Q}
    r = post(app_client, auth, seg, "generate", body)
    assert r.status_code == 202, r.text
    assert seen["overrides"]["sat_extra_zoom"] == 20 and seen["overrides"]["max_images"] == 120
    assert seen["quality"]["extraction_passes"] == 3 and seen["quality"]["review_rounds"] == 4


def test_imagery_estimate_counts_new_images_and_scales_with_passes_rounds_and_the_second_tile_set(app_client, auth, no_network):
    seg = new_segment("Imagery estimate", with_model=True)
    dense = {"sv_spacing_m": 12, "headings": ["forward", "back", "left", "right"], "sat_crop_zoom": 21, "sat_crop_spacing_m": 20, "max_images": 120}
    plain = post(app_client, auth, seg, "imagery/estimate", {"imagery": dense}).json()["data"]
    maxq = post(app_client, auth, seg, "imagery/estimate",
                {"imagery": {**dense, "sat_extra_zoom": 20}, "quality": PRESET_Q}).json()["data"]

    assert plain["planned"] == plain["counts"]["streetview"] + plain["counts"]["satellite_crop"] + plain["counts"]["satellite_overview"]
    assert plain["new"] + plain["cached"] == plain["planned"] and plain["upper_bound"] is True
    assert plain["extraction_passes"] == 1 and plain["review_rounds"] == get_settings().max_review_iterations
    assert maxq["extraction_passes"] == 3 and maxq["review_rounds"] == 4
    assert maxq["counts"]["satellite_crop"] == plain["counts"]["satellite_crop"] + 4, "the overlapping tile set"
    assert maxq["est"]["claude_usd"] > plain["est"]["claude_usd"] and maxq["est"]["google_usd"] > plain["est"]["google_usd"]
    want = improve.estimate_cost(maxq["new_streetview"], maxq["new_satellite"], None, passes=3, review_rounds=4)
    assert maxq["est"]["claude_usd"] == want.claude_usd
    # a different review count changes only the Claude part
    more = post(app_client, auth, seg, "imagery/estimate",
                {"imagery": {**dense, "sat_extra_zoom": 20}, "quality": {"preset": True, "review_rounds": 8}}).json()["data"]
    assert more["est"]["claude_usd"] > maxq["est"]["claude_usd"] and more["est"]["google_usd"] == maxq["est"]["google_usd"]
    # fetch-only prices no Claude
    assert post(app_client, auth, seg, "imagery/estimate", {"imagery": dense, "fuse": False}).json()["data"]["est"]["claude_usd"] == 0
    assert no_network == []


def test_imagery_estimate_rejects_bad_plans_and_unknown_models(app_client, auth):
    seg = new_segment("Imagery estimate 422")
    assert post(app_client, auth, seg, "imagery/estimate", {"imagery": {"sv_spacing_m": 1}}).status_code == 422
    assert post(app_client, auth, seg, "imagery/estimate", {"model": "gpt-9"}).status_code == 422
    assert app_client.post("/api/admin/segments/nope/imagery/estimate", headers=auth, json={}).status_code == 404
    assert app_client.post(f"/api/admin/segments/{seg.slug}/imagery/estimate", json={}).status_code == 401


def test_default_estimate_matches_the_documented_max_quality_numbers(app_client, auth):
    """A typical 100 m street at max quality: the figure quoted in spec/ui.md comes from this very function."""
    from hero.fusion.maxquality import PRESET
    from hero.imagery.plan import default_plan, merge_plan, plan_images
    from tests.fixtures.street_fixtures import straight_reference_line

    plan = merge_plan(default_plan(get_settings()), {k: PRESET[k] for k in (
        "sv_spacing_m", "headings", "pitches_deg", "fovs_deg", "sat_crop_zoom", "sat_extra_zoom", "sat_crop_spacing_m",
        "max_images")})
    _items, info = plan_images(straight_reference_line(100.0), plan)
    # 21 stations x 48 frames are planned; Street View has ~11 panoramas on 100 m, so acquire fetches ~11 x 48 = 528
    assert info.counts == {"streetview": 1008, "satellite_crop": 19, "satellite_overview": 1} and not info.trimmed
    n_sat = info.counts["satellite_crop"] + info.counts["satellite_overview"]
    opus = improve.estimate_cost(528, n_sat, "claude-opus-5-5", passes=3, review_rounds=4)
    sonnet = improve.estimate_cost(528, n_sat, "claude-sonnet-5-5", passes=3, review_rounds=4)
    assert opus.google_usd == sonnet.google_usd and opus.claude_usd == pytest.approx(2 * sonnet.claude_usd, rel=1e-3)
