"""hero.model.merge: matching, evidence arithmetic, states, contradictions, profile, buildings, critique."""

from __future__ import annotations

import math

import pytest

from hero.model.batch import FacadeDetection, ProjectedBatch, ProjectedDetection, StateSpan, StripSample, VisibleRegion
from hero.model.calibrate import calibrate_confidence, logit
from hero.model.diff import model_hash
from hero.model.merge import apply_critique, merge_batches
from hero.model.types import (
    ContradictionKind,
    EntityState,
    EntityType,
    EvidenceRole,
    FlagEntityOp,
    FootpathState,
    Material,
    Condition,
    NudgeEdgeOp,
    NudgeEntityOp,
    SetFootpathStateOp,
    StripKind,
    CanonicalModel,
)
from tests.fixtures.street_fixtures import FIXED_NOW, make_entity, make_model, straight_reference_line


def base_model(**kw):
    return make_model(straight_reference_line(100.0), **kw)


def det(key="d1", obs="obs-a", type=EntityType.pole, s=20.0, t=5.5, raw=0.8, weight=1.0, sigma=0.5, **kw):
    d = dict(key=key, observation_id=obs, type=type, subtype=None, s=s, t=t, sigma_m=sigma, extent_s=0.3, extent_t=0.3,
             height_m=None, orientation_deg=None, attributes={}, description="a thing", raw_confidence=raw,
             crop_px=None, weight=weight)
    d.update(kw)
    return ProjectedDetection(**d)


def batch(bid="b1", obs=("obs-a",), dets=(), strips=(), states=(), facades=(), visible=()):
    return ProjectedBatch(batch_id=bid, observation_ids=tuple(obs), detections=tuple(dets), strips=tuple(strips),
                          states=tuple(states), facades=tuple(facades), visible=tuple(visible))


def merge(m, bs, v=None):
    return merge_batches(m, bs, new_version=v if v is not None else m.version + 1, run_id="run-1", now=FIXED_NOW)


def vis(obs, a=0.0, b=100.0, side="both", w=1.0):
    return VisibleRegion(observation_id=obs, s_from=a, s_to=b, side=side, weight=w)


# ------------------------------------------------------------------ entities: creation and matching


def test_new_detection_creates_entity_with_calibrated_confidence_and_stable_id():
    r = merge(base_model(), [batch(dets=[det()])])
    (e,) = r.model.entities
    p = calibrate_confidence(0.8)
    assert e.id.startswith("ent-") and len(e.id) == 14
    assert e.logit == pytest.approx(logit(p))
    assert e.confidence == pytest.approx(p, abs=1e-6)
    assert e.first_seen_version == e.last_changed_version == 1
    assert e.provenance[0].role is EvidenceRole.supports and e.provenance[0].detection_key == "d1"
    assert r.model.kind.value == "fused" and r.model.parent_version == 0 and r.model.run_id == "run-1"
    assert r.model.fused_observation_ids == ["obs-a"]
    assert r.model.stats.entity_count == 1 and r.model.stats.observation_count == 1
    assert [c.kind for c in r.changes] == ["entity_added"]


def test_same_inputs_give_identical_ids():
    a = merge(base_model(), [batch(dets=[det(), det(key="d2", s=40)])]).model
    b = merge(base_model(), [batch(dets=[det(key="d2", s=40), det()])]).model
    assert [e.id for e in a.entities] == [e.id for e in b.entities]
    assert model_hash(a) == model_hash(b)


def test_same_type_same_spot_same_obs_get_distinct_ids_via_ordinal():
    r = merge(base_model(), [batch(dets=[det(key="d1", s=20, t=5.5), det(key="d2", s=20.02, t=5.5)])])
    ids = [e.id for e in r.model.entities]
    assert len(ids) == len(set(ids)) == 2  # one-to-one per observation: two detections, two entities


