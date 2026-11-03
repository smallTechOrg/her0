from __future__ import annotations

import random

from PIL import Image, ImageDraw

from hero.imagery.noimagery import looks_like_no_imagery
from tests.fixtures.google.fake_google import HERE


def test_placeholder_fixture_tile_is_detected():
    assert looks_like_no_imagery(Image.open(HERE / "placeholder_tile.png")) is True


def test_satellite_fixture_tile_is_not_flagged():
    assert looks_like_no_imagery(Image.open(HERE / "satellite_tile.png")) is False


def test_flat_dark_or_tinted_tiles_are_not_the_light_grey_placeholder():
    assert looks_like_no_imagery(Image.new("RGB", (256, 256), (10, 10, 10))) is False
    assert looks_like_no_imagery(Image.new("RGB", (256, 256), (40, 90, 160))) is False  # open water
    assert looks_like_no_imagery(Image.new("RGB", (256, 256), (230, 190, 120))) is False  # tinted, not grey


def test_light_grey_with_mild_noise_and_text_still_counts():
    rng = random.Random(1)
    img = Image.new("RGB", (400, 400), (228, 226, 222))
    px = img.load()
    for _ in range(3000):
        x, y = rng.randrange(400), rng.randrange(400)
        v = rng.randrange(-4, 5)
        px[x, y] = (228 + v, 226 + v, 222 + v)
    ImageDraw.Draw(img).text((100, 200), "Sorry, we have no imagery here.", fill=(110, 110, 110))
    assert looks_like_no_imagery(img) is True


def test_mode_and_size_agnostic():
    assert looks_like_no_imagery(Image.new("L", (32, 32), 230)) is True
    assert looks_like_no_imagery(Image.new("RGBA", (5, 5), (230, 230, 230, 255))) is True
