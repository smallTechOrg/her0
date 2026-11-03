"""P0 spike: which model slug x structured-output mode returns schema-valid output?

Sends ONE tiny synthetic image + the real ``StreetExtraction`` schema (fallback: a small local
schema if ``hero.fusion.schemas`` is not importable yet) to each slug in each mode and prints a
table (ok/fail + cost). Spends real money (a few cents): the orchestrator runs it, tests never do.
Results are written into the in-process forced-tool-rejection set for the rest of this run only.

Run: uv run python -m hero.tools.check_structured [--models a,b] [--modes forced_tool,auto_tool,json_text]
         [--max-calls N] [--out evidence.json]

``--max-calls N`` hard-caps the number of model REQUESTS the whole run may send (the guard refuses the N+1st, so a
schema re-ask or mode fallback cannot sneak in a second call); ``--out`` writes the evidence record (model, mode,
ok, tokens, USD from the app's own cost record, request counter, error) and then exits 0 whenever the evidence was
written (a failed check is a recorded outcome, not a crash).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

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

    def __init__(self, settings, cap_usd: float, max_calls: int | None = None):
        self.spent = 0.0
        self.max_calls = max_calls
        self.requests = 0          # model requests the guard let through (the "call counter")
        self.records: list = []    # every priced call the client reported
        super().__init__(settings, total_spent_fn=lambda: self.spent,
                         run_spent_fn=lambda _run_id: self.spent)
        self.budget_total_usd = float(cap_usd)  # the probe's own tiny hard stop (its spend is in memory only)

    def check_before_call(self, run_id, estimate_usd):  # type: ignore[override]
        if self.max_calls is not None and self.requests >= self.max_calls:
            raise BudgetExceeded(
                f"check_structured --max-calls {self.max_calls} reached: refusing another request.",
                spent_total_usd=self.spent, spent_run_usd=self.spent, estimate_usd=estimate_usd,
                budget_total_usd=self.budget_total_usd, scope="max_calls")
        decision = super().check_before_call(run_id, estimate_usd)
        self.requests += 1
        return decision

    def record(self, call):  # type: ignore[override]
        self.spent += call.cost_usd
        self.records.append(call)


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
    parser.add_argument("--max-calls", type=int, default=None, help="hard cap on model requests for the whole run")
    parser.add_argument("--out", default=None, help="write the evidence JSON here")
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
    guard = _ProbeGuard(settings, PROBE_CAP_USD, args.max_calls)
    table = load_price_table(settings)
    client = AnthropicClient(settings, guard, table)
    run_id = "check-structured"
    print(f"schema: {schema_name}")
    print(f"{'model':<22}{'mode':<13}{'ok':<6}{'cost_usd':<11}detail")
    total = 0.0
    results: dict[tuple[str, str], bool] = {}
    attempts: list[dict] = []
    for model in models:
        for mode in modes:
            before = len(guard.records)
            ok, cost, detail = probe(client, run_id, schema, model, mode)
            total += cost
            results[(model, mode)] = ok
            recs = guard.records[before:]
            attempts.append({
                "model": model, "structured_mode": mode, "ok": ok, "detail": detail,
                "input_tokens": sum(r.input_tokens for r in recs), "output_tokens": sum(r.output_tokens for r in recs),
                "cost_usd": round(sum(r.cost_usd for r in recs), 6),
                "recorded_calls": [{"ok": r.ok, "error": r.error, "input_tokens": r.input_tokens,
                                    "output_tokens": r.output_tokens, "cost_usd": r.cost_usd,
                                    "structured_mode": r.structured_mode} for r in recs]})
            print(f"{model:<22}{mode:<13}{'ok' if ok else 'FAIL':<6}{cost:<11.6f}{detail}")
    print(f"total_cost_usd: {total:.6f}")
    for model in models:
        passing = [m for m in modes if results[(model, m)]]
        print(f"{model}: passing modes = {', '.join(passing) or 'none'}")
    ok_5 = any(results.get(("claude-sonnet-5-5", m)) for m in ("auto_tool", "json_text"))
    print(f"claude-sonnet-5-5 usable via auto_tool/json_text: {ok_5}")
    if args.out:
        _write_evidence(Path(args.out), attempts, guard, table, schema_name, total)
        print(f"evidence written: {args.out}")
        return 0
    return 0 if any(results.values()) else 1


def _write_evidence(path: Path, attempts: list[dict], guard: _ProbeGuard, table, schema_name: str, total: float) -> None:
    first = attempts[0] if attempts else {}
    try:
        price = table.require(first.get("model", ""))
        price_row = {"input_usd_per_mtok": price.input_usd_per_mtok, "output_usd_per_mtok": price.output_usd_per_mtok,
                     "source_url": price.source_url, "verified_on": price.verified_on}
    except Exception:  # noqa: BLE001 - unknown model: recorded as null
        price_row = None
    record = {
        "model": first.get("model"), "structured_mode": first.get("structured_mode"), "ok": bool(first.get("ok")),
        "error": None if first.get("ok") else first.get("detail"),
        "input_tokens": sum(a["input_tokens"] for a in attempts), "output_tokens": sum(a["output_tokens"] for a in attempts),
        "cost_usd": round(total, 6), "cost_source": "app BudgetGuard record (in-memory probe guard; no llm_calls rows)",
        "calls_made": guard.requests, "max_calls": guard.max_calls, "schema": schema_name, "price": price_row,
        "attempts": attempts, "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
