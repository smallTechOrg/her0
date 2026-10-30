"""Address -> segment: geocode a name/address, find the road's direction, build a centred segment.

The admin types an address (or pastes "lat, lon"); we never ask for coordinates. Google Geocoding
(same key as the imagery) gives primary candidates, Nominatim (OpenStreetMap, free) a second
opinion because Google can resolve a street name to a different neighbourhood. The road heading
comes from the nearest OSM highway (Overpass); when Overpass is down the caller gets
``heading_deg=None`` and the admin enters a heading by hand.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from hero.imagery.cache import ImageryCache
from hero.scene.geo import METERS_PER_DEG_LAT, haversine_m

USER_AGENT = "hero-street-runner/0.1 (+local; contact via repo)"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_HALF_LENGTH_M = 50.0
MAX_HALF_LENGTH_M = 100.0
DEDUP_RADIUS_M = 60.0
ROAD_SEARCH_RADIUS_M = 45
_DRIVABLE = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


@dataclass
class Candidate:
    id: str
    label: str
    lat: float
    lon: float
    provider: str
    heading_deg: float | None = None
    heading_source: str | None = None
    road_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "label": self.label, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "provider": self.provider, "heading_deg": None if self.heading_deg is None else round(self.heading_deg, 1),
            "heading_source": self.heading_source, "road_name": self.road_name,
        }
        if self.heading_deg is not None:
            out["travel_compass"] = compass(self.heading_deg)
            out["left_side"] = compass(self.heading_deg - 90)
            out["right_side"] = compass(self.heading_deg + 90)
        return out


def compass(deg: float) -> str:
    return _COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def parse_coords(text: str) -> tuple[float, float] | None:
    m = _COORDS.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def _fetch_json(cache: ImageryCache, kind: str, params: dict[str, Any], do_fetch) -> Any:
    entry = cache.get_or_fetch(kind, params, do_fetch, suffix="json")
    return json.loads(entry.path.read_text(encoding="utf-8"))


def geocode(query: str, city: str, *, google_key: str, cache: ImageryCache, http: httpx.Client | None = None,
            limit: int = 3) -> list[Candidate]:
    """Candidates for ``query`` (+ ``city`` when the query does not already contain it)."""
    coords = parse_coords(query)
    if coords:
        return [Candidate(id="coords", label=f"{coords[0]:.6f}, {coords[1]:.6f}", lat=coords[0], lon=coords[1],
                          provider="coordinates")]
    text = query if city.lower() in query.lower() else f"{query}, {city}"
    owned = http is None
    client = http or httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
    found: list[Candidate] = []
    try:
        if google_key:
            def g() -> bytes:
                r = client.get(GOOGLE_GEOCODE_URL, params={"address": text, "key": google_key, "region": "in"})
                r.raise_for_status()
                return r.content
            try:
                body = _fetch_json(cache, "geocode_google", {"q": text}, g)
                for i, res in enumerate(body.get("results", [])[:limit]):
                    loc = res["geometry"]["location"]
                    found.append(Candidate(id=f"google-{i}", label=res.get("formatted_address", text),
                                           lat=loc["lat"], lon=loc["lng"], provider="google"))
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        def n() -> bytes:
            r = client.get(NOMINATIM_URL, params={"q": text, "format": "json", "limit": limit})
            r.raise_for_status()
            return r.content
        try:
            for i, res in enumerate(_fetch_json(cache, "geocode_nominatim", {"q": text}, n)[:limit]):
                found.append(Candidate(id=f"osm-{i}", label=res.get("display_name", text), lat=float(res["lat"]),
                                       lon=float(res["lon"]), provider="openstreetmap",
                                       road_name=res.get("name") if res.get("class") == "highway" else None))
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    finally:
        if owned:
            client.close()
    unique: list[Candidate] = []
    for c in found:
        if all(haversine_m(c.lat, c.lon, u.lat, u.lon) > DEDUP_RADIUS_M for u in unique):
            unique.append(c)
    return unique


def _to_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    return ((lon - lon0) * METERS_PER_DEG_LAT * math.cos(math.radians(lat0)), (lat - lat0) * METERS_PER_DEG_LAT)


def heading_from_ways(ways: list[dict[str, Any]], lat: float, lon: float, span_m: float = 50.0) -> tuple[float, str | None] | None:
    """Direction (deg, [0,180)) of the drivable road nearest to the point, averaged over ``span_m`` each side."""
    best: tuple[float, list[tuple[float, float]], int, str | None] | None = None
    for way in ways:
        geom = way.get("geometry") or []
        pts = [_to_xy(lat, lon, p["lat"], p["lon"]) for p in geom]
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
            d = math.hypot(x1 + t * dx, y1 + t * dy)
            if best is None or d < best[0]:
                best = (d, pts, i, (way.get("tags") or {}).get("name"))
    if best is None:
        return None
    _, pts, i, name = best
    lo = i
    acc = 0.0
    while lo > 0 and acc < span_m:
        acc += math.hypot(pts[lo][0] - pts[lo - 1][0], pts[lo][1] - pts[lo - 1][1])
        lo -= 1
    hi = i + 1
    acc = 0.0
    while hi < len(pts) - 1 and acc < span_m:
        acc += math.hypot(pts[hi + 1][0] - pts[hi][0], pts[hi + 1][1] - pts[hi][1])
        hi += 1
    (xa, ya), (xb, yb) = pts[lo], pts[hi]
    brg = (math.degrees(math.atan2(xb - xa, yb - ya)) + 360.0) % 360.0
    return brg % 180.0, name


NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def reverse_heading(lat: float, lon: float, *, cache: ImageryCache,
                    http: httpx.Client | None = None) -> tuple[float, str | None] | None:
    """Heading of the nearest named road via Nominatim reverse (returns the road's LineString)."""
    owned = http is None
    client = http or httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT})

    def f() -> bytes:
        r = client.get(NOMINATIM_REVERSE_URL, params={"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
                                                      "zoom": 17, "polygon_geojson": 1})
        r.raise_for_status()
        return r.content

    try:
        body = _fetch_json(cache, "nominatim_reverse", {"lat": round(lat, 5), "lon": round(lon, 5)}, f)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owned:
            client.close()
    geo = body.get("geojson") or {}
    if body.get("category") != "highway" or geo.get("type") != "LineString":
        return None
    way = {"geometry": [{"lat": c[1], "lon": c[0]} for c in geo.get("coordinates", [])], "tags": {"name": body.get("name")}}
    return heading_from_ways([way], lat, lon)


