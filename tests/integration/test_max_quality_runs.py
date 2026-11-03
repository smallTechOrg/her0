"""Max quality end to end on the test DB with ReplayLLM + the recorded Google transport (no Anthropic/Google spend):
N extraction passes with provenance, agreement merge into one new version, extraction cache (zero calls on a re-run),
early stop of the review rounds, per-bin fidelity in the report and the global hard stop."""

from __future__ import annotations

import re

import pytest

from hero.config import reset_settings
from hero.db import model_repo, repo
from hero.fusion import maxquality as MQ
from hero.fusion import publish as publish_mod
from hero.fusion.runner import run_fusion, run_inputs
from hero.config import get_settings
from hero.scene.schema import RunStatus
from tests.fixtures.google.fake_google import make_imagery
from tests.integration.test_fusion_run import (  # noqa: F401 - `world` is the shared fixture
    FORWARD_BACK, PricedReplay, good_street, out_of_budget, report_of, world)

Q3 = {"preset": True, "extraction_passes": 3, "review_rounds": 4, "review_stop_changes": 1}


def run_q(w, quality, *, overrides=FORWARD_BACK, llm=None, model=None):
    settings = get_settings()
    run = repo.create_run(w.seg.id, run_inputs(settings, w.seg, mode="generate", plan_overrides=overrides,
                                               base_version=model_repo.head_version(w.seg.id), quality=quality,
                                               model=model))
    _s, google = make_imagery(w.tmp, w.fake, run.id)
    return run_fusion(run.id, llm=llm if llm is not None else w.llm(), google=google, http=w.fake.client())


def street_rows(w, run):
    return [r for r in model_repo.list_extractions(w.seg.id, run.id) if r.kind == "streetlevel"]


def test_three_passes_store_every_pass_with_provenance_and_create_one_new_version(world):
    llm = world.llm()

    run = run_q(world, {**Q3, "review_rounds": 1}, llm=llm)

    assert run.status is RunStatus.succeeded
    rows = street_rows(world, run)
    n_calls = len(llm.calls_for("streetlevel"))
    assert n_calls == len(rows) and n_calls % 3 == 0 and n_calls >= 3
    by_batch: dict[tuple, set] = {}
    for r in rows:
        prov = MQ.prov_of(r.extraction)
        assert prov["model"] == r.model == get_settings().vision_model and prov["prompt_version"] == MQ.prompt_version("perceive_streetlevel")
        assert prov["cache_key"] and prov["image_shas"] and prov["cost_usd"] > 0
        by_batch.setdefault(tuple(r.observation_ids), set()).add(prov["pass_index"])
    assert all(passes == {0, 1, 2} for passes in by_batch.values()), "every batch was extracted by passes 0, 1 and 2"
    assert world.head().version == 1 and world.head().kind == "fused", "the result is a NEW model version"
    all_rows = model_repo.list_extractions(world.seg.id, run.id)
    assert sum(MQ.prov_of(r.extraction)["cost_usd"] for r in all_rows) == pytest.approx(run.cost_usd, abs=1e-5), \
        "the provenance cost of every stored answer adds up to the run cost"
    rep = report_of(run)
    assert rep.quality is not None and rep.quality.extraction_passes == 3 and rep.quality.preset
    assert rep.quality.agreement["passes"] == 3 * len(by_batch) and rep.quality.cache_hits == 0
    assert rep.cost.anthropic_calls == llm.call_count


def test_agreement_lowers_entity_confidence_but_keeps_every_entity(test_db, tmp_path, monkeypatch):
    from tests.integration.test_fusion_run import new_world

    results = {}
    for label, divergent in (("unanimous", False), ("divergent", True)):
        w = new_world(tmp_path / label, monkeypatch, pano_spacing_m=15.0)
        state = {"n": 0}

        def street(images, prompt, state=state, divergent=divergent):
            i, state["n"] = state["n"], state["n"] + 1
            data = good_street(images, prompt)
            if divergent and i % 3:
                data["detections"], data["facades"] = [], []
            return data

        run = run_q(w, {**Q3, "review_rounds": 0}, llm=w.llm(street=street))
        assert run.status is RunStatus.succeeded, run.error
        results[label] = {e.id: e.confidence for e in w.head().model.entities}
    reset_settings()
    uni, div = results["unanimous"], results["divergent"]
    assert len(div) >= len(uni) > 0, "disagreement never removes an entity"
    assert sum(div.values()) / len(div) < sum(uni.values()) / len(uni), "disagreement lowers the confidence"


