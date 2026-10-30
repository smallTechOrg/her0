"""The "Max quality" run preset and the pieces it adds to a fusion run (spec/ui.md "Max quality", spec/data.md).

A max-quality run is an ordinary fusion run that spends more tokens to extract more from the SAME evidence and folds
every result into the canonical model (a new model version; nothing is thrown away):

* ``extraction_passes`` independent street-level extractions per frame batch, merged by AGREEMENT (``agree``): a
  detection several passes saw keeps its confidence, one only some passes saw is scaled down by
  ``AGREE_FLOOR + (1 - AGREE_FLOOR) * support / passes`` and flagged - it is never dropped;
* up to ``review_rounds`` review-and-fix rounds (critique vs. the satellite crops and frames), stopping early when a
  round applies at most ``review_stop_changes`` change(s) (the diff is small);
* every extraction/critique is stored with provenance (``PROVENANCE_KEY``: model, prompt version, pass index, cost,
  image shas, cache key) and found again by ``cache_key`` (image shas + model + prompt version + pass index), so a
  re-run costs nothing for work already paid for.

There is NO per-run dollar cap; only the global ``HERO_CLAUDE_BUDGET_USD`` hard stop applies (``BudgetGuard``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from hero.model.batch import ProjectedBatch, ProjectedDetection
from hero.prompts import PROMPTS_DIR

PROVENANCE_KEY = "_prov"
AGREE_FLOOR = 0.5            # confidence factor of a detection that only one of many passes saw
MATCH_S_M = 2.0              # two detections of one type within this (s, t) distance are "the same object"
MATCH_T_M = 1.5
MAX_PASSES = 5
MAX_ROUNDS = 8

# The preset's run-time fields (all editable in the form; the server is the single source of the values).
PRESET_MODEL = "claude-opus-5-5"
PRESET = {
    "model": PRESET_MODEL,
    "sv_spacing_m": 12.0,                                   # a frame every ~10-15 m ...
    "headings": ["forward", "back", "left", "right"],       # ... and 4 headings per station (frames_per_station = 4)
    "sat_crop_zoom": 21,                                    # highest zoom ...
    "sat_extra_zoom": 20,                                   # ... plus an overlapping tile set one zoom lower
    "sat_crop_spacing_m": 20.0,
    "max_images": 120,
    "extraction_passes": 3,
    "review_rounds": 4,
    "review_stop_changes": 1,
}
LIMITS = {"extraction_passes": [1, MAX_PASSES], "review_rounds": [0, MAX_ROUNDS], "sv_spacing_m": [4, 40],
          "sat_crop_zoom": [18, 21], "sat_extra_zoom": [18, 21], "max_images": [1, 200]}


class Quality(BaseModel):
    """The quality options of one run (``run.inputs.quality``). The defaults are today's behaviour."""

    model_config = ConfigDict(extra="forbid")

    preset: bool = False
    extraction_passes: int = Field(default=1, ge=1, le=MAX_PASSES)
    review_rounds: int | None = Field(default=None, ge=0, le=MAX_ROUNDS)   # None = HERO_MAX_REVIEW_ITERATIONS
    review_stop_changes: int = Field(default=0, ge=0, le=20)               # stop when a round applied <= this many

    @classmethod
    def from_inputs(cls, raw: Any) -> "Quality":
        try:
            return cls.model_validate(raw) if isinstance(raw, dict) else cls()
        except Exception:  # noqa: BLE001 - a malformed stored value must not break a run: fall back to the default
            return cls()

    @classmethod
    def resolve(cls, raw: dict[str, Any] | None) -> "Quality":
        """API input -> Quality; with ``preset: true`` the fields the caller left out take the preset values.
        Raises ``pydantic.ValidationError`` on out-of-range values."""
        data = dict(raw or {})
        if data.get("preset"):
            for key in ("extraction_passes", "review_rounds", "review_stop_changes"):
                data.setdefault(key, PRESET[key])
        return cls.model_validate(data)

    def rounds(self, default: int) -> int:
        return default if self.review_rounds is None else self.review_rounds


def preset_payload() -> dict[str, Any]:
    return {"preset": {**PRESET, "frames_per_station": len(PRESET["headings"])}, "limits": LIMITS}


# ------------------------------------------------------------------ provenance + cache key


def prompt_version(prompt_name: str) -> str:
    """8 hex chars of the prompt file: changes whenever the prompt changes, so stale answers are never replayed."""
    try:
        data = (PROMPTS_DIR / f"{prompt_name}.md").read_bytes()
    except OSError:
        return "unknown"
    return hashlib.sha256(data).hexdigest()[:8]


