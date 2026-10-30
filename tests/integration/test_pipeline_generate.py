"""The real generation (shared ``generated_segment`` fixture: one run per session) checked against
every success criterion of spec/capabilities/street-generation.md, plus the budget hard stop and
the satellite-only fallback (both without any Anthropic request)."""

from __future__ import annotations

import os

import pytest

from hero.config import get_settings, reset_settings
from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.llm.pricing import load_price_table
from hero.pipeline.registry import create_queued_run
from hero.pipeline.runner import run_pipeline
from hero.scene.schema import Action, FootpathSide, LatLon, Quality, RunStatus, Scene, SegmentStatus

REQUIRED_STEPS = {"resolve_geometry", "fetch_satellite", "verify_streetview", "detect_layout", "assemble_scene",
                  "review_iteration_1", "finalize"}


@pytest.fixture(scope="module")
def run(generated_segment):
    seg = repo.require_segment(generated_segment)
    return repo.require_run(seg.latest_run_id)


def test_seed_generation_succeeds_with_a_valid_playable_scene(generated_segment, run):
    seg = repo.require_segment(generated_segment)
    assert run.status is RunStatus.succeeded and run.error is None
    assert seg.status is SegmentStatus.ready and seg.quality in (Quality.full, Quality.satellite_only)
    scene = Scene.model_validate(seg.scene.model_dump(mode="json"))
    assert scene.generation.run_id == run.id and scene.generation.quality is seg.quality
    coverage = scene.cross_section.footpath_coverage
    assert coverage[0].from_m == 0.0 and coverage[-1].to_m == pytest.approx(scene.segment.length_m, abs=1e-3)
    assert len(scene.buildings) >= 1
    assert len(scene.hazards()) >= 1
    w, h = scene.ground.size_px
    for obj in scene.objects:
        assert 0 <= obj.tile_px[0] <= w and 0 <= obj.tile_px[1] <= h
        assert abs(obj.latlon.lat - scene.segment.start.lat) < 0.01
    assert all(o.action in (Action.jump, Action.slide, Action.dodge) for o in scene.hazards())
    assert seg.thumbnail_path == f"previews/{run.id}/thumb.png"
    assert (get_settings().data_dir_abs / seg.thumbnail_path).exists()
    assert (get_settings().data_dir_abs / "previews" / run.id / "annotated.png").exists()


def test_run_record_has_every_required_step_with_positive_latency(run):
    by_name = {s.name: s for s in run.steps}
    assert REQUIRED_STEPS <= set(by_name)
    for name in REQUIRED_STEPS:
        assert by_name[name].status == "ok", f"{name}: {by_name[name].detail}"
        assert by_name[name].latency_ms > 0
    assert run.started_at is not None and run.finished_at is not None and run.finished_at > run.started_at


def test_llm_calls_are_two_to_four_priced_rows_summing_to_the_run_cost(run):
    calls = [c for c in repo.list_llm_calls(run.id) if c.ok]
    assert 2 <= len(calls) <= 4
    prices = load_price_table(get_settings())
    for call in calls:
        assert call.cost_usd > 0
        assert call.cost_usd == prices.cost_usd(call.model, call.input_tokens, call.output_tokens)
    assert run.cost_usd == pytest.approx(sum(c.cost_usd for c in repo.list_llm_calls(run.id)), abs=1e-6)
    assert run.input_tokens == sum(c.input_tokens for c in repo.list_llm_calls(run.id))
    assert run.cost_usd < get_settings().run_budget_usd


def test_google_requests_are_at_most_six_and_street_view_degrades_honestly(run):
    attempts = run.google_requests + run.google_cached_hits
    assert 1 <= attempts <= 6
    imagery = run.imagery
    assert imagery["satellite"]["url"].startswith("/media/imagery/")
    if imagery["streetview_available"]:
        assert 1 <= len(imagery["streetview"]) <= get_settings().streetview_samples
        assert all(0 <= p["x_m"] <= run.scene.segment.length_m for p in imagery["streetview"])
    else:
        assert run.scene.generation.quality is Quality.satellite_only
        assert any("Street View" in w for w in run.scene.generation.warnings)


