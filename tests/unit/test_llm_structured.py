"""Structured-output modes, repair retry, workspace header, cost recording and budget guards.

No network: the real Anthropic SDK runs over an ``httpx.MockTransport``."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2 as httpx
import pytest
from pydantic import BaseModel, Field

from hero.config import Settings
from hero.db import repo
from hero.db.repo import SegmentCreate
from hero.llm import anthropic_client as ac
from hero.llm.anthropic_client import AnthropicClient, parse_json_text
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMImage
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UpstreamError
from hero.llm.pricing import load_price_table
from hero.scene.schema import FootpathSide, LatLon, RunStatus

MODEL = "claude-sonnet-4-5"
NEW_MODEL = "claude-sonnet-5-5"
GOOD = {"colour": "blue", "count": 3}


class Report(BaseModel):
    colour: str
    count: int = Field(ge=0)


def _settings(**kw) -> Settings:
    base = dict(anthropic_api_key="sk-test", anthropic_workspace_id="wrkspc_test", google_maps_api_key="g",
                admin_password="p", claude_budget_usd=50.0)
    base.update(kw)
    return Settings(**base)


def _tool_reply(payload, tokens=(1000, 100)):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": MODEL, "stop_reason": "tool_use",
            "stop_sequence": None, "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "emit_result", "input": payload}]}


def _text_reply(text, tokens=(1000, 100)):
    return {"id": "msg_2", "type": "message", "role": "assistant", "model": MODEL, "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
            "content": [{"type": "text", "text": text}]}


def _error(status, message):
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}, status


class Transport:
    """Scripted replies, one per request; records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, tuple):
            return httpx.Response(reply[1], json=reply[0])
        return httpx.Response(200, json=reply)

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture(autouse=True)
def _clean_rejections():
    ac.reset_forced_tool_rejections()
    yield
    ac.reset_forced_tool_rejections()


@pytest.fixture()
def run_id(test_db):
    seg = repo.create_segment(SegmentCreate(name="LLM structured", city="Bengaluru",
                                            start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.97467, lon=77.64058),
                                            footpath_side=FootpathSide.left))
    run = repo.create_run(seg.id, {"test": True})
    yield run.id
    repo.update_run(run.id, repo.RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                           error={"code": "test", "message": "retired", "step": "test"}))


def _make(transport, settings=None, guard=None):
    settings = settings or _settings()
    http = httpx.Client(transport=httpx.MockTransport(transport))
    return AnthropicClient(settings, guard or BudgetGuard(settings), load_price_table(settings), http_client=http)


def _call(client, run_id, model=MODEL, mode=None, images=()):
    return client.vision_json(run_id=run_id, step="t", images=list(images), prompt="describe", schema=Report,
                              model=model, prompt_name="t", structured_mode=mode, max_tokens=500)


def _img():
    return LLMImage(data=b"\xff\xd8fake", media_type="image/jpeg", caption="c", width_px=64, height_px=64)


# ---------------------------------------------------------------- modes

