"""hero.model.diff: diff_models and model_hash."""

from __future__ import annotations

from hero.model.batch import ProjectedBatch, ProjectedDetection, StateSpan, VisibleRegion
from hero.model.diff import diff_models, model_hash
from hero.model.merge import merge_batches
from hero.model.types import EntityState, EntityType, FootpathState
from tests.fixtures.street_fixtures import FIXED_NOW, make_entity, make_fidelity_map, make_model, straight_reference_line


def base(**kw):
    return make_model(straight_reference_line(100.0), **kw)


def merge(m, bs):
    return merge_batches(m, bs, new_version=m.version + 1, run_id="r", now=FIXED_NOW)


def det(obs="o1", key="k", type=EntityType.pole, s=20.0, raw=0.9):
    return ProjectedDetection(key, obs, type, None, s, 5.5, 0.5, .3, .3, None, None, {}, "a thing", raw, None, 1.0)


def fid(level, length=100.0):
    return make_fidelity_map(length, level)


def test_hash_ignores_lineage_fields_but_not_content():
    a = base()
    b = a.model_copy(update={"version": 7, "parent_version": 3, "run_id": "x"})
    assert model_hash(a) == model_hash(b) and len(model_hash(a)) == 64
    c = base(entities=[make_entity()])
    assert model_hash(a) != model_hash(c)


def test_first_version_everything_is_added():
    b = merge(base(), [ProjectedBatch("b", ("o1",), (det(), det(key="k2", s=60, type=EntityType.tree)))]).model
    d = diff_models(None, b, None, fid(1))
    assert d.from_version is None and d.to_version == 1 and not d.is_empty
    assert len(d.entities.added) == 2 and d.observations.fused_added == 1
    assert d.fidelity.improved and d.fidelity.improved[0].from_ == "L0" and d.fidelity.improved[0].to == "L1"
    assert any("2 new objects" in x for x in d.summary_lines) and any("Fidelity L0" in x for x in d.summary_lines)


def test_identical_models_give_an_empty_diff():
    m = merge(base(), [ProjectedBatch("b", ("o1",), (det(),))]).model
    again = merge(m, [ProjectedBatch("b", ("o1",), (det(),))]).model
    d = diff_models(m, again, fid(1), fid(1))
    assert d.is_empty and d.entities.unchanged == 1 and d.summary_lines == [] and d.fidelity.unchanged_len_m == 100.0


def test_changed_added_removed_revived_and_thresholds():
    m1 = merge(base(), [ProjectedBatch("b", ("o1",), (det(raw=0.6),))]).model
    m2 = merge(m1, [ProjectedBatch("b2", ("o2",), (det("o2", s=20.2, raw=0.9), det("o2", "n", EntityType.tree, 70)))]).model
    d = diff_models(m1, m2, fid(1), fid(2))
    assert [x.type for x in d.entities.added] == ["tree"]
    (ch,) = d.entities.changed
    assert "confidence" in ch.fields and ch.fields["confidence"][0] < ch.fields["confidence"][1]
    assert "1 more supporting view" in ch.reason
    assert d.observations.fused_added == 1
    # sub-threshold movement is not a change
    tiny = m1.model_copy(deep=True)
    tiny.entities[0].confidence += 0.02
    tiny.entities[0].position.s += 0.1
    assert diff_models(m1, tiny, fid(1), fid(1)).entities.changed == []
    # removal then revival
    gone = m1.model_copy(deep=True)
    gone.entities[0].state = EntityState.removed
    assert [x.id for x in diff_models(m1, gone, fid(1), fid(1)).entities.removed] == [m1.entities[0].id]
    assert [x.id for x in diff_models(gone, m1, fid(1), fid(1)).entities.revived] == [m1.entities[0].id]


def test_profile_changes_and_merged_ranges():
    m1 = base()
    m2 = merge(m1, [ProjectedBatch("b", ("o1",), states=(StateSpan("o1", "left", 20, 40, FootpathState.missing, 0.9),))]).model
    d = diff_models(m1, m2, fid(0), fid(0))
    assert d.profile.changed_intervals
    row = d.profile.changed_intervals[0]
    assert "footpath_state unknown → missing" in row.what and 15 <= row.s_from <= 25 and 35 <= row.s_to <= 45


def test_contradictions_opened_and_resolved_are_listed():
    m1 = merge(base(), [ProjectedBatch("b", ("o1",), (det(raw=0.5),))]).model
    m2 = merge(m1, [ProjectedBatch("b2", ("o2",), visible=(VisibleRegion("o2", 0, 100, "both", 1.0),))]).model
    d = diff_models(m1, m2, fid(1), fid(1))
    assert [c["kind"] for c in d.contradictions.opened] == ["presence_conflict"] and not d.is_empty
    m3 = merge(m2, [ProjectedBatch("b3", ("o3",), (det("o3", raw=1.0),))]).model
    d2 = diff_models(m2, m3, fid(1), fid(1))
    assert [c["kind"] for c in d2.contradictions.resolved] == ["presence_conflict"]


def test_fidelity_improvement_ranges_and_overall():
    a = fid(1)
    b = make_fidelity_map(100.0, 2)
    d = diff_models(base(), base(), a, b)
    assert len(d.fidelity.improved) == 1
    imp = d.fidelity.improved[0]
    assert (imp.s_from, imp.s_to, imp.from_, imp.to) == (0.0, 100.0, "L1", "L2")
    assert d.fidelity.overall == (1.0, 2.0) and d.fidelity.unchanged_len_m == 0.0 and not d.is_empty
    same = diff_models(base(), base(), b, b)
    assert same.fidelity.improved == [] and same.fidelity.unchanged_len_m == 100.0
    # a drop is not "improved" (monotone fidelity is compute_fidelity's job) but is a visible difference
    assert diff_models(base(), base(), b, a).fidelity.improved == []
