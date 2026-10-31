"""Anthropic adapter: workspace header on every request, SDK retries (3, exponential backoff on
429/5xx/timeouts), 120 s timeout, three structured-output modes, budget check BEFORE every request,
priced ``llm_calls`` row + ``llm.call`` log AFTER it.

Structured modes (spec/architecture.md -> Stack -> LLM): ``forced_tool`` (tool_choice = the tool),
``auto_tool`` (tool_choice auto + a system instruction), ``json_text`` (no tools; one JSON object in
the reply, fenced blocks tolerated). ``auto`` starts in ``forced_tool`` unless the slug is known to
reject it (HTTP 400 mentioning ``tool_choice``; remembered per process), falls to ``auto_tool``, and
falls to ``json_text`` once if the reply carries no ``tool_use`` block. These mode fallbacks do not
use the schema re-ask. Schema failure handling (spec/agent.md #18): one re-ask quoting the
validation error; a second failure raises ``SchemaValidationError``. Auth / model errors are never
retried and surface as ``UpstreamError`` with an actionable message.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Sequence, TypeVar

import anthropic
from pydantic import BaseModel

from hero.config import STRUCTURED_MODES, Settings
from hero.db.repo import LlmCallCreate
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMImage, LLMUsage
from hero.llm.errors import SchemaValidationError, UpstreamError
from hero.llm.pricing import PriceTable, estimate_image_tokens, estimate_text_tokens

T = TypeVar("T", bound=BaseModel)

ANTHROPIC_TIMEOUT_S = 120.0
ANTHROPIC_MAX_RETRIES = 3
TOOL_NAME = "emit_result"
CONCRETE_MODES = ("forced_tool", "auto_tool", "json_text")
REASK_TEMPLATE = (
    "Your previous answer did not validate against the required schema. Validation errors:\n{errors}\n"
    "Answer again with a corrected, complete answer that satisfies the schema exactly."
)
AUTO_TOOL_INSTRUCTION = f"Answer by calling the `{TOOL_NAME}` tool exactly once. Do not reply with prose."
JSON_TEXT_INSTRUCTION = (
    "Reply with exactly one JSON object matching this JSON schema and nothing else (no prose; a single "
    "```json fenced block is tolerated):\n{schema}"
)

# Slugs that answered HTTP 400 about ``tool_choice`` in this process (filled at runtime and seeded by
# ``hero.tools.check_structured``). ``auto`` skips ``forced_tool`` for these.
_FORCED_TOOL_REJECTED: set[str] = set()


def mark_forced_tool_rejected(model: str) -> None:
    _FORCED_TOOL_REJECTED.add(model)


def forced_tool_rejected(model: str) -> bool:
    return model in _FORCED_TOOL_REJECTED


def reset_forced_tool_rejections() -> None:
    _FORCED_TOOL_REJECTED.clear()


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_json_text(text: str) -> Any:
    """Parse the first JSON object in ``text`` (fenced blocks tolerated). Raises ValueError."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    last = "no JSON object found in the reply"
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate[start:])
        except ValueError as exc:
            last = f"invalid JSON: {exc}"
            continue
        return obj
    raise ValueError(last)


def _summarise_response(obj: Any) -> str:
    if isinstance(obj, dict):
        parts = []
        for key, value in obj.items():
            if isinstance(value, list):
                parts.append(f"{key}={len(value)}")
            elif isinstance(value, (bool, int, float)):
                parts.append(f"{key}={value}")
        return ", ".join(parts) or "object"
    return type(obj).__name__


def _tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    json_schema = schema.model_json_schema()
    json_schema.pop("title", None)
    return json_schema


class _Reply:
    __slots__ = ("raw", "text", "tool_use_id", "has_tool_use", "tokens_in", "tokens_out", "latency_ms")

    def __init__(self, raw, text, tool_use_id, has_tool_use, tokens_in, tokens_out, latency_ms):
        self.raw, self.text, self.tool_use_id, self.has_tool_use = raw, text, tool_use_id, has_tool_use
        self.tokens_in, self.tokens_out, self.latency_ms = tokens_in, tokens_out, latency_ms