def test_forced_tool_sends_tool_choice_and_returns_validated_object(run_id):
    t = Transport(_tool_reply(GOOD))
    obj, usage = _call(_make(t), run_id, mode="forced_tool", images=[_img()])

    assert obj == Report(**GOOD) and usage.calls == 1
    body = t.body(0)
    assert body["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert body["tools"][0]["input_schema"]["required"] == ["colour", "count"]


def test_auto_tool_uses_tool_choice_auto_and_system_instruction(run_id):
    t = Transport(_tool_reply(GOOD))
    _call(_make(t), run_id, mode="auto_tool")

    body = t.body(0)
    assert body["tool_choice"] == {"type": "auto"}
    assert "emit_result" in body["system"] and "exactly once" in body["system"]


def test_json_text_sends_no_tools_and_parses_fenced_reply(run_id):
    t = Transport(_text_reply('Sure:\n```json\n{"colour": "red", "count": 1}\n```'))
    obj, _ = _call(_make(t), run_id, mode="json_text")

    assert obj == Report(colour="red", count=1)
    body = t.body(0)
    assert "tools" not in body and "tool_choice" not in body
    assert '"colour"' in body["system"]


def test_default_mode_comes_from_settings(run_id):
    t = Transport(_text_reply(json.dumps(GOOD)))
    _call(_make(t, settings=_settings(structured_mode="json_text")), run_id)

    assert "tools" not in t.body(0)


def test_unknown_mode_is_rejected_before_any_request(run_id):
    t = Transport()
    with pytest.raises(ValueError, match="structured_mode"):
        _call(_make(t), run_id, mode="telepathy")
    assert t.requests == []


# ---------------------------------------------------------------- auto fallbacks

def test_auto_falls_back_to_auto_tool_on_tool_choice_400_and_remembers_the_slug(run_id):
    t = Transport(_error(400, "tool_choice: forced tool use is not supported for this model"), _tool_reply(GOOD),
                  _tool_reply(GOOD))
    client = _make(t)

    obj, usage = _call(client, run_id, model=NEW_MODEL, mode="auto")
    _call(client, run_id, model=NEW_MODEL, mode="auto")

    assert obj == Report(**GOOD)
    assert [t.body(i)["tool_choice"]["type"] for i in range(3)] == ["tool", "auto", "auto"]
    assert ac.forced_tool_rejected(NEW_MODEL) and not ac.forced_tool_rejected(MODEL)
    assert client.supported_modes(NEW_MODEL) == ["auto_tool", "json_text"]
    assert client.supported_modes(MODEL) == ["forced_tool", "auto_tool", "json_text"]
    calls = repo.list_llm_calls(run_id)
    assert [c.ok for c in calls] == [False, True, True] and calls[0].cost_usd == 0.0


def test_auto_without_tool_use_block_repairs_once_in_json_text(run_id):
    ac.mark_forced_tool_rejected(MODEL)
    t = Transport(_text_reply("I will not call a tool"), _text_reply(json.dumps(GOOD)))
    obj, usage = _call(_make(t), run_id, mode="auto")

    assert obj == Report(**GOOD) and usage.calls == 2
    assert "tools" not in t.body(1)


def test_non_tool_choice_400_is_not_swallowed_and_records_failed_call(run_id):
    t = Transport(_error(400, "max_tokens too large"))
    with pytest.raises(UpstreamError) as exc:
        _call(_make(t), run_id, mode="auto")

    assert exc.value.status == 400 and len(t.requests) == 1
    assert not ac.forced_tool_rejected(MODEL)
    assert [c.ok for c in repo.list_llm_calls(run_id)] == [False]


def test_explicit_forced_tool_400_is_not_retried_in_another_mode(run_id):
    t = Transport(_error(400, "tool_choice not supported"))
    with pytest.raises(UpstreamError):
        _call(_make(t), run_id, model=NEW_MODEL, mode="forced_tool")
    assert len(t.requests) == 1


# ---------------------------------------------------------------- schema repair

@pytest.mark.parametrize("mode", ["forced_tool", "auto_tool"])
def test_tool_mode_schema_failure_gets_one_reask_quoting_the_error(run_id, mode):
    t = Transport(_tool_reply({"colour": "blue", "count": -1}), _tool_reply(GOOD))
    obj, usage = _call(_make(t), run_id, mode=mode)

    assert obj == Report(**GOOD) and usage.calls == 2
    turns = t.body(1)["messages"]
    assert turns[-2]["content"][0]["type"] == "tool_use" and turns[-2]["content"][0]["id"] == "toolu_1"
    assert "count" in turns[-1]["content"][1]["text"]
    calls = repo.list_llm_calls(run_id)
    assert [c.ok for c in calls] == [False, True] and "schema" in (calls[0].error or "")


def test_json_text_unparseable_then_valid_is_repaired(run_id):
    t = Transport(_text_reply("no json here"), _text_reply(json.dumps(GOOD)))
    obj, usage = _call(_make(t), run_id, mode="json_text")

    assert obj == Report(**GOOD) and usage.calls == 2
    assert t.body(1)["messages"][-1]["role"] == "user"


def test_two_schema_failures_raise_and_record_both_calls(run_id):
    bad = {"colour": "blue"}
    t = Transport(_tool_reply(bad), _tool_reply(bad), _tool_reply(GOOD))
    with pytest.raises(SchemaValidationError):
        _call(_make(t), run_id, mode="forced_tool")

    assert len(t.requests) == 2
    assert [c.ok for c in repo.list_llm_calls(run_id)] == [False, False]


def test_missing_tool_use_in_explicit_forced_mode_goes_through_reask(run_id):
    t = Transport(_text_reply("oops"), _tool_reply(GOOD))
    obj, _ = _call(_make(t), run_id, mode="forced_tool")
    assert obj == Report(**GOOD)


# ---------------------------------------------------------------- parse_json_text

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": {"b": [1, 2]}}\n```', {"a": {"b": [1, 2]}}),
    ('Here you go: {"a": 1} hope it helps {"b": 2}', {"a": 1}),
    ('```\n{"a": "}"}\n```', {"a": "}"}),
])
def test_parse_json_text_finds_the_first_object(text, expected):
    assert parse_json_text(text) == expected


@pytest.mark.parametrize("text", ["", "no braces", "{broken: json}", "```json\n```"])
def test_parse_json_text_rejects_non_json(text):
    with pytest.raises(ValueError):
        parse_json_text(text)


# ---------------------------------------------------------------- header, cost, budget

