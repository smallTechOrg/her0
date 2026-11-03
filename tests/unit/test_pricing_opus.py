"""claude-opus-5-5 price row (sourced), ``price_for`` and the budget guard pricing each call with the model used."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from hero.config import PROJECT_ROOT, Settings
from hero.llm.anthropic_client import AnthropicClient
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMImage
from hero.llm.errors import BudgetExceeded, UnknownModelError
from hero.llm.pricing import PriceTable, price_for

SHIPPED = PROJECT_ROOT / "src" / "hero" / "llm" / "prices.json"


def _settings(**kw) -> Settings:
    base = dict(anthropic_api_key="sk-test", anthropic_workspace_id="wrkspc_test", google_maps_api_key="g",
                admin_password="p", claude_budget_usd=50.0)
    base.update(kw)
    return Settings(**base)


def test_opus_row_is_the_documented_price_with_source_and_date():
    row = json.loads(SHIPPED.read_text())["claude-opus-5-5"]
    assert (row["input_usd_per_mtok"], row["output_usd_per_mtok"]) == (4.0, 20.0)  # platform.claude.com pricing page
    assert row["source_url"].startswith("https://") and row["verified_on"] == "2026-10-30"


def test_price_for_returns_the_row_and_refuses_unknown_models():
    price = price_for("claude-opus-5-5", _settings())
    assert (price.input_usd_per_mtok, price.output_usd_per_mtok) == (4.0, 20.0)
    assert price.source_url and price.verified_on
    with pytest.raises(UnknownModelError):
        price_for("claude-nope-1", _settings())


def test_opus_costs_more_than_sonnet_per_token():
    table = PriceTable.load(SHIPPED)
    assert table.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == 24.0
    assert table.cost_usd("claude-opus-5-5", 10_000, 1_000) > table.cost_usd("claude-sonnet-5-5", 10_000, 1_000)


# ---- the guard prices the call with the chosen model (two prices)

TWO = {"cheap-model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
       "pricey-model": {"input_usd_per_mtok": 10.0, "output_usd_per_mtok": 50.0}}


def _table(tmp_path) -> PriceTable:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(TWO))
    return PriceTable.load(path)


def test_guard_estimate_uses_the_chosen_models_price(tmp_path):
    guard = BudgetGuard(_settings(), prices=_table(tmp_path), total_spent_fn=lambda: 0.0, run_spent_fn=lambda _r: 0.0)
    assert guard.estimate_usd("cheap-model", 1000, 2000) == pytest.approx(0.011)
    assert guard.estimate_usd("pricey-model", 1000, 2000) == pytest.approx(0.11)


def test_same_remaining_budget_blocks_the_pricey_model_but_not_the_cheap_one(tmp_path):
    settings = _settings(claude_budget_usd=10.0)
    guard = BudgetGuard(settings, prices=_table(tmp_path), total_spent_fn=lambda: 9.95, run_spent_fn=lambda _r: 0.0)
    ok = guard.check_model_call("r", "cheap-model", 1000, 2000)       # $0.011 estimate < $0.05 left
    assert ok.estimate_usd == pytest.approx(0.011)
    with pytest.raises(BudgetExceeded) as exc:                         # $0.11 estimate > $0.05 left
        guard.check_model_call("r", "pricey-model", 1000, 2000)
    assert exc.value.estimate_usd == pytest.approx(0.11) and exc.value.scope == "total"


class _Out(BaseModel):
    ok: bool


class _Rec:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "tool_use",
            "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [{"type": "tool_use", "id": "t", "name": "emit_result", "input": {"ok": True}}]})


class _MemGuard(BudgetGuard):
    """Real guard logic, spend held in memory (no database)."""

    def __init__(self, settings, prices, spent):
        super().__init__(settings, prices=prices, total_spent_fn=lambda: spent, run_spent_fn=lambda _r: 0.0)
        self.records, self.estimates = [], []

    def check_before_call(self, run_id, estimate_usd):
        self.estimates.append(estimate_usd)
        return super().check_before_call(run_id, estimate_usd)

    def record(self, call):
        self.records.append(call)


def _client(tmp_path, spent, budget=10.0):
    settings = _settings(claude_budget_usd=budget)
    transport = _Rec()
    guard = _MemGuard(settings, _table(tmp_path), spent)
    client = AnthropicClient(settings, guard, _table(tmp_path),
                             http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    return client, guard, transport


def _call(client, model):
    img = LLMImage(data=b"\xff\xd8x", media_type="image/jpeg", caption="c", width_px=64, height_px=64)
    return client.vision_json(run_id="r", step="s", images=[img], prompt="p", schema=_Out, model=model,
                              prompt_name="t", max_tokens=2000, structured_mode="auto_tool")


def test_real_client_guard_blocks_at_the_chosen_models_price_before_any_request(tmp_path):
    client, guard, transport = _client(tmp_path, spent=9.95)          # $0.05 left
    _, usage = _call(client, "cheap-model")                            # estimate ~ $0.01 -> allowed
    assert usage.model == "cheap-model" and guard.records[0].model == "cheap-model"
    assert guard.estimates[0] < 0.05 and len(transport.requests) == 1
    with pytest.raises(BudgetExceeded):                                # estimate ~ $0.10 -> refused
        _call(client, "pricey-model")
    assert guard.estimates[1] > 0.05 and guard.estimates[1] == pytest.approx(guard.estimates[0] * 10, rel=0.02)
    assert len(transport.requests) == 1                                # the refused call never reached the wire


def test_recorded_cost_uses_each_models_own_price(tmp_path):
    client, guard, _ = _client(tmp_path, spent=0.0)
    _call(client, "cheap-model")
    _call(client, "pricey-model")
    cheap, pricey = guard.records
    assert cheap.cost_usd == pytest.approx(1000 * 1 / 1e6 + 100 * 5 / 1e6)
    assert pricey.cost_usd == pytest.approx(10 * cheap.cost_usd)
