"""Regenerate the Google/OSM response fixtures in this directory (deterministic, offline, $0).

    uv run python tests/fixtures/google/make_fixtures.py

The JSON files mirror the documented response shapes of Street View Static metadata, Nominatim reverse
(``polygon_geojson``) and Overpass ``out geom``; the images are small synthetic stand-ins (a textured satellite
tile, Google's grey "no imagery" placeholder, a street-level frame). They are NOT recordings of live traffic -
live traffic is exercised only by the single optional ``-m live`` test in tests/integration/test_imagery.py.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
TILE_PX = 1280


def _write_json(name: str, data: dict) -> None:
    (HERE / name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def satellite_tile() -> Image.Image:
    rng = random.Random(7)
    img = Image.new("RGB", (TILE_PX, TILE_PX), (92, 98, 84))
    d = ImageDraw.Draw(img)
    for _ in range(260):  # roofs / trees / paving: enough variance to be clearly not the placeholder
        x, y = rng.randrange(TILE_PX), rng.randrange(TILE_PX)
        w, h = rng.randrange(20, 120), rng.randrange(20, 120)
        c = (rng.randrange(40, 200), rng.randrange(40, 200), rng.randrange(40, 200))
        d.rectangle([x, y, x + w, y + h], fill=c)
    d.rectangle([TILE_PX // 2 - 40, 0, TILE_PX // 2 + 40, TILE_PX], fill=(60, 60, 64))  # a road down the middle
    return img


def placeholder_tile() -> Image.Image:
    img = Image.new("RGB", (TILE_PX, TILE_PX), (229, 227, 223))
    ImageDraw.Draw(img).text((TILE_PX // 2 - 110, TILE_PX // 2), "Sorry, we have no imagery here.", fill=(120, 120, 120))
    return img


def streetview_frame() -> Image.Image:
    img = Image.new("RGB", (640, 640), (150, 180, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 360, 640, 640], fill=(80, 80, 84))
    d.rectangle([0, 330, 640, 360], fill=(170, 165, 150))
    for x in range(40, 640, 160):
        d.rectangle([x, 120, x + 90, 330], fill=(190 - x // 8, 150, 120))
    return img


def main() -> None:
    satellite_tile().save(HERE / "satellite_tile.png", optimize=True)
    placeholder_tile().save(HERE / "placeholder_tile.png", optimize=True)
    streetview_frame().save(HERE / "streetview_frame.jpg", quality=70)

    _write_json("metadata_ok.json", {"copyright": "© Google", "date": "2023-03",
                                     "location": {"lat": 12.9741, "lng": 77.6405}, "pano_id": "PANO_TEMPLATE",
                                     "status": "OK"})
    _write_json("metadata_zero_results.json", {"status": "ZERO_RESULTS"})
    _write_json("metadata_not_found.json", {"status": "NOT_FOUND"})
    _write_json("error_403.json", {"error_message": "This API project is not authorized to use this API.",
                                   "status": "REQUEST_DENIED"})
    _write_json("nominatim_reverse_road.json", {
        "place_id": 1, "osm_type": "way", "osm_id": 4242, "category": "highway", "type": "primary",
        "name": "100 Feet Road", "display_name": "100 Feet Road, Indiranagar, Bengaluru, India",
        "geojson": {"type": "LineString", "coordinates": [[77.6405, 12.9730], [77.6405, 12.9740],
                                                          [77.6406, 12.9750], [77.6407, 12.9760]]}})
    _write_json("nominatim_reverse_not_road.json", {
        "place_id": 2, "osm_type": "way", "osm_id": 99, "category": "building", "type": "yes",
        "geojson": {"type": "Polygon", "coordinates": [[[77.6, 12.9], [77.61, 12.9], [77.61, 12.91], [77.6, 12.9]]]}})
    _write_json("overpass_roads.json", {"version": 0.6, "elements": [
        {"type": "way", "id": 7001, "tags": {"highway": "service", "name": "Back Lane"},
         "geometry": [{"lat": 12.97410, "lon": 77.64085}, {"lat": 12.97470, "lon": 77.64085}]},
        {"type": "way", "id": 7002, "tags": {"highway": "primary", "name": "100 Feet Road"},
         "geometry": [{"lat": 12.9730, "lon": 77.6405}, {"lat": 12.9740, "lon": 77.6405}, {"lat": 12.9750, "lon": 77.6406}]}]})
    _write_json("overpass_empty.json", {"version": 0.6, "elements": []})


if __name__ == "__main__":
    main()