def road_heading(lat: float, lon: float, *, cache: ImageryCache, overpass_url: str,
                 http: httpx.Client | None = None) -> tuple[float, str | None] | None:
    query = (f'[out:json][timeout:20];way(around:{ROAD_SEARCH_RADIUS_M},{lat:.6f},{lon:.6f})'
             f'[highway~"^({_DRIVABLE})$"];out geom;')
    owned = http is None
    client = http or httpx.Client(timeout=8.0, headers={"User-Agent": USER_AGENT})

    def f() -> bytes:
        last: Exception | None = None
        for _ in range(1):
            try:
                r = client.post(overpass_url, data={"data": query})
                if r.status_code == 200:
                    json.loads(r.content)
                    return r.content
                last = RuntimeError(f"HTTP {r.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"Overpass unavailable: {last!r}")

    try:
        body = _fetch_json(cache, "overpass_heading", {"lat": round(lat, 5), "lon": round(lon, 5), "r": ROAD_SEARCH_RADIUS_M}, f)
    except (RuntimeError, ValueError):
        return None
    finally:
        if owned:
            client.close()
    return heading_from_ways(body.get("elements", []), lat, lon)


def segment_endpoints(lat: float, lon: float, heading_deg: float, half_length_m: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Start/end points ``half_length_m`` either side of the centre along ``heading_deg``."""
    h = math.radians(heading_deg)
    dn, de = math.cos(h) * half_length_m, math.sin(h) * half_length_m
    per_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    return ((lat - dn / METERS_PER_DEG_LAT, lon - de / per_lon), (lat + dn / METERS_PER_DEG_LAT, lon + de / per_lon))


def resolve(query: str, city: str, *, google_key: str, data_dir, overpass_url: str, limit: int = 3) -> list[Candidate]:
    """Geocode and attach the road heading to every candidate."""
    geo_cache = ImageryCache(data_dir / "geocode")
    osm_cache = ImageryCache(data_dir / "osm")
    cands = geocode(query, city, google_key=google_key, cache=geo_cache, limit=limit)
    for c in cands:  # sequential: Nominatim's usage policy allows ~1 request/second (results are cached)
        got = reverse_heading(c.lat, c.lon, cache=geo_cache)
        source = "nearest OSM road"
        if got is None:
            got = road_heading(c.lat, c.lon, cache=osm_cache, overpass_url=overpass_url)
            source = "OSM highway (Overpass)"
        if got:
            c.heading_deg, c.road_name = got[0], got[1] or c.road_name
            c.heading_source = source
    return cands
