"""Admin imagery endpoints (spec/api.md -> Admin): observations list, photo upload, "add more imagery"
(acquire-only run) and the placement preview.

Multipart bodies are parsed with the stdlib ``email`` parser so the app needs no extra dependency.
The upload is the only endpoint here that writes files: ``data/uploads/<sha256>.jpg|png`` (content-addressed,
served at ``/media/uploads/...``); everything else reads or delegates.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import math
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Query, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hero.api.auth import require_admin
from hero.api.envelope import ApiError, ok
from hero.config import get_settings
from hero.db import model_repo, repo
from hero.db.model_repo import ObservationCreate
from hero.imagery.acquire import classify_view
from hero.imagery.annotate import annotate_reference_line
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.imagery.plan import SRange, default_plan, merge_plan, overview_zoom_for
from hero.imagery.roads import fetch_road_polyline
from hero.llm.errors import UpstreamError
from hero.logging import get_logger
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind, ObservationSource
from hero.scene.geo import TileGeoref, meters_per_px

log = get_logger("hero.api")
router = APIRouter(prefix="/api/admin", tags=["admin-imagery"], dependencies=[Depends(require_admin)])

UPLOAD_MAX_DISTANCE_M = 60.0
UPLOAD_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png")}
DEFAULT_UPLOAD_FOV = 70.0
MAX_FORM_OVERHEAD_BYTES = 64 * 1024


# ------------------------------------------------------------------ helpers


def _segment(slug: str) -> repo.SegmentRecord:
    seg = repo.get_segment(slug)
    if seg is None:
        raise ApiError("not_found", f"No segment with slug {slug!r}.")
    return seg


def reference_line_of(seg: repo.SegmentRecord) -> ReferenceLine | None:
    head = model_repo.head_version(seg.id)
    if head is None:
        return None
    rec = model_repo.get_version(seg.id, head)
    return ReferenceLine.from_model(rec.model.reference_line) if rec else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def observation_json(o: Observation, fused_in_version: int | None = None) -> dict[str, Any]:
    return {
        "id": o.id, "kind": o.kind.value, "source": o.source.value, "url": o.url, "lat": o.lat, "lon": o.lon,
        "heading_deg": o.heading_deg, "pitch_deg": o.pitch_deg, "fov_deg": o.fov_deg, "zoom": o.zoom,
        "view": o.view.value if o.view else None, "station_s": o.station_s, "offset_t": o.offset_t,
        "pano_id": o.pano_id, "capture_date": o.capture_date, "cost_usd": o.cost_usd, "sha256": o.sha256,
        "note": o.note, "created_at": _iso(o.created_at), "fused_in_version": fused_in_version,
    }


def _fused_versions(segment_id: str) -> dict[str, int]:
    """observation id -> first model version whose ``fused_observation_ids`` contains it."""
    first: dict[str, int] = {}
    for summary in model_repo.list_versions(segment_id):
        rec = model_repo.get_version(segment_id, summary.version)
        for oid in (rec.model.fused_observation_ids if rec else []):
            first.setdefault(oid, summary.version)
    return first


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[bytes, str | None]]:
    """``{field: (bytes, filename)}`` from a ``multipart/form-data`` body; ``ValueError`` when malformed."""
    if not content_type.lower().startswith("multipart/form-data") or "boundary=" not in content_type:
        raise ValueError("expected multipart/form-data with a boundary")
    msg = BytesParser(policy=HTTP).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("malformed multipart body")
    out: dict[str, tuple[bytes, str | None]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        out[str(name)] = (part.get_payload(decode=True) or b"", part.get_filename())
    return out


def _float_field(form: dict, name: str, *, required: bool, default: float | None = None,
                 lo: float | None = None, hi: float | None = None) -> float | None:
    if name not in form or not form[name][0].strip():
        if required:
            raise ApiError("validation_error", f"Field {name!r} is required.", {"field": name})
        return default
    try:
        value = float(form[name][0].decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        raise ApiError("validation_error", f"Field {name!r} must be a number.", {"field": name}) from None
    if not math.isfinite(value) or (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ApiError("validation_error", f"Field {name!r} is out of range.", {"field": name, "min": lo, "max": hi})
    return value


# ------------------------------------------------------------------ observations


@router.get("/segments/{slug}/observations")
def list_observations(slug: str, kind: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=1000),
                      offset: int = Query(default=0, ge=0)):
    seg = _segment(slug)
    if kind is not None and kind not in {k.value for k in ObservationKind}:
        raise ApiError("validation_error", f"Unknown kind {kind!r}.", {"allowed": [k.value for k in ObservationKind]})
    kinds = [kind] if kind else None
    total = sum(n for k, n in model_repo.observation_counts(seg.id).items() if kinds is None or k in kinds)
    rows = model_repo.get_observations(seg.id, kinds=kinds, limit=limit, offset=offset, newest_first=True)
    fused = _fused_versions(seg.id)
    return ok({"total": total, "items": [observation_json(o, fused.get(o.id)) for o in rows]})


@router.post("/segments/{slug}/observations/upload", status_code=201)
async def upload_observation(slug: str, request: Request):
    seg = _segment(slug)
    settings = get_settings()
    limit = settings.upload_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + MAX_FORM_OVERHEAD_BYTES:
        raise ApiError("validation_error", f"The photo is larger than HERO_UPLOAD_MAX_MB ({settings.upload_max_mb:g} MB).")
    too_big = ApiError("validation_error", f"The photo is larger than HERO_UPLOAD_MAX_MB ({settings.upload_max_mb:g} MB).",
                       {"field": "file", "max_bytes": limit})
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():  # bounded read: a missing/lying Content-Length cannot buffer an unbounded body
        received += len(chunk)
        if received > limit + MAX_FORM_OVERHEAD_BYTES:
            raise too_big
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        form = parse_multipart(request.headers.get("content-type", ""), body)
    except ValueError as exc:
        raise ApiError("validation_error", f"Send multipart/form-data with a 'file' part ({exc}).") from None
    if "file" not in form or not form["file"][0]:
        raise ApiError("validation_error", "Field 'file' (a jpeg or png photo) is required.", {"field": "file"})
    data = form["file"][0]
    if len(data) > limit:
        raise ApiError("validation_error", f"The photo is larger than HERO_UPLOAD_MAX_MB ({settings.upload_max_mb:g} MB).",
                       {"field": "file", "max_bytes": limit})
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = (img.format or "").upper(), img.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ApiError("validation_error", "The file is not a readable jpeg or png image.", {"field": "file"}) from None
    if fmt not in UPLOAD_FORMATS:
        raise ApiError("validation_error", f"Only jpeg or png photos are accepted (got {fmt or 'unknown'}).", {"field": "file"})

    lat = _float_field(form, "lat", required=True, lo=-90, hi=90)
    lon = _float_field(form, "lon", required=True, lo=-180, hi=180)
    heading = _float_field(form, "heading_deg", required=True, lo=-360, hi=720) % 360.0
    pitch = _float_field(form, "pitch_deg", required=False, default=0.0, lo=-90, hi=90)
    fov = _float_field(form, "fov_deg", required=False, default=DEFAULT_UPLOAD_FOV, lo=10, hi=180)
    note = form["note"][0].decode("utf-8", "replace").strip()[:500] or None if "note" in form else None

    ref = reference_line_of(seg)
    if ref is None:
        raise ApiError("validation_error", "This segment has no reference line yet; run Generate or the placement preview first.")
    proj = ref.to_st(lat, lon)
    if proj.dist_m > UPLOAD_MAX_DISTANCE_M:
        raise ApiError("validation_error", f"The camera is {proj.dist_m:.0f} m from the reference line (max {UPLOAD_MAX_DISTANCE_M:.0f} m).",
                       {"distance_m": round(proj.dist_m, 1), "max_m": UPLOAD_MAX_DISTANCE_M})

    sha = hashlib.sha256(data).hexdigest()
    ext, media = UPLOAD_FORMATS[fmt]
    folder = settings.data_dir_abs / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{sha}.{ext}"
    if not dest.exists():
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    s_clamped = min(max(proj.s, 0.0), ref.length_s)
    obs, created = model_repo.upsert_observation(ObservationCreate(
        segment_id=seg.id, obs_key=f"upload:{sha}", kind=ObservationKind.user_photo, source=ObservationSource.upload,
        sha256=sha, file_path=f"uploads/{dest.name}", media_type=media, width_px=width, height_px=height, bytes=len(data),
        lat=lat, lon=lon, heading_deg=round(heading, 2), pitch_deg=pitch, fov_deg=fov, station_s=round(proj.s, 3),
        offset_t=round(proj.t, 3), view=classify_view(heading, ref.bearing_deg(s_clamped)),
        georef={"size_px": [width, height], "note": note}, cost_usd=0.0, note=note))
    log.info("observation.ingested", run_id=None, observation_id=obs.id, kind=obs.kind.value,
             view=obs.view.value if obs.view else None, station_s=obs.station_s, cache_hit=not created, cost_usd=0.0)
    return ok(observation_json(obs, None), status_code=201 if created else 200)


# ------------------------------------------------------------------ add more imagery (acquire-only run)


class ImageryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sv_spacing_m: float | None = Field(default=None, ge=4, le=40)
    headings: list[str] | None = Field(default=None, min_length=1)
    sat_crop_zoom: int | None = Field(default=None, ge=18, le=21)
    sat_crop_spacing_m: float | None = Field(default=None, gt=0)
    max_images: int | None = Field(default=None, ge=1, le=200)
    s_range: SRange | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ImageryBody":
        if not self.model_dump(exclude_none=True):
            raise ValueError("at least one imagery field is required")
        return self

    def overrides(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


@router.post("/segments/{slug}/imagery", status_code=202)
def add_imagery(slug: str, raw: dict[str, Any] = Body(default_factory=dict)):
    seg = _segment(slug)
    try:  # validated by hand: the shared 422 handler cannot serialise validator ``ctx`` objects
        overrides = ImageryBody.model_validate(raw).overrides()
    except ValidationError as exc:
        raise ApiError("validation_error", "The imagery plan did not pass validation.",
                       exc.errors(include_url=False, include_context=False, include_input=False)) from None
    try:
        merge_plan(default_plan(get_settings()), overrides)
    except ValueError as exc:
        raise ApiError("validation_error", str(exc)) from None
    from hero.pipeline import registry  # lazy: the fusion runner (S3c) owns run orchestration

    params = inspect.signature(registry.start_run).parameters.values()
    if not any(p.name == "mode" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        raise ApiError("internal_error", "Acquire-only runs are not wired into the run registry yet.")
    try:
        run = registry.start_run(seg.slug, mode="acquire", plan_overrides=overrides)
    except registry.PlanError as exc:
        raise ApiError("validation_error", f"Invalid imagery plan: {exc}", {"field": "s_range"}) from None
    except registry.RunInProgress as exc:
        raise ApiError("run_in_progress", "A run is already in progress; wait for it to finish.",
                       {"run_id": exc.run.id, "status": exc.run.status.value}) from None
    except registry.BudgetExhausted as exc:
        raise ApiError("budget_exhausted", str(exc), {"spent_usd": round(exc.spent_usd, 4), "budget_usd": exc.budget_usd}) from None
    return ok({"run_id": run.id, "status": "queued", "mode": "acquire"}, status_code=202)


# ------------------------------------------------------------------ placement preview


@router.post("/segments/{slug}/placement-preview")
def placement_preview(slug: str):
    """No Claude: resolve the reference line (OSM road -> clipped, else straight chord), one cached overview tile,
    annotate it, and store the v0 prior model if the segment has none."""
    seg = _segment(slug)
    settings = get_settings()
    data_dir = settings.data_dir_abs
    warnings: list[str] = []
    mid = seg.midpoint
    chord = [(seg.start.lat, seg.start.lon), (seg.end.lat, seg.end.lon)]

    road = None
    try:
        with httpx.Client() as http:
            road = fetch_road_polyline(mid.lat, mid.lon, cache=ImageryCache(data_dir / "osm"),
                                       overpass_url=settings.overpass_url, http=http)
    except Exception as exc:  # noqa: BLE001 - degrade to the chord, never fail the preview
        warnings.append(f"Road lookup failed ({type(exc).__name__}); using a straight chord.")
    ref: ReferenceLine | None = None
    source, way_id, name, extrap, snap_m = "straight_chord", None, None, (0.0, 0.0), 0.0
    if road is not None:
        try:
            from hero.model.roadclip import clip_road

            ref, info = clip_road(road.latlons, (mid.lat, mid.lon), seg.length_m, seg.heading_deg)
            source, way_id, name = road.source, road.osm_way_id, road.name
            extrap, snap_m = (info.extrapolated_start_m, info.extrapolated_end_m), info.snap_m
        except (ImportError, ValueError) as exc:
            warnings.append(f"Could not clip the OSM road to the segment ({exc}); using a straight chord.")
    if ref is None:
        if road is None:
            warnings.append("No OSM road geometry available; using a straight chord (placement unverified).")
        ref = ReferenceLine.from_latlons(chord)

    zoom = overview_zoom_for(ref.length_s, mid.lat, settings.satellite_zoom)
    google = GoogleImagery(settings, ImageryCache(data_dir / "imagery"), None)
    try:
        tile = google.satellite_crop(mid.lat, mid.lon, zoom)
    except UpstreamError as exc:
        raise ApiError("upstream_error", exc.message, exc.detail) from None
    with Image.open(tile.path) as img:
        size = img.size
        georef = TileGeoref(center_lat=round(mid.lat, 6), center_lon=round(mid.lon, 6), zoom=zoom, scale=2, size_px=size)
        annotated = annotate_reference_line(img, georef, ref, footpath_side=seg.footpath_side.value,
                                            title=f"{seg.name} - {source}")
    out_dir = data_dir / "previews" / "placement"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{seg.slug}.png"
    annotated.save(out, format="PNG", optimize=True)

    ref_model = ref.to_model(source=source, osm_way_id=way_id, name=name, extrapolated_m=extrap)
    verified = source.startswith("osm")
    if model_repo.head_version(seg.id) is None:
        _store_prior(seg, ref, ref_model, warnings)
    return ok({
        "url": f"/media/previews/placement/{seg.slug}.png?v={int(datetime.now(UTC).timestamp())}",
        "reference_line": {"source": source, "osm_way_id": way_id, "name": name, "length_s": ref_model.length_s,
                           "extrapolated_m": {"start": extrap[0], "end": extrap[1]}, "snap_m": round(snap_m, 2)},
        "placement_verified": verified, "mpp": round(meters_per_px(mid.lat, zoom, 2), 4), "warnings": warnings,
    })


def _store_prior(seg: repo.SegmentRecord, ref: ReferenceLine, ref_model, warnings: list[str]) -> None:
    try:
        from hero.model.fidelity import compute_fidelity
        from hero.model.priors import build_prior_model
    except ImportError:
        warnings.append("Prior model not stored (model modules unavailable).")
        return
    now = datetime.now(UTC)
    model = build_prior_model(segment_slug=seg.slug, footpath_side=seg.footpath_side.value, ref=ref,
                              ref_model=ref_model, osm=None, now=now)
    model_repo.save_version_only(seg.id, model_repo.ModelVersionCreate(
        version=0, kind="prior", parent_version=None, run_id=None, model=model, scene=None,
        fidelity=compute_fidelity(model, {}), diff=None, summary="Prior model from OSM geometry (no imagery yet)."))