def test_second_observation_matches_and_raises_confidence_and_shrinks_sigma():
    m1 = merge(base_model(), [batch(dets=[det(raw=0.7)])]).model
    e1 = m1.entities[0]
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", s=20.4, t=5.7, raw=0.7)])])
    (e2,) = r.model.entities
    assert e2.id == e1.id
    assert e2.confidence > e1.confidence
    assert e2.position_sigma_m < e1.position_sigma_m
    assert 20.0 < e2.position.s < 20.4
    assert e2.first_seen_version == 1 and e2.last_changed_version == 2
    assert {c.kind for c in r.changes} == {"entity_updated"}


def test_support_weight_decays_with_each_repeat():
    m = base_model()
    gains, prev = [], None
    for i in range(4):
        m = merge(m, [batch(f"b{i}", (f"obs-{i}",), [det(key="k", obs=f"obs-{i}")])]).model
        if prev is not None:
            gains.append(m.entities[0].logit - prev)
        prev = m.entities[0].logit
    assert all(g2 < g1 for g1, g2 in zip(gains, gains[1:]))
    assert gains[0] == pytest.approx(0.75 * logit(calibrate_confidence(0.8)))


def test_far_detection_is_a_new_entity():
    m1 = merge(base_model(), [batch(dets=[det()])]).model
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", s=60)])])
    assert len(r.model.entities) == 2


def test_one_to_one_per_observation():
    m1 = merge(base_model(), [batch(dets=[det(s=20.0)])]).model
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", s=20.1), det(key="y", obs="obs-b", s=20.2)])])
    assert len(r.model.entities) == 2  # the second detection cannot also match the same entity


def test_other_matches_specific_type_and_is_upgraded_not_conflicting():
    m1 = merge(base_model(), [batch(dets=[det(type=EntityType.other)])]).model
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", type=EntityType.bollard)])])
    (e,) = r.model.entities
    assert e.type is EntityType.bollard and not r.model.contradictions


def test_local_key_collision_across_batches_is_not_double_counted():
    d1 = det(key="d1", obs="obs-a")
    m1 = merge(base_model(), [batch("b1", ("obs-a",), [d1])]).model
    # same (obs, key) arrives again in a different batch that also carries a new observation
    r = merge(m1, [batch("b2", ("obs-a", "obs-b"), [d1, det(key="d1", obs="obs-b", s=20.2)])])
    (e,) = r.model.entities
    assert len([x for x in e.provenance if x.role is EvidenceRole.supports]) == 2
    assert {(x.observation_id, x.detection_key) for x in e.provenance} == {("obs-a", "d1"), ("obs-b", "d1")}


def test_attribute_and_description_and_billboard_rules():
    m1 = merge(base_model(), [batch(dets=[det(weight=0.8, attributes={"colour": "white"}, description="first",
                                               crop_px=(1, 2, 3, 4))])]).model
    # lighter evidence: neither description nor billboard nor attribute changes
    m2 = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", weight=0.5, attributes={"colour": "yellow"},
                                                   description="weaker", crop_px=(9, 9, 9, 9))])]).model
    e = m2.entities[0]
    assert e.description == "first" and e.attributes["colour"] == "white"
    assert e.billboard.crop_px == (1, 2, 3, 4)
    (c,) = [c for c in m2.contradictions if c.kind is ContradictionKind.attribute_conflict]
    assert c.is_open and c.entity_ids == [e.id] and "colour" in c.description  # both sides >= 0.5 and disagree


def test_light_attribute_disagreement_is_not_a_conflict_and_heavier_evidence_wins():
    m1 = merge(base_model(), [batch(dets=[det(weight=0.8, attributes={"colour": "white"}, description="first")])]).model
    m2 = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", weight=0.3, attributes={"colour": "red"})])]).model
    assert m2.entities[0].attributes["colour"] == "white" and not m2.contradictions
    m3 = merge(m2, [batch("b3", ("obs-c",), [det(key="y", obs="obs-c", weight=1.3, description="better view",
                                                   attributes={"colour": "grey"})])]).model
    e = m3.entities[0]
    assert e.description == "better view"  # 1.3 >= 1.25 * 0.8 (top support weight)
    assert e.attributes["colour"] == "grey"  # accumulated 1.3 > 1.1 on the other side


