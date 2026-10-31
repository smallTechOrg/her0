"""BudgetGuard: global hard stop BEFORE every model call, USD accounting after.

``check_before_call`` reads the real spend from ``llm_calls`` (never a cached counter, so a
restart cannot under-count) and raises ``BudgetExceeded`` when
``spent_total + estimate > HERO_CLAUDE_BUDGET_USD``. There is no per-run dollar cap: a run's own spend is
reported (``spent_run_usd``) but never stops it.
``record`` persists the priced call and emits the ``llm.call`` log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hero.config import Settings
from hero.db import repo
from hero.llm.errors import BudgetExceeded
from hero.llm.pricing import PriceTable, load_price_table
from hero.logging import get_logger

log = get_logger("hero.llm")


@dataclass(frozen=True)
class BudgetDecision:
    spent_total_usd: float
    spent_run_usd: float
    estimate_usd: float
    budget_total_usd: float

    @property
    def remaining_total_usd(self) -> float:
        return max(0.0, self.budget_total_usd - self.spent_total_usd)


class BudgetGuard:
    def __init__(self, settings: Settings, *,
                 total_spent_fn: Callable[[], float] = repo.total_llm_cost_usd,
                 run_spent_fn: Callable[[str], float] = repo.run_spend_usd,
                 prices: PriceTable | None = None):
        self._settings = settings
        self._prices = prices
        self.budget_total_usd = float(settings.claude_budget_usd)
        self._total_spent = total_spent_fn
        self._run_spent = run_spent_fn

    def estimate_usd(self, model: str, input_tokens: int, max_output_tokens: int) -> float:
        """Worst-case price of one call on ``model`` (its own ``prices.json`` row; unpriced raises)."""
        if self._prices is None:
            self._prices = load_price_table(self._settings)
        return self._prices.cost_usd(model, input_tokens, max_output_tokens)

    def check_model_call(self, run_id: str, model: str, input_tokens: int, max_output_tokens: int) -> BudgetDecision:
        """``check_before_call`` with the estimate priced for the model actually used (global hard stop only)."""
        return self.check_before_call(run_id, self.estimate_usd(model, input_tokens, max_output_tokens))

    def remaining_usd(self) -> float:
        return max(0.0, self.budget_total_usd - self._total_spent())

    def check_before_call(self, run_id: str, estimate_usd: float) -> BudgetDecision:
        spent_total = self._total_spent()
        spent_run = self._run_spent(run_id)
        decision = BudgetDecision(spent_total, spent_run, estimate_usd, self.budget_total_usd)
        if spent_total + estimate_usd > self.budget_total_usd:
            raise BudgetExceeded(
                f"Generation budget of ${self.budget_total_usd:.2f} would be exceeded: ${spent_total:.4f} spent "
                f"+ ${estimate_usd:.4f} estimated for the next call. Raise HERO_CLAUDE_BUDGET_USD in .env to continue.",
                spent_total_usd=spent_total, spent_run_usd=spent_run, estimate_usd=estimate_usd,
                budget_total_usd=self.budget_total_usd, scope="total",
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
