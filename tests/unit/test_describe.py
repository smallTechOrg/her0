"""hero.model.describe: deterministic plain-language templates."""

from __future__ import annotations


from hero.model import describe
from hero.model.types import (
    Contradiction,
    ContradictionKind,
    EntityState,
    EntityType,
    Evidence,
    EvidenceRole,
    FootpathState,
    ModelDiff,
    ModelKind,
    ObservationKind,
)
from tests.fixtures.street_fixtures import make_entity, make_fidelity_map, make_model, make_observation, straight_reference_line


def model(**kw):
    return make_model(straight_reference_line(100.0), **kw)


def test_strip_text_mentions_material_kind_width_and_span():
    m = model(states=[FootpathState.intact])
    fp = [s for s in m.profile[0].strips if s.kind.value == "footpath"][0]
    text = describe.strip_text(fp, 22.0, 31.0)
    assert text == "Paver footpath about 1.5 m wide, fair from 22 to 31 m"


def test_interval_text_covers_each_footpath_state():
    m = model(states=[FootpathState.broken, FootpathState.missing, FootpathState.unknown, FootpathState.intact])
    texts = [describe.interval_text(iv, "left") for iv in m.profile]
    assert texts[0].startswith("0-25 m: broken footpath on the left side; carriageway about 8.5 m wide")
    assert "no footpath on the left side" in texts[1]
    assert "not yet traced" in texts[2]
    assert "intact footpath" in texts[3] and texts[3].endswith(".")


def test_entity_label_and_fallback_text():
    e = make_entity(type=EntityType.parked_vehicle, s=12.5).model_copy(
        update={"subtype": "auto-rickshaw", "attributes": {"colour": "yellow", "blocks_footpath": True}})
    assert describe.entity_label(e) == "auto-rickshaw (parked vehicle)"
    assert describe.entity_text(e) == "Yellow auto-rickshaw about 12 m from the start (blocks footpath)."
    bare = make_entity(type=EntityType.low_branch)
    assert describe.entity_label(bare) == "low branch"


def test_evidence_counts_by_source_and_summary_wording():
    e = make_entity(observation_id="a")
    extra = [Evidence(observation_id=o, role=EvidenceRole.supports, weight=0.5, added_in_version=1, detection_key=f"{o}:1")
             for o in ("b", "c", "a")]  # 'a' repeated: counted once
    e = e.model_copy(update={"provenance": e.provenance + extra})
    obs = {"a": make_observation(obs_id="a"),
           "b": make_observation(obs_id="b", kind=ObservationKind.user_photo),
           "c": make_observation(obs_id="c", kind=ObservationKind.satellite_crop, zoom=20, station_s=None, view=None)}
    assert describe.evidence_counts(e, obs) == {"street_view": 1, "satellite": 1, "upload": 1}
    s = describe.evidence_summary(e, obs)
    assert s == "Seen in 1 Street View frame, 1 satellite image, 1 uploaded photo."
    assert describe.evidence_counts(e, None)["street_view"] == 3  # unknown observations count as Street View


def test_evidence_summary_for_absence_dispute_and_legacy():
    e = make_entity(observation_id="a")
    e = e.model_copy(update={"state": EntityState.disputed, "provenance": e.provenance + [
        Evidence(observation_id="z", role=EvidenceRole.contradicts, weight=0.4, added_in_version=2)]})
    assert describe.evidence_summary(e, None) == "Seen in 1 Street View frame; not visible in 1 later view; disputed."
    bare = e.model_copy(update={"provenance": [], "state": EntityState.present})
    assert describe.evidence_summary(bare, None) == "No supporting photos yet."
    assert "legacy, placement unverified" in describe.evidence_summary(bare, None, legacy=True)


def test_low_confidence_parts_merge_adjacent_bins_and_name_the_reason():
    fid = make_fidelity_map(100.0, 2)
    assert describe.low_confidence_parts(model(), fid) == []
    fid = make_fidelity_map(100.0, 1)
    (part,) = describe.low_confidence_parts(model(), fid)
    assert (part.s_from, part.s_to) == (0.0, 100.0) and "one viewpoint" in part.reason
    fid0 = make_fidelity_map(100.0, 0)
    assert "map only" in describe.low_confidence_parts(model(), fid0)[0].reason


