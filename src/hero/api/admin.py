"""Admin JSON API: /api/admin/* (password header via ``require_admin``), spec/api.md -> ## Admin.

Status (login + spend + limits + demo street), segments list/create/resolve/from-address, ``generate`` (a fusion run
with optional ``{imagery, run_budget_usd}`` overrides; single-flight, budget-refused), run detail (polled by the
pipeline view; Phase-1 keys unchanged, Phase-2 keys additive) and the recent-runs list. The model, imagery and report
endpoints live in ``admin_model`` / ``admin_imagery``; seed and corrections are Phase 3 (UI stubs).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hero.api.auth import require_admin
from hero.api.envelope import ApiError, ok
from hero.config import get_settings
from hero.db import model_repo, repo
from hero.db.repo import InvalidInputError, NotFoundError, SegmentCreate
from hero.imagery import geocode as geocode_mod
from hero.imagery.plan import Heading, SRange, default_plan, merge_plan
from hero.model.types import ObservationKind
from hero.pipeline import registry
from hero.scene.schema import FootpathSide, LatLon

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

SEED_SEGMENT = {
    "name": "100 Feet Road, Indiranagar (west footpath)",
    "city": "Bengaluru",
    "start": {"lat": 12.97400, "lon": 77.64050},
    "end": {"lat": 12.97467, "lon": 77.64058},
    "footpath_side": FootpathSide.left.value,
}

DEMO_STREET = {
    "query": "100 Feet Road, Indiranagar", "city": "Bengaluru",
    "name": "100 Feet Road, Indiranagar, canonical demo (west footpath)",
    "hint": {"lat": 12.9711, "lon": 77.6413}, "half_length_m": 50, "footpath_side": FootpathSide.left.value,
}
FUSION_MODES = ("generate", "acquire")
OBS_LIMIT = 5000


# ------------------------------------------------------------------ serialisers


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _is_fusion(run: repo.PipelineRunRecord) -> bool:
    return (run.inputs or {}).get("mode") in FUSION_MODES


def _run_summary(run: repo.PipelineRunRecord | None) -> dict[str, Any] | None:
    if run is None:
        return None
    out = {"id": run.id, "status": run.status.value, "cost_usd": run.cost_usd, "started_at": _iso(run.started_at),
           "finished_at": _iso(run.finished_at), "created_at": _iso(run.created_at),
           "google_requests": run.google_requests, "google_cached_hits": run.google_cached_hits,
           "error": run.error}
    if _is_fusion(run):
        report = model_repo.get_run_report(run.id)
        out["mode"] = run.inputs["mode"]
        out["version_created"] = bool(report.version_created) if report else False
    return out


def _model_block(seg: repo.SegmentRecord) -> dict[str, Any] | None:
    """``null`` = no version yet (does NOT trigger the lazy legacy import)."""
    head = model_repo.head_version(seg.id)
    if head is None:
        return None
    rec = model_repo.get_version(seg.id, head)
    if rec is None:
        return None
    return {"head_version": head, "fidelity_level": f"L{rec.fidelity_level}", "legacy": rec.model.legacy,
            "photos": rec.observation_count, "placement_verified": rec.model.placement_verified}


def _segment(seg: repo.SegmentRecord, latest: repo.PipelineRunRecord | None) -> dict[str, Any]:
    return {
        "id": seg.id, "slug": seg.slug, "name": seg.name, "city": seg.city,
        "start": seg.start.model_dump(), "end": seg.end.model_dump(), "footpath_side": seg.footpath_side.value,
        "length_m": round(seg.length_m, 1), "heading_deg": round(seg.heading_deg, 1),
        "status": seg.status.value, "quality": seg.quality.value if seg.quality else None,
        "thumbnail_url": f"/media/{seg.thumbnail_path}" if seg.thumbnail_path else None,
        "latest_run_id": seg.latest_run_id, "best_score": seg.best_score, "best_distance_m": seg.best_distance_m,
        "walkability": seg.scene.walkability.model_dump() if seg.scene else None,
        "created_at": _iso(seg.created_at), "updated_at": _iso(seg.updated_at),
        "latest_run": _run_summary(latest),
        "model": _model_block(seg),
    }


def _imagery(imagery: dict[str, Any]) -> dict[str, Any]:
    sat = imagery.get("satellite") or None
    return {
        "satellite": {
            "url": sat["url"], "annotated_url": sat.get("annotated_url"), "zoom": sat["zoom"],
            "meters_per_px": sat["meters_per_px"], "size_px": sat["size_px"], "cache_hit": sat.get("cache_hit"),
        } if sat else None,
        "streetview": [{"url": p["url"], "x_m": p["x_m"], "heading": p["heading_deg"], "index": p["index"],
                        "cache_hit": p.get("cache_hit")} for p in imagery.get("streetview", [])],
        "osm_available": bool(imagery.get("osm_available")),
        "osm_error": imagery.get("osm_error"),
        # None until verify_streetview has run; True/False once checked.
        "streetview_available": imagery.get("streetview_available"),
        "streetview_status": imagery.get("streetview_status"),
        "warnings": imagery.get("warnings", []),
    }


def _fusion_imagery(run: repo.PipelineRunRecord, segment: repo.SegmentRecord | None) -> dict[str, Any]:
    """Phase-2 ``imagery`` block: the observations this run used; satellite / streetview filled so the Phase-1
    run view still renders (spec/api.md -> GET /api/admin/runs/{run_id})."""
    ij = run.imagery or {}
    ids = list(ij.get("observation_ids") or [])
    new_ids = set(ij.get("new_observation_ids") or [])
    used = model_repo.get_observations(segment.id, ids=ids, limit=OBS_LIMIT) if segment and ids else []
    overview = next((o for o in used if o.kind is ObservationKind.satellite_overview), None)
    if overview is None and segment is not None:
        found = model_repo.get_observations(segment.id, kinds=[ObservationKind.satellite_overview.value], limit=1)
        overview = found[0] if found else None
    street = sorted((o for o in used if o.is_street_level), key=lambda o: (o.station_s or 0.0, o.heading_deg or 0.0))
    placement = (get_settings().data_dir_abs / "previews" / "placement" / f"{segment.slug}.png") if segment else None
    satellite = None
    if overview is not None:
        satellite = {
            "url": overview.url,
            "annotated_url": f"/media/previews/placement/{segment.slug}.png" if placement and placement.exists() else None,
            "zoom": overview.zoom, "meters_per_px": overview.meters_per_px, "size_px": [overview.width_px, overview.height_px],
            "cache_hit": overview.id not in new_ids}
    finished = run.status.value in ("succeeded", "failed", "failed_budget")
    return {
        "satellite": satellite,
        "streetview": [{"url": o.url, "x_m": o.station_s, "heading": o.heading_deg, "index": i,
                        "cache_hit": o.id not in new_ids} for i, o in enumerate(street)],
        "osm_available": bool(ij.get("osm_available")), "osm_error": ij.get("osm_error"),
        "streetview_available": (bool(street) if finished else None),
        "streetview_status": ("OK" if street else "none") if finished else None,
        "warnings": ij.get("warnings", []),
        "observations": [{"id": o.id, "kind": o.kind.value, "view": o.view.value if o.view else None, "url": o.url,
                          "station_s": o.station_s, "capture_date": o.capture_date, "new": o.id in new_ids}
                         for o in sorted(used, key=lambda o: (o.kind.value, o.station_s or 0.0, o.id))],
        "planned": ij.get("planned"), "plan_trimmed": bool(ij.get("plan_trimmed")),
        "reference_line_source": ij.get("reference_line_source"),
    }


def _run_detail(run: repo.PipelineRunRecord) -> dict[str, Any]:
    settings = get_settings()
    segment = repo.get_segment_by_id(run.segment_id)
    totals = repo.run_llm_totals(run.id)
    google = repo.google_counts(run.id)
    spend = repo.spend_summary(settings.claude_budget_usd)
    has_scene = run.scene is not None
    fusion = _is_fusion(run)
    report = model_repo.get_run_report(run.id) if fusion else None
    detail = {
        "id": run.id,
        "segment": {"slug": segment.slug, "name": segment.name} if segment else None,
        "status": run.status.value,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at), "created_at": _iso(run.created_at),
        "inputs": run.inputs,
        "steps": [s.model_dump(mode="json") for s in run.steps],
        "imagery": _fusion_imagery(run, segment) if fusion else _imagery(run.imagery),
        "layout": run.layout,
        "obstacles": run.obstacles,
        "iterations": [{"n": it.get("n"), "preview_url": it.get("preview_url"), "accepted": it.get("accepted"),
                        "critique": it.get("critique"), "patch_ops": it.get("patch_ops", []),
                        "applied": it.get("applied"), "detail": it.get("detail")} for it in run.iterations],
        "scene": run.scene.model_dump(mode="json") if has_scene else None,
        "preview_url": f"/media/previews/{run.id}/final.png" if has_scene else None,
        "thumbnail_url": f"/media/previews/{run.id}/thumb.png" if has_scene else None,
        "cost": {
            "input_tokens": totals.input_tokens, "output_tokens": totals.output_tokens, "cost_usd": totals.cost_usd,
            "calls": [{"step": c.step, "model": c.model, "input_tokens": c.input_tokens,
                       "output_tokens": c.output_tokens, "cost_usd": c.cost_usd, "latency_ms": c.latency_ms,
                       "ok": c.ok, "error": c.error} for c in totals.calls],
        },
        "google": {"requests": google.requests, "cache_hits": google.cache_hits, "est_cost_usd": google.est_cost_usd},
        "error": run.error,
        "budget": {"spent_total_usd": spend.spent_usd, "budget_usd": spend.budget_usd,
                   "run_budget_usd": (run.inputs or {}).get("run_budget_usd") or settings.run_budget_usd},
    }
    if fusion:
        detail.update({
            "mode": run.inputs["mode"],
            "base_version": report.base_version if report else run.inputs.get("base_version"),
            "result_version": report.result_version if report else None,
            "version_created": bool(report.version_created) if report else False,
            "report_available": report is not None,
        })
        if has_scene:
            detail["preview_url"] = f"/media/previews/{run.id}/thumb.png"
    return detail


# ------------------------------------------------------------------ routes


@router.get("/status")
def status():
    settings = get_settings()
    spend = repo.spend_summary(settings.claude_budget_usd)
    google = repo.google_counts()
    stats = repo.run_stats()
    runs = repo.list_runs(limit=1)
    last_sv = runs[0].imagery.get("streetview_available") if runs and runs[0].imagery else None
    active = registry.active_run()
    return ok({
        "budget_usd": spend.budget_usd, "spent_usd": spend.spent_usd, "remaining_usd": spend.remaining_usd,
        "run_budget_usd": settings.run_budget_usd,
        "runs": stats.model_dump(),
        "google": {"requests": google.requests, "cache_hits": google.cache_hits, "est_cost_usd": google.est_cost_usd},
        "models": {"vision": settings.vision_model, "review": settings.review_model},
        "streetview_available_last": last_sv,
        "active_run_id": active.id if active else None,
        "seed_segment": SEED_SEGMENT,
        "demo_street": DEMO_STREET,
        "limits": {"run_budget_usd": settings.run_budget_usd, "max_images_per_run": settings.max_images_per_run,
                   "sv_spacing_m": settings.sv_spacing_m, "headings": settings.sv_headings_list,
                   "sat_crop_zoom": settings.sat_crop_zoom, "sat_crop_spacing_m": settings.sat_crop_spacing_m,
                   "batch_images": settings.perception_batch_images, "structured_mode": settings.structured_mode},
    })


@router.get("/segments")
def list_segments():
    segments = repo.list_segments()
    return ok([_segment(seg, repo.latest_run_for_segment(seg.id)) for seg in segments])


class SegmentBody(SegmentCreate):
    pass


class ResolveBody(BaseModel):
    address: str = Field(min_length=2, max_length=300)
    city: str = Field(min_length=1, max_length=120)


@router.post("/segments/resolve")
def resolve_address(body: ResolveBody):
    """Geocode an address (or pasted "lat, lon") into candidate centres, each with the road heading."""
    settings = get_settings()
    cands = geocode_mod.resolve(body.address.strip(), body.city.strip(),
                                google_key=settings.google_maps_api_key.get_secret_value(),
                                data_dir=settings.data_dir_abs, overpass_url=settings.overpass_url)
    if not cands:
        raise ApiError("not_found", "No match for that address. Try adding the area or city, or paste 'lat, lon'.")
    return ok({"candidates": [c.to_dict() for c in cands],
               "half_length_m": {"min": geocode_mod.MIN_HALF_LENGTH_M, "max": geocode_mod.MAX_HALF_LENGTH_M}})


class FromAddressBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading_deg: float = Field(ge=0, lt=360)
    half_length_m: float = Field(default=50.0, ge=geocode_mod.MIN_HALF_LENGTH_M, le=geocode_mod.MAX_HALF_LENGTH_M)
    footpath_side: FootpathSide = FootpathSide.left


@router.post("/segments/from-address", status_code=201)
def create_segment_from_address(body: FromAddressBody):
    """Create a segment centred on the address: ``half_length_m`` either way along the road."""
    (slat, slon), (elat, elon) = geocode_mod.segment_endpoints(body.lat, body.lon, body.heading_deg % 180.0, body.half_length_m)
    try:
        seg = repo.create_segment(SegmentCreate(name=body.name, city=body.city, start=LatLon(lat=slat, lon=slon),
                                                end=LatLon(lat=elat, lon=elon), footpath_side=body.footpath_side))
    except InvalidInputError as exc:
        raise ApiError("validation_error", str(exc), {"field": "half_length_m"})
    return ok(_segment(seg, None), status_code=201)


@router.post("/segments", status_code=201)
def create_segment(body: SegmentBody):
    try:
        seg = repo.create_segment(SegmentCreate(name=body.name, city=body.city, start=LatLon(**body.start.model_dump()),
                                                end=LatLon(**body.end.model_dump()), footpath_side=body.footpath_side))
    except InvalidInputError as exc:
        raise ApiError("validation_error", str(exc), {"field": "end"})
    return ok(_segment(seg, None), status_code=201)


class ImageryOverrides(BaseModel):
    """``generate.imagery``: every field optional (an empty object = all env defaults); ranges as in ``merge_plan``."""

    model_config = ConfigDict(extra="forbid")

    sv_spacing_m: float | None = Field(default=None, ge=4, le=40)
    headings: list[Heading] | None = Field(default=None, min_length=1)
    sat_crop_zoom: int | None = Field(default=None, ge=18, le=21)
    sat_crop_spacing_m: float | None = Field(default=None, gt=0)
    max_images: int | None = Field(default=None, ge=1, le=200)
    s_range: SRange | None = None


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imagery: ImageryOverrides | None = None
    run_budget_usd: float | None = Field(default=None, ge=0.05, le=5)


def _validation_error(exc: ValidationError) -> ApiError:  # the shared 422 handler cannot serialise validator ``ctx``
    return ApiError("validation_error", "The generate request did not pass validation.",
                    exc.errors(include_url=False, include_context=False, include_input=False))


@router.post("/segments/{slug}/generate", status_code=202)
def generate(slug: str, raw: dict[str, Any] | None = Body(default=None)):
    """Start a fusion run: ingest the imagery plan, perceive anything not yet fused, merge, publish a new model
    version (or finish as a free no-op when nothing is new). An empty body means all defaults."""
    if repo.get_segment(slug) is None:
        raise ApiError("not_found", f"No segment with slug {slug!r}.")
    try:
        body = GenerateBody.model_validate(raw or {})
    except ValidationError as exc:
        raise _validation_error(exc) from None
    overrides = body.imagery.model_dump(exclude_none=True) if body.imagery else {}
    try:
        merge_plan(default_plan(get_settings()), overrides)
    except ValueError as exc:
        raise ApiError("validation_error", f"Invalid imagery plan: {exc}", {"field": "imagery"}) from None
    try:
        run = registry.start_run(slug, mode="generate", plan_overrides=overrides, run_budget_usd=body.run_budget_usd)
    except NotFoundError as exc:
        raise ApiError("not_found", str(exc))
    except registry.PlanError as exc:
        raise ApiError("validation_error", f"Invalid imagery plan: {exc}", {"field": "imagery"})
    except registry.RunInProgress as exc:
        raise ApiError("run_in_progress", "A run is already in progress; wait for it to finish.",
                       {"run_id": exc.run.id, "status": exc.run.status.value})
    except registry.BudgetExhausted as exc:
        raise ApiError("budget_exhausted", str(exc), {"spent_usd": round(exc.spent_usd, 4), "budget_usd": exc.budget_usd})
    except Exception as exc:  # noqa: BLE001 - e.g. UnknownModelError: refuse to start, never 500 silently
        raise ApiError("upstream_error", f"Cannot start a run: {exc}")
    return ok({"run_id": run.id, "status": "queued", "mode": "generate"}, status_code=202)


@router.get("/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=100)):
    runs = repo.list_runs(limit=limit)
    segments = {s.id: s for s in repo.list_segments()}
    out = []
    for run in runs:
        seg = segments.get(run.segment_id)
        summary = _run_summary(run)
        summary["segment"] = {"slug": seg.slug, "name": seg.name} if seg else None
        duration_s = ((run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None)
        summary["duration_s"] = round(duration_s, 1) if duration_s is not None else None
        out.append(summary)
    return ok(out)


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    run = repo.get_run(run_id)
    if run is None:
        raise ApiError("not_found", f"Unknown run {run_id!r}.")
    return ok(_run_detail(run))
