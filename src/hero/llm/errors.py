"""Error types shared by the LLM layer, imagery fetchers and the pipeline runner."""

from __future__ import annotations

from typing import Any


class UpstreamError(Exception):
    """An external service (Anthropic, Google, Overpass) failed in a way we do not retry."""

    def __init__(self, message: str, *, kind: str | None = None, status: int | None = None,
                 detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.detail = detail or {}
        if kind is not None:
            self.detail.setdefault("kind", kind)
        if status is not None:
            self.detail.setdefault("status", status)


class BudgetExceeded(Exception):
    """Raised BEFORE an LLM request when the estimate would exceed the global or per-run budget."""

    def __init__(self, message: str, *, spent_total_usd: float, spent_run_usd: float, estimate_usd: float,
                 budget_total_usd: float, budget_run_usd: float, scope: str):
        super().__init__(message)
        self.message = message
        self.spent_total_usd = spent_total_usd
        self.spent_run_usd = spent_run_usd
        self.estimate_usd = estimate_usd
        self.budget_total_usd = budget_total_usd
        self.budget_run_usd = budget_run_usd
        self.scope = scope  # "total" | "run"

    def as_detail(self) -> dict[str, float | str]:
        return {
            "scope": self.scope,
            "spent_total_usd": round(self.spent_total_usd, 6),
            "spent_run_usd": round(self.spent_run_usd, 6),
            "estimate_usd": round(self.estimate_usd, 6),
            "budget_total_usd": self.budget_total_usd,
            "budget_run_usd": self.budget_run_usd,
        }


class SchemaValidationError(Exception):
    """The model's structured output did not validate against the schema, even after one re-ask."""

    def __init__(self, message: str, *, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.raw = raw


class UnknownModelError(Exception):
    """A model slug is missing from the price table: refuse to run rather than bill $0."""