def test_workspace_header_is_on_every_request_including_repairs(run_id):
    t = Transport(_tool_reply({"colour": "x"}), _tool_reply(GOOD))
    _call(_make(t), run_id, mode="forced_tool")

    assert len(t.requests) == 2
    assert {r.headers["anthropic-workspace-id"] for r in t.requests} == {"wrkspc_test"}


def test_usage_and_recorded_rows_are_priced_from_the_table(run_id):
    t = Transport(_tool_reply({"colour": "x"}, tokens=(2000, 50)), _tool_reply(GOOD, tokens=(3000, 200)))
    _, usage = _call(_make(t), run_id, mode="forced_tool")

    prices = load_price_table(_settings())
    calls = repo.list_llm_calls(run_id)
    assert [c.cost_usd for c in calls] == [prices.cost_usd(MODEL, 2000, 50), prices.cost_usd(MODEL, 3000, 200)]
    assert usage.input_tokens == 5000 and usage.output_tokens == 250
    assert usage.cost_usd == pytest.approx(sum(c.cost_usd for c in calls), abs=1e-6)
    assert all("mode=forced_tool" in c.request_summary for c in calls)
    assert repo.run_spend_usd(run_id) == pytest.approx(usage.cost_usd, abs=1e-6)


def test_sdk_retries_transient_500_then_succeeds(run_id):
    t = Transport(_error(500, "overloaded"), _tool_reply(GOOD))
    obj, usage = _call(_make(t), run_id, mode="forced_tool")

    assert obj == Report(**GOOD) and len(t.requests) == 2 and usage.calls == 1


def test_auth_error_is_actionable_and_recorded_at_zero_cost(run_id):
    t = Transport(_error(401, "invalid x-api-key"))
    with pytest.raises(UpstreamError) as exc:
        _call(_make(t), run_id, mode="forced_tool")

    assert "HERO_ANTHROPIC_API_KEY" in exc.value.message and len(t.requests) == 1
    calls = repo.list_llm_calls(run_id)
    assert len(calls) == 1 and calls[0].cost_usd == 0.0 and not calls[0].ok


def test_global_budget_blocks_before_any_request(run_id):
    settings = _settings()
    t = Transport(_tool_reply(GOOD))
    guard = BudgetGuard(settings, total_spent_fn=lambda: settings.claude_budget_usd - 0.0001)
    with pytest.raises(BudgetExceeded) as exc:
        _call(_make(t, settings, guard), run_id, mode="forced_tool")

    assert exc.value.scope == "total" and t.requests == [] and repo.list_llm_calls(run_id) == []


def test_global_stop_refuses_once_nothing_remains(run_id):
    settings = _settings()
    t = Transport(_tool_reply(GOOD))
    guard = BudgetGuard(settings, total_spent_fn=lambda: settings.claude_budget_usd)
    assert guard.remaining_usd() == 0.0
    with pytest.raises(BudgetExceeded) as exc:
        _call(_make(t, settings, guard), run_id, mode="forced_tool")

    assert exc.value.scope == "total" and t.requests == []


def test_a_run_is_not_stopped_by_its_own_spend_even_above_the_old_one_dollar_cap(run_id):
    settings = _settings()
    guard = BudgetGuard(settings, run_spent_fn=lambda _r: 7.50, total_spent_fn=lambda: 7.50)
    t = Transport(_tool_reply(GOOD))
    obj, _usage = _call(_make(t, settings, guard), run_id, mode="forced_tool")

    assert obj.colour == "blue" and len(t.requests) == 1
    assert not hasattr(guard, "budget_run_usd") and not hasattr(settings, "run_budget_usd")
    with pytest.raises(TypeError):
        BudgetGuard(settings, run_budget_usd=0.05)  # type: ignore[call-arg]


def test_budget_is_rechecked_before_the_reask_request(run_id):
    settings = _settings()
    spent = {"total": 0.0}
    guard = BudgetGuard(settings, total_spent_fn=lambda: spent["total"])
    t = Transport(_tool_reply({"colour": "x"}), _tool_reply(GOOD))
    client = _make(t, settings, guard)
    original_record = guard.record

    def record_and_exhaust(call):
        spent["total"] = settings.claude_budget_usd
        return original_record(call)

    guard.record = record_and_exhaust  # type: ignore[method-assign]
    with pytest.raises(BudgetExceeded):
        _call(client, run_id, mode="forced_tool")

    assert len(t.requests) == 1


def test_unknown_model_refused_before_request(run_id):
    t = Transport()
    with pytest.raises(Exception, match="claude-made-up-9"):
        _call(_make(t), run_id, model="claude-made-up-9")
    assert t.requests == []


def test_check_structured_rejects_unknown_mode_without_any_request(capsys):
    from hero.tools import check_structured

    assert check_structured.main(["--modes", "bogus"]) == 2
    assert "unknown mode" in capsys.readouterr().out
