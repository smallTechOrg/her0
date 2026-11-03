"""Frame / projection correctness of the texture bake (slice S1e): the strip is the true, unmirrored (t, s) frame."""

import math

from PIL import Image

from hero.imagery import bake
from hero.imagery.bake import facade_uv, ground_uv
from hero.model.types import Building, ObservationKind
from tests.fixtures.bake import synth
from tests.fixtures.street_fixtures import curved_reference_line, make_model, straight_reference_line


def _find(img: Image.Image, pred, box):
    x0, y0, x1, y1 = box
    return [(x, y) for y in range(max(0, y0), min(img.height, y1)) for x in range(max(0, x0), min(img.width, x1))
            if pred(img.getpixel((x, y)))]


def _red(p):
    return p[0] > 170 and p[1] < 90 and p[2] < 90


def _blue(p):
    return p[2] > 170 and p[0] < 90 and p[1] < 90


def _bake_markers(ref, tmp_path, markers):
    model = make_model(ref)
    geo = synth.tile_georef(ref, 50.0, 20, 1000)
    obs = synth.satellite_obs(ref, geo, tmp_path, "sat-1", markers)
    return model, bake.bake_ground(model, None, [obs], tmp_path)


def _expected_px(g, s, t, length):
    u, v = ground_uv(s, t, length)
    return u * g.image.width, v * g.image.height


def test_uv_helpers_match_the_contract():
    assert ground_uv(0.0, 14.0, 100.0) == (0.0, 1.0)      # column 0 = t +14, bottom row = s 0
    assert ground_uv(100.0, -14.0, 100.0) == (1.0, 0.0)   # right edge = t -14, row 0 = s L
    assert facade_uv(10.0, 0.0, 100.0, "left") == (0.1, 0.5)
    assert facade_uv(10.0, 12.0, 100.0, "left") == (0.1, 0.0)
    assert facade_uv(10.0, 6.0, 100.0, "right") == (0.1, 0.75)
    assert facade_uv(10.0, 40.0, 100.0, "right")[1] == 0.5  # heights above 12 m clamp


def test_markers_land_at_contract_uv_on_straight_and_curved_lines(tmp_path):
    for name, ref in (("straight", straight_reference_line(100.0)), ("curved", curved_reference_line())):
        d = tmp_path / name
        L = ref.length_s
        markers = [(35.0, 9.0, "red"), (65.0, -9.0, "blue")]
        _model, g = _bake_markers(ref, d, markers)
        for (s, t, col) in markers:
            ex, ey = _expected_px(g, s, t, L)
            hits = _find(g.image, _red if col == "red" else _blue, (int(ex) - 10, int(ey) - 10, int(ex) + 10, int(ey) + 10))
            assert len(hits) >= 30, f"{name}: {col} marker not found near ({ex:.0f},{ey:.0f})"
            cx = sum(h[0] for h in hits) / len(hits)
            cy = sum(h[1] for h in hits) / len(hits)
            assert abs(cx - ex) < 2.5 and abs(cy - ey) < 2.5, f"{name}: {col} at ({cx:.1f},{cy:.1f}) expected ({ex:.1f},{ey:.1f})"


def test_not_mirrored_left_is_left_and_travel_is_up(tmp_path):
    ref = straight_reference_line(100.0)
    _model, g = _bake_markers(ref, tmp_path, [(35.0, 9.0, "red"), (65.0, -9.0, "blue")])
    w, h = g.image.size
    red = _find(g.image, _red, (0, 0, w, h))
    blue = _find(g.image, _blue, (0, 0, w, h))
    assert red and blue
    rx, ry = sum(p[0] for p in red) / len(red), sum(p[1] for p in red) / len(red)
    bx, by = sum(p[0] for p in blue) / len(blue), sum(p[1] for p in blue) / len(blue)
    assert rx < w / 2 < bx            # t = +9 (left of travel) is image-left; t = -9 image-right
    assert by < h / 2 < ry            # s = 65 is nearer the top than s = 35 (row 0 = s = L)


def test_size_and_scale_follow_the_length(tmp_path):
    ref = straight_reference_line(100.0)
    _model, g = _bake_markers(ref, tmp_path, [])
    assert g.image.size == (280, 1000) and abs(g.m_per_px - 0.1) < 1e-9
    assert len(g.bin_source) == 20
    ref2 = straight_reference_line(200.0)
    model2 = make_model(ref2)
    g2 = bake.bake_ground(model2, None, [], tmp_path)
    assert max(g2.image.size) <= 1024 and abs(g2.m_per_px - 0.2) < 1e-9 and len(g2.bin_source) == 40


