"""Single-flight background worker: one run at a time, in one daemon thread. Dispatches to ``hero.fusion``.

Phase 2: ``start_run(slug, mode="generate"|"acquire", plan_overrides=..., run_budget_usd=...)`` queues a fusion
run (the Phase-1 pipeline in ``hero.pipeline.{runner,steps}`` is kept in the tree but no longer reachable from here).
It refuses with ``RunInProgress`` while any run is queued/running; a ``generate`` run also with ``BudgetExhausted``
when nothing remains of ``HERO_CLAUDE_BUDGET_USD``, and an ``acquire`` run (no Claude call) with
``GoogleBudgetExceeded`` (a ``BudgetExhausted``) when the Google estimate for the NEW images exceeds the run cap.
A queued/running row with no live worker thread in this process (left behind by a crashed/restarted server) can
never progress in this single-process design, so ``active_run`` retires it immediately as ``failed`` and the admin is
never locked out (stricter than the 15-minute rule of ``spec/api.md``, which is only needed across processes).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from hero.config import get_settings
from hero.db import model_repo, repo
from hero.db.repo import RunUpdate, StepRecord
from hero.fusion.runner import run_fusion, run_inputs
from hero.llm.budget import BudgetGuard
from hero.llm.pricing import load_price_table
from hero.logging import get_logger
from hero.scene.schema import RunStatus

log = get_logger("hero.pipeline")

_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}


class RunInProgress(Exception):
    def __init__(self, run: repo.PipelineRunRecord):
        super().__init__(f"Run {run.id} is {run.status.value}.")
        self.run = run


class BudgetExhausted(Exception):
    def __init__(self, spent_usd: float, budget_usd: float):
        super().__init__(
            f"Generation budget of ${budget_usd:.2f} is used up (${spent_usd:.2f} spent). "
            "Raise HERO_CLAUDE_BUDGET_USD in .env to continue."
        )
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd


def active_run() -> repo.PipelineRunRecord | None:
    """The queued/running run, after retiring any row that has no live worker thread here."""
    with _lock:
        run = repo.get_active_run()
        if run is None:
            return None
        thread = _threads.get(run.id)
        if thread is not None and thread.is_alive():
            return run
        _retire_orphan(run)
        return None


def _retire_orphan(run: repo.PipelineRunRecord) -> None:
    now = datetime.now(UTC)
    message = "Run was interrupted by a server restart and marked failed."
    steps = [s.model_copy(update={"status": "failed", "detail": message}) if s.status == "started" else s
             for s in run.steps]
    if not any(s.status == "failed" for s in steps):
        steps.append(StepRecord(name="interrupted", status="failed", started_at=now, latency_ms=0, detail=message))
    repo.update_run(run.id, RunUpdate(
        status=RunStatus.failed, finished_at=now, steps=steps,
        error={"code": "internal_error", "step": "unknown", "message": message},
    ))
    log.warning("pipeline.orphan_run_retired", run_id=run.id, previous_status=run.status.value)


class GoogleBudgetExceeded(BudgetExhausted):
    """The estimated Google cost of the NEW images of an acquire-only run exceeds the run cap."""

    def __init__(self, estimate_usd: float, cap_usd: float):
        Exception.__init__(
            self, f"Fetching the planned imagery would cost about ${estimate_usd:.2f} in Google requests, above the "
                  f"per-run cap of ${cap_usd:.2f}. Lower max_images or raise HERO_RUN_BUDGET_USD in .env.")
        self.spent_usd = estimate_usd
        self.budget_usd = cap_usd


MODES = ("generate", "acquire")


def _google_estimate_usd(segment: repo.SegmentRecord, settings, plan_overrides: dict[str, Any] | None) -> float:
    """Upper bound of the Google spend for the NEW images of a plan: the plan's cost minus what is already stored."""
    from hero.imagery.plan import default_plan, merge_plan, plan_images
    from hero.model.refline import ReferenceLine

    head = model_repo.head_version(segment.id)
    rec = model_repo.get_version(segment.id, head) if head is not None else None
    plan = merge_plan(default_plan(settings), dict(plan_overrides or {}))
    ref = (ReferenceLine.from_model(rec.model.reference_line) if rec is not None and not rec.model.legacy else
           ReferenceLine.from_latlons([(segment.start.lat, segment.start.lon), (segment.end.lat, segment.end.lon)]))
    _items, info = plan_images(ref, plan)
    stored = model_repo.observation_counts(segment.id)
    stored_paid = sum(n for kind, n in stored.items() if kind != "user_photo")
    paid = sum(info.counts.values())
    if paid <= 0:
        return 0.0
    return round(info.est_cost_usd * max(0, paid - stored_paid) / paid, 4)


def start_run(slug: str, *, mode: str = "generate", plan_overrides: dict[str, Any] | None = None,
              run_budget_usd: float | None = None) -> repo.PipelineRunRecord:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES} (got {mode!r})")
    settings = get_settings()
    segment = repo.require_segment(slug)
    if mode == "generate":
        prices = load_price_table(settings)
        prices.require(settings.vision_model)
        prices.require(settings.review_model)
    with _lock:
        current = active_run()
        if current is not None:
            raise RunInProgress(current)
        if mode == "generate":
            guard = BudgetGuard(settings, run_budget_usd=run_budget_usd)
            if guard.remaining_usd() <= 0:
                raise BudgetExhausted(spent_usd=repo.total_llm_cost_usd(), budget_usd=guard.budget_total_usd)
        else:
            cap = run_budget_usd if run_budget_usd is not None else settings.run_budget_usd
            estimate = _google_estimate_usd(segment, settings, plan_overrides)
            if estimate > cap:
                raise GoogleBudgetExceeded(estimate, cap)
        run = repo.create_run(segment.id, run_inputs(settings, segment, mode=mode, plan_overrides=plan_overrides,
                                                     run_budget_usd=run_budget_usd,
                                                     base_version=model_repo.head_version(segment.id)))
        thread = threading.Thread(target=_worker, args=(run.id,), name=f"hero-run-{run.id[:8]}", daemon=True)
        _threads[run.id] = thread
        thread.start()
    return run


def _worker(run_id: str) -> None:
    try:
        run_fusion(run_id)
    except Exception as exc:  # noqa: BLE001 - last line of defence: the server must never crash
        log.error("pipeline.crashed", run_id=run_id, error=repr(exc)[:300])
        try:
            repo.update_run(run_id, RunUpdate(status=RunStatus.failed, finished_at=datetime.now(UTC),
                                              error={"code": "internal_error", "step": "unknown",
                                                     "message": f"Pipeline crashed: {str(exc)[:300]}"}))
        except Exception:  # noqa: BLE001
            pass
    finally:
        _threads.pop(run_id, None)


def wait_for(run_id: str, timeout_s: float | None = None) -> None:
    thread = _threads.get(run_id)
    if thread is not None:
        thread.join(timeout_s)