def test_a_rerun_after_a_failed_publish_replays_every_pass_and_review_at_zero_calls(world, monkeypatch):
    real = publish_mod.assemble_version
    monkeypatch.setattr(publish_mod, "assemble_version", lambda **k: (_ for _ in ()).throw(RuntimeError("disk full (test)")))
    first_llm = world.llm()
    first = run_q(world, {**Q3, "review_rounds": 2}, llm=first_llm)
    assert first.status is RunStatus.failed and first.error["step"] == "publish"
    assert first_llm.call_count > 6 and model_repo.head_version(world.seg.id) == 0
    stored = len(model_repo.list_extractions(world.seg.id))
    monkeypatch.setattr(publish_mod, "assemble_version", real)

    llm = world.llm()
    second = run_q(world, {**Q3, "review_rounds": 2}, llm=llm)

    assert second.status is RunStatus.succeeded
    assert llm.call_count == 0 and repo.list_llm_calls(second.id) == [] and second.cost_usd == 0, "nothing paid twice"
    assert len(model_repo.list_extractions(world.seg.id)) == stored, "no duplicate rows"
    rep = report_of(second)
    assert rep.cost.anthropic_usd == 0.0 and rep.quality.cache_hits >= 6 and rep.quality.extraction_calls == 0
    assert world.head().version == 1 and world.head().run_id == second.id

    third = run_q(world, {**Q3, "review_rounds": 2}, llm=world.llm())  # and now it is a plain no-op
    assert third.status is RunStatus.succeeded and report_of(third).version_created is False


def test_a_different_pass_count_only_buys_the_missing_passes(world, monkeypatch):
    real = publish_mod.assemble_version
    monkeypatch.setattr(publish_mod, "assemble_version", lambda **k: (_ for _ in ()).throw(RuntimeError("stop (test)")))
    first = run_q(world, {"extraction_passes": 2, "review_rounds": 0})
    assert first.status is RunStatus.failed
    n_batches = len(street_rows(world, first)) // 2
    monkeypatch.setattr(publish_mod, "assemble_version", real)
    llm = world.llm()

    second = run_q(world, {"extraction_passes": 3, "review_rounds": 0}, llm=llm)

    assert second.status is RunStatus.succeeded
    assert len(llm.calls_for("streetlevel")) == n_batches, "only the third pass of each batch was bought"
    assert {MQ.pass_index_of(r.extraction) for r in street_rows(world, second)} == {2}
    assert llm.calls_for("overhead") == []


OTHER_MODEL = "claude-opus-5-5"


def _fail_publish_once(monkeypatch):
    monkeypatch.setattr(publish_mod, "assemble_version",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("stop (test)")))


def test_pass_zero_is_not_replayed_for_a_different_model(world, monkeypatch):
    assert get_settings().vision_model != OTHER_MODEL
    real = publish_mod.assemble_version
    _fail_publish_once(monkeypatch)
    first = run_q(world, {"extraction_passes": 1, "review_rounds": 0})
    assert first.status is RunStatus.failed
    monkeypatch.setattr(publish_mod, "assemble_version", real)
    llm = world.llm()

    second = run_q(world, {"extraction_passes": 1, "review_rounds": 0}, llm=llm, model=OTHER_MODEL)

    assert second.status is RunStatus.succeeded, second.error
    assert len(llm.calls_for("streetlevel")) > 0 and len(llm.calls_for("overhead")) > 0, "pass 0 was bought again"
    new_rows = [r for r in model_repo.list_extractions(world.seg.id, second.id) if r.kind in ("streetlevel", "overhead")]
    assert new_rows and all(r.model == OTHER_MODEL and MQ.prov_of(r.extraction)["model"] == OTHER_MODEL
                            for r in new_rows)


def test_pass_zero_rerun_with_the_same_model_makes_zero_calls(world, monkeypatch):
    real = publish_mod.assemble_version
    _fail_publish_once(monkeypatch)
    first = run_q(world, {"extraction_passes": 1, "review_rounds": 0}, model=OTHER_MODEL)
    assert first.status is RunStatus.failed
    monkeypatch.setattr(publish_mod, "assemble_version", real)
    llm = world.llm()

    second = run_q(world, {"extraction_passes": 1, "review_rounds": 0}, llm=llm, model=OTHER_MODEL)

    assert second.status is RunStatus.succeeded, second.error
    assert llm.calls_for("streetlevel") == [] and llm.calls_for("overhead") == []


def test_a_prompt_version_change_invalidates_pass_zero(world, monkeypatch):
    real = publish_mod.assemble_version
    _fail_publish_once(monkeypatch)
    first = run_q(world, {"extraction_passes": 1, "review_rounds": 0})
    assert first.status is RunStatus.failed
    monkeypatch.setattr(publish_mod, "assemble_version", real)
    monkeypatch.setattr(MQ, "prompt_version", lambda name: "changed00")
    llm = world.llm()

    second = run_q(world, {"extraction_passes": 1, "review_rounds": 0}, llm=llm)

    assert second.status is RunStatus.succeeded, second.error
    assert len(llm.calls_for("streetlevel")) > 0 and len(llm.calls_for("overhead")) > 0