def test_review_iterations_are_bounded_with_nonempty_critique_and_at_most_ten_ops(run):
    assert 1 <= len(run.iterations) <= get_settings().max_review_iterations
    for it in run.iterations:
        assert it["critique"].strip()
        assert len(it["patch_ops"]) <= 10
        assert (get_settings().data_dir_abs / "previews" / run.id / f"iter{it['n']}.png").exists()
    assert run.scene.generation.review_iterations == len(run.iterations)


def test_second_generation_imagery_steps_make_zero_new_google_requests(generated_segment, run):
    """Only the (free, cached) imagery steps are re-run here; the LLM passes are not re-spent."""
    from hero.pipeline import steps as S
    from hero.pipeline.runner import build_context, run_step, Step, settings_snapshot
    from hero.pipeline.state import PipelineState
    from hero.scene.geo import LocalFrame

    seg = repo.require_segment(generated_segment)
    second = create_queued_run(seg)
    frame = LocalFrame.from_endpoints(seg.start.lat, seg.start.lon, seg.end.lat, seg.end.lon, seg.footpath_side)
    state = PipelineState(run=second, segment=seg, settings_snapshot=settings_snapshot(get_settings(), seg), frame=frame)
    ctx = build_context(second.id)
    for step in (Step("resolve_geometry", S.resolve_geometry, False), Step("fetch_satellite", S.fetch_satellite, False),
                 Step("verify_streetview", S.verify_streetview, True),
                 Step("fetch_streetview", S.fetch_streetview, True, when=lambda s: s.streetview_available)):
        assert run_step(state, ctx, step)

    counts = repo.google_counts(second.id)
    assert counts.requests == 0
    assert counts.cache_hits >= run.google_requests + run.google_cached_hits
    repo.update_run(second.id, repo.RunUpdate(status=RunStatus.failed, error={"code": "test", "message": "imagery-only"}))


def test_budget_below_first_call_estimate_ends_failed_budget_with_no_llm_call(generated_segment, monkeypatch):
    seg = repo.require_segment(generated_segment)
    before_calls = sum(len(repo.list_llm_calls(r.id)) for r in repo.list_runs(limit=100))
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0.000001")
    reset_settings()
    try:
        run = run_pipeline(create_queued_run(seg).id)
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()

    assert run.status is RunStatus.failed_budget
    assert run.error["code"] == "budget_exhausted" and run.error["step"] == "detect_layout"
    assert "HERO_CLAUDE_BUDGET_USD" in run.error["message"]
    assert repo.list_llm_calls(run.id) == []
    assert sum(len(repo.list_llm_calls(r.id)) for r in repo.list_runs(limit=100)) == before_calls
    assert {s.name for s in run.steps} >= {"resolve_geometry", "fetch_satellite", "verify_streetview", "detect_layout"}
    assert repo.require_segment(generated_segment).status is SegmentStatus.ready  # previous scene kept


def test_segment_without_street_view_coverage_falls_back_to_satellite_only_without_failing(test_db, monkeypatch):
    """Ocean endpoints: Street View metadata is ZERO_RESULTS. The budget is then zeroed so the run
    stops before pass A - proving the fallback path and that satellite-only is a flag, not a failure."""
    seg = repo.create_segment(SegmentCreate(name="South Pacific (no street view)", city="Ocean",
                                            start=LatLon(lat=-30.0, lon=-140.0), end=LatLon(lat=-29.9994, lon=-140.0),
                                            footpath_side=FootpathSide.left))
    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0.000001")
    reset_settings()
    try:
        run = run_pipeline(create_queued_run(seg).id)
    finally:
        monkeypatch.delenv("HERO_CLAUDE_BUDGET_USD", raising=False)
        reset_settings()

    by_name = {s.name: s for s in run.steps}
    assert by_name["verify_streetview"].status == "ok" and "satellite-only" in (by_name["verify_streetview"].detail or "")
    assert by_name["fetch_streetview"].status == "skipped"
    assert run.imagery["streetview_available"] is False and run.imagery["streetview"] == []
    assert run.status is RunStatus.failed_budget and run.error["step"] == "detect_layout"
    assert repo.list_llm_calls(run.id) == []
