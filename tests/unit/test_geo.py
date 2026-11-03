"""hero.scene.geo: haversine, bearing, local frame <-> lat/lon, Web-Mercator tile px, frame affine."""

from __future__ import annotations

import math

import pytest

from hero.scene.geo import (
    LocalFrame,
    TileGeoref,
    apply_frame_to_px,
    bearing_deg,
    compute_frame_to_px,
    haversine_m,
    latlon_to_tile_px,
    mean_lane_lat_m,
    meters_per_px,
    object_lat_m,
    tile_px_to_latlon,
)
from hero.scene.schema import FootpathSide, load_example_scene

START = (12.97400, 77.64050)
END = (12.97467, 77.64058)


def test_haversine_matches_known_distances():
    assert haversine_m(0, 0, 0, 0) == 0.0
    # spec/data.md rounds the example length to 74.6; the true great-circle distance is 75.0
    assert haversine_m(*START, *END) == pytest.approx(75.0, abs=0.1)
    # one degree of latitude at the equator ~ 111.2 km
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)


def test_bearing_cardinal_directions_and_example_segment():
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0.0, abs=1e-6)
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90.0, abs=1e-6)
    assert bearing_deg(0, 0, -1, 0) == pytest.approx(180.0, abs=1e-6)
    assert bearing_deg(0, 0, 0, -1) == pytest.approx(270.0, abs=1e-6)
    assert bearing_deg(*START, *END) == pytest.approx(6.7, abs=0.2)


def test_local_frame_from_endpoints_derives_length_and_heading():
    frame = LocalFrame.from_endpoints(*START, *END, footpath_side=FootpathSide.left)
    assert frame.length_m == pytest.approx(75.0, abs=0.1)
    assert frame.heading_deg == pytest.approx(6.7, abs=0.2)
    assert frame.origin_lat == START[0] and frame.origin_lon == START[1]


def test_local_frame_round_trips_and_end_point_lands_on_end():
    frame = LocalFrame.from_endpoints(*START, *END, footpath_side=FootpathSide.left)

    end = frame.to_latlon(frame.length_m, 0.0)
    assert haversine_m(end.lat, end.lon, *END) < 0.2

    x_m, lat_m = frame.from_latlon(end.lat, end.lon)
    assert x_m == pytest.approx(frame.length_m, abs=0.05)
    assert lat_m == pytest.approx(0.0, abs=0.05)

    p = frame.to_latlon(12.5, 4.5)
    assert frame.from_latlon(p.lat, p.lon) == pytest.approx((12.5, 4.5), abs=0.01)


def test_positive_lat_m_points_to_the_footpath_side():
    # heading ~north: left is west (smaller lon), right is east (larger lon)
    left = LocalFrame.from_endpoints(*START, *END, footpath_side=FootpathSide.left)
    right = LocalFrame.from_endpoints(*START, *END, footpath_side=FootpathSide.right)
    assert left.to_latlon(0.0, 5.0).lon < START[1]
    assert right.to_latlon(0.0, 5.0).lon > START[1]


def test_meters_per_px_matches_example_ground():
    assert meters_per_px(12.974335, zoom=19, scale=2) == pytest.approx(0.1455, abs=0.0005)
    assert meters_per_px(0.0, zoom=0, scale=1) == pytest.approx(156_543.03, rel=1e-4)


def test_web_mercator_tile_px_center_and_round_trip():
    georef = TileGeoref(center_lat=12.974335, center_lon=77.64054, zoom=19, scale=2, size_px=(1280, 1280))

    assert latlon_to_tile_px(12.974335, 77.64054, georef) == pytest.approx((640.0, 640.0), abs=1e-6)
    px = latlon_to_tile_px(*START, georef)
    assert px == pytest.approx((610.2, 894.8), abs=2.0)  # spec origin_px is hand-rounded
    back = tile_px_to_latlon(px[0], px[1], georef)
    assert haversine_m(back.lat, back.lon, *START) < 0.05


