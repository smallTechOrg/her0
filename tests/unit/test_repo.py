"""hero.db.repo against the real SQLite test DB (migrated by the ``test_db`` fixture)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hero.db import repo
from hero.db.repo import (
    GameRunCreate,
    GoogleRequestCreate,
    InvalidInputError,
    LlmCallCreate,
    NotFoundError,
    RunUpdate,
    SegmentCreate,
    SegmentUpdate,
    StepRecord,
)
from hero.scene.schema import FootpathSide, LatLon, Quality, RunStatus, SegmentStatus, load_example_scene

START = LatLon(lat=12.97400, lon=77.64050)
END = LatLon(lat=12.97467, lon=77.64058)


def _segment_create(name: str | None = None, side: FootpathSide = FootpathSide.left) -> SegmentCreate:
    name = name or f"Test Street {uuid.uuid4().hex[:6]} (west footpath)"
    return SegmentCreate(name=name, city="Bengaluru", start=START, end=END, footpath_side=side)


@pytest.fixture()
def segment(test_db):
    return repo.create_segment(_segment_create())


# ---------------------------------------------------------------- segments


def test_create_segment_derives_length_heading_slug_and_draft_status(test_db):
    created = repo.create_segment(
        SegmentCreate(name="100 Feet Road, Indiranagar (west footpath)", city="Bengaluru",
                      start=START, end=END, footpath_side=FootpathSide.left)
    )

    assert created.slug == "100-feet-road-indiranagar-w"
    assert created.length_m == pytest.approx(75.0, abs=0.1)
    assert created.heading_deg == pytest.approx(6.7, abs=0.2)
    assert created.status is SegmentStatus.draft
    assert created.scene is None and created.quality is None
    assert created.best_score is None and created.best_distance_m is None
    assert created.created_at.tzinfo is not None
    assert uuid.UUID(created.id).version == 4


def test_create_segment_slug_collision_gets_numeric_suffix(test_db):
    name = f"Collision Lane {uuid.uuid4().hex[:6]}"
    first = repo.create_segment(_segment_create(name))
    second = repo.create_segment(_segment_create(name))
    third = repo.create_segment(_segment_create(name))
    assert second.slug == first.slug + "-2"
    assert third.slug == first.slug + "-3"


def test_create_segment_honours_explicit_slug(test_db):
    slug = f"custom-{uuid.uuid4().hex[:8]}"
    data = _segment_create().model_copy(update={"slug": slug})
    assert repo.create_segment(data).slug == slug


def test_create_segment_rejects_length_outside_30_to_200_m(test_db):
    too_short = SegmentCreate(name="Short", city="X", start=START,
                              end=LatLon(lat=12.97410, lon=77.64050), footpath_side=FootpathSide.left)
    with pytest.raises(InvalidInputError, match="30"):
        repo.create_segment(too_short)
    too_long = SegmentCreate(name="Long", city="X", start=START,
                             end=LatLon(lat=12.97800, lon=77.64050), footpath_side=FootpathSide.left)
    with pytest.raises(InvalidInputError, match="200"):
        repo.create_segment(too_long)


def test_get_segment_by_slug_and_id_and_missing(segment):
    assert repo.get_segment(segment.slug) == segment
    assert repo.get_segment_by_id(segment.id) == segment
    assert repo.get_segment("no-such-street") is None
    with pytest.raises(NotFoundError):
        repo.require_segment("no-such-street")


def test_list_segments_newest_first_and_ready_filter(test_db):
    a = repo.create_segment(_segment_create())
    b = repo.create_segment(_segment_create())
    run = repo.create_run(b.id, inputs={"x": 1})
    repo.finalize_segment_scene(b.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                thumbnail_path=f"previews/{run.id}/thumb.png")

    all_slugs = [s.slug for s in repo.list_segments()]
    assert all_slugs.index(b.slug) < all_slugs.index(a.slug)
    ready = repo.list_ready_segments()
    assert b.slug in [s.slug for s in ready]
    assert a.slug not in [s.slug for s in ready]
    assert all(s.status is SegmentStatus.ready and s.scene is not None for s in ready)


def test_finalize_segment_scene_marks_ready_and_stores_scene(segment):
    run = repo.create_run(segment.id, inputs={})
    scene = load_example_scene()

    updated = repo.finalize_segment_scene(segment.id, run_id=run.id, scene=scene, quality=Quality.satellite_only,
                                          thumbnail_path="previews/x/thumb.png")

    assert updated.status is SegmentStatus.ready
    assert updated.quality is Quality.satellite_only
    assert updated.latest_run_id == run.id
    assert updated.thumbnail_path == "previews/x/thumb.png"
    assert updated.scene is not None and updated.scene.walkability.score == 50
    assert repo.get_segment(segment.slug).scene == scene


def test_update_segment_status_keeps_previous_scene_on_failed_regeneration(segment):
    run = repo.create_run(segment.id, inputs={})
    repo.finalize_segment_scene(segment.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                thumbnail_path=None)

    repo.update_segment(segment.id, SegmentUpdate(status=SegmentStatus.generating))
    assert repo.get_segment(segment.slug).status is SegmentStatus.generating
    failed = repo.update_segment(segment.id, SegmentUpdate(status=SegmentStatus.ready, latest_run_id=run.id))

    assert failed.scene is not None
    assert failed.status is SegmentStatus.ready


def test_update_segment_unknown_id_raises(test_db):
    with pytest.raises(NotFoundError):
        repo.update_segment(str(uuid.uuid4()), SegmentUpdate(status=SegmentStatus.failed))


# ---------------------------------------------------------------- pipeline runs


def test_create_run_is_queued_with_inputs_and_empty_lists(segment):
    run = repo.create_run(segment.id, inputs={"zoom": 19, "models": {"vision": "m"}})

    assert run.status is RunStatus.queued
    assert run.segment_id == segment.id
    assert run.inputs == {"zoom": 19, "models": {"vision": "m"}}
    assert run.steps == [] and run.iterations == [] and run.imagery == {}
    assert run.cost_usd == 0.0 and run.input_tokens == 0 and run.google_requests == 0
    assert run.started_at is None and run.finished_at is None and run.error is None
    assert repo.get_run(run.id) == run
    assert repo.get_run("nope") is None
    with pytest.raises(NotFoundError):
        repo.require_run("nope")


def test_update_run_and_append_steps_upsert_by_name(segment):
    run = repo.create_run(segment.id, inputs={})
    t0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    repo.update_run(run.id, RunUpdate(status=RunStatus.running, started_at=t0))
    repo.append_run_step(run.id, StepRecord(name="resolve_geometry", status="started", started_at=t0))
    repo.append_run_step(run.id, StepRecord(name="resolve_geometry", status="ok", started_at=t0,
                                            latency_ms=12, detail="74.6 m"))
    repo.append_run_step(run.id, StepRecord(name="fetch_satellite", status="started", started_at=t0))
    final = repo.update_run(run.id, RunUpdate(
        status=RunStatus.failed, finished_at=t0, error={"code": "upstream_error", "message": "403", "step": "fetch_satellite"},
        imagery={"satellite": {"path": "imagery/a.png"}, "streetview": [], "streetview_available": False},
        layout={"road_width_m": 9.0}, iterations=[{"n": 1, "accepted": True}],
    ))

    assert final.status is RunStatus.failed
    assert final.started_at == t0 and final.finished_at == t0
    assert [s.name for s in final.steps] == ["resolve_geometry", "fetch_satellite"]
    assert final.steps[0].status == "ok" and final.steps[0].latency_ms == 12 and final.steps[0].detail == "74.6 m"
    assert final.error == {"code": "upstream_error", "message": "403", "step": "fetch_satellite"}
    assert final.imagery["satellite"]["path"] == "imagery/a.png"
    assert final.layout == {"road_width_m": 9.0}
    assert final.iterations[0]["accepted"] is True


def test_update_run_scene_stores_typed_scene(segment):
    run = repo.create_run(segment.id, inputs={})
    updated = repo.update_run(run.id, RunUpdate(scene=load_example_scene(), status=RunStatus.succeeded))
    assert updated.scene is not None and updated.scene.segment.slug == "100-feet-road-indiranagar-w"


def test_active_run_detects_queued_or_running_only(segment):
    assert repo.get_active_run() is None or repo.get_active_run().status in (RunStatus.queued, RunStatus.running)
    for stale in repo.list_runs(limit=1000):
        if stale.status in (RunStatus.queued, RunStatus.running):
            repo.update_run(stale.id, RunUpdate(status=RunStatus.failed))
    assert repo.get_active_run() is None

    run = repo.create_run(segment.id, inputs={})
    assert repo.get_active_run().id == run.id
    repo.update_run(run.id, RunUpdate(status=RunStatus.running))
    assert repo.get_active_run().id == run.id
    repo.update_run(run.id, RunUpdate(status=RunStatus.succeeded))
    assert repo.get_active_run() is None


def test_list_runs_newest_first_limit_and_per_segment(segment):
    runs = [repo.create_run(segment.id, inputs={"n": n}) for n in range(3)]
    for r in runs:  # never leave queued rows behind: the shared session DB must have no orphan active run
        repo.update_run(r.id, RunUpdate(status=RunStatus.failed))

    recent = repo.list_runs(limit=2)
    assert len(recent) == 2 and recent[0].id == runs[2].id
    mine = repo.list_runs_for_segment(segment.id)
    assert [r.id for r in mine] == [runs[2].id, runs[1].id, runs[0].id]
    assert repo.latest_run_for_segment(segment.id).id == runs[2].id
    assert repo.latest_run_for_segment(str(uuid.uuid4())) is None


def test_run_stats_counts_by_status(segment):
    before = repo.run_stats()
    a = repo.create_run(segment.id, inputs={})
    b = repo.create_run(segment.id, inputs={})
    c = repo.create_run(segment.id, inputs={})
    repo.update_run(a.id, RunUpdate(status=RunStatus.succeeded))
    repo.update_run(b.id, RunUpdate(status=RunStatus.failed))
    repo.update_run(c.id, RunUpdate(status=RunStatus.failed_budget))

    after = repo.run_stats()

    assert after.total == before.total + 3
    assert after.succeeded == before.succeeded + 1
    assert after.failed == before.failed + 1
    assert after.failed_budget == before.failed_budget + 1


# ---------------------------------------------------------------- llm calls


def test_record_llm_call_and_run_totals_and_global_spend(segment):
    run = repo.create_run(segment.id, inputs={})
    spent_before = repo.total_llm_cost_usd()

    first = repo.record_llm_call(LlmCallCreate(
        run_id=run.id, step="detect_layout", model="claude-sonnet-4-5", input_tokens=1000, output_tokens=200,
        cost_usd=0.006, latency_ms=1500, ok=True, request_summary="detect_layout; 2 images; 400000 bytes",
        response_summary="coverage=3 buildings=1",
    ))
    repo.record_llm_call(LlmCallCreate(
        run_id=run.id, step="review_iteration_1", model="claude-sonnet-4-5", input_tokens=500, output_tokens=100,
        cost_usd=0.003, latency_ms=900, ok=False, error="schema invalid", request_summary="r", response_summary="",
    ))

    totals = repo.run_llm_totals(run.id)

    assert first.cost_usd == pytest.approx(0.006) and first.ok is True and first.created_at.tzinfo is not None
    assert totals.input_tokens == 1500 and totals.output_tokens == 300
    assert totals.cost_usd == pytest.approx(0.009)
    assert [c.step for c in totals.calls] == ["detect_layout", "review_iteration_1"]
    assert [c.step for c in repo.list_llm_calls(run.id)] == ["detect_layout", "review_iteration_1"]
    assert repo.total_llm_cost_usd() == pytest.approx(spent_before + 0.009)
    assert repo.run_llm_totals("no-run").cost_usd == 0.0 and repo.run_llm_totals("no-run").calls == []


def test_llm_call_summaries_are_truncated_to_500_chars(segment):
    run = repo.create_run(segment.id, inputs={})
    call = repo.record_llm_call(LlmCallCreate(
        run_id=run.id, step="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1, ok=True,
        request_summary="x" * 900, response_summary="y" * 900,
    ))
    assert len(call.request_summary) == 500 and len(call.response_summary) == 500


def test_spend_summary_reflects_budget(segment):
    run = repo.create_run(segment.id, inputs={})
    repo.record_llm_call(LlmCallCreate(run_id=run.id, step="s", model="m", input_tokens=1, output_tokens=1,
                                       cost_usd=0.5, latency_ms=1, ok=True, request_summary="", response_summary=""))
    spent = repo.total_llm_cost_usd()

    summary = repo.spend_summary(budget_usd=spent + 1.0)

    assert summary.spent_usd == pytest.approx(spent)
    assert summary.remaining_usd == pytest.approx(1.0)
    assert repo.spend_summary(budget_usd=0.0).remaining_usd == 0.0
    assert repo.run_spend_usd(run.id) == pytest.approx(0.5)


# ---------------------------------------------------------------- google requests


def test_google_requests_count_new_vs_cached_per_run_and_globally(segment):
    run_a = repo.create_run(segment.id, inputs={})
    run_b = repo.create_run(segment.id, inputs={})
    global_before = repo.google_counts()

    repo.record_google_request(GoogleRequestCreate(run_id=run_a.id, kind="satellite", cache_key="k1",
                                                   cache_hit=False, est_cost_usd=0.002, path="imagery/k1.png"))
    repo.record_google_request(GoogleRequestCreate(run_id=run_a.id, kind="streetview_metadata", cache_key="k2",
                                                   cache_hit=False, est_cost_usd=0.0, path=None))
    repo.record_google_request(GoogleRequestCreate(run_id=run_a.id, kind="streetview", cache_key="k3",
                                                   cache_hit=False, est_cost_usd=0.007, path="imagery/k3.png"))
    repo.record_google_request(GoogleRequestCreate(run_id=run_b.id, kind="satellite", cache_key="k1",
                                                   cache_hit=True, est_cost_usd=0.002, path="imagery/k1.png"))
    repo.record_google_request(GoogleRequestCreate(run_id=None, kind="satellite", cache_key="k9",
                                                   cache_hit=False, est_cost_usd=0.002, path=None))

    a = repo.google_counts(run_a.id)
    b = repo.google_counts(run_b.id)
    total = repo.google_counts()

    assert (a.requests, a.cache_hits, a.attempts) == (3, 0, 3)
    assert a.est_cost_usd == pytest.approx(0.009)
    assert (b.requests, b.cache_hits) == (0, 1)
    assert b.est_cost_usd == pytest.approx(0.0)  # cache hits cost nothing
    assert total.requests == global_before.requests + 4
    assert total.cache_hits == global_before.cache_hits + 1
    assert [g.kind for g in repo.list_google_requests(run_a.id)] == ["satellite", "streetview_metadata", "streetview"]


def test_google_request_rejects_unknown_kind(segment):
    with pytest.raises(ValueError):
        GoogleRequestCreate(run_id=None, kind="drone", cache_key="k", cache_hit=False, est_cost_usd=0.0, path=None)


# ---------------------------------------------------------------- game runs


@pytest.fixture()
def ready_segment(segment):
    run = repo.create_run(segment.id, inputs={})
    return repo.finalize_segment_scene(segment.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                       thumbnail_path=None)


def _game_run(score: int, distance_m: float = 120.0, tokens: int = 2, loops: int = 1) -> GameRunCreate:
    return GameRunCreate(score=score, distance_m=distance_m, tokens_collected=tokens, loops=loops,
                         crashed_into_type="parked_vehicle", crashed_into_id="o1", duration_s=14.2)


def test_record_game_run_tracks_best_score_per_segment(ready_segment):
    first = repo.record_game_run(ready_segment.slug, _game_run(score=200), client="pytest")
    lower = repo.record_game_run(ready_segment.slug, _game_run(score=150), client="pytest")
    higher = repo.record_game_run(ready_segment.slug, _game_run(score=220, distance_m=130.0), client="pytest")

    assert first.is_new_best is True and first.best_score == 200
    assert lower.is_new_best is False and lower.best_score == 200
    assert higher.is_new_best is True and higher.best_score == 220 and higher.best_distance_m == 130.0
    assert first.run.score == 200 and first.run.crashed_into_type == "parked_vehicle" and first.run.client == "pytest"

    seg = repo.get_segment(ready_segment.slug)
    assert seg.best_score == 220 and seg.best_distance_m == 130.0
    assert repo.best_game_run(seg.id).score == 220
    assert repo.count_game_runs(seg.id) == 3
    assert [r.score for r in repo.list_top_game_runs(seg.id, limit=2)] == [220, 200]


def test_record_game_run_rejects_impossible_score(ready_segment):
    # score > distance_m + 50*tokens + 1  -> 120 + 100 + 1 = 221
    with pytest.raises(InvalidInputError, match="score"):
        repo.record_game_run(ready_segment.slug, _game_run(score=222), client="pytest")
    with pytest.raises(InvalidInputError, match="distance"):
        repo.record_game_run(ready_segment.slug, _game_run(score=10, distance_m=5001.0), client="pytest")
    # tokens_collected <= loops*tokens_in_scene + tokens_in_scene = 1*2 + 2 = 4
    with pytest.raises(InvalidInputError, match="tokens"):
        repo.record_game_run(ready_segment.slug, _game_run(score=10, tokens=5, loops=1), client="pytest")
    assert repo.count_game_runs(ready_segment.id) == 0


def test_record_game_run_requires_ready_segment(segment):
    with pytest.raises(NotFoundError):
        repo.record_game_run(segment.slug, _game_run(score=1), client="pytest")
    with pytest.raises(NotFoundError):
        repo.record_game_run("missing-street", _game_run(score=1), client="pytest")


def test_game_run_create_rejects_negative_values():
    with pytest.raises(ValueError):
        GameRunCreate(score=-1, distance_m=1.0, tokens_collected=0, loops=0, crashed_into_type=None,
                      crashed_into_id=None, duration_s=1.0)


def test_client_user_agent_is_truncated_to_120_chars(ready_segment):
    result = repo.record_game_run(ready_segment.slug, _game_run(score=5), client="A" * 300)
    assert len(result.run.client) == 120


def test_best_score_survives_engine_reset(ready_segment):
    from hero.db.session import reset_engine

    repo.record_game_run(ready_segment.slug, _game_run(score=77), client="pytest")
    reset_engine()

    assert repo.get_segment(ready_segment.slug).best_score == 77


# ---------------------------------------------------------------- Phase 2: scene v1 | v2 discrimination


def test_segment_record_scene_parses_v1_and_v2_by_schema_version(segment):
    from hero.model.scene_v2 import SceneV2
    from hero.scene.schema import Scene
    from tests.fixtures.street_fixtures import minimal_scene_v2_dict

    run = repo.create_run(segment.id, inputs={})
    v1 = repo.finalize_segment_scene(segment.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                     thumbnail_path=None)
    assert isinstance(v1.scene, Scene)

    v2_scene = SceneV2.model_validate(minimal_scene_v2_dict(100.0, slug=segment.slug))
    v2 = repo.finalize_segment_scene(segment.id, run_id=run.id, scene=v2_scene, quality=Quality.full, thumbnail_path=None)
    assert isinstance(v2.scene, SceneV2) and v2.scene.about.model_version == 1
    reloaded = repo.get_segment(segment.slug)
    assert isinstance(reloaded.scene, SceneV2) and reloaded.scene == v2_scene
    assert [o.id for o in reloaded.scene.hazards()] == ["ent-0000000001"]


def test_run_scene_and_game_run_validation_work_with_a_v2_scene(segment):
    from hero.model.scene_v2 import SceneV2
    from tests.fixtures.street_fixtures import minimal_scene_v2_dict

    scene = SceneV2.model_validate(minimal_scene_v2_dict(100.0, slug=segment.slug))
    run = repo.create_run(segment.id, inputs={})
    assert isinstance(repo.update_run(run.id, RunUpdate(scene=scene)).scene, SceneV2)
    repo.finalize_segment_scene(segment.id, run_id=run.id, scene=scene, quality=Quality.full, thumbnail_path=None)
    result = repo.record_game_run(segment.slug, _game_run(90, distance_m=60.0, tokens=1, loops=0), "pytest")
    assert result.is_new_best and result.best_score == 90   # token count validated from the v2 scene's tokens


def test_llm_call_create_accepts_structured_mode_without_persisting_it(segment):
    run = repo.create_run(segment.id, inputs={})
    rec = repo.record_llm_call(LlmCallCreate(run_id=run.id, step="perceive_streetlevel", model="claude-sonnet-4-5",
                                             input_tokens=10, output_tokens=5, cost_usd=0.01, latency_ms=3, ok=True,
                                             structured_mode="auto_tool"))
    assert rec.step == "perceive_streetlevel" and not hasattr(rec, "structured_mode")
