"""hero.model.report.build_run_report: deterministic run report text + accounting."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from hero.model.batch import ProjectedBatch, ProjectedDetection
from hero.model.diff import diff_models
from hero.model.merge import merge_batches
from hero.model.report import build_run_report
from hero.model.types import (
    EntityType,
    ModelKind,
    ObservationKind,
    ReportCost,
    ReportSkipped,
    RunReport,
)
from tests.fixtures.street_fixtures import (
    FIXED_NOW,
    make_fidelity_map,
    make_model,
    make_observation,
    straight_reference_line,
)


def det(obs, key, type, s, raw=0.9):
    return ProjectedDetection(key, obs, type, None, s, 5.5, 0.5, 0.3, 0.3, None, None, {}, f"a {type.value}", raw,
                              None, 1.0)


def merged(base, batch_id, obs_ids, dets):
    return merge_batches(base, [ProjectedBatch(batch_id, tuple(obs_ids), tuple(dets))], new_version=base.version + 1,
                         run_id="run-1", now=FIXED_NOW).model


def new_obs(n, *, start=0, source_kind=ObservationKind.streetview):
    out = []
    for i in range(start, start + n):
        o = make_observation(obs_id=f"n{i}", station_s=float(i * 10), view=None, kind=source_kind).model_copy(
            update={"cost_usd": 0.007})
        out.append(o)
    return out


@pytest.fixture()
def v0():
    return make_model(straight_reference_line(100.0))


@pytest.fixture()
def v1(v0):
    return merged(v0, "b1", ["n0", "n1"], [det("n0", "k1", EntityType.pole, 20), det("n1", "k2", EntityType.tree, 60)])


RUN = SimpleNamespace(id="run-1", started_at=FIXED_NOW, finished_at=FIXED_NOW + timedelta(seconds=171.44))


def report(v0, v1, **kw):
    diff = diff_models(v0, v1, make_fidelity_map(100.0, 0), make_fidelity_map(100.0, 1))
    args = dict(run=RUN, mode="generate", base=v0, result=v1, diff=diff, fid_before=make_fidelity_map(100.0, 0),
                fid_after=make_fidelity_map(100.0, 1), new_observations=new_obs(2), cached_count=3,
                costs={"anthropic_usd": 0.27, "anthropic_calls": 4}, warnings=[], now=FIXED_NOW)
    args.update(kw)
    return build_run_report(**args)


def test_first_generation_report(v0, v1):
    r = report(v0, v1)
    assert isinstance(r, RunReport) and r.run_id == "run-1" and r.segment_slug == v0.segment_slug
    assert (r.base_version, r.result_version, r.version_created) == (0, 1, True)
    assert r.duration_s == 171.4
    assert r.images.new == 2 and r.images.cached == 3 and r.images.used_total == 5 and r.images.planned == 5
    assert r.images.fused_this_run == 2 and r.images.new_observation_ids == ["n0", "n1"]
    assert r.images.by_source == {"google_streetview": 2}
    assert [d.type for d in r.detections.new] == ["pole", "tree"] or {d.type for d in r.detections.new} == {"pole", "tree"}
    assert r.fidelity.before.overall == 0.0 and r.fidelity.after.overall == 1.0
    assert r.fidelity.improved_intervals and r.fidelity.improved_intervals[0].to == "L1"
    assert "Run added 2 new images (3 of 5 images were already cached)" in r.summary
    assert "created model v1 from v0" in r.summary and "2 new objects" in r.summary and "fidelity L0 -> L1" in r.summary
    assert r.suggestions


def test_images_account_for_skipped_and_cached_observations(v0, v1):
    r = report(v0, v1, skipped=[ReportSkipped(station_s=90.0, view="left", reason="streetview_zero_results")],
               cached_observations=new_obs(3, start=10))
    assert r.images.planned == r.images.new + r.images.cached + 1 == 6
    assert r.images.used_total == r.images.planned - len(r.images.skipped)
    assert r.images.by_source["google_streetview"] == 5
    assert r.images.skipped[0].reason == "streetview_zero_results"


def test_cost_defaults_come_from_the_observations_and_explicit_values_win(v0, v1):
    r = report(v0, v1, costs={"anthropic_usd": 0.27}, new_observations=new_obs(2) + new_obs(1, start=5, source_kind=ObservationKind.user_photo).copy())
    assert r.cost.anthropic_usd == 0.27 and r.cost.google_cached == 3
    assert r.cost.google_new_requests == 2  # the uploaded photo is not a Google request
    assert r.cost.google_est_usd == pytest.approx(0.021)
    assert r.cost.total_usd == pytest.approx(0.291)
    explicit = report(v0, v1, costs=ReportCost(anthropic_usd=0.5, total_usd=0.6, run_cap_usd=1.0, google_new_requests=9,
                                               google_cached=1, google_est_usd=0.1))
    assert explicit.cost.total_usd == 0.6 and explicit.cost.google_new_requests == 9 and explicit.cost.run_cap_usd == 1.0


def test_changed_entities_and_confidence_changes(v0, v1):
    v2 = merged(v1, "b2", ["n2"], [det("n2", "k3", EntityType.pole, 20.1, raw=0.9), det("n2", "k4", EntityType.bin, 80)])
    diff = diff_models(v1, v2, make_fidelity_map(100.0, 1), make_fidelity_map(100.0, 2))
    r = build_run_report(run=RUN, mode="generate", base=v1, result=v2, diff=diff, fid_before=make_fidelity_map(100.0, 1),
                         fid_after=make_fidelity_map(100.0, 2), new_observations=new_obs(1, start=2), cached_count=2,
                         costs=None, warnings=[], now=FIXED_NOW)
    assert [d.type for d in r.detections.new] == ["bin"]
    assert len(r.detections.changed) == 1 and "confidence" in r.detections.changed[0].what
    (cc,) = r.confidence_changes
    assert cc.type == "pole" and cc.after > cc.before
    assert r.detections.unchanged_count == 1
    assert "L1 -> L2" in r.summary and r.cost.total_usd == pytest.approx(0.007)


def test_no_op_run_is_free_and_says_so(v1):
    r = build_run_report(run=RUN, mode="generate", base=v1, result=v1, diff=None, fid_before=make_fidelity_map(100.0, 1),
                         fid_after=make_fidelity_map(100.0, 1), new_observations=[], cached_count=48,
                         costs={"anthropic_usd": 0.0}, warnings=[], now=FIXED_NOW)
    assert not r.version_created and r.base_version == r.result_version == 1
    assert r.cost.anthropic_usd == 0.0 and r.cost.google_new_requests == 0 and r.cost.total_usd == 0.0
    assert r.images.new == 0 and r.images.cached == 48 and r.images.fused_this_run == 0
    assert "no new images" in r.summary and "nothing new was spent" in r.summary and "stays at v1" in r.summary


def test_new_images_but_unchanged_model_is_not_a_no_op(v1):
    r = build_run_report(run="run-9", mode="acquire", base=v1, result=v1, diff=None, fid_before=None, fid_after=None,
                         new_observations=new_obs(2), cached_count=0, costs=None, warnings=[], now=FIXED_NOW)
    assert r.mode == "acquire" and not r.version_created and r.duration_s == 0.0
    assert "did not change" in r.summary and "no new images" not in r.summary


def test_failed_run_without_a_result_model(v0):
    r = build_run_report(run=RUN, mode="generate", base=v0, result=None, diff=None, fid_before=None, fid_after=None,
                         new_observations=[], cached_count=0, costs=None, warnings=["perception failed"],
                         now=FIXED_NOW, degraded=True)
    assert r.result_version == 0 and not r.version_created and r.degraded
    assert "did not produce a model" in r.summary and "1 warning raised" in r.summary
    assert any("warnings" in s for s in r.suggestions)


def test_needs_a_model():
    with pytest.raises(ValueError):
        build_run_report(run="r", mode="generate", base=None, result=None, diff=None, fid_before=None, fid_after=None,
                         new_observations=[], cached_count=0, costs=None, warnings=[], now=FIXED_NOW)


def test_first_run_on_a_legacy_style_base_without_started_at(v0, v1):
    r = report(v0, v1, run="plain-id")
    assert r.run_id == "plain-id" and r.started_at is None and r.duration_s == 0.0


def test_report_is_deterministic_and_round_trips(v0, v1):
    a, b = report(v0, v1), report(v0, v1)
    assert a.model_dump_json() == b.model_dump_json()
    assert RunReport.model_validate_json(a.model_dump_json()) == a
    assert v1.kind is ModelKind.fused
