"""Colour / tint helpers (slice S1e)."""

import pytest
from PIL import Image

from hero.imagery import tints


def lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def test_hex_round_trip_and_clamp():
    assert tints.hex_to_rgb("#c9b79c") == (201, 183, 156)
    assert tints.rgb_to_hex((201, 183, 156)) == "#c9b79c"
    assert tints.rgb_to_hex((300, -4, 12.6)) == "#ff000d"
    with pytest.raises(ValueError):
        tints.hex_to_rgb("#abc")


def test_mix_and_shade():
    assert tints.mix((0, 0, 0), (200, 100, 50), 0.5) == (100, 50, 25)
    assert tints.mix((10, 10, 10), (20, 20, 20), 5.0) == (20, 20, 20)
    assert tints.shade((100, 200, 250), 0.5) == (50, 100, 125)


def test_state_tints_ordered_intact_broken_missing():
    intact, broken, missing = (tints.state_tint(s) for s in ("intact", "broken", "missing"))
    assert intact == (255, 255, 255)
    assert lum(intact) > lum(broken) > lum(missing)
    assert missing[0] > missing[2]  # earthy, warm
    # unknown names fall back to the neutral "unknown" tint instead of raising
    assert tints.state_tint("???") == tints.state_tint("unknown")


def test_vertex_rgba_darkens_road_not_footpath():
    foot = tints.state_vertex_rgba("intact", "footpath")
    road = tints.state_vertex_rgba("intact", "road")
    assert foot == (255, 255, 255, 255)
    assert road[3] == 255 and lum(road) < lum(foot)
    assert lum(tints.state_vertex_rgba("broken", "footpath")) < lum(foot)


def test_condition_tint_poor_darker_than_good():
    assert lum(tints.condition_tint("poor")) < lum(tints.condition_tint("good"))
    assert tints.condition_tint("nonsense") == tints.condition_tint("unknown")


def test_surface_grade_known_kinds():
    assert tints.surface_grade("road")["gain"] < tints.surface_grade("footpath")["gain"]
    assert tints.surface_grade("nothing") is None


def test_median_colour_whole_and_box():
    img = Image.new("RGB", (10, 10), (10, 20, 30))
    for x in range(5, 10):
        for y in range(10):
            img.putpixel((x, y), (200, 100, 50))
    assert tints.median_colour(img, (0, 0, 4, 10)) == "#0a141e"
    assert tints.median_colour(img, (6, 0, 4, 10)) == "#c86432"
    with pytest.raises(ValueError):
        tints.median_colour(img, (50, 50, 5, 5))


def test_entity_tint_pulled_toward_type_reference():
    blue = Image.new("RGB", (100, 100), (30, 60, 220))
    tree = tints.hex_to_rgb(tints.entity_tint_colour(blue, (10, 10, 80, 80), "tree"))
    pole = tints.hex_to_rgb(tints.entity_tint_colour(blue, (10, 10, 80, 80), "pole"))
    assert tree[1] > 60 and tree[2] < 220          # greener, less blue than the raw median
    assert tree[2] < pole[2] < 220                  # a pole is pulled less than a tree
    assert tints.entity_tint_colour(blue, (10, 10, 80, 80), "unknown_type") == "#1e3cdc"


def test_neutral_facade_colour_deterministic_and_varied():
    seen = {tints.neutral_facade_colour("street-a", "left", i) for i in range(40)}
    assert seen <= set(tints.FACADE_PALETTE) and len(seen) >= 4
    assert tints.neutral_facade_colour("street-a", "left", 3) == tints.neutral_facade_colour("street-a", "left", 3)
    assert any(tints.neutral_facade_colour("street-a", "left", i) != tints.neutral_facade_colour("street-a", "right", i)
               for i in range(20))