def cache_key(*, kind: str, shas: Iterable[str], model: str, prompt_ver: str, pass_index: int,
              extra: str = "") -> str:
    blob = json.dumps([kind, sorted(shas), model, prompt_ver, pass_index, extra], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def provenance(*, kind: str, shas: Sequence[str], model: str, prompt_name: str, pass_index: int, cost_usd: float,
               extra: str = "") -> dict[str, Any]:
    ver = prompt_version(prompt_name)
    return {"model": model, "prompt_name": prompt_name, "prompt_version": ver, "pass_index": pass_index,
            "cost_usd": round(float(cost_usd), 6), "image_shas": sorted(shas),
            "cache_key": cache_key(kind=kind, shas=shas, model=model, prompt_ver=ver, pass_index=pass_index, extra=extra)}


def prov_of(extraction: dict[str, Any]) -> dict[str, Any]:
    prov = extraction.get(PROVENANCE_KEY) if isinstance(extraction, dict) else None
    return prov if isinstance(prov, dict) else {}


def pass_index_of(extraction: dict[str, Any]) -> int:
    """Records written before this preset carry no provenance: they are pass 0."""
    return int(prov_of(extraction).get("pass_index", 0) or 0)


# ------------------------------------------------------------------ agreement merge


def _same(a: ProjectedDetection, b: ProjectedDetection) -> bool:
    return a.type == b.type and abs(a.s - b.s) <= MATCH_S_M and abs(a.t - b.t) <= MATCH_T_M


def agree(passes: Sequence[ProjectedBatch]) -> ProjectedBatch:
    """Merge the projections of N independent passes over the SAME batch into one batch by agreement.

    Never drops a detection: every detection of every pass ends up in exactly one cluster; a cluster's confidence is
    scaled by how many passes found it. Strips / states / facades are kept from every pass at ``weight / N`` (their
    sum equals one pass), ``visible`` comes from the first pass."""
    n = len(passes)
    if n == 0:
        raise ValueError("agree() needs at least one pass")
    if n == 1:
        return passes[0]
    clusters: list[list[tuple[int, ProjectedDetection]]] = []
    for p, batch in enumerate(passes):
        taken: set[int] = set()
        for det in batch.detections:
            best, best_d = None, None
            for ci, members in enumerate(clusters):
                if ci in taken or any(pi == p for pi, _ in members):
                    continue
                rep = members[0][1]
                if _same(rep, det):
                    d = abs(rep.s - det.s) + abs(rep.t - det.t)
                    if best_d is None or d < best_d:
                        best, best_d = ci, d
            if best is None:
                clusters.append([(p, det)])
            else:
                clusters[best].append((p, det))
                taken.add(best)
    detections: list[ProjectedDetection] = []
    used_keys: set[str] = set()
    for members in clusters:
        support = len({p for p, _ in members})
        rep = max((d for _, d in members), key=lambda d: (d.raw_confidence, d.key))
        factor = AGREE_FLOOR + (1.0 - AGREE_FLOOR) * support / n
        flags = tuple(dict.fromkeys([*rep.flags, "pass_agreement" if support == n else "pass_disagreement"]))
        key = rep.key if rep.key not in used_keys else f"{rep.key}~p{members[0][0]}"
        used_keys.add(key)
        detections.append(dataclasses.replace(
            rep, key=key, s=sum(d.s for _, d in members) / len(members), t=sum(d.t for _, d in members) / len(members),
            raw_confidence=round(min(1.0, rep.raw_confidence * factor), 4), flags=flags))
    detections.sort(key=lambda d: (d.s, d.t, d.key))

    def scaled(items: Iterable[Any]) -> tuple[Any, ...]:
        return tuple(dataclasses.replace(x, weight=x.weight / n) for x in items)

    return ProjectedBatch(
        batch_id=passes[0].batch_id, observation_ids=passes[0].observation_ids, detections=tuple(detections),
        strips=scaled(s for b in passes for s in b.strips), states=scaled(s for b in passes for s in b.states),
        facades=scaled(f for b in passes for f in b.facades), visible=passes[0].visible)


def agreement_stats(passes: Sequence[ProjectedBatch], merged: ProjectedBatch) -> dict[str, int]:
    n = len(passes)
    unanimous = sum(1 for d in merged.detections if "pass_agreement" in d.flags)
    return {"passes": n, "detections_in": sum(len(b.detections) for b in passes), "detections_out": len(merged.detections),
            "unanimous": unanimous, "disputed": len(merged.detections) - unanimous if n > 1 else 0}
