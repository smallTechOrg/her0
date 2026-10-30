"""Admin "How to improve this model" endpoints (spec/api.md -> "Phase 2.1 - add-to-model endpoints"):

``GET improve-plan`` (no spend, no network), ``POST fill-gaps/preview`` (plans, no request to Google or Claude),
``POST fill-gaps`` (starts the run for exactly the previewed targets) and ``POST regenerate`` (rebuild the derived scene
and exports from stored data: no Google, no Claude, ever).

Dollar figures are informational estimates; nothing here is refused because of a price (only the global hard stop
inside ``registry.start_run`` applies).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hero.api.auth import require_admin
from hero.api.envelope import ApiError, ok
from hero.config import get_settings
from hero.db import model_repo, repo
from hero.fusion import maxquality
from hero.fusion.recompile import RecompileError
from hero.imagery.plan import SRange
from hero.llm import models as llm_models
from hero.llm.models import ModelChoiceError
from hero.logging import get_logger
from hero.model import improve
from hero.model.gap_types import GapReport
from hero.model.refline import ReferenceLine
from hero.model.types import Observation

log = get_logger("hero.api")
router = APIRouter(prefix="/api/admin", tags=["admin-improve"], dependencies=[Depends(require_admin)])

UPLOAD_FIELDS = ["file", "s", "side", "facing", "heading_deg", "captured_at", "lat", "lon", "pitch_deg", "fov_deg", "note"]
UPLOAD_ACCEPTS = ["image/jpeg", "image/png"]
CURRENT_SCENE_SCHEMA = 3
DEFAULT_FILL = {"target_level": 2, "max_images": 40, "max_usd": 1.0}
ADD_IMAGERY_DEFAULTS = {"sv_spacing_m": 10, "headings": ["forward", "back", "left", "right"], "sat_crop_zoom": 20}

STEPS = [
    "1. Look at the coverage bar: grey/amber stretches are the weakest.",
    "2. Press Fill gaps to see which photos would help and what they cost.",
    "3. Or upload your own photos: pick the files, click on the map where each was taken.",
    "4. Press Generate to fuse new photos into the next model version (a few cents of Claude).",
    "5. Press Regenerate with what we have any time to rebuild the scene from stored data for $0.",
]
STEPS_NO_MODEL = [
    "1. Run Check placement, then Generate once to create the first model.",
    "2. Come back here: the weakest stretches and what would improve them appear.",
    "3. Fill gaps, upload your own photos, or add more imagery; then Generate again.",
    "4. Regenerate with what we have rebuilds the scene from stored data for $0.",
]


def _segment(slug: str) -> repo.SegmentRecord:
    seg = repo.get_segment(slug)
    if seg is None:
        raise ApiError("not_found", f"No segment with slug {slug!r}.")
    return seg


def _head(seg: repo.SegmentRecord):
    head = model_repo.head_version(seg.id)
    return model_repo.get_version(seg.id, head) if head is not None else None


def _scene_schema(rec) -> int | None:
    value = getattr(rec, "scene_schema", None)
    if value is None and rec.scene:
        value = rec.scene.get("schema_version")
    return int(value) if value is not None else None


def _gap_report(seg: repo.SegmentRecord, rec, observations: list[Observation]) -> GapReport | None:
    """Stored gap report when the row carries one, else computed on the fly (slice S1b); ``None`` when neither exists."""
    stored = getattr(rec, "gaps_json", None) or getattr(rec, "gaps", None)
    if stored:
        try:
            return stored if isinstance(stored, GapReport) else GapReport.model_validate(stored)
        except ValidationError:
            pass
    try:
        from hero.model.gaps import analyze_gaps
    except ImportError:
        return None
    try:
        by_id = {o.id: o for o in observations}
        return analyze_gaps(rec.model, by_id, rec.fidelity, None)
    except Exception as exc:  # noqa: BLE001 - gaps are advice; the plan still works from fidelity alone
        log.warning("improve.gaps_failed", slug=seg.slug, error=repr(exc)[:200])
        return None


def _all_observations(seg: repo.SegmentRecord) -> list[Observation]:
    return model_repo.get_observations(seg.id, limit=100_000)


def _active_run():
    from hero.pipeline import registry

    return registry.active_run()


# ------------------------------------------------------------------ improve-plan


@router.get("/segments/{slug}/improve-plan")
def improve_plan(slug: str):
    seg = _segment(slug)
    rec = _head(seg)
    if rec is None:
        return ok({
            "head_version": None, "scene_schema": None, "fidelity": None, "intervals": [], "weakest": [],
            "gap_summary": {"uncovered": {n: 0 for n in improve.GAP_NAMES}, "interpolated_m": 0.0},
            "actions": {
                "fill_gaps": {"available": False, "default_body": dict(DEFAULT_FILL)},
                "add_imagery": {"available": False, "defaults": dict(ADD_IMAGERY_DEFAULTS)},
                "regenerate_cached": {"available": False, "scene_stale": False, "unfused_observations": 0,
                                      "stored_extractions": 0, "missing_extractions": 0, "cost_usd": 0.0},
                "upload": {"available": False, "max_mb": get_settings().upload_max_mb, "accepts": UPLOAD_ACCEPTS,
                           "fields": UPLOAD_FIELDS, "s_range": None}},
            "steps": list(STEPS_NO_MODEL)})
    settings = get_settings()
    observations = _all_observations(seg)
    gaps = _gap_report(seg, rec, observations)
    intervals = improve.intervals_of(rec.fidelity, gaps)
    counts = {n: 0 for n in improve.GAP_NAMES}
    for b in rec.fidelity.bins:
        for g in improve.bin_gaps(b, gaps):
            counts[g] += 1
    if gaps is not None:
        counts = {n: int(getattr(gaps.uncovered, n, counts[n])) for n in improve.GAP_NAMES}
    interpolated = round(sum(max(0.0, i.s_to - i.s_from) for i in gaps.interpolations), 3) if gaps is not None else 0.0

    fused = set(rec.model.fused_observation_ids)
    unfused = [o for o in observations if o.id not in fused and o.kind.value != "satellite_overview"]
    extractions = model_repo.list_extractions(seg.id)
    covered = {oid for e in extractions for oid in e.observation_ids}
    missing = [o for o in unfused if o.id not in covered]
    schema = _scene_schema(rec)
    busy = _active_run() is not None
    return ok({
        "head_version": rec.version, "scene_schema": schema,
        "fidelity": {"overall": rec.fidelity.overall.model_dump(mode="json")},
        "intervals": intervals, "weakest": improve.weakest_of(intervals),
        "gap_summary": {"uncovered": counts, "interpolated_m": interpolated},
        "actions": {
            "fill_gaps": {"available": not busy and any(iv["actions"] for iv in intervals),
                          "default_body": dict(DEFAULT_FILL)},
            "add_imagery": {"available": not busy, "defaults": dict(ADD_IMAGERY_DEFAULTS)},
            "regenerate_cached": {"available": not busy and not rec.model.legacy,
                                  "scene_stale": schema != CURRENT_SCENE_SCHEMA,
                                  "unfused_observations": len(unfused), "stored_extractions": len(extractions),
                                  "missing_extractions": len(missing), "cost_usd": 0.0},
            "upload": {"available": True, "max_mb": settings.upload_max_mb, "accepts": UPLOAD_ACCEPTS,
                       "fields": UPLOAD_FIELDS, "s_range": [0.0, round(rec.model.reference_line.length_s, 3)]}},
        "steps": list(STEPS)})


# ------------------------------------------------------------------ fill gaps


class FillGapsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_level: Literal[2, 3] = 2
    max_images: int = Field(default=40, ge=1, le=200)
    max_usd: float = Field(default=1.0, ge=0.05, le=5.0)
    s_range: SRange | None = None
    model: str | None = Field(default=None, min_length=1, max_length=80)
    fuse: bool = True
    quality: dict[str, Any] | None = None  # max-quality options (preset, extraction_passes, review_rounds); no $ cap


def _parse(raw: dict[str, Any]) -> FillGapsBody:
    try:  # validated by hand: the shared 422 handler cannot serialise validator ``ctx`` objects
        return FillGapsBody.model_validate(raw)
    except ValidationError as exc:
        raise ApiError("validation_error", "The fill-gaps request did not pass validation.",
                       exc.errors(include_url=False, include_context=False, include_input=False)) from None


def _quality(body: FillGapsBody) -> maxquality.Quality:
    try:
        return maxquality.Quality.resolve(body.quality)
    except ValidationError as exc:
        raise ApiError("validation_error", "The quality options did not pass validation.",
                       exc.errors(include_url=False, include_context=False, include_input=False)) from None


@router.get("/quality-preset")
def quality_preset():
    """The "Max quality" preset values and limits (the form fills its editable fields from here)."""
    return ok(maxquality.preset_payload())


def _resolve_model(requested: str | None) -> str | None:
    """Model id to price/run with. ``None`` = the configured default (priced inside ``estimate_cost``).
    Unknown / unpriced / disabled ids -> 422 ``validation_error`` (``ModelChoiceError``) before anything is spent."""
    if requested is None:
        return None
    try:
        return llm_models.resolve_model(requested, get_settings()).model
    except ModelChoiceError as exc:
        raise ApiError("validation_error", exc.message, {"field": "model", "reason": exc.reason}) from None


def _build_plan(seg: repo.SegmentRecord, body: FillGapsBody, model: str | None) -> improve.ImprovePlan:
    quality = _quality(body)
    # plain runs keep their old estimate (no review line); max-quality / explicit options price every planned round
    rounds = (quality.rounds(get_settings().max_review_iterations)
              if quality.preset or quality.review_rounds is not None else 0)
    rec = _head(seg)
    if rec is None:
        raise ApiError("validation_error", "Nothing to fill yet: run Check placement, then Generate once to create the first model.")
    observations = _all_observations(seg)
    gaps = _gap_report(seg, rec, observations)
    ref = ReferenceLine.from_model(rec.model.reference_line)
    from hero.llm.budget import BudgetGuard

    try:
        remaining = BudgetGuard(get_settings()).remaining_usd()
    except Exception:  # noqa: BLE001
        remaining = None
    try:
        plan = improve.plan_improvements(
            fidelity=rec.fidelity, gaps=gaps, observations=observations, ref=ref, target_level=body.target_level,
            max_images=body.max_images, max_usd=1e9 if quality.preset else body.max_usd,  # no $ cap at max quality
            s_range=body.s_range, model=model, remaining_usd=remaining, fuse=body.fuse,
            passes=quality.extraction_passes, review_rounds=rounds)
    except ValueError as exc:
        raise ApiError("validation_error", str(exc), {"field": "s_range"}) from None
    log.info("improve.plan", slug=seg.slug, new=plan.counts["new"], cached=plan.counts["cached"],
             est_usd=plan.est.total_usd)
    return plan


def _plan_json(plan: improve.ImprovePlan) -> dict[str, Any]:
    return {"plan": plan.plan.model_dump(mode="json", exclude_none=False), "images": [i.as_dict() for i in plan.images],
            "counts": dict(plan.counts), "intervals_targeted": plan.intervals_targeted, "est": plan.est.as_dict(),
            "trimmed": plan.trimmed, "will_exceed_budget": plan.will_exceed_budget, "note": plan.note}


@router.post("/segments/{slug}/fill-gaps/preview")
def fill_gaps_preview(slug: str, raw: dict[str, Any] = Body(default_factory=dict)):
    seg = _segment(slug)
    body = _parse(raw)
    model = _resolve_model(body.model)
    data = _plan_json(_build_plan(seg, body, model))
    q = _quality(body)
    data["quality"] = {"preset": q.preset, "extraction_passes": q.extraction_passes,
                       "review_rounds": q.rounds(get_settings().max_review_iterations)
                       if q.preset or q.review_rounds is not None else None}
    return ok(data)


@router.post("/segments/{slug}/fill-gaps", status_code=202)
def fill_gaps(slug: str, raw: dict[str, Any] = Body(default_factory=dict)):
    seg = _segment(slug)
    body = _parse(raw)
    resolved = _resolve_model(body.model)  # validated whenever supplied, fused or not
    model = resolved if body.fuse else None
    plan = _build_plan(seg, body, model)
    if not plan.images:
        raise ApiError("validation_error", plan.note)
    from hero.pipeline import registry

    mode = "generate" if body.fuse else "acquire"
    try:
        extra = {"quality": _quality(body).model_dump()} if body.quality is not None and body.fuse else {}
        run = registry.start_run(seg.slug, mode=mode, plan_overrides=plan.overrides(), model=model, **extra)
    except ModelChoiceError as exc:
        raise ApiError("validation_error", exc.message, {"field": "model", "reason": exc.reason}) from None
    except registry.PlanError as exc:
        raise ApiError("validation_error", f"Invalid imagery plan: {exc}", {"field": "targets"}) from None
    except registry.RunInProgress as exc:
        raise ApiError("run_in_progress", "A run is already in progress; wait for it to finish.",
                       {"run_id": exc.run.id, "status": exc.run.status.value}) from None
    except registry.BudgetExhausted as exc:
        raise ApiError("budget_exhausted", str(exc), {"spent_usd": round(exc.spent_usd, 4), "budget_usd": exc.budget_usd}) from None
    return ok({"run_id": run.id, "status": "queued", "mode": mode,
               "plan_summary": {"new": plan.counts["new"], "cached": plan.counts["cached"],
                                "est_total_usd": plan.est.total_usd}}, status_code=202)


# ------------------------------------------------------------------ regenerate with what we have


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["compile", "remerge"] = "compile"


def _export_summary(result: Any, rec: Any) -> dict[str, Any]:
    """``{sha8, mesh_sha256, triangles, primitives}`` from the recompile result, else the stored ``export`` column."""
    stored = getattr(rec, "export", None) if rec is not None else None
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, Any] = {}
    for key, attr in (("sha8", "export_sha8"), ("mesh_sha256", "mesh_sha256"), ("triangles", "triangles"),
                      ("primitives", "primitives")):
        value = getattr(result, attr, None)
        out[key] = value if value is not None else stored.get(key)
    return out


@router.post("/segments/{slug}/regenerate")
def regenerate(slug: str, raw: dict[str, Any] = Body(default_factory=dict)):
    seg = _segment(slug)
    try:
        body = RegenerateBody.model_validate(raw)
    except ValidationError as exc:
        raise ApiError("validation_error", "mode must be 'compile' or 'remerge'.",
                       exc.errors(include_url=False, include_context=False, include_input=False)) from None
    active = _active_run()
    if active is not None:
        raise ApiError("run_in_progress", "A run is in progress; wait for it to finish.",
                       {"run_id": active.id, "status": active.status.value})
    if _head(seg) is None:
        raise ApiError("validation_error", "Nothing to regenerate: generate once first.")
    try:
        from hero.fusion.recompile import recompile_segment
    except ImportError:
        raise ApiError("internal_error", "The recompile tool is not available in this build.") from None
    try:
        result = recompile_segment(seg.slug, remerge=body.mode == "remerge")  # no Google, no Claude: guarded in the tool
    except RecompileError as exc:  # e.g. remerge is not built -> 422 "remerge not available: ..."
        raise ApiError(exc.code, exc.message, {"field": "mode"} if body.mode == "remerge" else None) from None
    rec = _head(seg)
    log.info("scene.regenerated", slug=seg.slug, mode=body.mode, version=result.version, cost_usd=0.0)
    return ok({
        "mode": body.mode, "version": result.version, "version_created": bool(result.version_created),
        "scene_schema": result.scene_schema_after, "previous_scene_schema": result.scene_schema_before,
        "export": _export_summary(result, rec), "unchanged": bool(result.unchanged),
        "cost_usd": 0.0, "google_requests": 0, "anthropic_calls": 0,
        "interpolated_m": result.interpolated_m, "dynamic_excluded": result.dynamic_excluded,
        "missing_extractions": list(result.missing_extractions), "warnings": list(result.warnings)})
