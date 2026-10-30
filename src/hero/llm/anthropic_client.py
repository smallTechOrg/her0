"""Anthropic adapter: workspace header on every request, SDK retries (3, exponential backoff on
429/5xx/timeouts), 120 s timeout, forced tool-use structured output from a Pydantic schema,
budget check BEFORE the request, priced ``llm_calls`` row + ``llm.call`` log AFTER it.

Schema failure handling (spec/agent.md #18): one re-ask quoting the validation error; a second
failure raises ``SchemaValidationError``. Auth / model errors are never retried and surface as
``UpstreamError`` with an actionable message.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Sequence, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from hero.config import Settings
from hero.db.repo import LlmCallCreate
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMImage, LLMUsage
from hero.llm.errors import SchemaValidationError, UpstreamError
from hero.llm.pricing import PriceTable, estimate_image_tokens, estimate_text_tokens

T = TypeVar("T", bound=BaseModel)

ANTHROPIC_TIMEOUT_S = 120.0
ANTHROPIC_MAX_RETRIES = 3
TOOL_NAME = "emit_result"
REASK_TEMPLATE = (
    "Your previous answer did not validate against the required schema. Validation errors:\n{errors}\n"
    "Call the tool again with a corrected, complete answer that satisfies the schema exactly."
)


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


class AnthropicClient:
    def __init__(self, settings: Settings, guard: BudgetGuard, prices: PriceTable):
        self._guard = guard
        self._prices = prices
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            default_headers={"anthropic-workspace-id": settings.anthropic_workspace_id.get_secret_value()},
            timeout=ANTHROPIC_TIMEOUT_S,
            max_retries=ANTHROPIC_MAX_RETRIES,
        )

    # ------------------------------------------------------------ public

    def vision_json(self, *, run_id: str, step: str, images: Sequence[LLMImage], prompt: str,
                    schema: type[T], model: str, prompt_name: str, system: str | None = None,
                    max_tokens: int = 4096) -> tuple[T, LLMUsage]:
        self._prices.require(model)
        tool = {"name": TOOL_NAME, "description": f"Return the {schema.__name__} for this task.",
                "input_schema": _tool_schema(schema)}
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._content(images, prompt)}]
        image_bytes = sum(len(i.data) for i in images)
        request_summary = f"{prompt_name}: images={len(images)} image_bytes={image_bytes} prompt_chars={len(prompt)}"
        estimate = self._estimate_usd(model, images, prompt + (system or ""), max_tokens)

        totals = {"input": 0, "output": 0, "cost": 0.0, "latency": 0, "calls": 0}
        last_error: str | None = None
        for attempt in (1, 2):
            self._guard.check_before_call(run_id, estimate)
            raw, usage_in, usage_out, latency_ms = self._request(
                model=model, system=system, messages=messages, tool=tool, max_tokens=max_tokens,
                run_id=run_id, step=step, request_summary=request_summary, attempt=attempt,
            )
            cost = self._prices.cost_usd(model, usage_in, usage_out)
            totals["input"] += usage_in
            totals["output"] += usage_out
            totals["cost"] += cost
            totals["latency"] += latency_ms
            totals["calls"] += 1
            try:
                parsed = schema.model_validate(raw)
            except ValidationError as exc:
                last_error = str(exc)[:1500]
                self._guard.record(LlmCallCreate(
                    run_id=run_id, step=step, model=model, input_tokens=usage_in, output_tokens=usage_out,
                    cost_usd=cost, latency_ms=latency_ms, ok=False, error=f"schema: {last_error[:400]}",
                    request_summary=request_summary + (" (re-ask)" if attempt == 2 else ""),
                    response_summary=_summarise_response(raw),
                ))
                messages = messages + [
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_prev", "name": TOOL_NAME,
                                                       "input": raw if isinstance(raw, dict) else {}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_prev", "content": "rejected: schema invalid"},
                        {"type": "text", "text": REASK_TEMPLATE.format(errors=last_error)},
                    ]},
                ]
                continue
            self._guard.record(LlmCallCreate(
                run_id=run_id, step=step, model=model, input_tokens=usage_in, output_tokens=usage_out,
                cost_usd=cost, latency_ms=latency_ms, ok=True,
                request_summary=request_summary + (" (re-ask)" if attempt == 2 else ""),
                response_summary=_summarise_response(raw),
            ))
            return parsed, LLMUsage(model=model, input_tokens=totals["input"], output_tokens=totals["output"],
                                    cost_usd=round(totals["cost"], 6), latency_ms=totals["latency"],
                                    calls=totals["calls"])
        raise SchemaValidationError(
            f"{step}: the model's output did not match {schema.__name__} after one re-ask: {last_error}",
            raw=None,
        )

    # ------------------------------------------------------------ internals

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

    def _request(self, *, model: str, system: str | None, messages: list[dict[str, Any]], tool: dict[str, Any],
                 max_tokens: int, run_id: str, step: str, request_summary: str, attempt: int
                 ) -> tuple[Any, int, int, int]:
        started = time.perf_counter()
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, messages=messages, tools=[tool],
                                      tool_choice={"type": "tool", "name": TOOL_NAME})
        if system:
            kwargs["system"] = system
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = self._describe_status_error(exc, model)
            self._guard.record(LlmCallCreate(
                run_id=run_id, step=step, model=model, input_tokens=0, output_tokens=0, cost_usd=0.0,
                latency_ms=latency_ms, ok=False, error=message[:400],
                request_summary=request_summary + (" (re-ask)" if attempt == 2 else ""), response_summary="",
            ))
            raise UpstreamError(message, kind="anthropic", status=exc.status_code) from exc
        except anthropic.APIConnectionError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = f"Anthropic request failed after retries ({type(exc).__name__}): check network / Anthropic status."
            self._guard.record(LlmCallCreate(
                run_id=run_id, step=step, model=model, input_tokens=0, output_tokens=0, cost_usd=0.0,
                latency_ms=latency_ms, ok=False, error=message[:400],
                request_summary=request_summary, response_summary="",
            ))
            raise UpstreamError(message, kind="anthropic") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage_in = int(getattr(response.usage, "input_tokens", 0) or 0)
        usage_out = int(getattr(response.usage, "output_tokens", 0) or 0)
        raw: Any = None
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                raw = block.input
                break
        if raw is None:
            raw = {"_error": f"no tool_use block (stop_reason={response.stop_reason})"}
        return raw, usage_in, usage_out, latency_ms

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
