"""Address -> segment geometry: parsing, compass, centred endpoints, road heading, geocoder merging."""

from __future__ import annotations

import json

import httpx
import pytest

from hero.imagery import geocode as g
from hero.imagery.cache import ImageryCache
from hero.scene.geo import bearing_deg, haversine_m


def test_parse_coords_accepts_lat_lon_pairs_and_rejects_addresses():
    assert g.parse_coords("12.9716, 77.5946") == (12.9716, 77.5946)
    assert g.parse_coords("12.9716 77.5946") == (12.9716, 77.5946)
    assert g.parse_coords("100 Feet Road") is None
    assert g.parse_coords("123, 77.5") is None  # latitude out of range


@pytest.mark.parametrize("deg,name", [(0, "north"), (44, "north-east"), (90, "east"), (181, "south"), (-90, "west"), (359, "north")])
def test_compass(deg, name):
    assert g.compass(deg) == name


@pytest.mark.parametrize("heading", [0, 20, 90, 135, 179])
@pytest.mark.parametrize("half", [50, 75, 100])
def test_segment_endpoints_are_centred_and_half_length_either_way(heading, half):
    lat, lon = 12.975, 77.641
    (slat, slon), (elat, elon) = g.segment_endpoints(lat, lon, heading, half)
    assert haversine_m(slat, slon, elat, elon) == pytest.approx(2 * half, abs=0.5)
    assert haversine_m(lat, lon, slat, slon) == pytest.approx(half, abs=0.5)
    assert haversine_m(lat, lon, elat, elon) == pytest.approx(half, abs=0.5)
    assert bearing_deg(slat, slon, elat, elon) == pytest.approx(heading, abs=0.5)


def _way(points, name="Road"):
    return {"geometry": [{"lat": a, "lon": b} for a, b in points], "tags": {"name": name}}


def test_heading_from_a_north_south_road_is_zero():
    ways = [_way([(12.9740, 77.6410), (12.9750, 77.6410), (12.9760, 77.6410)])]
    h, name = g.heading_from_ways(ways, 12.9750, 77.64105)
    assert h == pytest.approx(0, abs=1) or h == pytest.approx(180, abs=1)
    assert name == "Road"


def test_heading_normalises_travel_direction_into_0_180():
    south = [_way([(12.9760, 77.6410), (12.9750, 77.6410), (12.9740, 77.6410)])]
    h, _ = g.heading_from_ways(south, 12.9750, 77.6410)
    assert 0 <= h < 180


def test_heading_picks_the_nearest_of_two_crossing_roads():
    ns = _way([(12.9740, 77.6410), (12.9760, 77.6410)], "NS")
    ew = _way([(12.9750, 77.6400), (12.9750, 77.6420)], "EW")
    # point 5 m east of the N-S road, 40 m north... closer to NS road than EW
    h, name = g.heading_from_ways([ns, ew], 12.9745, 77.64105)
    assert name == "NS" and (h < 5 or h > 175)
    h2, name2 = g.heading_from_ways([ns, ew], 12.97505, 77.6415)
    assert name2 == "EW" and h2 == pytest.approx(90, abs=2)


def test_heading_with_no_ways_is_none():
    assert g.heading_from_ways([], 12.9, 77.6) is None


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_geocode_merges_google_and_nominatim_dedups_nearby_and_appends_city(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "googleapis" in req.url.host or "geocode" in req.url.path:
            return httpx.Response(200, json={"status": "OK", "results": [
                {"formatted_address": "100 Feet Rd, Bengaluru", "geometry": {"location": {"lat": 12.9506, "lng": 77.6392}}},
                {"formatted_address": "Near duplicate", "geometry": {"location": {"lat": 12.95062, "lng": 77.63921}}}]})
        return httpx.Response(200, json=[{"display_name": "100 Feet Road, Indiranagar", "lat": "12.9711", "lon": "77.6413",
                                          "class": "highway", "name": "100 Feet Road"}])

    cands = g.geocode("100 Feet Road", "Bengaluru", google_key="k", cache=ImageryCache(tmp_path), http=_client(handler))
    assert [c.provider for c in cands] == ["google", "openstreetmap"]  # near-duplicate removed
    assert cands[1].road_name == "100 Feet Road"
    assert any("100+Feet+Road%2C+Bengaluru" in u or "100%20Feet%20Road%2C%20Bengaluru" in u for u in seen)


def test_geocode_survives_google_failure_and_uses_nominatim(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if "googleapis" in req.url.host:
            return httpx.Response(500)
        return httpx.Response(200, json=[{"display_name": "X", "lat": "1.0", "lon": "2.0"}])

    cands = g.geocode("somewhere", "City", google_key="k", cache=ImageryCache(tmp_path), http=_client(handler))
    assert [c.provider for c in cands] == ["openstreetmap"]


def test_geocode_of_pasted_coordinates_makes_no_network_calls(tmp_path):
    def boom(req):  # pragma: no cover
        raise AssertionError("no network expected")

    cands = g.geocode("12.9716, 77.5946", "Bengaluru", google_key="k", cache=ImageryCache(tmp_path), http=_client(boom))
    assert len(cands) == 1 and cands[0].provider == "coordinates" and cands[0].lat == 12.9716


def test_reverse_heading_reads_the_road_linestring_and_ignores_non_roads(tmp_path):
    road = {"category": "highway", "name": "100 Feet Road", "geojson": {"type": "LineString",
            "coordinates": [[77.6410, 12.9740], [77.6410, 12.9760]]}}
    building = {"category": "building", "name": "Shop", "geojson": {"type": "Polygon", "coordinates": []}}
    got = g.reverse_heading(12.975, 77.641, cache=ImageryCache(tmp_path / "a"), http=_client(lambda r: httpx.Response(200, json=road)))
    assert got is not None and got[1] == "100 Feet Road" and (got[0] < 2 or got[0] > 178)
    assert g.reverse_heading(12.975, 77.641, cache=ImageryCache(tmp_path / "b"),
                             http=_client(lambda r: httpx.Response(200, json=building))) is None


def test_candidate_dict_names_which_compass_side_is_left_and_right():
    d = g.Candidate(id="x", label="L", lat=1, lon=2, provider="google", heading_deg=0).to_dict()
    assert (d["travel_compass"], d["left_side"], d["right_side"]) == ("north", "west", "east")
    assert "left_side" not in g.Candidate(id="y", label="L", lat=1, lon=2, provider="google").to_dict()
