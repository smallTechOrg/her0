"""Acquire the planned imagery for a segment into georeferenced, hashed, costed ``observations``.

Cost guards (all unit-tested):
* the planner never plans more than ``plan.max_images`` paid items (``hero.imagery.plan``);
* ``acquire`` stops fetching once ``plan.max_images`` NEW paid requests were made (including the zoom-19 fallback
  re-fetch of a placeholder crop) and records the rest as ``skipped``; ``GoogleImagery.limit_paid_requests`` is the
  independent backstop underneath;
* Street View metadata is free: one call per station (shared by its headings), cached, and a non-OK status skips
  that station's frames without any paid request;
* frames are requested by pano id and de-duplicated on ``(pano_id, heading, pitch, fov)``; stored images are cache
  hits (zero Google requests), and ``upsert_observation`` is idempotent on ``obs_key``.

Nothing here raises for a bad frame; only a failed *overview* raises ``UpstreamError`` (unless one is stored).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from hero.db import model_repo
from hero.db.model_repo import ObservationCreate
from hero.imagery.google import EST_COST_USD, FetchedImage, GoogleImagery, PANO_FOV, PANO_PITCH, SpendCapExceeded
from hero.imagery.noimagery import looks_like_no_imagery
from hero.imagery.plan import ImageryPlan, PlannedImage, plan_images
from hero.llm.errors import UpstreamError
from hero.logging import get_logger
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind, ObservationSource, ViewKind
from hero.scene.geo import meters_per_px

log = get_logger("hero.imagery")

HEADING_QUANT_DEG = 5.0  # requested headings are snapped to 5 degrees: float noise / gentle curves dedupe + cache
FALLBACK_CROP_ZOOM = 19
SAT_SCALE = 2
VIEW_TOL_DEG = 30.0


@dataclass
class AcquireResult:
    observations: list[Observation] = field(default_factory=list)
    new: int = 0
    cached: int = 0
    skipped: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)
    planned: int = 0
    deduped: int = 0
    trimmed: bool = False
    paid_requests: int = 0  # NEW paid image requests actually sent (incl. wasted placeholder fetches)


def quantize_heading(heading_deg: float) -> float:
    return (round(heading_deg / HEADING_QUANT_DEG) * HEADING_QUANT_DEG) % 360.0


def classify_view(heading_deg: float, road_bearing_deg: float) -> ViewKind:
    """View class of a camera heading relative to the road tangent."""
    d = (heading_deg - road_bearing_deg + 180.0) % 360.0 - 180.0
    if abs(d) <= VIEW_TOL_DEG:
        return ViewKind.forward
    if abs(d) >= 180.0 - VIEW_TOL_DEG:
        return ViewKind.back
    if abs(abs(d) - 90.0) <= VIEW_TOL_DEG:
        return ViewKind.right if d > 0 else ViewKind.left
    return ViewKind.oblique


def file_facts(path: str | Path) -> tuple[str, int, int, int, str]:
    """``(sha256, width, height, bytes, media_type)`` of an image file."""
    data = Path(path).read_bytes()
    with Image.open(path) as img:
        width, height = img.size
        fmt = (img.format or "").upper()
    media = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(fmt, "application/octet-stream")
    return hashlib.sha256(data).hexdigest(), width, height, len(data), media


def acquire(*, segment_id: str, run_id: str, ref: ReferenceLine, plan: ImageryPlan, google: GoogleImagery,
            data_dir: Path) -> AcquireResult:
    items, info = plan_images(ref, plan)
    result = AcquireResult(planned=len(items), trimmed=info.trimmed, warnings=list(info.notes))
    google.limit_paid_requests(plan.max_images)
    state = _State(segment_id, run_id, ref, plan, google, Path(data_dir), result)

    for item in (i for i in items if i.kind == "satellite_overview"):
        state.satellite(item, ObservationKind.satellite_overview, required=True)
    for item in (i for i in items if i.kind == "satellite_crop"):
        state.satellite(item, ObservationKind.satellite_crop, required=False)
    stations: dict[float, list[PlannedImage]] = {}
    for item in items:
        if item.kind == "streetview":
            stations.setdefault(item.station_s, []).append(item)
    for s in sorted(stations):
        state.station(s, stations[s])

    if result.new == 0 and result.cached == len(items):
        log.info("acquire.all_cached", run_id=run_id, planned=len(items))
    result.cost_usd = round(result.cost_usd, 4)
    return result


class _State:
    def __init__(self, segment_id: str, run_id: str, ref: ReferenceLine, plan: ImageryPlan, google: GoogleImagery,
                 data_dir: Path, result: AcquireResult):
        self.segment_id, self.run_id, self.ref, self.plan = segment_id, run_id, ref, plan
        self.google, self.data_dir, self.result = google, data_dir, result
        self.seen: set[tuple[str, float, int, int]] = set()
        self.auth_warned = False

    # -------------------------------------------------------- guards

    def cap_reached(self) -> bool:
        return self.result.paid_requests >= self.plan.max_images

    def _fetch(self, fn, *args, **kwargs) -> FetchedImage:
        before = self.google.paid_requests
        try:
            return fn(*args, **kwargs)
        finally:
            self.result.paid_requests += self.google.paid_requests - before

    def _spend(self, fetched: FetchedImage, kind: str) -> float:
        if fetched.cache_hit:
            return 0.0
        cost = EST_COST_USD[kind]
        self.result.cost_usd += cost
        return cost

    def _skip(self, item: PlannedImage, reason: str) -> None:
        self.result.skipped.append({"station_s": round(item.station_s, 2), "view": item.view, "kind": item.kind,
                                    "reason": reason})
        log.info("acquire.skipped", run_id=self.run_id, kind=item.kind, view=item.view,
                 station_s=round(item.station_s, 2), reason=reason)

    def _auth_hint(self, exc: UpstreamError) -> None:
        if exc.status in (400, 401, 403) and not self.auth_warned:
            self.auth_warned = True
            self.result.warnings.append(f"Google rejected requests ({exc.status}): {exc.message[:160]}")

    # -------------------------------------------------------- satellite

    def satellite(self, item: PlannedImage, kind: ObservationKind, *, required: bool) -> None:
        zoom = item.zoom or 19
        if self.cap_reached():
            self._skip(item, "max_images_reached")
            return
        try:
            fetched = self._fetch(self.google.satellite_crop, item.lat, item.lon, zoom)
            if kind == ObservationKind.satellite_crop:
                with Image.open(fetched.path) as img:
                    placeholder = looks_like_no_imagery(img)
                if placeholder:
                    self._spend(fetched, "satellite")  # the placeholder request was paid for
                    if zoom > FALLBACK_CROP_ZOOM and not self.cap_reached():
                        zoom = FALLBACK_CROP_ZOOM
                        fetched = self._fetch(self.google.satellite_crop, item.lat, item.lon, zoom)
                        with Image.open(fetched.path) as img:
                            placeholder = looks_like_no_imagery(img)
                    if placeholder:
                        self._skip(item, "no_imagery")
                        return
                    self.result.warnings.append(
                        f"close-up at s={item.station_s:.0f} m fell back to zoom {FALLBACK_CROP_ZOOM} (no imagery at zoom {item.zoom})")
                    cost = self._spend(fetched, "satellite")
                    self._store_satellite(item, kind, fetched, zoom, cost)
                    return
        except UpstreamError as exc:
            self._auth_hint(exc)
            if required and not self._has_stored(kind):
                raise
            self._skip(item, f"upstream_error: {exc.message[:120]}")
            return
        cost = self._spend(fetched, "satellite")
        self._store_satellite(item, kind, fetched, zoom, cost)

    def _has_stored(self, kind: ObservationKind) -> bool:
        return bool(model_repo.get_observations(self.segment_id, kinds=[kind.value], limit=1))

    def _store_satellite(self, item: PlannedImage, kind: ObservationKind, fetched: FetchedImage, zoom: int,
                         cost: float) -> None:
        sha, w, h, size, media = file_facts(fetched.path)
        mpp = meters_per_px(item.lat, zoom, SAT_SCALE)
        georef = {"center_lat": round(item.lat, 6), "center_lon": round(item.lon, 6), "zoom": zoom, "scale": SAT_SCALE,
                  "size_px": [w, h], "meters_per_px": round(mpp, 6)}
        obs, _ = model_repo.upsert_observation(ObservationCreate(
            segment_id=self.segment_id, obs_key=fetched.cache_key, kind=kind,
            source=ObservationSource.google_static_maps, sha256=sha, file_path=fetched.rel_path, media_type=media,
            width_px=w, height_px=h, bytes=size, lat=item.lat, lon=item.lon, zoom=zoom, scale=SAT_SCALE,
            meters_per_px=round(mpp, 6), station_s=round(item.station_s, 3), offset_t=0.0, georef=georef,
            cost_usd=cost, acquired_run_id=self.run_id))
        if obs.kind != kind:  # same tile as an already stored observation (e.g. a zoom-19 crop at the overview centre)
            self._skip(item, f"same_image_as_{obs.kind.value}")
            return
        self._done(obs, fetched.cache_hit, cost)

    # -------------------------------------------------------- street view

    def station(self, s: float, group: list[PlannedImage]) -> None:
        first = group[0]
        try:
            meta = self.google.streetview_metadata(first.lat, first.lon)  # free
        except UpstreamError as exc:
            self._auth_hint(exc)
            for item in group:
                self._skip(item, f"metadata_error: {exc.message[:120]}")
            return
        if not meta.ok or not meta.pano_id:
            for item in group:
                self._skip(item, meta.status if not meta.ok else "NO_PANO_ID")
            return
        cam_lat = meta.lat if meta.lat is not None else first.lat
        cam_lon = meta.lon if meta.lon is not None else first.lon
        proj = self.ref.to_st(cam_lat, cam_lon)
        for item in group:
            heading = quantize_heading(item.heading_deg or 0.0)
            key = (meta.pano_id, heading, PANO_PITCH, PANO_FOV)
            if key in self.seen:
                self.result.deduped += 1
                continue
            self.seen.add(key)
            if self.cap_reached():
                self._skip(item, "max_images_reached")
                continue
            try:
                fetched = self._fetch(self.google.streetview_image_pano, meta.pano_id, heading,
                                      fov=PANO_FOV, pitch=PANO_PITCH)
            except SpendCapExceeded:
                self._skip(item, "max_images_reached")
                continue
            except UpstreamError as exc:
                self._auth_hint(exc)
                self._skip(item, f"frame_error: {exc.message[:120]}")
                continue
            cost = self._spend(fetched, "streetview")
            sha, w, h, size, media = file_facts(fetched.path)
            view = classify_view(heading, self.ref.bearing_deg(min(max(proj.s, 0.0), self.ref.length_s)))
            obs, _ = model_repo.upsert_observation(ObservationCreate(
                segment_id=self.segment_id, obs_key=fetched.cache_key, kind=ObservationKind.streetview,
                source=ObservationSource.google_streetview, sha256=sha, file_path=fetched.rel_path, media_type=media,
                width_px=w, height_px=h, bytes=size, lat=cam_lat, lon=cam_lon, heading_deg=heading,
                pitch_deg=float(PANO_PITCH), fov_deg=float(PANO_FOV), pano_id=meta.pano_id,
                capture_date=meta.date, station_s=round(proj.s, 3), offset_t=round(proj.t, 3), view=view,
                georef={"size_px": [w, h], "request": fetched.params}, cost_usd=cost, acquired_run_id=self.run_id))
            self._done(obs, fetched.cache_hit, cost)

    # -------------------------------------------------------- bookkeeping

    def _done(self, obs: Observation, cache_hit: bool, cost: float) -> None:
        self.result.observations.append(obs)
        if cache_hit:
            self.result.cached += 1
        else:
            self.result.new += 1
        log.info("observation.ingested", run_id=self.run_id, observation_id=obs.id, kind=obs.kind.value,
                 view=obs.view.value if obs.view else None, station_s=obs.station_s, cache_hit=cache_hit, cost_usd=cost)
