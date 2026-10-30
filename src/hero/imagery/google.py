"""Google Static Maps (satellite tile), Street View Static metadata + image: cached, counted.

Every attempt - hit or miss - is recorded via ``repo.record_google_request`` with ``cache_hit``
and logged as ``google.request``. Retries: 3 with exponential backoff on 429/5xx/timeouts
(Google timeout 20 s). Auth / enablement errors are not retried and raise ``UpstreamError``
with a hint naming the env var and the API to enable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from hero.config import Settings
from hero.db import repo
from hero.imagery.cache import ImageryCache
from hero.llm.errors import UpstreamError
from hero.logging import get_logger

log = get_logger("hero.imagery")

STATIC_MAPS_URL = "https://maps.googleapis.com/maps/api/staticmap"
STREETVIEW_URL = "https://maps.googleapis.com/maps/api/streetview"
STREETVIEW_META_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
GOOGLE_TIMEOUT_S = 20.0
GOOGLE_RETRIES = 3
TILE_SIZE = "640x640"
TILE_SCALE = 2
PANO_SIZE = "640x640"
PANO_FOV = 90
PANO_PITCH = 0
PANO_RADIUS_M = 50
EST_COST_USD = {"satellite": 0.002, "streetview": 0.007, "streetview_metadata": 0.0}
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FetchedImage:
    path: str  # absolute path on disk
    rel_path: str  # relative to the data dir (e.g. imagery/<sha>.png)
    url: str  # /media/imagery/<sha>.png
    cache_key: str
    cache_hit: bool
    params: dict[str, Any] = field(default_factory=dict, compare=False)  # canonical request params (no key)


@dataclass(frozen=True)
class StreetViewMeta:
    status: str
    pano_id: str | None
    lat: float | None
    lon: float | None
    cache_hit: bool
    date: str | None = None  # "YYYY-MM" (Street View capture month) when Google reports it

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def _fmt(value: float) -> str:
    return f"{value:.6f}"


class SpendCapExceeded(UpstreamError):
    """The per-run cap on NEW paid image requests was reached; nothing was sent to Google."""


class GoogleImagery:
    def __init__(self, settings: Settings, cache: ImageryCache, run_id: str | None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self._key = settings.google_maps_api_key.get_secret_value()
        self._cache = cache
        self._run_id = run_id
        self._data_dir = settings.data_dir_abs
        self._http = http or httpx.Client(timeout=GOOGLE_TIMEOUT_S)
        self._sleep = sleep
        self.paid_requests = 0  # NEW (non-cache) image requests sent through this client
        self._paid_cap: int | None = None

    # ------------------------------------------------------------ cost guard

    def limit_paid_requests(self, n: int | None) -> None:
        """Allow at most ``n`` more NEW paid image requests (cache hits and free metadata never count)."""
        self._paid_cap = None if n is None else self.paid_requests + max(0, n)

    @property
    def paid_remaining(self) -> int | None:
        return None if self._paid_cap is None else max(0, self._paid_cap - self.paid_requests)

    def _charge(self, kind: str) -> None:
        if self._paid_cap is not None and self.paid_requests >= self._paid_cap:
            raise SpendCapExceeded("Per-run cap on new paid Google image requests reached; request not sent.",
                                   kind=kind, detail={"cap": self._paid_cap})
        self.paid_requests += 1

    # ------------------------------------------------------------ public

    def satellite_tile(self, lat: float, lon: float, zoom: int) -> FetchedImage:
        params = {"center": f"{_fmt(lat)},{_fmt(lon)}", "zoom": zoom, "size": TILE_SIZE, "scale": TILE_SCALE,
                  "maptype": "satellite"}
        return self._cached_image("satellite", STATIC_MAPS_URL, params, suffix="png")

    def satellite_crop(self, lat: float, lon: float, zoom: int) -> FetchedImage:
        """Close-up / overview tile: the same call (and cache key) as ``satellite_tile``."""
        return self.satellite_tile(lat, lon, zoom)

    def streetview_metadata(self, lat: float, lon: float) -> StreetViewMeta:
        params = {"location": f"{_fmt(lat)},{_fmt(lon)}", "radius": PANO_RADIUS_M, "source": "outdoor"}
        entry = self._cache.get_or_fetch(
            "streetview_metadata", params,
            lambda: self._get(STREETVIEW_META_URL, params, kind="streetview_metadata", expect_image=False),
            suffix="meta.json",
        )
        self._count("streetview_metadata", entry.key, entry.hit, entry.path)
        try:
            body = json.loads(entry.path.read_text(encoding="utf-8"))
        except ValueError:
            body = {"status": "INVALID_RESPONSE"}
        loc = body.get("location") or {}
        return StreetViewMeta(status=str(body.get("status", "UNKNOWN")), pano_id=body.get("pano_id"),
                              lat=loc.get("lat"), lon=loc.get("lng"), cache_hit=entry.hit,
                              date=body.get("date") if isinstance(body.get("date"), str) else None)

    def streetview_image(self, lat: float, lon: float, heading_deg: float) -> FetchedImage:
        params = {"location": f"{_fmt(lat)},{_fmt(lon)}", "heading": round(heading_deg, 1), "fov": PANO_FOV,
                  "pitch": PANO_PITCH, "size": PANO_SIZE, "radius": PANO_RADIUS_M, "source": "outdoor",
                  "return_error_code": "true"}
        return self._cached_image("streetview", STREETVIEW_URL, params, suffix="jpg")

    def streetview_image_pano(self, pano_id: str, heading_deg: float, *, fov: int = PANO_FOV,
                              pitch: int = PANO_PITCH) -> FetchedImage:
        """Street View frame by panorama id (so two stations on the same pano are one cached request)."""
        params = {"pano": pano_id, "heading": round(heading_deg, 1), "fov": fov, "pitch": pitch, "size": PANO_SIZE,
                  "return_error_code": "true"}
        return self._cached_image("streetview", STREETVIEW_URL, params, suffix="jpg")

    # ------------------------------------------------------------ internals

    def _cached_image(self, kind: str, url: str, params: dict[str, Any], *, suffix: str) -> FetchedImage:
        def fetch() -> bytes:
            self._charge(kind)
            return self._get(url, params, kind=kind, expect_image=True)

        entry = self._cache.get_or_fetch(kind, params, fetch, suffix=suffix)
        self._count(kind, entry.key, entry.hit, entry.path)
        rel = entry.path.relative_to(self._data_dir).as_posix()
        return FetchedImage(path=str(entry.path), rel_path=rel, url=f"/media/{rel}", cache_key=entry.key,
                            cache_hit=entry.hit, params=dict(params))

    def _count(self, kind: str, cache_key: str, hit: bool, path) -> None:
        rel = None
        try:
            rel = path.relative_to(self._data_dir).as_posix()
        except ValueError:
            rel = str(path)
        repo.record_google_request(repo.GoogleRequestCreate(
            run_id=self._run_id, kind=kind, cache_key=cache_key, cache_hit=hit,
            est_cost_usd=0.0 if hit else EST_COST_USD[kind], path=rel,
        ))
        log.info("google.request", run_id=self._run_id, kind=kind, cache_hit=hit)

    def _get(self, url: str, params: dict[str, Any], *, kind: str, expect_image: bool) -> bytes:
        query = {**params, "key": self._key}
        last_exc: Exception | None = None
        for attempt in range(GOOGLE_RETRIES + 1):
            try:
                resp = self._http.get(url, params=query)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < GOOGLE_RETRIES:
                    self._sleep(2 ** attempt)
                    continue
                raise UpstreamError(
                    f"Google {kind} request failed after {GOOGLE_RETRIES} retries ({type(exc).__name__}).",
                    kind=kind,
                ) from exc
            if resp.status_code in RETRY_STATUSES and attempt < GOOGLE_RETRIES:
                self._sleep(2 ** attempt)
                continue
            return self._accept(resp, kind=kind, expect_image=expect_image)
        raise UpstreamError(f"Google {kind} request failed ({last_exc!r}).", kind=kind)

    @staticmethod
    def _accept(resp: httpx.Response, *, kind: str, expect_image: bool) -> bytes:
        content_type = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            api = {"satellite": "Maps Static API", "streetview": "Street View Static API",
                   "streetview_metadata": "Street View Static API"}[kind]
            hint = (f"check HERO_GOOGLE_MAPS_API_KEY and that the {api} is enabled"
                    if resp.status_code in (400, 401, 403) else "Google returned an error")
            snippet = resp.text[:200].replace("\n", " ") if not content_type.startswith("image/") else ""
            raise UpstreamError(
                f"Google {api} returned {resp.status_code}: {hint}. {snippet}".strip(),
                kind=kind, status=resp.status_code,
            )
        if expect_image and not content_type.startswith("image/"):
            raise UpstreamError(
                f"Google {kind} returned a non-image response ({content_type or 'no content-type'}): "
                f"{resp.text[:200]}", kind=kind, status=resp.status_code,
            )
        return resp.content
