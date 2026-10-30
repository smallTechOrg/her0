"""BudgetGuard: hard stop BEFORE every model call (global + per-run), USD accounting after.

``check_before_call`` reads the real spend from ``llm_calls`` (never a cached counter, so a
restart cannot under-count) and raises ``BudgetExceeded`` when
``spent_total + estimate > HERO_CLAUDE_BUDGET_USD`` or ``spent_run + estimate > HERO_RUN_BUDGET_USD``.
``record`` persists the priced call and emits the ``llm.call`` log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hero.config import Settings
from hero.db import repo
from hero.llm.errors import BudgetExceeded
from hero.logging import get_logger

log = get_logger("hero.llm")


@dataclass(frozen=True)
class BudgetDecision:
    spent_total_usd: float
    spent_run_usd: float
    estimate_usd: float
    budget_total_usd: float
    budget_run_usd: float

    @property
    def remaining_total_usd(self) -> float:
        return max(0.0, self.budget_total_usd - self.spent_total_usd)


class BudgetGuard:
    def __init__(self, settings: Settings, *, run_budget_usd: float | None = None,
                 total_spent_fn: Callable[[], float] = repo.total_llm_cost_usd,
                 run_spent_fn: Callable[[str], float] = repo.run_spend_usd):
        self.budget_total_usd = float(settings.claude_budget_usd)
        if run_budget_usd is not None and not run_budget_usd > 0:
            raise ValueError(f"run_budget_usd override must be positive (got {run_budget_usd!r})")
        # A per-request override only changes the per-run cap; the global cap is never touched.
        self.budget_run_usd = float(settings.run_budget_usd if run_budget_usd is None else run_budget_usd)
        self._total_spent = total_spent_fn
        self._run_spent = run_spent_fn

    def remaining_usd(self) -> float:
        return max(0.0, self.budget_total_usd - self._total_spent())

    def check_before_call(self, run_id: str, estimate_usd: float) -> BudgetDecision:
        spent_total = self._total_spent()
        spent_run = self._run_spent(run_id)
        decision = BudgetDecision(spent_total, spent_run, estimate_usd, self.budget_total_usd, self.budget_run_usd)
        if spent_total + estimate_usd > self.budget_total_usd:
            raise BudgetExceeded(
                f"Generation budget of ${self.budget_total_usd:.2f} would be exceeded: ${spent_total:.4f} spent "
                f"+ ${estimate_usd:.4f} estimated for the next call. Raise HERO_CLAUDE_BUDGET_USD in .env to continue.",
                spent_total_usd=spent_total, spent_run_usd=spent_run, estimate_usd=estimate_usd,
                budget_total_usd=self.budget_total_usd, budget_run_usd=self.budget_run_usd, scope="total",
            )
        if spent_run + estimate_usd > self.budget_run_usd:
            raise BudgetExceeded(
                f"Per-run budget of ${self.budget_run_usd:.2f} would be exceeded: ${spent_run:.4f} spent on this run "
                f"+ ${estimate_usd:.4f} estimated for the next call. Raise HERO_RUN_BUDGET_USD in .env to allow it.",
                spent_total_usd=spent_total, spent_run_usd=spent_run, estimate_usd=estimate_usd,
                budget_total_usd=self.budget_total_usd, budget_run_usd=self.budget_run_usd, scope="run",
            )
        return decision

    def record(self, call: repo.LlmCallCreate) -> repo.LlmCallRecord:
        record = repo.record_llm_call(call)
        log.info(
            "llm.call", run_id=call.run_id, step=call.step, model=call.model, input_tokens=call.input_tokens,
            output_tokens=call.output_tokens, cost_usd=call.cost_usd, latency_ms=call.latency_ms, ok=call.ok,
            error=call.error, structured_mode=call.structured_mode,
        )
        return record
