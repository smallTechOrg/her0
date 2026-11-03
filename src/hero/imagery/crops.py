"""Billboard / facade crops cut from Street View frames: <= 256 px PNGs under data/crops/<run_id>/."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

MAX_CROP_PX = 256
MIN_CROP_PX = 8


def crop_box(image: Image.Image, crop_px: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """Clamp an ``[x, y, w, h]`` box to the image; ``None`` when nothing usable remains."""
    x, y, w, h = (int(v) for v in crop_px)
    left = max(0, min(x, image.width - 1))
    top = max(0, min(y, image.height - 1))
    right = max(left, min(x + w, image.width))
    bottom = max(top, min(y + h, image.height))
    if right - left < MIN_CROP_PX or bottom - top < MIN_CROP_PX:
        return None
    return left, top, right, bottom


def cut_crop(image: Image.Image, crop_px: tuple[int, int, int, int], max_px: int = MAX_CROP_PX) -> Image.Image | None:
    box = crop_box(image, crop_px)
    if box is None:
        return None
    crop = image.crop(box).convert("RGBA")
    crop.thumbnail((max_px, max_px), Image.LANCZOS)
    return crop


def save_crop(image: Image.Image, crop_px: tuple[int, int, int, int], dest: Path) -> Path | None:
    crop = cut_crop(image, crop_px)
    if crop is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest, format="PNG", optimize=True)
    return dest
