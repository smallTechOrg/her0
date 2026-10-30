"""Max-quality building blocks: agreement merge, provenance/cache key, preset values, estimate scaling, overlapping tiles."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hero.config import get_settings
from hero.fusion import maxquality as MQ
from hero.imagery.plan import ImageryPlan, default_plan, merge_plan, plan_images
from hero.model import improve
from hero.model.batch import ProjectedBatch, ProjectedDetection, StripSample
from hero.model.types import Condition, EntityType, Material, StripKind
from tests.fixtures.street_fixtures import straight_reference_line


def det(key, s, t=2.0, conf=0.8, typ=EntityType.bollard, **kw):
    return ProjectedDetection(key=key, observation_id="o1", type=typ, subtype=None, s=s, t=t, sigma_m=0.5, extent_s=0.3,
                              extent_t=0.3, height_m=1.0, orientation_deg=None, attributes={}, description="x",
                              raw_confidence=conf, crop_px=None, weight=1.0, **kw)


def batch(*dets, strips=()):
    return ProjectedBatch(batch_id="sv-1", observation_ids=("o1",), detections=tuple(dets), strips=tuple(strips))


def strip(w=1.0):
    return StripSample(s=5.0, kind=StripKind.footpath, side="left", t_lo=2, t_hi=4, material=Material.concrete,
                       condition=Condition.good, weight=w, observation_id="o1")


# ------------------------------------------------------------------ agreement merge


def test_single_pass_is_returned_unchanged():
    b = batch(det("a", 10.0))
    assert MQ.agree([b]) is b


def test_unanimous_detections_keep_their_confidence():
    passes = [batch(det(f"a{i}", 10.0 + 0.2 * i, conf=0.8)) for i in range(3)]
    merged = MQ.agree(passes)
    assert len(merged.detections) == 1
    d = merged.detections[0]
    assert d.raw_confidence == pytest.approx(0.8) and "pass_agreement" in d.flags and "pass_disagreement" not in d.flags


def test_disagreement_lowers_confidence_and_never_drops_a_detection():
    # pass 0 sees A and B, pass 1 sees only A, pass 2 sees A and a different object C
    passes = [batch(det("a0", 10.0), det("b0", 30.0)), batch(det("a1", 10.1)), batch(det("a2", 9.9), det("c2", 55.0))]
    merged = MQ.agree(passes)
    by = {round(d.s): d for d in merged.detections}
    assert len(merged.detections) == 3, "A, B and C all survive (the union, nothing dropped)"
    assert set(by) == {10, 30, 55}
    assert by[10].raw_confidence == pytest.approx(0.8)                       # 3 of 3 passes
    assert by[30].raw_confidence == pytest.approx(0.8 * (0.5 + 0.5 / 3), abs=1e-4)  # 1 of 3 passes
    assert by[55].raw_confidence < by[10].raw_confidence and "pass_disagreement" in by[55].flags
    assert all(d.raw_confidence > 0 for d in merged.detections)


def test_different_types_or_far_apart_detections_are_not_merged():
    merged = MQ.agree([batch(det("a", 10.0)), batch(det("b", 10.0, typ=EntityType.tree)), batch(det("c", 20.0))])
    assert len(merged.detections) == 3


def test_agreement_keeps_unique_keys_and_scales_profile_evidence_so_it_still_sums_to_one_pass():
    passes = [batch(det("d1", 10.0), strips=[strip(1.0)]), batch(det("d1", 40.0), strips=[strip(1.0)]),
              batch(det("d1", 70.0), strips=[strip(1.0)])]
    merged = MQ.agree(passes)
    assert len({d.key for d in merged.detections}) == 3, "local ids repeat across passes; keys must not"
    assert len(merged.strips) == 3 and sum(s.weight for s in merged.strips) == pytest.approx(1.0)
    stats = MQ.agreement_stats(passes, merged)
    assert stats["detections_in"] == 3 and stats["detections_out"] == 3 and stats["disputed"] == 3 and stats["unanimous"] == 0


# ------------------------------------------------------------------ provenance + cache key


def test_cache_key_covers_image_shas_model_prompt_version_and_pass_index():
    base = dict(kind="streetlevel", shas=["a", "b"], model="m", prompt_ver="v1", pass_index=0)
    k = MQ.cache_key(**base)
    assert k == MQ.cache_key(**{**base, "shas": ["b", "a"]}), "order of the images does not matter"
    for change in ({"shas": ["a", "c"]}, {"model": "m2"}, {"prompt_ver": "v2"}, {"pass_index": 1}, {"kind": "overhead"}):
        assert MQ.cache_key(**{**base, **change}) != k, change


def test_provenance_records_model_prompt_version_pass_cost_and_shas():
    p = MQ.provenance(kind="streetlevel", shas=["s2", "s1"], model="claude-opus-5-5", prompt_name="perceive_streetlevel",
                      pass_index=2, cost_usd=0.123456789)
    assert p["model"] == "claude-opus-5-5" and p["pass_index"] == 2 and p["cost_usd"] == 0.123457
    assert p["image_shas"] == ["s1", "s2"] and p["prompt_version"] == MQ.prompt_version("perceive_streetlevel") != "unknown"
    assert MQ.pass_index_of({MQ.PROVENANCE_KEY: p}) == 2 and MQ.pass_index_of({"windows": []}) == 0


# ------------------------------------------------------------------ preset + options


def test_preset_values_match_the_brief():
    p = MQ.preset_payload()["preset"]
    assert p["model"] == "claude-opus-5-5" and p["sv_spacing_m"] <= 6 and len(p["headings"]) == 8
    assert p["frames_per_station"] == 8 * len(p["pitches_deg"]) * len(p["fovs_deg"]) == 48 and min(p["pitches_deg"]) < 0 < max(p["pitches_deg"])
    assert p["sat_crop_zoom"] == 21 and p["sat_extra_zoom"] in (19, 20) and p["extraction_passes"] == 3 and p["review_rounds"] == 4
    # the plan fields are valid for the imagery plan (the form posts them as-is)
    plan = merge_plan(default_plan(get_settings()), {k: p[k] for k in ("sv_spacing_m", "headings", "sat_crop_zoom", "sat_extra_zoom",
                                                                     "sat_crop_spacing_m", "max_images", "pitches_deg", "fovs_deg")})
    assert plan.sat_extra_zoom == 20 and plan.max_images == 2000 and plan.frames_per_station == 48


def test_quality_resolve_fills_preset_defaults_and_validates():
    q = MQ.Quality.resolve({"preset": True})
    assert (q.extraction_passes, q.review_rounds, q.review_stop_changes) == (3, 4, 1)
    q = MQ.Quality.resolve({"preset": True, "extraction_passes": 2, "review_rounds": 0})
    assert (q.extraction_passes, q.review_rounds) == (2, 0)
    assert MQ.Quality.resolve(None) == MQ.Quality() and MQ.Quality().rounds(1) == 1
    for bad in ({"extraction_passes": 0}, {"extraction_passes": 6}, {"review_rounds": 9}, {"nope": 1}):
        with pytest.raises(ValidationError):
            MQ.Quality.resolve(bad)
    assert MQ.Quality.from_inputs("garbage") == MQ.Quality()


# ------------------------------------------------------------------ estimate scales with passes and rounds


def test_estimate_scales_with_extraction_passes_and_review_rounds():
    base = improve.estimate_cost(36, 10, "claude-sonnet-5-5")
    p3 = improve.estimate_cost(36, 10, "claude-sonnet-5-5", passes=3)
    r4 = improve.estimate_cost(36, 10, "claude-sonnet-5-5", review_rounds=4)
    both = improve.estimate_cost(36, 10, "claude-sonnet-5-5", passes=3, review_rounds=4)
    assert base.claude_usd < p3.claude_usd < both.claude_usd and base.claude_usd < r4.claude_usd < both.claude_usd
    assert all(e.google_usd == base.google_usd for e in (p3, r4, both)), "Google cost does not depend on Claude passes"
    assert improve.estimate_cost(36, 10, "claude-sonnet-5-5", passes=1, review_rounds=0) == base
    # exactly linear in review rounds
    r1 = improve.estimate_cost(36, 10, "claude-sonnet-5-5", review_rounds=1).claude_usd
    r2 = improve.estimate_cost(36, 10, "claude-sonnet-5-5", review_rounds=2).claude_usd
    assert (r2 - r1) == pytest.approx(r1 - base.claude_usd, abs=1e-4)
    # satellite-only: extra passes re-send street frames only
    assert improve.estimate_cost(0, 10, "claude-sonnet-5-5", passes=3).claude_usd == improve.estimate_cost(0, 10, "claude-sonnet-5-5").claude_usd
    # nothing new -> no Claude spend, even at max quality; fetch-only never prices Claude
    assert improve.estimate_cost(0, 0, "claude-sonnet-5-5", passes=3, review_rounds=4).claude_usd == 0.0
    assert improve.estimate_cost(36, 10, "claude-sonnet-5-5", passes=3, review_rounds=4, fuse=False).claude_usd == 0.0
    with pytest.raises(ValueError):
        improve.estimate_cost(1, 1, passes=0)


def test_opus_costs_twice_sonnet_per_repo_prices():
    s = improve.estimate_cost(36, 10, "claude-sonnet-5-5", passes=3, review_rounds=4).claude_usd
    o = improve.estimate_cost(36, 10, "claude-opus-5-5", passes=3, review_rounds=4).claude_usd
    assert o == pytest.approx(2 * s, rel=1e-3)  # $4/$20 vs $2/$10 per Mtok


# ------------------------------------------------------------------ dense sampling + overlapping tile set


def test_overlapping_satellite_set_adds_crops_at_the_extra_zoom_and_respects_max_images():
    ref = straight_reference_line(100.0)
    plain = ImageryPlan(sv_spacing_m=12, headings=["forward", "back", "left", "right"], sat_crop_zoom=21,
                        sat_crop_spacing_m=20, max_images=120)
    dense = plain.model_copy(update={"sat_extra_zoom": 20})
    items, info = plan_images(ref, plain)
    items2, info2 = plan_images(ref, dense)
    assert info.counts["streetview"] == info2.counts["streetview"] == 9 * 4, "a frame every 12 m, four headings per station"
    extra = [i for i in items2 if i.kind == "satellite_crop" and i.zoom == 20]
    assert len(extra) == 4 and all(i.zoom == 21 for i in items2 if i.kind == "satellite_crop" and i not in extra)
    assert info2.counts["satellite_crop"] == info.counts["satellite_crop"] + 4
    assert info2.est_cost_usd > info.est_cost_usd
    cap = dense.model_copy(update={"max_images": 45})
    items3, info3 = plan_images(ref, cap)
    assert info3.paid_total <= 45
