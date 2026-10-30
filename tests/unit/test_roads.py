"""hero.imagery.roads: Nominatim reverse -> Overpass -> None, cached, never raises."""

from __future__ import annotations

import httpx
import pytest

from hero.imagery.cache import ImageryCache
from hero.imagery.roads import RoadPolyline, distance_to_polyline_m, fetch_road_polyline
from tests.fixtures.google.fake_google import fixture_json

LAT, LON = 12.9740, 77.6405
OVERPASS = "https://overpass.test/api/interpreter"


def transport(nominatim=None, overpass=None, log=None):
    """nominatim/overpass: dict body, int status, or an Exception to raise."""
    def handler(req: httpx.Request) -> httpx.Response:
        which = "nominatim" if "nominatim" in req.url.host else "overpass"
        if log is not None:
            log.append(which)
        spec = nominatim if which == "nominatim" else overpass
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, int):
            return httpx.Response(spec, text="busy")
        return httpx.Response(200, json=spec)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("hero.imagery.roads.time.sleep", lambda _s: None)


def fetch(tmp_path, http):
    return fetch_road_polyline(LAT, LON, cache=ImageryCache(tmp_path), overpass_url=OVERPASS, http=http)


def test_nominatim_road_becomes_a_polyline_with_way_id_and_name(tmp_path):
    road = fetch(tmp_path, transport(nominatim=fixture_json("nominatim_reverse_road.json")))
    assert isinstance(road, RoadPolyline) and road.source == "osm_nominatim"
    assert road.osm_way_id == 4242 and road.name == "100 Feet Road" and road.tags["highway"] == "primary"
    assert road.latlons[0] == (12.9730, 77.6405) and len(road.latlons) == 4  # GeoJSON lon,lat flipped to lat,lon


def test_non_road_nominatim_answer_falls_back_to_overpass_and_picks_the_nearest_main_road(tmp_path):
    log: list[str] = []
    road = fetch(tmp_path, transport(nominatim=fixture_json("nominatim_reverse_not_road.json"),
                                     overpass=fixture_json("overpass_roads.json"), log=log))
    assert log == ["nominatim", "overpass"]
    assert road.source == "osm_overpass" and road.osm_way_id == 7002 and road.name == "100 Feet Road"


def test_overpass_failure_and_empty_answers_degrade_to_none(tmp_path):
    bad = fixture_json("nominatim_reverse_not_road.json")
    assert fetch(tmp_path / "a", transport(nominatim=bad, overpass=503)) is None
    assert fetch(tmp_path / "b", transport(nominatim=bad, overpass=fixture_json("overpass_empty.json"))) is None
    assert fetch(tmp_path / "c", transport(nominatim=httpx.ConnectError("down"), overpass=httpx.ConnectTimeout("slow"))) is None


def test_nominatim_down_but_overpass_up_still_finds_the_road(tmp_path):
    road = fetch(tmp_path, transport(nominatim=500, overpass=fixture_json("overpass_roads.json")))
    assert road is not None and road.source == "osm_overpass"


def test_road_far_from_the_query_point_is_rejected(tmp_path):
    far = fixture_json("nominatim_reverse_road.json")
    far["geojson"]["coordinates"] = [[77.70, 12.90], [77.71, 12.91]]
    assert fetch(tmp_path, transport(nominatim=far, overpass=fixture_json("overpass_empty.json"))) is None


def test_second_lookup_is_served_from_the_cache(tmp_path):
    log: list[str] = []
    http = transport(nominatim=fixture_json("nominatim_reverse_road.json"), log=log)
    first = fetch(tmp_path, http)
    second = fetch(tmp_path, http)
    assert log == ["nominatim"] and second.latlons == first.latlons


def test_malformed_json_degrades_instead_of_raising(tmp_path):
    def handler(req):
        return httpx.Response(200, text="<html>not json</html>")
    assert fetch(tmp_path, httpx.Client(transport=httpx.MockTransport(handler))) is None


def test_distance_to_polyline():
    pts = [(12.9730, 77.6405), (12.9750, 77.6405)]
    assert distance_to_polyline_m(12.9740, 77.6405, pts) == pytest.approx(0.0, abs=0.01)
    assert distance_to_polyline_m(12.9740, 77.6405 + 0.0001, pts) == pytest.approx(10.85, abs=0.3)
