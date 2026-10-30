"""Admin JSON API: /api/admin/* (password header via ``require_admin``), spec/api.md -> ## Admin.

Phase 1: status (login + spend), segments list/create, generate (single-flight, budget-refused),
run detail (polled by the pipeline view) and the recent-runs list. Phase 2 endpoints (seed,
corrections) are not registered - spec/api.md lists them under "Phase 2" and the UI stubs them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from hero.api.auth import require_admin
from hero.api.envelope import ApiError, ok
from hero.config import get_settings
from hero.db import repo
from hero.imagery import geocode as geocode_mod
from hero.db.repo import InvalidInputError, NotFoundError, SegmentCreate
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


# ------------------------------------------------------------------ serialisers


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _run_summary(run: repo.PipelineRunRecord | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"id": run.id, "status": run.status.value, "cost_usd": run.cost_usd, "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at), "created_at": _iso(run.created_at),
            "google_requests": run.google_requests, "google_cached_hits": run.google_cached_hits,
            "error": run.error}


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


def _run_detail(run: repo.PipelineRunRecord) -> dict[str, Any]:
    settings = get_settings()
    segment = repo.get_segment_by_id(run.segment_id)
    totals = repo.run_llm_totals(run.id)
    google = repo.google_counts(run.id)
    spend = repo.spend_summary(settings.claude_budget_usd)
    has_scene = run.scene is not None
    return {
        "id": run.id,
        "segment": {"slug": segment.slug, "name": segment.name} if segment else None,
        "status": run.status.value,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at), "created_at": _iso(run.created_at),
        "inputs": run.inputs,
        "steps": [s.model_dump(mode="json") for s in run.steps],
        "imagery": _imagery(run.imagery),
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
                   "run_budget_usd": settings.run_budget_usd},
    }


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


@router.post("/segments/{slug}/generate", status_code=202)
def generate(slug: str):
    try:
        run = registry.start_run(slug)
    except NotFoundError as exc:
        raise ApiError("not_found", str(exc))
    except registry.RunInProgress as exc:
        raise ApiError("run_in_progress", "A generation run is already in progress; wait for it to finish.",
                       {"run_id": exc.run.id, "status": exc.run.status.value})
    except registry.BudgetExhausted as exc:
        raise ApiError("budget_exhausted", str(exc), {"spent_usd": round(exc.spent_usd, 4), "budget_usd": exc.budget_usd})
    except Exception as exc:  # noqa: BLE001 - e.g. UnknownModelError: refuse to start, never 500 silently
        raise ApiError("upstream_error", f"Cannot start a run: {exc}")
    return ok({"run_id": run.id, "status": run.status.value}, status_code=202)


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
