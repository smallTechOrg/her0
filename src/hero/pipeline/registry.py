"""Single-flight background worker: one pipeline run at a time, in one daemon thread.

``start_run`` refuses with ``RunInProgress`` while any run is queued/running and with
``BudgetExhausted`` when nothing remains of ``HERO_CLAUDE_BUDGET_USD``. A queued/running row with
no live worker thread in this process (left behind by a crashed/restarted server) can never
progress in this single-process design, so ``active_run`` retires it immediately as ``failed``
and the admin is never locked out.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from hero.config import get_settings
from hero.db import repo
from hero.db.repo import RunUpdate, StepRecord
from hero.llm.budget import BudgetGuard
from hero.llm.pricing import load_price_table
from hero.logging import get_logger
from hero.pipeline.runner import run_pipeline, settings_snapshot
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


def create_queued_run(segment: repo.SegmentRecord) -> repo.PipelineRunRecord:
    settings = get_settings()
    return repo.create_run(segment.id, settings_snapshot(settings, segment))


def start_run(slug: str) -> repo.PipelineRunRecord:
    settings = get_settings()
    segment = repo.require_segment(slug)
    prices = load_price_table(settings)
    prices.require(settings.vision_model)
    prices.require(settings.review_model)
    with _lock:
        current = active_run()
        if current is not None:
            raise RunInProgress(current)
        guard = BudgetGuard(settings)
        remaining = guard.remaining_usd()
        if remaining <= 0:
            raise BudgetExhausted(spent_usd=repo.total_llm_cost_usd(), budget_usd=guard.budget_total_usd)
        run = create_queued_run(segment)
        thread = threading.Thread(target=_worker, args=(run.id,), name=f"hero-run-{run.id[:8]}", daemon=True)
        _threads[run.id] = thread
        thread.start()
    return run


def _worker(run_id: str) -> None:
    try:
        run_pipeline(run_id)
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
