"""P0 spike: which model slug x structured-output mode returns schema-valid output?

Sends ONE tiny synthetic image + the real ``StreetExtraction`` schema (fallback: a small local
schema if ``hero.fusion.schemas`` is not importable yet) to each slug in each mode and prints a
table (ok/fail + cost). Spends real money (a few cents): the orchestrator runs it, tests never do.
Results are written into the in-process forced-tool-rejection set for the rest of this run only.

Run: uv run python -m hero.tools.check_structured [--models a,b] [--modes forced_tool,auto_tool,json_text]
"""

from __future__ import annotations

import argparse
import sys

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from hero.config import STRUCTURED_MODES, ConfigError, get_settings
from hero.llm.anthropic_client import AnthropicClient
from hero.llm.budget import BudgetGuard
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UnknownModelError, UpstreamError
from hero.llm.pricing import load_price_table

DEFAULT_MODELS = ("claude-sonnet-4-5", "claude-sonnet-5-5")
DEFAULT_MODES = ("forced_tool", "auto_tool", "json_text")
PROBE_CAP_USD = 1.0
PROMPT = ("This is a synthetic street-level test image. Extract what the schema asks for from it; "
          "return empty lists where nothing is visible. Keep the answer short.")


class _FallbackSchema(BaseModel):
    dominant_colour: str = Field(description="one lowercase colour word")
    shapes: list[str]


def load_schema() -> tuple[type[BaseModel], str]:
    try:
        from hero.fusion.schemas import StreetExtraction  # type: ignore[import-not-found]
        return StreetExtraction, "StreetExtraction"
    except ImportError:
        return _FallbackSchema, "_FallbackSchema (hero.fusion.schemas not importable)"


def _image():
    from hero.pipeline.steps import image_payload

    img = Image.new("RGB", (256, 192), (150, 170, 200))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 120, 256, 192), fill=(90, 90, 95))
    draw.rectangle((20, 60, 90, 120), fill=(180, 120, 90))
    return image_payload(img, "a synthetic test image", 256)


class _ProbeGuard(BudgetGuard):
    """Budget guard that keeps the probe's spend in memory: no segment/run/llm_calls rows are written
    to the real database by a diagnostic tool."""

    def __init__(self, settings, cap_usd: float):
        self.spent = 0.0
        super().__init__(settings, run_budget_usd=cap_usd, total_spent_fn=lambda: self.spent,
                         run_spent_fn=lambda _run_id: self.spent)

    def record(self, call):  # type: ignore[override]
        self.spent += call.cost_usd


def probe(client: AnthropicClient, run_id: str, schema: type[BaseModel], model: str, mode: str) -> tuple[bool, float, str]:
    try:
        _, usage = client.vision_json(run_id=run_id, step="check_structured", images=[_image()], prompt=PROMPT,
                                      schema=schema, model=model, prompt_name="check_structured",
                                      max_tokens=2048, structured_mode=mode)
        return True, usage.cost_usd, f"calls={usage.calls}"
    except (UpstreamError, SchemaValidationError, BudgetExceeded, UnknownModelError) as exc:
        return False, 0.0, f"{type(exc).__name__}: {getattr(exc, 'message', str(exc))[:110]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES))
    args = parser.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in STRUCTURED_MODES]
    if bad:
        print(f"unknown mode(s): {', '.join(bad)}")
        return 2
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"config: {exc}")
        return 1
    schema, schema_name = load_schema()
    guard = _ProbeGuard(settings, PROBE_CAP_USD)
    client = AnthropicClient(settings, guard, load_price_table(settings))
    run_id = "check-structured"
    print(f"schema: {schema_name}")
    print(f"{'model':<22}{'mode':<13}{'ok':<6}{'cost_usd':<11}detail")
    total = 0.0
    results: dict[tuple[str, str], bool] = {}
    for model in models:
        for mode in modes:
            ok, cost, detail = probe(client, run_id, schema, model, mode)
            total += cost
            results[(model, mode)] = ok
            print(f"{model:<22}{mode:<13}{'ok' if ok else 'FAIL':<6}{cost:<11.6f}{detail}")
    print(f"total_cost_usd: {total:.6f}")
    for model in models:
        passing = [m for m in modes if results[(model, m)]]
        print(f"{model}: passing modes = {', '.join(passing) or 'none'}")
    ok_5 = any(results.get(("claude-sonnet-5-5", m)) for m in ("auto_tool", "json_text"))
    print(f"claude-sonnet-5-5 usable via auto_tool/json_text: {ok_5}")
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
