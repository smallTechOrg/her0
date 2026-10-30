"""hero.llm against the REAL Anthropic API (real-call tests are marked ``live``; run with ``-m live``) (workspace header, forced tool-use, pricing, budget).

Structure-only assertions on real outputs. The budget tests never reach Anthropic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PIL import Image
from pydantic import BaseModel, Field

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.llm.anthropic_client import AnthropicClient
from hero.llm.budget import BudgetGuard
from hero.llm.errors import BudgetExceeded, UpstreamError
from hero.llm.pricing import load_price_table
from hero.pipeline.steps import image_payload
from hero.scene.schema import FootpathSide, LatLon, RunStatus


class ColourReport(BaseModel):
    dominant_colour: str = Field(description="one lowercase colour word")
    width_px: int = Field(ge=1)
    shapes: list[str]


@pytest.fixture()
def run_id(test_db):
    seg = repo.create_segment(SegmentCreate(name="LLM client test", city="Bengaluru",
                                            start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.97467, lon=77.64058),
                                            footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"test": True})
    yield run.id
    repo.update_run(run.id, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                           error={"code": "test", "message": "test run retired", "step": "test"}))


def _client(settings=None) -> AnthropicClient:
    settings = settings or get_settings()
    return AnthropicClient(settings, BudgetGuard(settings), load_price_table(settings))


def _red_square() -> Image.Image:
    img = Image.new("RGB", (160, 160), (200, 20, 20))
    for x in range(40, 120):
        for y in range(40, 120):
            img.putpixel((x, y), (20, 20, 200))
    return img


@pytest.mark.live
def test_vision_json_returns_schema_valid_object_priced_and_recorded(run_id):
    settings = get_settings()
    image = image_payload(_red_square(), "a synthetic test image", 256)

    report, usage = _client(settings).vision_json(
        run_id=run_id, step="test_vision", images=[image], schema=ColourReport, model=settings.vision_model,
        prompt_name="test", max_tokens=300,
        prompt="Describe the attached image: its dominant colour as one lowercase word, its width in pixels "
               "(it is 160), and a list of the simple shapes you see.",
    )

    assert isinstance(report, ColourReport)
    assert report.dominant_colour.strip() and report.width_px > 0
    assert usage.model == settings.vision_model and usage.calls in (1, 2)
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    prices = load_price_table(settings)
    calls = repo.list_llm_calls(run_id)
    assert len(calls) == usage.calls
    for call in calls:
        assert call.cost_usd == prices.cost_usd(call.model, call.input_tokens, call.output_tokens)
        assert call.cost_usd > 0 and call.latency_ms > 0 and call.step == "test_vision"
    assert usage.cost_usd == pytest.approx(sum(c.cost_usd for c in calls), abs=1e-6)
    assert repo.run_spend_usd(run_id) == pytest.approx(usage.cost_usd, abs=1e-6)


def test_budget_guard_blocks_call_that_would_exceed_total_before_any_request(run_id, monkeypatch):
    settings = get_settings()
    guard = BudgetGuard(settings, total_spent_fn=lambda: settings.claude_budget_usd - 0.0001)
    client = AnthropicClient(settings, guard, load_price_table(settings))

    with pytest.raises(BudgetExceeded) as exc:
        client.vision_json(run_id=run_id, step="blocked", images=[], prompt="hi", schema=ColourReport,
                           model=settings.vision_model, prompt_name="test")

    assert exc.value.scope == "total"
    assert "HERO_CLAUDE_BUDGET_USD" in exc.value.message
    assert repo.list_llm_calls(run_id) == []


def test_budget_guard_blocks_call_that_would_exceed_per_run_budget(run_id):
    settings = get_settings()
    guard = BudgetGuard(settings, run_spent_fn=lambda _rid: settings.run_budget_usd)
    client = AnthropicClient(settings, guard, load_price_table(settings))

    with pytest.raises(BudgetExceeded) as exc:
        client.vision_json(run_id=run_id, step="blocked", images=[], prompt="hi", schema=ColourReport,
                           model=settings.vision_model, prompt_name="test")

    assert exc.value.scope == "run"
    assert repo.list_llm_calls(run_id) == []


def test_unknown_model_slug_is_refused_before_the_request(run_id):
    settings = get_settings()

    with pytest.raises(Exception) as exc:
        _client(settings).vision_json(run_id=run_id, step="x", images=[], prompt="hi", schema=ColourReport,
                                      model="claude-made-up-9", prompt_name="test")

    assert "claude-made-up-9" in str(exc.value)
    assert repo.list_llm_calls(run_id) == []


@pytest.mark.live
def test_wrong_api_key_surfaces_as_actionable_upstream_error_and_records_failed_call(run_id):
    settings = get_settings().model_copy(update={"anthropic_api_key": type(get_settings().anthropic_api_key)("sk-ant-invalid")})

    with pytest.raises(UpstreamError) as exc:
        _client(settings).vision_json(run_id=run_id, step="bad_key", images=[], prompt="hi", schema=ColourReport,
                                      model=settings.vision_model, prompt_name="test", max_tokens=50)

    assert "HERO_ANTHROPIC_API_KEY" in exc.value.message
    calls = repo.list_llm_calls(run_id)
    assert len(calls) == 1 and calls[0].ok is False and calls[0].cost_usd == 0.0


@pytest.mark.live
@pytest.mark.parametrize("mode", ["auto", "json_text"])
def test_vision_json_real_call_in_each_structured_mode_returns_valid_object(run_id, mode):
    settings = get_settings()
    image = image_payload(_red_square(), "a synthetic test image", 256)

    report, usage = _client(settings).vision_json(
        run_id=run_id, step=f"test_{mode}", images=[image], schema=ColourReport, model=settings.vision_model,
        prompt_name="test", max_tokens=300, structured_mode=mode,
        prompt="Describe the attached image: dominant colour as one lowercase word, width in pixels (160), shapes.",
    )

    assert isinstance(report, ColourReport) and report.width_px > 0
    assert usage.cost_usd > 0 and all(c.step == f"test_{mode}" for c in repo.list_llm_calls(run_id))
