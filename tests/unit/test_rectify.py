"""rectify_window: straighten a tile along the reference line (straight and curved)."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from hero.imagery.rectify import rectify_window
from hero.model.refline import RectGrid
from hero.scene.geo import TileGeoref, latlon_to_tile_px
from tests.fixtures.google.fake_google import HERE
from tests.fixtures.street_fixtures import curved_reference_line, straight_reference_line

MPP_OUT = 0.1


def georef_for(ref, zoom=19) -> TileGeoref:
    lat, lon = ref.point_at(ref.length_s / 2, 0.0)
    return TileGeoref(center_lat=lat, center_lon=lon, zoom=zoom, scale=2, size_px=(1280, 1280))


def marked_tile(tmp_path, ref, georef, s_values, *, t=0.0, radius=3):
    img = Image.new("RGB", (1280, 1280), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for s in s_values:
        lat, lon = ref.point_at(s, t)
        x, y = latlon_to_tile_px(lat, lon, georef)
        d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(255, 255, 255))
    path = tmp_path / "tile.png"
    img.save(path)
    return path


def centroid_x_near(img: Image.Image, y_px: float, half: int = 6):
    gray = img.convert("L")
    xs, ws = 0.0, 0.0
    for y in range(max(0, int(y_px) - half), min(img.height, int(y_px) + half + 1)):
        for x in range(img.width):
            v = gray.getpixel((x, y))
            if v > 128:
                xs += x * v
                ws += v
    return xs / ws + 0.5 if ws else None  # +0.5: pixel index -> pixel-centre (edge) coordinates, as in RectGrid


def symmetric_grid(s_top, s_bottom, half_t=6.0):
    return RectGrid(s_top=s_top, s_bottom=s_bottom, t_left=half_t, t_right=-half_t, m_per_px=MPP_OUT)


def test_output_has_the_grid_size_for_the_fixture_tile():
    ref = straight_reference_line(100.0)
    grid = symmetric_grid(70.0, 30.0)
    out = rectify_window(HERE / "satellite_tile.png", georef_for(ref), ref, grid, overlay=False)
    assert out.size == grid.size_px == (120, 400) and out.mode == "RGB"


@pytest.mark.parametrize("make_ref", [lambda: straight_reference_line(100.0),
                                      lambda: curved_reference_line(radius_m=80.0, length_m=100.0),
                                      lambda: curved_reference_line(radius_m=60.0, length_m=100.0, turn="left")],
                         ids=["straight", "right-arc", "left-arc"])
def test_points_on_the_reference_line_land_on_the_vertical_centre_line(tmp_path, make_ref):
    ref = make_ref()
    georef = georef_for(ref)
    s_marks = [35.0, 50.0, 65.0]
    tile = marked_tile(tmp_path, ref, georef, s_marks)
    grid = symmetric_grid(75.0, 25.0)
    out = rectify_window(tile, georef, ref, grid, overlay=False)
    cx = grid.st_to_px(50.0, 0.0)[0]
    for s in s_marks:
        y = grid.st_to_px(s, 0.0)[1]
        got = centroid_x_near(out, y, half=4)
        assert got is not None and abs(got - cx) <= 1.0, (s, got, cx)


def test_points_offset_to_the_left_land_on_the_left_of_the_output(tmp_path):
    ref = curved_reference_line()
    georef = georef_for(ref)
    tile = marked_tile(tmp_path, ref, georef, [50.0], t=3.0)
    grid = symmetric_grid(60.0, 40.0)
    out = rectify_window(tile, georef, ref, grid, overlay=False)
    expected_x, y = grid.st_to_px(50.0, 3.0)
    got = centroid_x_near(out, y, half=4)
    assert got is not None and abs(got - expected_x) <= 1.0 and got < grid.size_px[0] / 2


def test_window_beyond_the_tile_is_black_not_an_error(tmp_path):
    ref = straight_reference_line(400.0)
    georef = georef_for(ref)  # centred at 200 m; the 1280 px z19 tile only spans ~190 m
    path = tmp_path / "t.png"
    Image.new("RGB", (1280, 1280), (200, 200, 200)).save(path)
    out = rectify_window(path, georef, ref, symmetric_grid(400.0, 300.0), overlay=False)
    assert out.getpixel((60, 5)) == (0, 0, 0)


def test_overlay_draws_something_and_overlay_false_does_not(tmp_path):
    ref = straight_reference_line(100.0)
    georef = georef_for(ref)
    path = tmp_path / "t.png"
    Image.new("RGB", (1280, 1280), (60, 60, 60)).save(path)
    grid = symmetric_grid(70.0, 30.0)
    plain = rectify_window(path, georef, ref, grid, overlay=False)
    marked = rectify_window(path, georef, ref, grid, overlay=True)
    assert plain.getcolors(10) == [(plain.width * plain.height, (60, 60, 60))]
    assert marked.tobytes() != plain.tobytes()


def test_georef_that_disagrees_with_the_tile_size_is_refused(tmp_path):
    ref = straight_reference_line(100.0)
    path = tmp_path / "t.png"
    Image.new("RGB", (640, 640), (60, 60, 60)).save(path)
    with pytest.raises(ValueError, match="georef"):
        rectify_window(path, georef_for(ref), ref, symmetric_grid(70.0, 30.0))


def test_grid_st_px_roundtrip():
    grid = symmetric_grid(75.0, 25.0)
    for x, y in [(0, 0), (60.5, 250.25), (119, 499)]:
        s, t = grid.px_to_st(x, y)
        assert grid.st_to_px(s, t) == pytest.approx((x, y))