def _left_wall_model(ref, wall_t, footprint_side="left"):
    model = make_model(ref)
    sign = 1.0 if footprint_side == "left" else -1.0
    b = Building(id="bld-test", source="osm", side=footprint_side, height_m=10.0, confidence=0.9,
                 footprint=[(0.0, sign * wall_t), (100.0, sign * wall_t), (100.0, sign * (wall_t + 4)), (0.0, sign * (wall_t + 4))])
    return model.model_copy(update={"buildings": [b]})


def _check_ribbon(img, s_lo, s_hi, length=100.0, tol=(14, 16)):
    W, H = img.size
    worst_r = worst_g = 0
    n = 0
    for s in [s_lo + k * (s_hi - s_lo) / 12 for k in range(13)]:
        for h in (1.0, 2.5, 4.0, 6.0):
            r, g, _b = img.getpixel((min(W - 1, int(s / length * W)), min(H - 1, int((1 - h / 12.0) * H))))
            er, eg, _ = synth.wall_colour(s, h, length)
            worst_r, worst_g, n = max(worst_r, abs(r - er)), max(worst_g, abs(g - eg)), n + 1
    assert worst_r <= tol[0] and worst_g <= tol[1], f"ribbon off by (R {worst_r}, G {worst_g}) over {n} samples"


def test_frontage_projection_recovers_the_wall_left_side(tmp_path):
    ref = straight_reference_line(100.0)  # heading north; left of travel = west = heading 270
    model = _left_wall_model(ref, 10.0)
    obs = [synth.render_wall_view(ref, tmp_path, "sv-a", s_c=30.0, t_c=3.0, heading_deg=270.0, wall_t=10.0),
           synth.render_wall_view(ref, tmp_path, "sv-b", s_c=36.0, t_c=3.0, heading_deg=270.0, wall_t=10.0)]
    fb = bake.bake_frontage(model, None, obs, tmp_path, "modelled")
    assert fb.image.size == (1000, 128)
    _check_ribbon(fb.image, 26.0, 40.0)   # includes the cross-fade zone: no seam, colour = the wall function everywhere
    assert fb.bin_source[5:8] == ["street_view"] * 3   # 25-40 m is real evidence
    assert fb.bin_source[0] in ("interpolated", "none") and fb.bin_source[-1] == "none"


def test_frontage_projection_right_side_is_the_opposite_ribbon(tmp_path):
    ref = straight_reference_line(100.0)
    model = _left_wall_model(ref, 10.0, "right")           # building on the right, footpath (modelled) on the left
    obs = [synth.render_wall_view(ref, tmp_path, "sv-r", s_c=40.0, t_c=3.0, heading_deg=90.0, wall_t=-10.0)]
    fb = bake.bake_frontage(model, None, obs, tmp_path, "opposite")
    _check_ribbon(fb.image, 37.0, 43.0)
    modelled = bake.bake_frontage(model, None, obs, tmp_path, "modelled")
    assert "street_view" not in modelled.bin_source        # the camera never saw the footpath-side wall


def test_user_photo_is_reported_as_user_photo(tmp_path):
    ref = straight_reference_line(100.0)
    model = _left_wall_model(ref, 10.0)
    obs = [synth.render_wall_view(ref, tmp_path, "up-1", s_c=40.0, t_c=3.0, heading_deg=270.0, wall_t=10.0,
                                  kind=ObservationKind.user_photo, cam_h=1.5)]
    fb = bake.bake_frontage(model, None, obs, tmp_path, "modelled")
    assert "user_photo" in fb.bin_source and "street_view" not in fb.bin_source


def test_frontage_on_a_curved_line_has_no_black_texel(tmp_path):
    ref = curved_reference_line()
    model = _left_wall_model(ref, 10.0)
    obs = [synth.render_wall_view(ref, tmp_path, "sv-c", s_c=50.0, t_c=3.0,
                                  heading_deg=(ref.bearing_deg(50.0) - 90.0) % 360.0, wall_t=10.0, length=ref.length_s)]
    fb = bake.bake_frontage(model, None, obs, tmp_path, "modelled")
    lo = fb.image.convert("L").getextrema()[0]
    assert lo > 8
    assert math.isclose(fb.s_to, model.reference_line.length_s)
