"""hero.imagery.preview + annotate: top-down render and the pass-A annotated tile (Pillow only)."""

from __future__ import annotations

from PIL import Image, ImageChops

from hero.imagery.annotate import annotate_tile
from hero.imagery.preview import ACTION_COLORS, make_thumbnail, render_topdown
from hero.scene.geo import LocalFrame, TileGeoref
from hero.scene.schema import load_example_scene


def _flat_tile(size: int = 1280) -> Image.Image:
    return Image.new("RGB", (size, size), (40, 40, 40))


def test_render_topdown_draws_hazard_marker_at_its_tile_px_in_its_action_colour():
    scene = load_example_scene()
    tile = _flat_tile()

    out = render_topdown(scene, tile, crop=False)

    assert out.size == tile.size
    pothole = next(o for o in scene.objects if o.type.value == "pothole")
    px, py = pothole.tile_px
    assert out.getpixel((round(px), round(py)))[:3] == ACTION_COLORS["jump"]


def test_render_topdown_crops_around_the_segment_when_requested():
    scene = load_example_scene()

    out = render_topdown(scene, _flat_tile(), crop=True)

    assert out.width < 1280 and out.height < 1280
    assert out.width >= 200 and out.height >= 200


def test_render_topdown_handles_a_scene_with_no_objects_or_tokens():
    scene = load_example_scene().model_copy(update={"objects": [], "tokens": []})

    out = render_topdown(scene, _flat_tile(), crop=False)

    assert out.getbbox() is not None


def test_thumbnail_is_bounded_and_keeps_aspect():
    img = Image.new("RGB", (1280, 640), (1, 2, 3))

    thumb = make_thumbnail(img, max_px=320)

    assert thumb.size == (320, 160)


def test_annotate_tile_draws_start_end_and_tick_marks_without_changing_size():
    frame = LocalFrame.from_endpoints(12.97400, 77.64050, 12.97467, 77.64058, "left")
    georef = TileGeoref(center_lat=12.974335, center_lon=77.64054, zoom=19, scale=2, size_px=(1280, 1280))
    tile = _flat_tile()

    out = annotate_tile(tile, frame, georef, osm=None)

    assert out.size == tile.size
    diff = ImageChops.difference(out, tile).convert("L")
    changed = sum(1 for v in diff.get_flattened_data() if v)
    assert changed > 2000  # line, ticks, labels and the side arrow were drawn
