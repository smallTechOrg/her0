"""run_fusion end to end on the test DB with the recorded Google transport and ReplayLLM (spec/capabilities/model-fusion.md).

No Anthropic spend: perception answers come from ``tests/fixtures/extractions`` through ``ReplayLLM``; the
``PricedReplay`` wrapper writes ``llm_calls`` rows (and enforces the budget) the way ``AnthropicClient`` does, so cost
accounting, the budget guard and the ``llm.call`` log line are exercised for real.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.repo import LlmCallCreate, SegmentCreate
from hero.fusion.runner import run_fusion, run_inputs
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMUsage
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UpstreamError
from hero.llm.pricing import load_price_table
from structlog.testing import capture_logs
from hero.scene.schema import FootpathSide, LatLon, RunStatus, SegmentStatus
from tests.fixtures.google.fake_google import FakeGoogle, make_imagery, straight_ref
from tests.fixtures.replay_llm import ReplayLLM

PER_CALL_USD = 0.02  # what PricedReplay charges per call (input/output token split below)


class PricedReplay(ReplayLLM):
    """ReplayLLM + the BudgetGuard bookkeeping of AnthropicClient: check before, ``llm_calls`` row + log after."""

    def __init__(self, guard: BudgetGuard, **kw):
        super().__init__(**kw)
        self.guard = guard
        self.prices = load_price_table(get_settings())

    def vision_json(self, *, run_id, step, images, prompt, schema, model, prompt_name, **kw):
        self.guard.check_before_call(run_id, PER_CALL_USD)
        obj, usage = super().vision_json(run_id=run_id, step=step, images=images, prompt=prompt, schema=schema,
                                         model=model, prompt_name=prompt_name, **kw)
        cost = self.prices.cost_usd(model, usage.input_tokens, usage.output_tokens)
        self.guard.record(LlmCallCreate(run_id=run_id, step=step, model=model, input_tokens=usage.input_tokens,
                                        output_tokens=usage.output_tokens, cost_usd=cost, latency_ms=5, ok=True,
                                        request_summary=f"{prompt_name}: images={len(images)}"))
        return obj, LLMUsage(model=model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                             cost_usd=cost, latency_ms=5, calls=1)


@dataclass
class World:
    seg: repo.SegmentRecord
    fake: FakeGoogle
    tmp: Path

    def llm(self, run_budget: float | None = None, **kw) -> PricedReplay:
        return PricedReplay(BudgetGuard(get_settings(), run_budget_usd=run_budget), **kw)

    def run(self, *, mode="generate", overrides=None, run_budget=None, llm=None, fake=None, http=None):
        fake = fake or self.fake
        settings = get_settings()
        run = repo.create_run(self.seg.id, run_inputs(settings, self.seg, mode=mode, plan_overrides=overrides or {},
                                                      run_budget_usd=run_budget,
                                                      base_version=model_repo.head_version(self.seg.id)))
        _s, google = make_imagery(self.tmp, fake, run.id)
        llm = llm if llm is not None else self.llm(run_budget)
        return run_fusion(run.id, llm=llm, google=google, http=http or fake.client())

    def segment(self) -> repo.SegmentRecord:
        return repo.require_segment(self.seg.slug)

    def head(self):
        return model_repo.get_version(self.seg.id, model_repo.head_version(self.seg.id))


def new_world(tmp_path, monkeypatch, *, length=100.0, **fake_kw) -> World:
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    ref = straight_ref(length)
    lat0, lon0 = ref.point_at(0.0, 0.0)
    lat1, lon1 = ref.point_at(ref.length_s, 0.0)
    seg = repo.create_segment(SegmentCreate(
        name=f"Fusion test {uuid.uuid4().hex[:6]} (west footpath)", city="Bengaluru", start=LatLon(lat=lat0, lon=lon0),
        end=LatLon(lat=lat1, lon=lon1), footpath_side=FootpathSide.left))
    return World(seg=seg, fake=FakeGoogle(ref, **fake_kw), tmp=tmp_path)


@pytest.fixture()
def world(test_db, tmp_path, monkeypatch):
    w = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    yield w
    reset_settings()


FORWARD_BACK = {"headings": ["forward", "back"], "sv_spacing_m": 10}
ALL_FOUR = {"headings": ["forward", "back", "left", "right"], "sv_spacing_m": 10}
REQUIRED_STEPS = ["acquire_imagery", "select_unfused", "perceive_streetlevel", "merge", "publish", "report"]
TEMPLATE = ReplayLLM()


def step_names(run):
    return [s.name for s in run.steps]


def good_street(images, prompt):
    """The default canned street answer, usable as an entry of a ``street=[...]`` list."""
    return TEMPLATE._street_from_template(images)


def out_of_budget(scope="total"):
    """A ``street=[...]`` entry that raises BudgetExceeded when consumed (ReplayLLM deep-copies exception entries,
    which a keyword-only exception cannot survive, so a callable is used)."""
    def raise_it(images, prompt):
        raise BudgetExceeded("Generation budget would be exceeded (test).", spent_total_usd=49.9, spent_run_usd=0.5,
                             estimate_usd=0.2, budget_total_usd=50.0, budget_run_usd=1.0, scope=scope)
    return raise_it


def observation_count(w: World) -> int:
    return sum(model_repo.observation_counts(w.seg.id).values())


def report_of(run) -> "RunReport":  # noqa: F821
    rep = model_repo.get_run_report(run.id)
    assert rep is not None, f"run {run.id} has no report ({run.status}, {run.error})"
    return rep


def entity_ids(rec) -> set[str]:
    return {e.id for e in rec.model.entities}


# ------------------------------------------------------------------ run 1


def test_first_run_publishes_v1_with_evidence_report_and_costs(world):
    llm = world.llm()

    with capture_logs() as events:
        run = world.run(overrides=FORWARD_BACK, llm=llm)

    assert run.status is RunStatus.succeeded and run.error is None
    names = step_names(run)
    assert all(n in names for n in REQUIRED_STEPS) and names[0] == "load_base_model"
    by_name = {s.name: s for s in run.steps}
    assert all(by_name[n].status == "ok" and by_name[n].latency_ms > 0 for n in REQUIRED_STEPS)
    head = world.head()
    assert head.version == 1 and head.parent_version == 0 and head.kind == "fused" and head.run_id == run.id
    assert head.entity_count >= 5 and head.fidelity_level >= 1
    assert model_repo.get_version(world.seg.id, 0).kind == "prior"
    for e in head.model.entities:
        assert e.description.strip() and e.provenance, e.id
    seg = world.segment()
    assert seg.status is SegmentStatus.ready and seg.latest_run_id == run.id and seg.scene.schema_version == 2
    assert seg.scene.model.version == 1 and seg.scene.generation.models["vision"] == get_settings().vision_model
    assert seg.quality is not None and (world.tmp / seg.thumbnail_path).exists()
    # one extraction row per model call, one report, costs add up
    rows = model_repo.list_extractions(world.seg.id, run.id)
    assert len(rows) == llm.call_count == len(repo.list_llm_calls(run.id))
    assert {r.kind for r in rows} == {"overhead", "streetlevel", "critique"}
    rep = report_of(run)
    calls = repo.list_llm_calls(run.id)
    assert rep.cost.anthropic_usd == pytest.approx(sum(c.cost_usd for c in calls)) == pytest.approx(run.cost_usd)
    assert rep.cost.anthropic_calls == len(calls) and 0 < rep.cost.anthropic_usd < get_settings().run_budget_usd
    assert rep.images.new + rep.images.cached == rep.images.used_total == rep.images.planned - len(rep.images.skipped)
    assert rep.images.new == len(rep.images.new_observation_ids) > 10 and rep.images.cached == 0
    assert rep.summary.strip() and rep.suggestions and rep.version_created and rep.result_version == 1
    assert rep.fidelity.before.overall == 0.0 and rep.fidelity.after.overall == head.fidelity.overall.score
    assert rep.cost.google_new_requests >= rep.images.new and rep.cost.total_usd > rep.cost.anthropic_usd
    # structured logs
    kinds = {e["event"] for e in events}
    assert {"pipeline.step", "llm.call", "model.merge", "model.published", "fusion.report"} <= kinds
    llm_events = [e for e in events if e["event"] == "llm.call"]
    assert len(llm_events) == llm.call_count and all(e["cost_usd"] > 0 for e in llm_events)
    published = next(e for e in events if e["event"] == "model.published")
    assert published["version"] == 1 and published["entity_count"] == head.entity_count
    assert next(e for e in events if e["event"] == "model.merge")["entities_added"] >= 5


def test_run_row_is_persisted_for_the_admin_view(world):
    run = world.run(overrides=FORWARD_BACK)

    assert run.inputs["mode"] == "generate" and run.inputs["imagery_plan"]["headings"] == ["forward", "back"]
    assert run.inputs["base_version"] is None and run.inputs["models"]["vision"]
    assert run.started_at and run.finished_at and run.finished_at >= run.started_at
    ij = run.imagery
    assert len(ij["observation_ids"]) == len(ij["new_observation_ids"]) > 10
    assert ij["reference_line_source"] == "osm_nominatim" and ij["osm_available"] is True
    assert run.scene is not None and run.scene.schema_version == 2
    assert run.iterations and run.iterations[0]["n"] == 1 and run.iterations[0]["preview_url"].endswith("critique1.png")
    assert (world.tmp / "previews" / run.id / "critique1.png").exists()
    assert run.input_tokens > 0 and run.cost_usd > 0 and run.google_requests > 0


def test_evidence_is_unique_per_observation_and_detection_key_even_when_local_ids_repeat_across_batches(world):
    run = world.run(overrides=ALL_FOUR)
    assert run.status is RunStatus.succeeded

    seen: set[tuple[str, str]] = set()
    for e in world.head().model.entities:
        for p in e.provenance:
            if p.role.value == "supports":
                key = (p.observation_id, p.detection_key)
                assert key not in seen, f"evidence {key} counted twice"
                seen.add(key)
    batches = [json.loads(r.model_dump_json())["extraction"] for r in model_repo.list_extractions(world.seg.id, run.id)
               if r.kind == "streetlevel"]
    local_ids = [d["local_id"] for b in batches for d in b["detections"]]
    assert len(batches) >= 3 and len(local_ids) > len(set(local_ids)), "the canned batches DO reuse local ids"


# ------------------------------------------------------------------ no-op / run 2


def test_identical_rerun_is_a_free_noop(world):
    first = world.run(overrides=FORWARD_BACK)
    versions = len(model_repo.list_versions(world.seg.id))
    obs_before = observation_count(world)
    llm = world.llm()

    run = world.run(overrides=FORWARD_BACK, llm=llm)

    assert run.status is RunStatus.succeeded and llm.call_count == 0
    assert repo.list_llm_calls(run.id) == [] and run.cost_usd == 0
    assert repo.google_counts(run.id).requests == 0 and repo.google_counts(run.id).cache_hits > 0
    assert len(model_repo.list_versions(world.seg.id)) == versions and observation_count(world) == obs_before
    rep = report_of(run)
    assert rep.version_created is False and rep.result_version == 1 and rep.base_version == 1
    assert rep.cost.anthropic_usd == 0.0 and rep.cost.google_new_requests == 0 and rep.images.new == 0
    assert "no new images" in rep.summary and rep.images.cached == obs_before
    assert run.scene is None
    names = step_names(run)
    assert "select_unfused" in names and "report" in names and "merge" not in names and "perceive_streetlevel" not in names
    assert world.segment().latest_run_id == first.id and world.segment().status is SegmentStatus.ready


def test_second_run_with_left_and_right_views_creates_v2_and_keeps_every_v1_entity(world):
    first = world.run(overrides=FORWARD_BACK)
    v1 = world.head()
    new_obs_total = observation_count(world)
    llm = world.llm()

    run = world.run(overrides=ALL_FOUR, llm=llm)

    assert run.status is RunStatus.succeeded
    v2 = world.head()
    assert v2.version == 2 and v2.parent_version == 1 and v2.run_id == run.id
    assert entity_ids(v1) <= entity_ids(v2), "no code path deletes an entity"
    assert v2.diff is not None and v2.diff.fidelity.improved, "fidelity improved on the stretches that got new images"
    assert v2.fidelity.overall.score - v1.fidelity.overall.score >= 0.5
    fused_new = set(v2.model.fused_observation_ids) - set(v1.model.fused_observation_ids)
    assert v2.diff.observations.fused_added == len(fused_new) > 0
    rep = report_of(run)
    run1_new = report_of(first).images.new
    assert rep.images.cached >= run1_new, "everything run 1 stored is a cache hit"
    assert rep.cost.google_new_requests <= rep.images.planned - run1_new
    assert rep.base_version == 1 and rep.result_version == 2 and rep.version_created
    assert rep.fidelity.improved_intervals and rep.cost.anthropic_usd <= get_settings().run_budget_usd
    assert llm.calls_for("overhead") == [], "the satellite images were fused in run 1 and are never re-sent"
    assert observation_count(world) > new_obs_total
    assert world.segment().scene.model.version == 2


# ------------------------------------------------------------------ budget guards


def test_run_cap_too_small_for_any_call_fails_budget_before_the_first_request_and_publishes_nothing(world):
    world.run(overrides=FORWARD_BACK)
    before = world.segment()
    versions = len(model_repo.list_versions(world.seg.id))
    obs_before = observation_count(world)
    llm = world.llm()

    run = world.run(overrides=ALL_FOUR, run_budget=0.05, llm=llm)

    assert run.status is RunStatus.failed_budget and run.error["code"] == "budget_exhausted"
    assert run.error["step"] == "prepare_views" and llm.call_count == 0
    assert repo.list_llm_calls(run.id) == []
    assert len(model_repo.list_versions(world.seg.id)) == versions
    assert observation_count(world) > obs_before, "the images fetched before the stop stay stored"
    after = world.segment()
    assert after.scene.model_dump_json() == before.scene.model_dump_json() and after.latest_run_id == before.latest_run_id
    assert after.status is SegmentStatus.ready
    rep = report_of(run)
    assert rep.degraded and rep.version_created is False and "did not produce a model" in rep.summary


def test_cap_that_fits_part_of_the_batches_trims_the_rest_and_says_so(world):
    llm = world.llm()

    run = world.run(overrides=ALL_FOUR, run_budget=0.30, llm=llm)

    assert run.status is RunStatus.succeeded
    rep = report_of(run)
    assert any(w.startswith("budget_trimmed") for w in rep.warnings) and rep.degraded
    street_calls = len(llm.calls_for("streetlevel"))
    assert 1 <= street_calls < 4, "some, not all, of the four street batches were sent"
    assert run.cost_usd <= 0.30
    head = world.head()
    assert len(head.model.fused_observation_ids) < observation_count(world), "dropped batches stay unfused"


def test_global_budget_exhausted_in_the_middle_ends_failed_budget_and_a_rerun_replays_the_paid_batches(world):
    llm = PricedReplay(BudgetGuard(get_settings()), street=[good_street, out_of_budget()])

    run = world.run(overrides=ALL_FOUR, llm=llm)

    assert run.status is RunStatus.failed_budget and run.error["step"] == "perceive_streetlevel"
    paid = model_repo.list_extractions(world.seg.id, run.id)
    assert {r.kind for r in paid} == {"overhead", "streetlevel"} and len(paid) == 2, "paid answers are kept"
    seg = world.segment()
    assert model_repo.head_version(world.seg.id) == 0, "no partial merge was published"
    assert seg.status is SegmentStatus.ready and seg.scene.about.degraded and seg.quality.value == "generic", \
        "a first generation that produced nothing still leaves the labelled prior scene"

    again = world.llm()
    run2 = world.run(overrides=ALL_FOUR, llm=again)

    assert run2.status is RunStatus.succeeded
    assert again.calls_for("overhead") == [], "the stored overhead extraction is replayed, not re-bought"
    assert len(again.calls_for("streetlevel")) == len(
        [b for b in model_repo.list_extractions(world.seg.id, run2.id) if b.kind == "streetlevel"])
    assert next(s for s in run2.steps if s.name == "perceive_streetlevel").detail.count("1 replayed") == 1
    head = world.head()
    assert head.version == 1 and len(head.model.fused_observation_ids) == observation_count(world)
    assert report_of(run2).cost.anthropic_usd < 0.30


# ------------------------------------------------------------------ degraded perception


def test_invalid_street_batch_is_skipped_stays_unfused_and_is_picked_up_next_run(world):
    bad = SchemaValidationError("StreetExtraction invalid twice (test)")
    llm = world.llm(street=[good_street, bad, good_street, good_street])

    run = world.run(overrides=ALL_FOUR, llm=llm)

    assert run.status is RunStatus.succeeded
    rep = report_of(run)
    assert rep.degraded and any("was skipped" in w and "retried next run" in w for w in rep.warnings)
    head = world.head()
    unfused = observation_count(world) - len(head.model.fused_observation_ids)
    assert 0 < unfused <= get_settings().perception_batch_images
    assert next(s for s in run.steps if s.name == "perceive_streetlevel").detail.endswith("1 skipped")

    again = world.llm()
    run2 = world.run(overrides=ALL_FOUR, llm=again)

    assert run2.status is RunStatus.succeeded and len(again.calls_for("streetlevel")) == 1
    v2 = world.head()
    assert v2.version == 2 and len(v2.model.fused_observation_ids) == observation_count(world)
    assert entity_ids(head) <= entity_ids(v2)


def test_every_perception_call_failing_fails_the_run_and_publishes_nothing(world):
    boom = UpstreamError("Anthropic rejected the key (test)", kind="anthropic", status=401)
    llm = world.llm(overhead=boom, street=boom, critique=boom)

    run = world.run(overrides=FORWARD_BACK, llm=llm)

    assert run.status is RunStatus.failed and run.error["code"] == "upstream_error"
    assert run.error["step"] == "project_detections"
    assert model_repo.head_version(world.seg.id) == 0
    assert world.segment().scene.about.degraded
    rep = report_of(run)
    assert rep.degraded and rep.result_version == 0 and not rep.version_created


def test_critique_ops_are_applied_with_bounds_and_never_delete(world):
    def ops(images, prompt):
        import re

        eid = re.search(r"(ent-[0-9a-f]+):", prompt).group(1)
        return {"accepted": False, "critique": "nudge the first object; also an absurd move",
                "ops": [{"op": "nudge_entity", "entity_id": eid, "ds_m": 0.5, "dt_m": 0.2, "reason": "slightly off"},
                        {"op": "nudge_entity", "entity_id": eid, "ds_m": 9.0, "dt_m": 0.0, "reason": "too far"},
                        {"op": "flag_entity", "entity_id": "ent-0000000999", "reason": "unknown id"}]}

    llm = world.llm(critique=ops)

    run = world.run(overrides=FORWARD_BACK, llm=llm)

    assert run.status is RunStatus.succeeded
    it = run.iterations[0]
    assert it["accepted"] is False and len(it["patch_ops"]) == 3 and it["applied_count"] >= 1
    assert any("unknown" in r for r in it["skipped"]) and len(it["skipped"]) >= 2
    merged_entities = [r for r in model_repo.list_extractions(world.seg.id, run.id) if r.kind == "critique"]
    assert len(merged_entities) == 1
    assert world.head().entity_count >= 5
    assert any(p.role.value == "critique" for e in world.head().model.entities for p in e.provenance)


# ------------------------------------------------------------------ acquire-only


def test_acquire_only_run_stores_observations_without_any_model_call(world):
    llm = world.llm()

    run = world.run(mode="acquire", overrides=ALL_FOUR, llm=llm)

    assert run.status is RunStatus.succeeded and llm.call_count == 0 and repo.list_llm_calls(run.id) == []
    assert run.inputs["mode"] == "acquire"
    names = step_names(run)
    assert names[-1] == "report" and "select_unfused" not in names and "merge" not in names
    rep = report_of(run)
    assert rep.mode == "acquire" and rep.version_created is False and rep.images.new > 10
    assert rep.cost.anthropic_usd == 0.0 and rep.cost.google_est_usd > 0
    assert world.segment().status is SegmentStatus.draft, "an acquire run does not publish or change the segment"
    assert model_repo.head_version(world.seg.id) == 0, "only the prior exists"
    stored = observation_count(world)
    assert stored == rep.images.new

    gen = world.run(overrides=ALL_FOUR)

    assert gen.status is RunStatus.succeeded
    head = world.head()
    assert head.version == 1 and head.diff.observations.fused_added == stored == len(head.model.fused_observation_ids)
    assert report_of(gen).images.new == 0 and report_of(gen).images.cached == stored


# ------------------------------------------------------------------ placement fallbacks and failures


def test_without_road_geometry_the_reference_line_is_a_flagged_straight_chord(world):
    offline = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="gone")))

    run = world.run(overrides=FORWARD_BACK, http=offline)

    assert run.status is RunStatus.succeeded
    head = world.head()
    assert head.model.reference_line.source == "straight_chord" and head.model.placement_verified is False
    rep = report_of(run)
    assert any("straight chord" in w for w in rep.warnings) and any("Overpass" in w for w in rep.warnings)
    assert world.segment().scene.about.placement_verified is False
    assert run.imagery["reference_line_source"] == "straight_chord" and run.imagery["osm_available"] is False


def test_a_second_run_inherits_the_stored_reference_line(world):
    world.run(overrides=FORWARD_BACK)
    ref_v0 = model_repo.get_version(world.seg.id, 0).model.reference_line
    offline = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="gone")))

    run = world.run(overrides=ALL_FOUR, http=offline)

    assert run.status is RunStatus.succeeded
    assert world.head().model.reference_line == ref_v0
    assert next(s for s in run.steps if s.name == "resolve_reference_line").detail.startswith("inherited")


def test_overview_failure_fails_the_run_and_the_segment_still_gets_the_labelled_prior_scene(world):
    denied = FakeGoogle(world.fake.ref, require_key="a-different-key")
    llm = world.llm()

    run = world.run(overrides=FORWARD_BACK, fake=denied, http=world.fake.client(), llm=llm)

    assert run.status is RunStatus.failed and run.error["code"] == "upstream_error"
    assert run.error["step"] == "acquire_imagery" and "Google" in run.error["message"]
    assert llm.call_count == 0
    seg = world.segment()
    assert seg.status is SegmentStatus.ready and seg.quality.value == "generic"
    assert seg.scene.schema_version == 2 and seg.scene.about.degraded and seg.scene.about.fidelity.level == "L0"
    assert report_of(run).degraded
    assert model_repo.head_version(world.seg.id) == 0


def test_a_failed_rerun_never_replaces_the_published_scene(world):
    world.run(overrides=FORWARD_BACK)
    before = world.segment().scene.model_dump_json()
    boom = UpstreamError("Anthropic is down (test)", kind="anthropic", status=529)

    run = world.run(overrides=ALL_FOUR, llm=world.llm(street=boom, critique=boom))

    assert run.status is RunStatus.failed and run.error["step"] == "project_detections"
    assert world.segment().scene.model_dump_json() == before and world.segment().status is SegmentStatus.ready
    assert model_repo.head_version(world.seg.id) == 1


def test_streets_without_any_street_view_coverage_still_publish_an_overhead_only_model(world):
    no_sv = FakeGoogle(world.fake.ref, coverage=(1e6, 2e6))  # no panorama anywhere on the street

    run = world.run(overrides=FORWARD_BACK, fake=no_sv)

    assert run.status is RunStatus.succeeded
    rep = report_of(run)
    assert rep.images.by_kind.get("streetview", 0) == 0 and rep.images.skipped
    head = world.head()
    assert head.fidelity_level == 0 and head.kind == "fused"
    assert world.segment().scene.about.fidelity.level == "L0"
    assert world.segment().quality.value == "satellite_only"


# ------------------------------------------------------------------ legacy lineage


def test_phase1_segment_gets_a_legacy_v0_and_a_fresh_lineage(test_db, tmp_path, monkeypatch):
    from hero.scene.schema import Quality, Scene

    scene = Scene.model_validate(json.loads((Path(__file__).resolve().parents[1] / "e2e" / "fixtures"
                                             / "scene_example.json").read_text()))
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    try:
        seg = repo.create_segment(SegmentCreate(
            name=f"Legacy {uuid.uuid4().hex[:6]}", city="Bengaluru", start=scene.segment.start, end=scene.segment.end,
            footpath_side=scene.segment.footpath_side))
        repo.finalize_segment_scene(seg.id, run_id="phase1-run", scene=scene, quality=Quality.full,
                                    thumbnail_path="previews/phase1/thumb.png")
        from hero.model.refline import ReferenceLine

        ref = ReferenceLine.from_latlons([(seg.start.lat, seg.start.lon), (seg.end.lat, seg.end.lon)])
        w = World(seg=seg, fake=FakeGoogle(ref, pano_spacing_m=15.0), tmp=tmp_path)
        offline = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="gone")))

        run = w.run(overrides=FORWARD_BACK, http=offline)

        assert run.status is RunStatus.succeeded, run.error
        v0 = model_repo.get_version(seg.id, 0)
        assert v0.kind == "legacy" and v0.model.legacy and v0.scene["schema_version"] == 1
        v1 = model_repo.get_version(seg.id, 1)
        assert v1.kind == "fused" and v1.parent_version == 0 and not v1.model.legacy
        legacy_ids = {e.id for e in v0.model.entities}
        assert legacy_ids and not (legacy_ids & entity_ids(v1)), "nothing is merged from legacy objects"
        assert all(e.provenance for e in v1.model.entities)
        assert next(s for s in run.steps if s.name == "load_base_model").detail.startswith("imported Phase-1 scene")
        assert repo.require_segment(seg.slug).scene.schema_version == 2
        rep = report_of(run)
        assert rep.base_version == 0 and rep.result_version == 1 and rep.version_created
        assert run.inputs["base_version"] is None
    finally:
        reset_settings()
