"""Per-run model choice end to end, no spend: ``GET /api/admin/models``, the 422 rules of ``generate``, and a full
fusion run over ``ReplayLLM`` proving the chosen model reaches run.inputs, every llm_calls row and the RunReport."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from hero.config import PROJECT_ROOT, get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.repo import RunUpdate, SegmentCreate
from hero.fusion.runner import run_inputs
from hero.llm import models as models_mod
from hero.llm.pricing import price_for
from hero.pipeline import registry
from hero.scene.schema import FootpathSide, LatLon, RunStatus
from tests.integration.test_fusion_run import FORWARD_BACK, new_world

OPUS = "claude-opus-5-5"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "phase2_1" / "model_check_claude-opus-5-5.json"


@pytest.fixture()
def auth():
    return {"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()}


@pytest.fixture()
def segment(test_db):
    return repo.create_segment(SegmentCreate(
        name=f"Model choice {uuid.uuid4().hex[:6]} (west footpath)", city="Bengaluru",
        start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.9749, lon=77.6405), footpath_side=FootpathSide.left))


@pytest.fixture()
def idle_worker(monkeypatch):
    ran: list[str] = []

    def worker(run_id):
        ran.append(run_id)
        repo.update_run(run_id, RunUpdate(status=RunStatus.succeeded, finished_at=datetime.now(UTC)))

    monkeypatch.setattr(registry, "run_fusion", worker)
    return ran


@pytest.fixture()
def opus_disabled(tmp_path, monkeypatch):
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"ok": False, "error": "HTTP 404 model not found"}))
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"models": [
        {"id": "claude-sonnet-5-5", "label": "Sonnet", "evidence": None},
        {"id": OPUS, "label": "Opus", "evidence": str(ev)}]}))
    monkeypatch.setattr(models_mod, "MODELS_FILE", models)


def _generate(client, auth, slug, body):
    return client.post(f"/api/admin/segments/{slug}/generate", json=body, headers=auth)


# ---------------------------------------------------------------- GET /api/admin/models


def test_models_requires_the_admin_password(client):
    assert client.get("/api/admin/models").status_code == 401


def test_models_lists_default_and_opus_matching_the_evidence(client, auth):
    data = client.get("/api/admin/models", headers=auth).json()["data"]
    assert data["default"] == get_settings().vision_model == "claude-sonnet-5-5"
    options = {o["id"]: o for o in data["options"]}
    assert options[data["default"]]["enabled"] is True
    evidence = json.loads(EVIDENCE.read_text())
    opus = options[OPUS]
    assert opus["enabled"] is evidence["ok"]
    assert opus["price_source_url"].startswith("https://") and opus["price_verified_on"]
    assert (opus["input_usd_per_mtok"], opus["output_usd_per_mtok"]) == (4.0, 20.0)
    assert opus["evidence"].endswith("model_check_claude-opus-5-5.json")
    if not evidence["ok"]:
        assert opus["disabled_reason"]


def test_models_lists_opus_disabled_with_the_reason(client, auth, opus_disabled):
    options = {o["id"]: o for o in client.get("/api/admin/models", headers=auth).json()["data"]["options"]}
    assert options[OPUS]["enabled"] is False and "model not found" in options[OPUS]["disabled_reason"]
    assert options["claude-sonnet-5-5"]["enabled"] is True


# ---------------------------------------------------------------- generate: 422 rules and acceptance


def test_generate_unknown_model_is_422_and_queues_nothing(client, auth, segment, idle_worker):
    before = repo.get_active_run()  # other tests may leave a run behind in the shared session DB: compare, don't assume
    resp = _generate(client, auth, segment.slug, {"model": "claude-made-up-9"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"
    assert "claude-made-up-9" in resp.json()["error"]["message"]
    after = repo.get_active_run()
    assert idle_worker == [] and (after.id if after else None) == (before.id if before else None)


def test_generate_disabled_model_is_422_with_the_reason(client, auth, segment, idle_worker, opus_disabled):
    resp = _generate(client, auth, segment.slug, {"model": OPUS})
    err = resp.json()["error"]
    assert resp.status_code == 422 and err["code"] == "validation_error"
    assert "disabled" in err["message"] and "model not found" in err["message"]
    assert err["detail"]["field"] == "model" and idle_worker == []


def test_generate_without_model_uses_the_default(client, auth, segment, idle_worker):
    resp = _generate(client, auth, segment.slug, {})
    assert resp.status_code == 202 and resp.json()["data"]["model"] == get_settings().vision_model
    registry.wait_for(resp.json()["data"]["run_id"], 10)
    run = repo.require_run(resp.json()["data"]["run_id"])
    assert run.inputs["models"] == {"vision": "claude-sonnet-5-5", "review": "claude-sonnet-5-5", "source": "default"}


def test_generate_with_a_chosen_model_records_it_on_the_run_inputs(client, auth, segment, idle_worker):
    resp = _generate(client, auth, segment.slug, {"model": OPUS})
    if not json.loads(EVIDENCE.read_text())["ok"]:  # verification failed: the option is disabled and refused
        assert resp.status_code == 422
        return
    assert resp.status_code == 202 and resp.json()["data"]["model"] == OPUS
    registry.wait_for(resp.json()["data"]["run_id"], 10)
    run = repo.require_run(resp.json()["data"]["run_id"])
    assert run.inputs["models"] == {"vision": OPUS, "review": OPUS, "source": "request"}
    detail = client.get(f"/api/admin/runs/{run.id}", headers=auth).json()["data"]
    assert detail["inputs"]["models"]["vision"] == OPUS


# ---------------------------------------------------------------- a whole fusion run with the chosen model


def test_fusion_run_with_the_chosen_model_reaches_every_record(test_db, tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    from tests.integration.test_fusion_run import PricedReplay
    from hero.llm.budget import BudgetGuard

    world = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    try:
        settings = get_settings()
        inputs = run_inputs(settings, world.seg, mode="generate", plan_overrides=FORWARD_BACK,
                            base_version=model_repo.head_version(world.seg.id), model=OPUS)
        assert inputs["models"] == {"vision": OPUS, "review": OPUS, "source": "request"}
        run = repo.create_run(world.seg.id, inputs)
        from tests.fixtures.google.fake_google import make_imagery
        from hero.fusion.runner import run_fusion

        _s, google = make_imagery(world.tmp, world.fake, run.id)
        llm = PricedReplay(BudgetGuard(settings))
        with capture_logs() as events:
            done = run_fusion(run.id, llm=llm, google=google, http=world.fake.client())
        assert done.status is RunStatus.succeeded, done.error
        calls = repo.list_llm_calls(run.id)
        assert calls and {c.model for c in calls} == {OPUS}                      # vision AND review passes
        steps = {c.step for c in calls}
        assert any(s.startswith("perceive") for s in steps) and any("critique" in s or "review" in s for s in steps)
        report = model_repo.get_run_report(run.id)
        assert report.model == OPUS and done.inputs["models"]["vision"] == OPUS
        assert world.segment().scene.generation.models == {"vision": OPUS, "review": OPUS}
        assert {e["model"] for e in events if e["event"] == "llm.call"} == {OPUS}
        # priced at Opus rates (4 / 20 per Mtok), not the default's
        prices = price_for(OPUS, settings)
        assert all(c.cost_usd == pytest.approx(c.input_tokens * prices.input_usd_per_mtok / 1e6
                                               + c.output_tokens * prices.output_usd_per_mtok / 1e6, abs=1e-6)
                   for c in calls)
    finally:
        reset_settings()


def test_fusion_run_without_a_choice_uses_the_default_everywhere(test_db, tmp_path, monkeypatch):
    world = new_world(tmp_path, monkeypatch, pano_spacing_m=15.0)
    try:
        run = world.run(overrides=FORWARD_BACK)
        default = get_settings().vision_model
        assert run.inputs["models"]["source"] == "default"
        assert {c.model for c in repo.list_llm_calls(run.id)} == {default}
        assert model_repo.get_run_report(run.id).model == default
    finally:
        reset_settings()
