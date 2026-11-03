"""hero.model.improve: cost estimate (priced with the run's model), intervals, fill-gaps planning, photo effect (L3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hero.imagery.plan import SRange, plan_images
from hero.llm import pricing
from hero.model import improve
from hero.model.fidelity import compute_fidelity
from hero.model.refline import ReferenceLine
from hero.model.types import CanonicalModel, ModelKind, Observation, ObservationKind, ViewKind
from tests.fixtures.street_fixtures import make_model, make_observation, straight_reference_line

BELL = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "bellandur"
PRICES = {"cheap": (3.0, 15.0), "dear": (30.0, 150.0)}


@pytest.fixture(autouse=True)
def fake_prices(monkeypatch):
    def price_for(model):
        if model not in PRICES:
            raise KeyError(model)
        return SimpleNamespace(input_usd_per_mtok=PRICES[model][0], output_usd_per_mtok=PRICES[model][1])

    monkeypatch.setattr(pricing, "price_for", price_for, raising=False)


# ---------------------------------------------------------------- cost


def test_cost_at_three_fifteen_equals_the_r1_figures():
    est = improve.estimate_cost(10, 4, "cheap")
    assert est.google_usd == pytest.approx(10 * 0.007 + 4 * 0.002)
    # 14 images x 0.006 + 0.03 per run
    assert est.claude_usd == pytest.approx(14 * 0.006 + 0.03, abs=1e-4)
    assert est.total_usd == pytest.approx(est.google_usd + est.claude_usd, abs=1e-4) and est.model == "cheap"


def test_claude_cost_scales_with_the_model_price_and_google_does_not():
    a, b = improve.estimate_cost(10, 4, "cheap"), improve.estimate_cost(10, 4, "dear")
    assert b.google_usd == a.google_usd
    assert b.claude_usd == pytest.approx(10 * a.claude_usd, rel=1e-3)


def test_nothing_new_costs_nothing_and_acquire_only_has_no_claude():
    assert improve.estimate_cost(0, 0, "cheap").total_usd == 0.0
    assert improve.estimate_cost(5, 0, "cheap", fuse=False).claude_usd == 0.0


def test_unknown_model_or_negative_counts_raise_never_silently_zero():
    with pytest.raises(KeyError):
        improve.estimate_cost(1, 0, "nope")
    with pytest.raises(ValueError):
        improve.estimate_cost(-1, 0, "cheap")


# ---------------------------------------------------------------- helpers


def l1_street(length=100.0):
    """Forward-only photos every 10 m: every bin is L1 (single viewpoint, no side views)."""
    ref = straight_reference_line(length)
    obs = {}
    for i, s in enumerate(range(0, int(length) + 1, 10)):
        o = make_observation(obs_id=f"f{i}", station_s=float(s), view=ViewKind.forward)
        obs[o.id] = o
    model = make_model(ref, version=1, kind=ModelKind.fused, fused_observation_ids=sorted(obs))
    return ref, model, obs, compute_fidelity(model, obs)


# ---------------------------------------------------------------- intervals


def test_intervals_fold_equal_bins_and_explain_the_weakness():
    ref, model, obs, fid = l1_street()
    ivs = improve.intervals_of(fid)
    assert all(iv["level"] == 1 for iv in ivs)
    assert ivs[0]["s_from"] == 0.0 and ivs[-1]["s_to"] == 100.0
    assert all(a["s_to"] == b["s_from"] for a, b in zip(ivs, ivs[1:]))
    assert "no left-facing photo" in ivs[0]["weakest_reason"] and "L2" in ivs[0]["hint"]
    assert ivs[0]["actions"] == ["fill_gaps", "upload_photo"]


def test_weakest_ranks_level_then_gaps_then_length_and_skips_finished_stretches():
    ivs = [
        {"s_from": 0, "s_to": 10, "level": 2, "gaps": [], "actions": []},
        {"s_from": 10, "s_to": 20, "level": 1, "gaps": ["ground"], "actions": []},
        {"s_from": 20, "s_to": 50, "level": 1, "gaps": ["ground"], "actions": []},
        {"s_from": 50, "s_to": 60, "level": 0, "gaps": [], "actions": []},
        {"s_from": 60, "s_to": 70, "level": 3, "gaps": [], "actions": []},
        {"s_from": 70, "s_to": 80, "level": 1, "gaps": ["ground", "footpath"], "actions": []},
    ]
    got = [(iv["s_from"]) for iv in improve.weakest_of(ivs)]
    assert got == [50, 70, 20, 10, 0]


# ---------------------------------------------------------------- plan


def plan_for(ref, fid, obs, **kw):
    args = dict(fidelity=fid, gaps=None, observations=list(obs.values()), ref=ref, target_level=2, max_images=200,
                max_usd=5.0, s_range=None, model="cheap")
    args.update(kw)
    return improve.plan_improvements(**args)


def test_plan_adds_left_right_views_and_hires_crops_for_l1_bins():
    ref, model, obs, fid = l1_street()
    plan = plan_for(ref, fid, obs)
    views = {i.view for i in plan.images if i.kind == "streetview"}
    assert {"left", "right"} <= views
    crops = [i for i in plan.images if i.kind == "satellite_crop"]
    assert crops and all(c.zoom == 20 for c in crops)
    assert plan.intervals_targeted and plan.intervals_targeted[0]["from_level"] == 1
    assert plan.intervals_targeted[0]["to_level"] == 2
    assert plan.counts["planned"] == len(plan.images) == plan.counts["new"] + plan.counts["cached"]
    assert plan.est.google_usd == pytest.approx(
        sum(0.007 if i.kind == "streetview" else 0.002 for i in plan.images if not i.cached))
    assert not plan.trimmed and not plan.will_exceed_budget


def test_applying_the_plan_lifts_every_bin_to_l2():
    """Simulate the planned frames as stored photos at the planned stations: the weakest bins reach L2."""
    ref, model, obs, fid = l1_street()
    plan = plan_for(ref, fid, obs, target_level=2)
    new = dict(obs)
    for n, img in enumerate(i for i in plan.images if i.kind == "streetview"):
        o = make_observation(obs_id=f"n{n}", station_s=img.station_s, view=ViewKind(img.view), heading_deg=img.heading_deg)
        new[o.id] = o
    after = compute_fidelity(model.model_copy(update={"fused_observation_ids": sorted(new)}), new)
    assert min(b.level for b in after.bins) >= 2


def test_plan_overrides_are_exactly_targets_and_replan_through_plan_images():
    ref, model, obs, fid = l1_street()
    plan = plan_for(ref, fid, obs)
    ov = plan.overrides()
    assert set(ov) == {"targets"}
    items, info = plan_images(ref, plan.plan)
    assert info.counts["streetview"] == sum(1 for i in plan.images if i.kind == "streetview")


def test_max_images_trims_the_tail_and_says_so():
    ref, model, obs, fid = l1_street()
    full = plan_for(ref, fid, obs)
    small = plan_for(ref, fid, obs, max_images=4)
    assert small.trimmed and 0 < len(small.images) <= 4 < len(full.images)
    assert "trimmed" in small.note


def test_max_usd_trims_by_total_estimate():
    ref, model, obs, fid = l1_street()
    tiny = plan_for(ref, fid, obs, max_usd=0.1)
    assert tiny.trimmed and tiny.est.total_usd <= 0.1 + 1e-9


def test_s_range_limits_the_plan():
    ref, model, obs, fid = l1_street()
    plan = plan_for(ref, fid, obs, s_range=SRange(s_from=40, s_to=60))
    assert plan.images and all(25 <= i.station_s <= 75 for i in plan.images)
    with pytest.raises(ValueError, match="outside"):
        plan_for(ref, fid, obs, s_range=SRange(s_from=500, s_to=600))


def test_stored_matching_observation_makes_the_target_cached_and_free():
    ref, model, obs, fid = l1_street()
    first = plan_for(ref, fid, obs)
    img = next(i for i in first.images if i.kind == "streetview")
    stored = make_observation(obs_id="unfused", station_s=img.station_s, view=ViewKind(img.view))
    again = plan_for(ref, fid, {**obs, stored.id: stored})
    match = next(i for i in again.images if i.kind == "streetview" and i.view == img.view and i.station_s == img.station_s)
    assert match.cached and again.counts["cached"] >= 1
    assert again.est.google_usd < first.est.google_usd


def test_will_exceed_budget_only_when_claude_estimate_exceeds_remaining():
    ref, model, obs, fid = l1_street()
    p = plan_for(ref, fid, obs, remaining_usd=0.01)
    assert p.will_exceed_budget and p.est.claude_usd > 0.01
    assert not plan_for(ref, fid, obs, remaining_usd=100.0).will_exceed_budget
    assert not plan_for(ref, fid, obs, remaining_usd=None).will_exceed_budget


def test_estimate_uses_the_requested_model_price():
    ref, model, obs, fid = l1_street()
    a, b = plan_for(ref, fid, obs, model="cheap"), plan_for(ref, fid, obs, model="dear")
    assert b.est.claude_usd == pytest.approx(10 * a.est.claude_usd, rel=1e-3) and b.est.model == "dear"
    assert [i.as_dict() for i in a.images] == [i.as_dict() for i in b.images]


def test_nothing_to_fill_when_every_bin_is_at_target():
    ref = straight_reference_line(100.0)
    rec = json.loads((BELL / "fidelity_v1.json").read_text())
    from hero.model.types import FidelityMap
    fid = FidelityMap.model_validate(rec)  # recorded: all L2, hi-res ground everywhere
    plan = improve.plan_improvements(fidelity=fid, gaps=None, observations=[], ref=ReferenceLine.from_latlons(ref.latlons),
                                     target_level=2, max_images=40, max_usd=1.0, s_range=None, model="cheap")
    assert plan.images == [] and "Nothing to fill" in plan.note
    l3 = improve.plan_improvements(fidelity=fid, gaps=None, observations=[], ref=ReferenceLine.from_latlons(ref.latlons),
                                   target_level=3, max_images=40, max_usd=1.0, s_range=None, model="cheap")
    assert l3.images == [] and "close-up" in l3.note


def test_bad_target_level_raises():
    ref, model, obs, fid = l1_street()
    with pytest.raises(ValueError):
        plan_for(ref, fid, obs, target_level=4)


# ---------------------------------------------------------------- uploaded photo lifts to L3 (recorded Bellandur)


def bellandur():
    model = CanonicalModel.model_validate(json.loads((BELL / "model_v1.json").read_text()))
    obs = {o["id"]: Observation.model_validate(o) for o in json.loads((BELL / "observations.json").read_text())}
    return model, obs


def user_photo(ref, s, facing_view=ViewKind.left, oid="up-1"):
    t = 5.5 if facing_view is ViewKind.left else -5.5
    lat, lon = ref.point_at(s, t)
    heading = (ref.bearing_deg(s) + (270 if facing_view is ViewKind.left else 90)) % 360
    return make_observation(obs_id=oid, kind=ObservationKind.user_photo, station_s=s, offset_t=t, view=facing_view,
                            lat=lat, lon=lon, heading_deg=heading)


def test_recorded_street_is_l2_and_an_uploaded_photo_lifts_its_stretch_to_l3():
    model, obs = bellandur()
    ref = ReferenceLine.from_model(model.reference_line)
    before = compute_fidelity(model, obs)
    assert before.overall.histogram_m["L2"] == pytest.approx(100.0) and before.overall.histogram_m["L3"] == 0.0

    photo = user_photo(ref, 50.0)
    effect = improve.photo_effect(model, obs, photo, ref)
    assert effect["can_reach_level"] == 3 and "L3" in effect["message"]
    assert effect["s_from"] == pytest.approx(44.0, abs=0.5) and effect["s_to"] == pytest.approx(56.0, abs=0.5)

    fused = model.model_copy(update={"fused_observation_ids": sorted({*model.fused_observation_ids, photo.id})})
    after = compute_fidelity(fused, {**obs, photo.id: photo})
    lifted = [b for b in after.bins if b.level == 3]
    assert lifted and all(44.0 - 1e-6 <= b.s_from and b.s_to <= 56.0 + 1e-6 for b in lifted)
    assert sum(b.s_to - b.s_from for b in lifted) >= 5.0
    assert [b.level for b in after.bins if b.s_to <= 40.0] == [2] * 8        # elsewhere unchanged


def test_photo_on_an_l1_stretch_does_not_claim_l3():
    ref, model, obs, fid = l1_street()
    effect = improve.photo_effect(model, obs, user_photo(ref, 50.0), ref)
    assert effect["can_reach_level"] == 2
    assert effect["s_from"] is not None


def test_photo_with_no_cells_reports_so():
    model, obs = bellandur()
    ref = ReferenceLine.from_model(model.reference_line)
    sat = make_observation(obs_id="sat-x", kind=ObservationKind.satellite_overview, station_s=None, view=None)
    effect = improve.photo_effect(model, obs, sat, ref)
    assert effect["s_from"] is None and "no stretch" in effect["message"]
