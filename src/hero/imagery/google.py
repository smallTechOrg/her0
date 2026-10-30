"""Google Static Maps (satellite tile), Street View Static metadata + image: cached, counted.

Every attempt - hit or miss - is recorded via ``repo.record_google_request`` with ``cache_hit``
and logged as ``google.request``. Retries: 3 with exponential backoff on 429/5xx/timeouts
(Google timeout 20 s). Auth / enablement errors are not retried and raise ``UpstreamError``
with a hint naming the env var and the API to enable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class StreetViewMeta:
    status: str
    pano_id: str | None
    lat: float | None
    lon: float | None
    cache_hit: bool

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def _fmt(value: float) -> str:
    return f"{value:.6f}"


class GoogleImagery:
    def __init__(self, settings: Settings, cache: ImageryCache, run_id: str | None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self._key = settings.google_maps_api_key.get_secret_value()
        self._cache = cache
        self._run_id = run_id
        self._data_dir = settings.data_dir_abs
        self._http = http or httpx.Client(timeout=GOOGLE_TIMEOUT_S)
        self._sleep = sleep

    # ------------------------------------------------------------ public

    def satellite_tile(self, lat: float, lon: float, zoom: int) -> FetchedImage:
        params = {"center": f"{_fmt(lat)},{_fmt(lon)}", "zoom": zoom, "size": TILE_SIZE, "scale": TILE_SCALE,
                  "maptype": "satellite"}
        return self._cached_image("satellite", STATIC_MAPS_URL, params, suffix="png")

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
                              lat=loc.get("lat"), lon=loc.get("lng"), cache_hit=entry.hit)

    def streetview_image(self, lat: float, lon: float, heading_deg: float) -> FetchedImage:
        params = {"location": f"{_fmt(lat)},{_fmt(lon)}", "heading": round(heading_deg, 1), "fov": PANO_FOV,
                  "pitch": PANO_PITCH, "size": PANO_SIZE, "radius": PANO_RADIUS_M, "source": "outdoor",
                  "return_error_code": "true"}
        return self._cached_image("streetview", STREETVIEW_URL, params, suffix="jpg")

    # ------------------------------------------------------------ internals

    def _cached_image(self, kind: str, url: str, params: dict[str, Any], *, suffix: str) -> FetchedImage:
        entry = self._cache.get_or_fetch(kind, params, lambda: self._get(url, params, kind=kind, expect_image=True),
                                         suffix=suffix)
        self._count(kind, entry.key, entry.hit, entry.path)
        rel = entry.path.relative_to(self._data_dir).as_posix()
        return FetchedImage(path=str(entry.path), rel_path=rel, url=f"/media/{rel}", cache_key=entry.key,
                            cache_hit=entry.hit)

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