def test_compute_frame_to_px_reproduces_example_affine():
    frame = LocalFrame(origin_lat=START[0], origin_lon=START[1], heading_deg=6.7, length_m=74.6,
                       footpath_side=FootpathSide.left)
    georef = TileGeoref(center_lat=12.974335, center_lon=77.64054, zoom=19, scale=2, size_px=(1280, 1280))

    affine = compute_frame_to_px(frame, georef)

    assert affine.origin_px == pytest.approx((610.2, 894.8), abs=2.0)  # spec origin_px is hand-rounded
    assert affine.along_px_per_m == pytest.approx((0.80, -6.83), abs=0.02)
    assert affine.across_px_per_m == pytest.approx((-6.83, -0.80), abs=0.02)


def test_right_side_footpath_flips_the_across_vector():
    frame = LocalFrame(origin_lat=START[0], origin_lon=START[1], heading_deg=6.7, length_m=74.6,
                       footpath_side=FootpathSide.right)
    georef = TileGeoref(center_lat=12.974335, center_lon=77.64054, zoom=19, scale=2, size_px=(1280, 1280))
    affine = compute_frame_to_px(frame, georef)
    assert affine.across_px_per_m == pytest.approx((6.83, 0.80), abs=0.02)


def test_mean_lane_lat_m_and_object_lat_m():
    scene = load_example_scene()
    lanes = scene.cross_section.lanes
    assert mean_lane_lat_m([0, 1], lanes) == pytest.approx(4.5)
    assert mean_lane_lat_m([2], lanes) == pytest.approx(1.8)
    assert object_lat_m(scene.objects[0], lanes) == pytest.approx(4.5)
    with pytest.raises(ValueError):
        mean_lane_lat_m([7], lanes)


def test_shipped_example_tile_px_and_latlon_are_consistent_within_tolerance():
    """spec/data.md: every object/token's tile_px and latlon must match the affine + local frame."""
    scene = load_example_scene()
    seg = scene.segment
    frame = LocalFrame(origin_lat=seg.start.lat, origin_lon=seg.start.lon, heading_deg=seg.heading_deg,
                       length_m=seg.length_m, footpath_side=seg.footpath_side)
    lanes = scene.cross_section.lanes

    checked = 0
    for obj in scene.objects:
        lat_m = object_lat_m(obj, lanes)
        px = apply_frame_to_px(scene.ground.frame_to_px, obj.x_m, lat_m)
        assert px == pytest.approx(tuple(obj.tile_px), abs=1.0), obj.id
        ll = frame.to_latlon(obj.x_m, lat_m)
        assert haversine_m(ll.lat, ll.lon, obj.latlon.lat, obj.latlon.lon) < 0.5, obj.id
        checked += 1
    for tok in scene.tokens:
        lat_m = mean_lane_lat_m([tok.lane], lanes)
        px = apply_frame_to_px(scene.ground.frame_to_px, tok.x_m, lat_m)
        assert px == pytest.approx(tuple(tok.tile_px), abs=1.0), tok.id
        ll = frame.to_latlon(tok.x_m, lat_m)
        assert haversine_m(ll.lat, ll.lon, tok.latlon.lat, tok.latlon.lon) < 0.5, tok.id
        checked += 1
    assert checked == 7


def test_example_latlon_projects_onto_tile_px_through_web_mercator():
    scene = load_example_scene()
    g = scene.ground
    georef = TileGeoref(center_lat=g.center.lat, center_lon=g.center.lon, zoom=g.zoom, scale=g.scale,
                        size_px=tuple(g.size_px))
    for obj in scene.objects:
        px = latlon_to_tile_px(obj.latlon.lat, obj.latlon.lon, georef)
        assert math.dist(px, tuple(obj.tile_px)) < 2.5, obj.id  # example origin_px is rounded (~1.8 px)