def _critique(applied: bool):
    def answer(images, prompt):
        if not applied:
            return {"accepted": False, "critique": "small remaining difference", "ops": []}
        eid = re.search(r"(ent-[0-9a-f]+):", prompt).group(1)
        return {"accepted": False, "critique": "nudge", "ops": [
            {"op": "nudge_entity", "entity_id": eid, "ds_m": 0.4, "dt_m": 0.1, "reason": "slightly off"},
            {"op": "nudge_entity", "entity_id": eid, "ds_m": 0.3, "dt_m": 0.0, "reason": "again"}]}
    return answer


def test_review_rounds_stop_early_when_the_diff_is_small(world):
    llm = world.llm(critique=_critique(applied=False))

    run = run_q(world, {"preset": True, "extraction_passes": 1, "review_rounds": 4, "review_stop_changes": 1}, llm=llm)

    assert run.status is RunStatus.succeeded
    assert len(llm.calls_for("critique")) == 1, "round 1 changed nothing -> rounds 2-4 were never bought"
    rep = report_of(run)
    assert rep.quality.review_rounds_planned == 4 and rep.quality.review_rounds_run == 1
    assert "early stop after round 1" in rep.quality.early_stop
    assert world.head().version == 1


def test_review_rounds_keep_going_while_they_change_the_model_up_to_the_limit(world):
    llm = world.llm(critique=_critique(applied=True))

    run = run_q(world, {"preset": True, "extraction_passes": 1, "review_rounds": 3, "review_stop_changes": 1}, llm=llm)

    assert run.status is RunStatus.succeeded
    assert len(llm.calls_for("critique")) == 3, "each round applied > 1 change, so all three rounds ran"
    rep = report_of(run)
    assert rep.quality.review_rounds_run == 3 and rep.quality.early_stop == ""
    assert [it["n"] for it in run.iterations] == [1, 2, 3]


def test_report_lists_fidelity_before_and_after_per_5m_bin(world):
    first = run_q(world, {**Q3, "review_rounds": 1})
    rep = report_of(first)
    bins = rep.fidelity.bins
    assert len(bins) == 20 and bins[0].s_from == 0 and bins[-1].s_to == 100 and all(b.s_to - b.s_from == 5 for b in bins)
    assert all(b.level_before == 0 for b in bins), "a first run starts from the map-only prior"
    assert all(b.level_after >= b.level_before for b in bins) and any(b.level_after > 0 for b in bins)
    from tests.integration.test_fusion_run import ALL_FOUR

    second = run_q(world, {**Q3, "review_rounds": 1}, overrides=ALL_FOUR)
    bins2 = report_of(second).fidelity.bins
    assert len(bins2) == 20 and [b.level_before for b in bins2] == [b.level_after for b in bins], "run 2 starts where run 1 ended"
    assert all(b.level_after >= b.level_before for b in bins2), "fidelity never falls"
    assert any(b.level_after > b.level_before for b in bins2)


def test_extra_passes_stop_at_the_global_hard_stop_and_keep_what_was_bought(world):
    from hero.llm.budget import BudgetGuard

    llm = PricedReplay(BudgetGuard(get_settings()), street=[good_street, out_of_budget(), good_street, good_street, good_street])

    run = run_q(world, {"extraction_passes": 3, "review_rounds": 0}, llm=llm)

    assert run.status is RunStatus.succeeded, run.error
    rep = report_of(run)
    assert rep.degraded and any("global budget" in w for w in rep.warnings)
    assert rep.quality.passes_skipped >= 2
    rows = street_rows(world, run)
    assert {MQ.pass_index_of(r.extraction) for r in rows} == {0}, "only pass 0 of each batch was bought"
    assert len(llm.calls_for("streetlevel")) == len(rows) + 1, "after the hard stop no further extra pass was even tried"
    assert world.head().version == 1


def test_registry_refuses_a_max_quality_run_when_the_global_budget_is_used_up(world, monkeypatch):
    from hero.pipeline import registry

    monkeypatch.setenv("HERO_CLAUDE_BUDGET_USD", "0.0001")
    reset_settings()
    repo_spent = repo.total_llm_cost_usd()
    if repo_spent <= 0:  # make sure something is "spent": a recorded call on a throwaway run
        from hero.db.repo import LlmCallCreate

        r = repo.create_run(world.seg.id, {"mode": "generate"})
        repo.record_llm_call(LlmCallCreate(run_id=r.id, step="x", model="claude-sonnet-5-5", input_tokens=1, output_tokens=1,
                                           cost_usd=0.01, latency_ms=1, ok=True, request_summary="seed"))
    with pytest.raises(registry.BudgetExhausted):
        registry.start_run(world.seg.slug, mode="generate", quality=Q3)
    reset_settings()
