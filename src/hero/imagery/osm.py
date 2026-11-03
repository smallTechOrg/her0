"""Overpass bbox query -> ways (highway / footway / building) projected into the local frame.

Cached under ``data/osm/<sha256>.json``; 30 s timeout, 3 retries with backoff. Any failure raises
``UpstreamError`` - the ``resolve_geometry`` step turns that into ``osm=None`` + a warning.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from hero.imagery.cache import ImageryCache
from hero.llm.errors import UpstreamError
from hero.scene.geo import LocalFrame

OVERPASS_TIMEOUT_S = 30.0
OVERPASS_RETRIES = 3
USER_AGENT = "hero-street-runner/0.1 (+local; contact via repo)"  # overpass-api.de rejects the default UA with 406
BBOX_LATERAL_M = 60.0
BBOX_ALONG_MARGIN_M = 25.0
QUERY_TEMPLATE = (
    "[out:json][timeout:25];"
    "(way[\"highway\"]({bbox});way[\"building\"]({bbox});way[\"building:part\"]({bbox}););"
    "out geom;"
)
DEFAULT_LEVEL_HEIGHT_M = 3.2
DEFAULT_BUILDING_HEIGHT_M = 9.0


@dataclass(frozen=True)
class OsmWay:
    id: int
    kind: str  # highway | footway | building
    tags: dict[str, str]
    points: list[tuple[float, float]]  # (x_m, lat_m) in the local frame
    latlons: list[tuple[float, float]]

    @property
    def is_closed(self) -> bool:
        return len(self.latlons) >= 4 and self.latlons[0] == self.latlons[-1]

    def height_m(self) -> float:
        tags = self.tags
        try:
            if "height" in tags:
                return max(2.5, float(str(tags["height"]).split()[0]))
            if "building:levels" in tags:
                return max(2.5, float(tags["building:levels"]) * DEFAULT_LEVEL_HEIGHT_M)
        except ValueError:
            pass
        return DEFAULT_BUILDING_HEIGHT_M


@dataclass
class OsmGeometry:
    bbox: tuple[float, float, float, float]  # south, west, north, east
    ways: list[OsmWay] = field(default_factory=list)
    cache_path: str | None = None
    cache_hit: bool = False

    @property
    def buildings(self) -> list[OsmWay]:
        return [w for w in self.ways if w.kind == "building"]

    @property
    def highways(self) -> list[OsmWay]:
        return [w for w in self.ways if w.kind in ("highway", "footway")]

    def summary(self, length_m: float) -> str:
        roads = [w for w in self.highways if w.kind == "highway"]
        foots = [w for w in self.highways if w.kind == "footway"]
        names = sorted({w.tags.get("name", "") for w in roads if w.tags.get("name")})
        near = [b for b in self.buildings if any(abs(lat) <= 40 and -10 <= x <= length_m + 10 for x, lat in b.points)]
        parts = [
            f"{len(roads)} road way(s)" + (f" named {', '.join(names[:4])}" if names else ""),
            f"{len(foots)} footway/path way(s)",
            f"{len(near)} building footprint(s) within 40 m of the segment",
        ]
        return "; ".join(parts)


def bbox_for(frame: LocalFrame) -> tuple[float, float, float, float]:
    corners = [frame.to_latlon(x, lat) for x in (-BBOX_ALONG_MARGIN_M, frame.length_m + BBOX_ALONG_MARGIN_M)
               for lat in (-BBOX_LATERAL_M, BBOX_LATERAL_M)]
    lats = [c.lat for c in corners]
    lons = [c.lon for c in corners]
    return (round(min(lats), 5), round(min(lons), 5), round(max(lats), 5), round(max(lons), 5))


def _classify(tags: dict[str, Any]) -> str | None:
    if "building" in tags or "building:part" in tags:
        return "building"
    highway = tags.get("highway")
    if highway in ("footway", "path", "pedestrian", "steps", "sidewalk"):
        return "footway"
    if highway:
        return "highway"
    return None


def parse_overpass(body: dict[str, Any], frame: LocalFrame) -> list[OsmWay]:
    ways: list[OsmWay] = []
    for element in body.get("elements", []):
        if element.get("type") != "way":
            continue
        tags = {str(k): str(v) for k, v in (element.get("tags") or {}).items()}
        kind = _classify(tags)
        if kind is None:
            continue
        latlons = [(float(p["lat"]), float(p["lon"])) for p in element.get("geometry", []) if "lat" in p]
        if len(latlons) < 2:
            continue
        points = [frame.from_latlon(lat, lon) for lat, lon in latlons]
        ways.append(OsmWay(id=int(element["id"]), kind=kind, tags=tags, points=points, latlons=latlons))
    return ways


def fetch_osm(frame: LocalFrame, cache: ImageryCache, overpass_url: str, *, http: httpx.Client | None = None,
              sleep: Callable[[float], None] = time.sleep) -> OsmGeometry:
    bbox = bbox_for(frame)
    bbox_str = ",".join(f"{v:.5f}" for v in bbox)
    query = QUERY_TEMPLATE.format(bbox=bbox_str)
    owned = http is None
    client = http or httpx.Client(timeout=OVERPASS_TIMEOUT_S, headers={"User-Agent": USER_AGENT})

    def fetch() -> bytes:
        last: Exception | None = None
        for attempt in range(OVERPASS_RETRIES + 1):
            try:
                resp = client.post(overpass_url, data={"data": query})
            except httpx.HTTPError as exc:
                last = exc
                if attempt < OVERPASS_RETRIES:
                    sleep(2 ** attempt)
                    continue
                raise UpstreamError(f"Overpass unreachable after retries ({type(exc).__name__}).", kind="osm") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < OVERPASS_RETRIES:
                sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise UpstreamError(f"Overpass returned HTTP {resp.status_code}: {resp.text[:200]}",
                                    kind="osm", status=resp.status_code)
            try:
                json.loads(resp.content)
            except ValueError as exc:
                raise UpstreamError("Overpass returned a non-JSON body.", kind="osm") from exc
            return resp.content
        raise UpstreamError(f"Overpass failed ({last!r}).", kind="osm")

    try:
        entry = cache.get_or_fetch("overpass", {"bbox": bbox_str, "query": "highway+building+part"}, fetch, suffix="json")
    finally:
        if owned:
            client.close()
    body = json.loads(entry.path.read_text(encoding="utf-8"))
    return OsmGeometry(bbox=bbox, ways=parse_overpass(body, frame), cache_path=str(entry.path), cache_hit=entry.hit)
