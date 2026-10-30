"""Deterministic run report (spec/data.md -> "Run report"; spec/capabilities/street-description.md).

``build_run_report`` turns the structured outcome of one fusion run into a ``RunReport`` whose ``summary`` and
``suggestions`` are templates over that data (no LLM, no clock; ``now`` is the fallback for a missing ``finished_at``).
Numbers in the text always equal the numbers in the structured fields.

Interpretation notes
--------------------
* ``run`` is duck-typed: anything with ``id`` (or a plain ``str`` id) and optionally ``started_at`` / ``finished_at``
  (a ``PipelineRunRecord`` works).
* ``costs`` is a ``ReportCost`` or a mapping with any of its keys; missing Google numbers default to the observations
  (``google_new_requests`` = new observations whose source is Google, ``google_cached`` = ``cached_count``,
  ``google_est_usd`` = sum of the new observations' ``cost_usd``); ``total_usd`` defaults to Anthropic + Google.
* Images: ``new_observations`` are this run's newly stored observations; ``cached_observations`` (optional) are the
  already-stored ones the plan reused. ``by_source/by_kind/by_view`` cover both; ``planned = new + cached + skipped``.
* A no-op run (``version_created`` false and nothing fused) says "no new images" and costs nothing.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from hero.model import describe
from hero.model.types import (
    CanonicalModel,
    FidelityMap,
    ModelDiff,
    Observation,
    ObservationSource,
    ReportChanged,
    ReportConfidenceChange,
    ReportContradicted,
    ReportCost,
    ReportDetections,
    ReportFidelity,
    ReportFidelitySide,
    ReportImages,
    ReportNewDetection,
    ReportSkipped,
    RunReport,
)

GOOGLE_SOURCES = (ObservationSource.google_streetview, ObservationSource.google_static_maps)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _side(f: FidelityMap | None) -> ReportFidelitySide:
    if f is None:
        return ReportFidelitySide()
    return ReportFidelitySide(overall=f.overall.score,
                              histogram_m={k: v for k, v in sorted(f.overall.histogram_m.items()) if v > 0})


def _cost(costs: ReportCost | Mapping[str, Any] | None, new: Sequence[Observation], cached: int) -> ReportCost:
    raw = costs.model_dump() if isinstance(costs, ReportCost) else dict(costs or {})
    google_new = sum(1 for o in new if o.source in GOOGLE_SOURCES)
    raw.setdefault("google_new_requests", google_new)
    raw.setdefault("google_cached", cached)
    raw.setdefault("google_est_usd", round(sum(o.cost_usd for o in new), 4))
    raw.setdefault("anthropic_usd", 0.0)
    raw.setdefault("total_usd", round(float(raw["anthropic_usd"]) + float(raw["google_est_usd"]), 4))
    return ReportCost(**raw)


def _fields_text(fields: Mapping[str, Any]) -> str:
    bits = []
    for k in sorted(fields):
        v = fields[k]
        if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
            bits.append(f"{k} {v[0]:g} -> {v[1]:g}")
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            bits.append(f"{k} {v[0]} -> {v[1]}")
        else:
            bits.append(f"{k} changed")
    return ", ".join(bits)


def build_run_report(*, run: Any, mode: str, base: CanonicalModel | None, result: CanonicalModel | None,
                     diff: ModelDiff | None, fid_before: FidelityMap | None, fid_after: FidelityMap | None,
                     new_observations: Sequence[Observation], cached_count: int,
                     costs: ReportCost | Mapping[str, Any] | None, warnings: Sequence[str], now: datetime,
                     cached_observations: Sequence[Observation] = (), skipped: Sequence[ReportSkipped] = (),
                     degraded: bool = False) -> RunReport:
    model = result or base
    if model is None:
        raise ValueError("build_run_report needs a base or a result model")
    run_id = run if isinstance(run, str) else str(run.id)
    started = None if isinstance(run, str) else getattr(run, "started_at", None)
    finished = None if isinstance(run, str) else getattr(run, "finished_at", None)
    duration = max(0.0, ((finished or now) - started).total_seconds()) if started else 0.0
    version_created = result is not None and (base is None or result.version != base.version)
    base_v = base.version if base is not None else None
    result_v = result.version if result is not None else base_v

    # ---- images
    used = list(new_observations) + list(cached_observations)
    n_new, n_cached = len(new_observations), cached_count
    fused_now = (len(set(result.fused_observation_ids) - set(base.fused_observation_ids if base else ()))
                 if result is not None else 0)
    images = ReportImages(
        planned=n_new + n_cached + len(skipped), used_total=n_new + n_cached, new=n_new, cached=n_cached,
        fused_this_run=fused_now,
        by_source=dict(sorted(Counter(o.source.value for o in used).items())),
        by_kind=dict(sorted(Counter(o.kind.value for o in used).items())),
        by_view=dict(sorted(Counter(o.view.value for o in used if o.view is not None).items())),
        new_observation_ids=sorted(o.id for o in new_observations), skipped=list(skipped))

    # ---- detections / profile / fidelity / confidence
    dets = ReportDetections()
    profile_changes: list[str] = []
    conf_changes: list[ReportConfidenceChange] = []
    improved = []
    if diff is not None:
        dets = ReportDetections(
            new=[ReportNewDetection(id=e.id, type=e.type, description=e.description, confidence=e.confidence)
                 for e in diff.entities.added],
            changed=[ReportChanged(id=c.id, what=_fields_text(c.fields) or c.reason) for c in diff.entities.changed]
                    + [ReportChanged(id=e.id, what="revived (supported again)") for e in diff.entities.revived],
            removed=[ReportChanged(id=r.id, what=r.reason or "no longer supported") for r in diff.entities.removed],
            contradicted=[ReportContradicted(id=str(c.get("id", "")), description=str(c.get("description", "")))
                          for c in diff.contradictions.opened],
            unchanged_count=diff.entities.unchanged)
        profile_changes = [f"{describe._num(c.s_from)}-{describe._num(c.s_to)} m: {c.what}"
                           for c in diff.profile.changed_intervals]
        improved = list(diff.fidelity.improved)
        types = {e.id: str(e.type) for e in model.entities}
        for c in diff.entities.changed:
            pair = c.fields.get("confidence")
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                conf_changes.append(ReportConfidenceChange(entity_id=c.id, type=types.get(c.id, "other"),
                                                           before=float(pair[0]), after=float(pair[1]),
                                                           reason=c.reason))
    fidelity = ReportFidelity(before=_side(fid_before), after=_side(fid_after or fid_before), improved_intervals=improved)
    cost = _cost(costs, new_observations, cached_count)

    # ---- text
    after = fid_after or fid_before
    noop = not version_created and fused_now == 0 and n_new == 0
    if result is None:
        summary = ("The run did not produce a model" + (f"; it stayed at v{base_v}" if base_v is not None else "")
                   + ". See the warnings for the reason.")
    elif noop:
        summary = (f"Run found no new images to fuse, so the model stays at v{result_v} and nothing new was spent "
                   f"({_plural(n_cached, 'image')} already stored).")
    elif not version_created:
        summary = (f"Run added {_plural(n_new, 'new image')} but the model did not change, so it stays at v{result_v}.")
    else:
        lvl = f"fidelity L{fid_before.overall.level} -> L{after.overall.level}" if fid_before and after else ""
        summary = (f"Run added {_plural(n_new, 'new image')} ({n_cached} of {n_new + n_cached} images were already "
                   f"cached) and created model v{result_v} from v{base_v}: {_plural(len(dets.new), 'new object')}, "
                   f"{len(dets.changed)} changed, {len(dets.removed)} removed, {len(dets.contradicted)} newly "
                   f"contradicted" + (f"; {lvl}" if lvl else "") + f". Cost ${cost.total_usd:.2f}.")
    if warnings:
        summary += f" {_plural(len(warnings), 'warning')} raised."

    suggestions: list[str] = []
    if result is not None and after is not None:
        parts = describe.low_confidence_parts(result, after)
        if parts:
            worst = parts[0]
            suggestions.append(f"Metres {describe._num(worst.s_from)}-{describe._num(worst.s_to)} are the least "
                               f"certain ({worst.reason}).")
        suggestions.append(describe.improve_hint(after))
    if degraded or warnings:
        suggestions.append("Review the warnings, then run again to pick up anything that was skipped.")
    if not suggestions:
        suggestions.append("Add more imagery (left and right views) and run again.")

    return RunReport(
        run_id=run_id, segment_slug=model.segment_slug, mode=mode, base_version=base_v,  # type: ignore[arg-type]
        result_version=result_v, version_created=version_created, started_at=started, finished_at=finished,
        duration_s=round(duration, 1), images=images, detections=dets, profile_changes=profile_changes,
        fidelity=fidelity, confidence_changes=conf_changes, cost=cost, warnings=list(warnings), degraded=degraded,
        summary=summary, suggestions=suggestions)