# ------------------------------------------------------------------ absence, states, contradictions


def seeded(conf_raw=0.9, extra=()):
    return merge(base_model(), [batch(dets=[det(raw=conf_raw), *extra])]).model


def test_absence_lowers_confidence_adds_contradicting_evidence_and_is_weaker_than_presence():
    m1 = seeded()
    e1 = m1.entities[0]
    r = merge(m1, [batch("b2", ("obs-b",), visible=[vis("obs-b")])])
    e2 = r.model.entities[0]
    assert e2.id == e1.id and e2.confidence < e1.confidence
    assert e2.logit == pytest.approx(e1.logit - 0.5)
    (c,) = e2.contradictions()
    assert c.observation_id == "obs-b" and c.weight == 1.0
    assert abs(e2.logit - e1.logit) < abs(0.75 * logit(calibrate_confidence(0.9)))  # weaker than a repeat sighting


def test_absence_only_inside_visible_region_with_enough_weight_and_correct_side():
    m1 = seeded()
    for kw in (dict(a=50, b=90), dict(w=0.4), dict(side="right")):
        r = merge(m1, [batch("b2", ("obs-b",), visible=[vis("obs-b", **kw)])])
        assert r.model.entities[0].confidence == m1.entities[0].confidence, kw


def test_absence_is_unique_per_observation_and_skips_entities_that_observation_supports():
    m1 = seeded()
    r = merge(m1, [batch("b2", ("obs-b",), visible=[vis("obs-b"), vis("obs-b", 0, 50)])])
    assert len(r.model.entities[0].contradictions()) == 1
    # an observation that produced the entity is never a contradiction of it
    r2 = merge(base_model(), [batch(dets=[det()], visible=[vis("obs-a")])])
    assert not r2.model.entities[0].contradictions()


def test_removed_after_two_contradicting_observations_then_revived_with_same_id():
    m = seeded(conf_raw=0.3)
    eid = m.entities[0].id
    for i in range(6):
        m = merge(m, [batch(f"n{i}", (f"obs-n{i}",), visible=[vis(f"obs-n{i}")])]).model
        if m.entities[0].state is EntityState.removed:
            break
    e = m.entities[0]
    assert e.state is EntityState.removed and e.confidence < 0.15 and len(e.contradictions()) >= 2
    assert m.stats.removed_count == 1 and m.stats.entity_count == 0
    for i in range(6):
        r = merge(m, [batch(f"r{i}", (f"obs-r{i}",), [det(key="k", obs=f"obs-r{i}", raw=1.0)])])
        m = r.model
        if m.entities[0].state is not EntityState.removed:
            break
    assert len(m.entities) == 1 and m.entities[0].id == eid
    assert m.entities[0].state is not EntityState.removed and m.entities[0].confidence >= 0.35


def test_disputed_state_opens_presence_conflict_and_keeps_it_listed():
    m = seeded(conf_raw=0.5)  # calibrated ~0.39
    r = merge(m, [batch("b2", ("obs-b",), visible=[vis("obs-b")])])
    e = r.model.entities[0]
    assert e.state is EntityState.disputed and 0.15 <= e.confidence < 0.5
    (c,) = [c for c in r.model.contradictions if c.kind is ContradictionKind.presence_conflict]
    assert c.is_open and c.entity_ids == [e.id]
    assert [x.kind for x in r.changes if x.kind.startswith("contradiction")] == ["contradiction_opened"]
    assert any(x.kind == "entity_state" for x in r.changes)
    # a later strong sighting resolves it, but the record stays in the model
    r2 = merge(r.model, [batch("b3", ("obs-c",), [det(key="z", obs="obs-c", raw=1.0)])])
    (c2,) = [c for c in r2.model.contradictions if c.kind is ContradictionKind.presence_conflict]
    assert not c2.is_open and c2.resolved_in_version == 3
    assert any(x.kind == "contradiction_resolved" for x in r2.changes)