class AnthropicClient:
    def __init__(self, settings: Settings, guard: BudgetGuard, prices: PriceTable, *,
                 http_client: Any = None):
        self._guard = guard
        self._prices = prices
        self._default_mode = settings.structured_mode
        extra: dict[str, Any] = {"http_client": http_client} if http_client is not None else {}
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            default_headers={"anthropic-workspace-id": settings.anthropic_workspace_id.get_secret_value()},
            timeout=ANTHROPIC_TIMEOUT_S,
            max_retries=ANTHROPIC_MAX_RETRIES,
            **extra,
        )

    # ------------------------------------------------------------ public

    def supported_modes(self, model: str) -> list[str]:
        """Concrete modes worth trying for ``model`` (``forced_tool`` dropped once the slug rejected it)."""
        return [m for m in CONCRETE_MODES if not (m == "forced_tool" and forced_tool_rejected(model))]

    def vision_json(self, *, run_id: str, step: str, images: Sequence[LLMImage], prompt: str,
                    schema: type[T], model: str, prompt_name: str, system: str | None = None,
                    max_tokens: int = 4096, structured_mode: str | None = None) -> tuple[T, LLMUsage]:
        self._prices.require(model)
        requested = (structured_mode or self._default_mode or "auto").strip().lower()
        if requested not in STRUCTURED_MODES:
            raise ValueError(f"structured_mode must be one of {', '.join(STRUCTURED_MODES)} (got {requested!r})")
        auto = requested == "auto"
        mode = (("auto_tool" if forced_tool_rejected(model) else "forced_tool") if auto else requested)

        json_schema = _tool_schema(schema)
        tool = {"name": TOOL_NAME, "description": f"Return the {schema.__name__} for this task.",
                "input_schema": json_schema}
        schema_text = json.dumps(json_schema, separators=(",", ":"))
        base_messages: list[dict[str, Any]] = [{"role": "user", "content": self._content(images, prompt)}]
        messages = base_messages
        image_bytes = sum(len(i.data) for i in images)
        request_summary = f"{prompt_name}: images={len(images)} image_bytes={image_bytes} prompt_chars={len(prompt)}"
        estimate = self._estimate_usd(model, images, prompt + (system or "") + schema_text, max_tokens)

        totals = {"input": 0, "output": 0, "cost": 0.0, "latency": 0, "calls": 0}
        last_error: str | None = None
        attempt = 1
        fallbacks = 0
        while attempt <= 2:
            summary = f"{request_summary} mode={mode}" + (" (re-ask)" if attempt == 2 else "")
            self._guard.check_before_call(run_id, estimate)
            try:
                reply = self._request(model=model, system=self._system_for(mode, system, schema_text),
                                      messages=messages, tool=tool, mode=mode, max_tokens=max_tokens,
                                      run_id=run_id, step=step, request_summary=summary)
            except UpstreamError as exc:
                if auto and mode == "forced_tool" and exc.detail.get("tool_choice_rejected") and fallbacks < 2:
                    mark_forced_tool_rejected(model)
                    mode, fallbacks = "auto_tool", fallbacks + 1
                    continue
                raise
            cost = self._prices.cost_usd(model, reply.tokens_in, reply.tokens_out)
            totals["input"] += reply.tokens_in
            totals["output"] += reply.tokens_out
            totals["cost"] += cost
            totals["latency"] += reply.latency_ms
            totals["calls"] += 1

            def _record(ok: bool, error: str | None, raw: Any) -> None:
                self._guard.record(LlmCallCreate(
                    run_id=run_id, step=step, model=model, input_tokens=reply.tokens_in,
                    output_tokens=reply.tokens_out, cost_usd=cost, latency_ms=reply.latency_ms, ok=ok,
                    error=error, request_summary=summary, response_summary=_summarise_response(raw),
                    structured_mode=mode,
                ))

            if mode == "auto_tool" and auto and not reply.has_tool_use and fallbacks < 2:
                _record(False, "no tool_use block in auto_tool reply; repairing in json_text", None)
                mode, messages, fallbacks = "json_text", base_messages, fallbacks + 1
                continue

            try:
                raw = parse_json_text(reply.text) if mode == "json_text" else reply.raw
                parsed = schema.model_validate(raw)
            except ValueError as exc:  # pydantic ValidationError is a ValueError
                last_error = str(exc)[:1500]
                _record(False, f"schema: {last_error[:400]}", reply.raw)
                messages = base_messages + self._reask_turns(mode, reply, last_error)
                attempt += 1
                continue
            _record(True, None, raw)
            return parsed, LLMUsage(model=model, input_tokens=totals["input"], output_tokens=totals["output"],
                                    cost_usd=round(totals["cost"], 6), latency_ms=totals["latency"],
                                    calls=totals["calls"])
        raise SchemaValidationError(
            f"{step}: the model's output did not match {schema.__name__} after one re-ask: {last_error}",
            raw=None,
        )

    # ------------------------------------------------------------ internals

    @staticmethod
    def _system_for(mode: str, system: str | None, schema_text: str) -> str | None:
        if mode == "auto_tool":
            extra = AUTO_TOOL_INSTRUCTION
        elif mode == "json_text":
            extra = JSON_TEXT_INSTRUCTION.format(schema=schema_text)
        else:
            return system
        return f"{system}\n\n{extra}" if system else extra

    @staticmethod
    def _reask_turns(mode: str, reply: _Reply, errors: str) -> list[dict[str, Any]]:
        if mode == "json_text":
            return [{"role": "assistant", "content": reply.text.strip() or "{}"},
                    {"role": "user", "content": REASK_TEMPLATE.format(errors=errors)}]
        tool_id = reply.tool_use_id or "toolu_prev"
        if not reply.has_tool_use:
            return [{"role": "assistant", "content": reply.text.strip() or "(no answer)"},
                    {"role": "user", "content": REASK_TEMPLATE.format(errors=errors)}]
        return [
            {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": TOOL_NAME,
                                               "input": reply.raw if isinstance(reply.raw, dict) else {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "rejected: schema invalid",
                 "is_error": True},
                {"type": "text", "text": REASK_TEMPLATE.format(errors=errors)},
            ]},
        ]

    def _estimate_usd(self, model: str, images: Sequence[LLMImage], text: str, max_tokens: int) -> float:
        tokens_in = sum(estimate_image_tokens(i.width_px, i.height_px) for i in images) + estimate_text_tokens(text)
        return self._prices.cost_usd(model, tokens_in + 200, max_tokens)

    @staticmethod
    def _content(images: Sequence[LLMImage], prompt: str) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for index, image in enumerate(images, start=1):
            content.append({"type": "text", "text": f"Image {index}: {image.caption}"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            }})
        content.append({"type": "text", "text": prompt})
        return content

    def _fail(self, *, run_id: str, step: str, model: str, started: float, message: str,
              request_summary: str, mode: str) -> None:
        self._guard.record(LlmCallCreate(
            run_id=run_id, step=step, model=model, input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=int((time.perf_counter() - started) * 1000), ok=False, error=message[:400],
            request_summary=request_summary, response_summary="", structured_mode=mode,
        ))

    def _request(self, *, model: str, system: str | None, messages: list[dict[str, Any]], tool: dict[str, Any],
                 mode: str, max_tokens: int, run_id: str, step: str, request_summary: str) -> _Reply:
        started = time.perf_counter()
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, messages=messages)
        if mode == "forced_tool":
            kwargs.update(tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME})
        elif mode == "auto_tool":
            kwargs.update(tools=[tool], tool_choice={"type": "auto"})
        if system:
            kwargs["system"] = system
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            message = self._describe_status_error(exc, model)
            self._fail(run_id=run_id, step=step, model=model, started=started, message=message,
                       request_summary=request_summary, mode=mode)
            err = UpstreamError(message, kind="anthropic", status=exc.status_code)
            if exc.status_code == 400 and "tool_choice" in str(exc).lower():
                err.detail["tool_choice_rejected"] = True
            raise err from exc
        except anthropic.APIConnectionError as exc:
            message = f"Anthropic request failed after retries ({type(exc).__name__}): check network / Anthropic status."
            self._fail(run_id=run_id, step=step, model=model, started=started, message=message,
                       request_summary=request_summary, mode=mode)
            raise UpstreamError(message, kind="anthropic") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage_in = int(getattr(response.usage, "input_tokens", 0) or 0)
        usage_out = int(getattr(response.usage, "output_tokens", 0) or 0)
        raw: Any = None
        tool_id: str | None = None
        has_tool = False
        texts: list[str] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "tool_use" and not has_tool:
                raw, tool_id, has_tool = block.input, getattr(block, "id", None), True
            elif kind == "text":
                texts.append(block.text)
        if not has_tool:
            raw = {"_error": f"no tool_use block (stop_reason={response.stop_reason})"}
        return _Reply(raw, "\n".join(texts), tool_id, has_tool, usage_in, usage_out, latency_ms)

    @staticmethod
    def _describe_status_error(exc: anthropic.APIStatusError, model: str) -> str:
        status = exc.status_code
        if status in (401, 403):
            return ("Anthropic rejected the key: check HERO_ANTHROPIC_API_KEY / HERO_ANTHROPIC_WORKSPACE_ID in .env "
                    f"(HTTP {status}).")
        if status == 404:
            return f"Anthropic does not know model {model!r}: check HERO_VISION_MODEL / HERO_REVIEW_MODEL (HTTP 404)."
        if status == 400:
            return f"Anthropic rejected the request (HTTP 400): {str(exc)[:300]}"
        if status == 429:
            return "Anthropic rate limit persisted after 3 retries (HTTP 429); wait and generate again."
        return f"Anthropic returned HTTP {status} after retries: {str(exc)[:300]}"