def test_low_confidence_parts_flag_weak_entities_and_open_contradictions():
    fid = make_fidelity_map(100.0, 2)
    weak = make_entity(entity_id="ent-0000000001", s=62.0, confidence=0.45)
    strong = make_entity(entity_id="ent-0000000002", s=10.0, confidence=0.9)
    ctr = Contradiction(id="ctr-1", kind=ContradictionKind.presence_conflict, entity_ids=["ent-0000000002"],
                        description="x", opened_in_version=1)
    done = ctr.model_copy(update={"id": "ctr-2", "entity_ids": ["ent-0000000001"], "resolved_in_version": 2})
    m = model(entities=[weak, strong]).model_copy(update={"contradictions": [ctr, done]})
    parts = describe.low_confidence_parts(m, fid)
    assert [(p.s_from, p.s_to) for p in parts] == [(10.0, 15.0), (60.0, 65.0)]
    assert "unresolved conflict" in parts[0].reason and "below 60 % confidence" in parts[1].reason
    removed = weak.model_copy(update={"state": EntityState.removed})
    assert describe.low_confidence_parts(model(entities=[removed]), fid) == []


def test_improve_hint_points_at_the_lowest_run():
    from hero.model.types import FidelityMap

    fm = make_fidelity_map(100.0, 2)
    bins = [b.model_copy(update={"level": 1}) if 12 <= i <= 14 else b for i, b in enumerate(fm.bins)]
    fm = FidelityMap(bin_m=5.0, length_s=100.0, bins=bins, overall=fm.overall)
    assert describe.improve_hint(fm) == "A photo facing left or right from another spot around 60-75 m would raise it to L2."
    assert "L3" in describe.improve_hint(make_fidelity_map(100.0, 2))
    assert "highest fidelity" in describe.improve_hint(make_fidelity_map(100.0, 3))
    assert "first street-level view" in describe.improve_hint(make_fidelity_map(100.0, 0))


def test_about_summary_has_the_required_phrases():
    m = model(version=2, kind=ModelKind.fused, fused_observation_ids=[f"o{i}" for i in range(48)])
    text = describe.about_summary(m, make_fidelity_map(100.0, 2), name="100 Feet Road, Indiranagar")
    for needle in ("fidelity L2", "Multi-view", "48 photos", "model v2", "100 m of 100 Feet Road, Indiranagar"):
        assert needle in text
    assert describe.about_summary(m, make_fidelity_map(100.0, 2)) == text.replace("100 Feet Road, Indiranagar", "100 Feet Road")


def test_about_summary_singular_legacy_map_only_and_removed_notes():
    one = model(fused_observation_ids=["o1"])
    assert "from 1 photo " in describe.about_summary(one, make_fidelity_map(100.0, 1))
    zero = describe.about_summary(model(), make_fidelity_map(100.0, 0))
    assert "map-only sketch" in zero and "L0" in zero
    legacy = describe.about_summary(model(kind=ModelKind.legacy, source="legacy_straight"), make_fidelity_map(100.0, 0))
    assert "legacy, placement unverified" in legacy
    gone = make_entity(state=EntityState.removed)
    assert "Previously seen but no longer supported: 1 object." in describe.about_summary(
        model(entities=[gone]), make_fidelity_map(100.0, 2))


def test_version_summary_with_and_without_a_diff():
    m = model(version=2, kind=ModelKind.fused, entities=[make_entity()], fused_observation_ids=["a", "b"])
    fid = make_fidelity_map(100.0, 2)
    plain = describe.version_summary(m, None, fid)
    assert plain == "Model v2 (fused from photos): 1 object, 0 buildings, fidelity L2 (score 2.00) from 2 photos."
    empty = ModelDiff(from_version=1, to_version=2, is_empty=True)
    assert describe.version_summary(m, empty, fid).endswith("No change since v1.")
    full = ModelDiff(from_version=1, to_version=2, is_empty=False, summary_lines=["3 new objects", "Fidelity L1 to L2"])
    assert describe.version_summary(m, full, fid).endswith("Since v1: 3 new objects; Fidelity L1 to L2.")
    assert "map-only prior" in describe.version_summary(model(), None, make_fidelity_map(100.0, 0))