def test_type_conflict_opens_when_two_confident_types_share_a_spot_and_both_are_kept():
    m1 = seeded(conf_raw=0.95)
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", type=EntityType.tree, raw=0.95, s=20.3)])])
    assert len(r.model.entities) == 2
    (c,) = [c for c in r.model.contradictions if c.kind is ContradictionKind.type_conflict]
    assert c.is_open and len(c.entity_ids) == 2 and "pole" in c.description and "tree" in c.description


def test_low_confidence_other_type_nearby_is_not_a_type_conflict():
    m1 = seeded(conf_raw=0.95)
    r = merge(m1, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b", type=EntityType.tree, raw=0.1, s=20.3)])])
    assert len(r.model.entities) == 2 and not r.model.contradictions


# ------------------------------------------------------------------ batches: ordering, ignoring, input immutability


def test_batch_order_does_not_change_the_result():
    b1 = batch("b1", ("o1",), [det(key="a", obs="o1", s=10)])
    b2 = batch("b2", ("o2",), [det(key="a", obs="o2", s=10.3), det(key="b", obs="o2", s=70, type=EntityType.tree)])
    assert model_hash(merge(base_model(), [b1, b2]).model) == model_hash(merge(base_model(), [b2, b1]).model)


def test_already_fused_batches_are_ignored_and_listed():
    b = batch("b1", ("o1",), [det(obs="o1")])
    m1 = merge(base_model(), [b]).model
    r = merge(m1, [b])
    assert r.ignored_batches == ["b1"] and r.changes == []
    assert model_hash(r.model) == model_hash(m1)
    assert r.model.version == 2 and r.model.parent_version == 1
    r2 = merge(base_model(), [b, b])  # duplicate within one call
    assert r2.ignored_batches == ["b1"] and len(r2.model.entities) == 1


def test_base_model_is_not_mutated():
    m = seeded()
    before = m.model_dump_json()
    merge(m, [batch("b2", ("obs-b",), [det(key="x", obs="obs-b")], visible=[vis("obs-b")])])
    assert m.model_dump_json() == before


def test_existing_entities_of_the_base_are_matched_and_kept_even_without_provenance_keys():
    m = base_model(entities=[make_entity(s=20.0, t=5.5, confidence=0.6)])
    r = merge(m, [batch(dets=[det(s=20.2)])])
    assert len(r.model.entities) == 1 and r.model.entities[0].id == "ent-0000000001"


# ------------------------------------------------------------------ profile


def footpath_samples(obs, s_values, t=(4.0, 5.2), w=1.0, material=Material.concrete):
    return [StripSample(s=s, kind=StripKind.footpath, side="left", t_lo=t[0], t_hi=t[1], material=material,
                        condition=Condition.good, weight=w, observation_id=obs) for s in s_values]


def fp_at(model, s):
    return model.interval_at(s).footpath_strip("left")


def test_traced_strip_evidence_replaces_prior_then_blends():
    m = base_model()
    assert fp_at(m, 30).weight == pytest.approx(0.15)
    m1 = merge(m, [batch("b1", ("o1",), strips=footpath_samples("o1", [30.0]))]).model
    fp = fp_at(m1, 30)
    assert fp.t_lo[0] == pytest.approx(4.0, abs=1e-6) and fp.t_hi[0] == pytest.approx(5.2, abs=1e-6)
    assert fp.weight == pytest.approx(1.0, abs=0.3) and fp.material is Material.concrete
    assert fp.confidence == pytest.approx(min(0.95, 1 - math.exp(-fp.weight)), abs=1e-3)
    assert fp_at(m1, 80).weight == pytest.approx(0.15)  # untouched stations keep their prior
    m2 = merge(m1, [batch("b2", ("o2",), strips=footpath_samples("o2", [30.0], t=(4.4, 5.6)))]).model
    fp2 = fp_at(m2, 30)
    assert 4.0 < fp2.t_lo[0] < 4.4 and fp2.weight > fp.weight and fp2.confidence > fp.confidence
    assert "o1" in fp2.provenance and "o2" in fp2.provenance


def test_profile_stays_a_valid_tiling_with_bounded_interval_lengths():
    m = merge(base_model(), [batch("b1", ("o1",), strips=footpath_samples("o1", [12.0, 30.0, 55.0, 90.0]))]).model
    CanonicalModel.model_validate(m.model_dump(mode="json"))  # contiguous, starts 0, ends length
    assert all(2.5 - 1e-6 <= iv.length <= 30.0 + 1e-6 for iv in m.profile)
    assert m.profile[0].s_from == 0 and m.profile[-1].s_to == pytest.approx(100.0)


def test_state_votes_set_footpath_state_and_missing_removes_the_strip():
    spans = [StateSpan("o1", "left", 20.0, 40.0, FootpathState.missing, 0.8)]
    r = merge(base_model(), [batch("b1", ("o1",), states=spans)])
    m = r.model
    assert m.interval_at(30).footpath_state is FootpathState.missing and fp_at(m, 30) is None
    assert m.interval_at(5).footpath_state is FootpathState.unknown and fp_at(m, 5) is not None
    assert m.interval_at(30).state_votes.get("missing", 0) > 0
    assert any(c.kind == "state_changed" for c in r.changes)
    # later intact evidence on the same stretch flips it back and restores a footpath strip
    spans2 = [StateSpan("o2", "left", 20.0, 40.0, FootpathState.intact, 2.0)]
    m2 = merge(m, [batch("b2", ("o2",), states=spans2)]).model
    assert m2.interval_at(30).footpath_state is FootpathState.intact and fp_at(m2, 30) is not None


def test_close_state_votes_keep_leader_and_open_a_footpath_state_conflict():
    spans = [StateSpan("o1", "left", 20.0, 40.0, FootpathState.broken, 1.0),
             StateSpan("o2", "left", 20.0, 40.0, FootpathState.intact, 0.9)]
    m = merge(base_model(), [batch("b1", ("o1", "o2"), states=spans)]).model
    assert m.interval_at(30).footpath_state is FootpathState.broken
    (c,) = [c for c in m.contradictions if c.kind is ContradictionKind.footpath_state_conflict]
    assert c.is_open and c.interval is not None and c.interval[0] <= 30 <= c.interval[1]


def test_opposite_side_state_spans_do_not_drive_the_modelled_side():
    spans = [StateSpan("o1", "right", 20.0, 40.0, FootpathState.missing, 1.0)]
    m = merge(base_model(), [batch("b1", ("o1",), states=spans)]).model
    assert m.interval_at(30).footpath_state is FootpathState.unknown


def test_profile_evidence_of_already_fused_observations_is_not_recounted():
    b = batch("b1", ("o1",), strips=footpath_samples("o1", [30.0]))
    m1 = merge(base_model(), [b]).model
    b_again = batch("b1x", ("o1", "o2"), strips=footpath_samples("o1", [30.0]))
    m2 = merge(m1, [b_again]).model
    assert fp_at(m2, 30).weight == pytest.approx(fp_at(m1, 30).weight, abs=1e-6)


# ------------------------------------------------------------------ buildings


def facade(obs="o1", side="left", a=10.0, b=30.0, hexc="#aabbcc", storeys=3, w=1.0, crop=(0, 0, 10, 10)):
    return FacadeDetection(observation_id=obs, side=side, s_from=a, s_to=b, colour_hex=hexc, storeys=storeys,
                           description="a shop house", crop_px=crop, weight=w)


def test_facade_creates_vision_building_then_later_facade_updates_it():
    r = merge(base_model(), [batch("b1", ("o1",), facades=[facade()])])
    (b,) = r.model.buildings
    assert b.id.startswith("bld-v-") and b.source == "vision" and b.side == "left" and b.storeys == 3
    assert b.height_m == pytest.approx(9.6) and b.colour_hex == "#aabbcc" and all(p[1] > 5 for p in b.footprint)
    assert [c.kind for c in r.changes] == ["building_added"]
    r2 = merge(r.model, [batch("b2", ("o2",), facades=[facade("o2", a=12, b=28, crop=(1, 1, 5, 5))])])
    (b2,) = r2.model.buildings
    assert b2.id == b.id and len(b2.facades) == 2 and b2.confidence > b.confidence
    assert [c.kind for c in r2.changes] == ["building_updated"]
    right = merge(r2.model, [batch("b3", ("o3",), facades=[facade("o3", side="right")])]).model
    assert len(right.buildings) == 2 and all(p[1] < 0 for p in right.buildings[1].footprint)


def test_invalid_colour_and_empty_span_are_ignored_safely():
    m = merge(base_model(), [batch("b1", ("o1",), facades=[facade(hexc="blue"), facade("o1", a=5, b=5, crop=(1, 1, 2, 2))])]).model
    assert len(m.buildings) == 1 and m.buildings[0].colour_hex is None


# ------------------------------------------------------------------ critique


def crit_model():
    return merge(base_model(), [batch(dets=[det(raw=1.0), det(key="d2", s=60, type=EntityType.tree, raw=1.0)])]).model


def test_critique_flag_lowers_confidence_by_at_most_015_and_never_deletes():
    m = crit_model()
    eid = m.entities[0].id
    ops = [FlagEntityOp(entity_id=eid, reason="not visible in satellite"), FlagEntityOp(entity_id=eid, reason="again")]
    out, changes, skipped = apply_critique(m, ops, run_id="r", now=FIXED_NOW)
    assert len(out.entities) == len(m.entities)
    assert 0 < m.entities[0].confidence - out.entities[0].confidence <= 0.15 + 1e-9
    assert any("cap" in s for s in skipped)  # the second flag hits the per-entity cap
    assert out.entities[0].provenance[-1].role is EvidenceRole.critique and out.entities[0].provenance[-1].weight == 0.3
    assert [c.kind for c in changes] == ["entity_updated"]
    assert out.run_id == "r"


def test_critique_bounds_unknown_ids_and_duplicates_are_skipped_and_listed():
    m = crit_model()
    eid = m.entities[0].id
    ops = [NudgeEntityOp(entity_id=eid, ds_m=3.5, dt_m=0, reason="far"),
           NudgeEntityOp(entity_id="ent-nope", ds_m=1, dt_m=0),
           FlagEntityOp(entity_id="ent-nope"),
           NudgeEdgeOp(kind=StripKind.footpath, side="left", s_from=0, s_to=10, dt_m=1.5),
           SetFootpathStateOp(side="right", s_from=0, s_to=10, state=FootpathState.missing),
           SetFootpathStateOp(side="left", s_from=0, s_to=10, state=FootpathState.unknown),
           NudgeEdgeOp(kind=StripKind.kerb, side="left", s_from=0, s_to=10, dt_m=0.5)]
    out, changes, skipped = apply_critique(m, ops, run_id=None, now=FIXED_NOW)
    assert len(skipped) == len(ops) and changes == []
    assert out.model_dump(exclude={"created_at"}) == m.model_dump(exclude={"created_at"})
    op = NudgeEntityOp(entity_id=eid, ds_m=1.0, dt_m=-0.5)
    once, _, _ = apply_critique(m, [op], run_id=None, now=FIXED_NOW)
    twice, ch, sk = apply_critique(once, [op], run_id=None, now=FIXED_NOW)
    assert once.entities[0].position.s == pytest.approx(m.entities[0].position.s + 1.0)
    assert twice.entities[0].position == once.entities[0].position and ch == [] and len(sk) == 1


def test_critique_footpath_state_and_edge_nudge():
    m = base_model()
    out, changes, skipped = apply_critique(
        m, [SetFootpathStateOp(side="left", s_from=20, s_to=40, state=FootpathState.broken, reason="cracked"),
            NudgeEdgeOp(kind=StripKind.footpath, side="left", s_from=60, s_to=80, dt_m=0.5)], run_id=None, now=FIXED_NOW)
    assert not skipped
    assert out.interval_at(30).footpath_state is FootpathState.broken
    assert fp_at(out, 70).t_hi[0] == pytest.approx(fp_at(m, 70).t_hi[0] + 0.5)
    assert {"state_changed", "strip_changed"} <= {c.kind for c in changes}
    CanonicalModel.model_validate(out.model_dump(mode="json"))
