"""Road polyline near a point: Nominatim reverse (``polygon_geojson``) first, Overpass second, ``None`` = degrade.

Both answers are cached on disk (``ImageryCache``), carry no API key, and never raise: any network / parse failure
returns ``None`` so the caller falls back to a straight chord with a warning (Overpass is flaky by nature).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from hero.imagery.cache import ImageryCache
from hero.imagery.geocode import USER_AGENT
from hero.logging import get_logger
from hero.scene.geo import METERS_PER_DEG_LAT

log = get_logger("hero.imagery")

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_TIMEOUT_S = 10.0
OVERPASS_TIMEOUT_S = 15.0
OVERPASS_RETRIES = 1
SEARCH_RADIUS_M = 40
MAX_ROAD_DISTANCE_M = 60.0
_DRIVABLE = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|"
             "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")
_RANK = {"motorway": 0, "trunk": 0, "primary": 0, "secondary": 0, "tertiary": 1, "unclassified": 1,
         "residential": 1, "living_street": 2, "service": 3}


@dataclass
class RoadPolyline:
    latlons: list[tuple[float, float]]
    source: Literal["osm_nominatim", "osm_overpass"]
    osm_way_id: int | None = None
    name: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


def distance_to_polyline_m(lat: float, lon: float, latlons: list[tuple[float, float]]) -> float:
    m_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return (p[1] - lon) * m_lon, (p[0] - lat) * METERS_PER_DEG_LAT

    best = math.inf
    pts = [xy(p) for p in latlons]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        u = 0.0 if seg2 == 0 else min(1.0, max(0.0, -(x0 * dx + y0 * dy) / seg2))
        best = min(best, math.hypot(x0 + u * dx, y0 + u * dy))
    if len(pts) == 1:
        best = math.hypot(*pts[0])
    return best


def _json(cache: ImageryCache, kind: str, params: dict[str, Any], fetch) -> Any:
    entry = cache.get_or_fetch(kind, params, fetch, suffix="json")
    return json.loads(entry.path.read_text(encoding="utf-8"))


def _nominatim(lat: float, lon: float, cache: ImageryCache, http: httpx.Client) -> RoadPolyline | None:
    params = {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 17, "polygon_geojson": 1}

    def fetch() -> bytes:
        resp = http.get(NOMINATIM_REVERSE_URL, params=params, headers={"User-Agent": USER_AGENT},
                        timeout=NOMINATIM_TIMEOUT_S)
        resp.raise_for_status()
        return resp.content

    body = _json(cache, "nominatim_reverse", params, fetch)
    geo = body.get("geojson") or {}
    if body.get("category") != "highway" or geo.get("type") not in ("LineString", "MultiLineString"):
        return None
    lines = [geo["coordinates"]] if geo["type"] == "LineString" else list(geo["coordinates"])
    latlons: list[tuple[float, float]] = []
    for line in lines:  # MultiLineString: keep the parts that touch each other in order given
        latlons.extend((float(c[1]), float(c[0])) for c in line)
    if len(latlons) < 2 or distance_to_polyline_m(lat, lon, latlons) > MAX_ROAD_DISTANCE_M:
        return None
    way_id = body.get("osm_id") if body.get("osm_type") == "way" else None
    name = body.get("name") or (body.get("address") or {}).get("road")
    return RoadPolyline(latlons=latlons, source="osm_nominatim", osm_way_id=int(way_id) if way_id else None,
                        name=name, tags={"highway": str(body.get("type", "")), **({"name": name} if name else {})})


def _overpass(lat: float, lon: float, cache: ImageryCache, overpass_url: str, http: httpx.Client) -> RoadPolyline | None:
    query = (f'[out:json][timeout:12];way(around:{SEARCH_RADIUS_M},{lat:.6f},{lon:.6f})'
             f'["highway"~"^({_DRIVABLE})$"];out geom;')
    params = {"url": overpass_url, "query": query}

    def fetch() -> bytes:
        last: Exception | None = None
        for attempt in range(OVERPASS_RETRIES + 1):
            try:
                resp = http.post(overpass_url, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                 timeout=OVERPASS_TIMEOUT_S)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < OVERPASS_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.content
            except httpx.TransportError as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise last or RuntimeError("overpass failed")

    body = _json(cache, "overpass_road", params, fetch)
    best: tuple[tuple[int, float], dict[str, Any], list[tuple[float, float]]] | None = None
    for el in body.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        pts = [(float(g["lat"]), float(g["lon"])) for g in el["geometry"]]
        if len(pts) < 2:
            continue
        tags = el.get("tags", {})
        key = (_RANK.get(tags.get("highway", ""), 4), distance_to_polyline_m(lat, lon, pts))
        # prefer the nearest way; the class rank only breaks near-ties (within 8 m) towards the main road
        if best is None or key[1] + 8.0 * key[0] < best[0][1] + 8.0 * best[0][0]:
            best = (key, el, pts)
    if best is None or best[0][1] > MAX_ROAD_DISTANCE_M:
        return None
    _, el, pts = best
    tags = {str(k): str(v) for k, v in el.get("tags", {}).items()}
    return RoadPolyline(latlons=pts, source="osm_overpass", osm_way_id=int(el["id"]), name=tags.get("name"), tags=tags)


def fetch_road_polyline(lat: float, lon: float, *, cache: ImageryCache, overpass_url: str,
                        http: httpx.Client | None = None) -> RoadPolyline | None:
    client = http or httpx.Client()
    try:
        for name, attempt in (("nominatim", lambda: _nominatim(lat, lon, cache, client)),
                              ("overpass", lambda: _overpass(lat, lon, cache, overpass_url, client))):
            try:
                road = attempt()
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                log.warning("roads.degraded", provider=name, error=f"{type(exc).__name__}: {str(exc)[:120]}")
                continue
            if road is not None:
                log.info("roads.found", provider=name, osm_way_id=road.osm_way_id, points=len(road.latlons))
                return road
        return None
    finally:
        if http is None:
            client.close()
