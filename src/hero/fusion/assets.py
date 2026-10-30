"""Entity / building crops and the thumbnail for a published model version.

Crops are cut from the observation that supplied the entity's billboard box and written to
``data/crops/<segment_slug>/<entity_or_building_id>.png``; the URL carries ``?v=<version>`` so a changed crop
busts the one-day media cache (files are otherwise stable per id). Pure file work: no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from hero.imagery.crops import save_crop
from hero.imagery.preview import make_thumbnail
from hero.model.types import CanonicalModel, EntityState, Observation

THUMB_PX = 480


def crops_dir(data_dir: Path, segment_slug: str) -> Path:
    return Path(data_dir) / "crops" / segment_slug


def textures_dir(data_dir: Path, segment_slug: str) -> Path:
    """Baked, content-addressed textures of a street (``data/textures/<slug>/``; served at ``/media/textures``)."""
    return Path(data_dir) / "textures" / segment_slug


def read_manifest(segment_slug: str, export: Mapping[str, Any] | None, *, data_dir: Path | None = None
                  ) -> dict[str, Any] | None:
    """The stored ``street.manifest.json`` of ``model_versions.export_json`` (``None`` when absent/unreadable)."""
    sha8 = (export or {}).get("sha8")
    if not sha8:
        return None
    from hero.export import store  # local: the export package is heavier than this module's other users need

    if data_dir is None:
        from hero.config import get_settings

        data_dir = get_settings().data_dir_abs
    files = store.open_export(Path(data_dir), segment_slug, str(sha8))
    if files is None:
        return None
    try:
        return json.loads(files.manifest.read_bytes())
    except (OSError, ValueError):
        return None


def crop_url(segment_slug: str, item_id: str, version: int) -> str:
    return f"/media/crops/{segment_slug}/{item_id}.png?v={version}"


def _open(data_dir: Path, obs: Observation) -> Image.Image | None:
    path = Path(data_dir) / obs.file_path
    try:
        with Image.open(path) as img:
            return img.convert("RGB")
    except (OSError, ValueError):
        return None


def cut_model_crops(model: CanonicalModel, observations: Mapping[str, Observation], *, data_dir: Path,
                    version: int) -> dict[str, str]:
    """``{entity_id | building_id: url}`` for every entity billboard and building facade that could be cut."""
    out: dict[str, str] = {}
    cache: dict[str, Image.Image | None] = {}

    def image_of(observation_id: str) -> Image.Image | None:
        if observation_id not in cache:
            obs = observations.get(observation_id)
            cache[observation_id] = _open(data_dir, obs) if obs is not None else None
        return cache[observation_id]

    dest_dir = crops_dir(data_dir, model.segment_slug)
    for e in sorted(model.entities, key=lambda x: x.id):
        if e.state is EntityState.removed or e.billboard is None:
            continue
        img = image_of(e.billboard.observation_id)
        if img is None:
            continue
        if save_crop(img, e.billboard.crop_px, dest_dir / f"{e.id}.png") is not None:
            out[e.id] = crop_url(model.segment_slug, e.id, version)
    for b in sorted(model.buildings, key=lambda x: x.id):
        if not b.facades:
            continue
        best = max(b.facades, key=lambda f: (f.weight, f.observation_id))
        img = image_of(best.observation_id)
        if img is None:
            continue
        if save_crop(img, best.crop_px, dest_dir / f"{b.id}.png") is not None:
            out[b.id] = crop_url(model.segment_slug, b.id, version)
    return out


def save_thumbnail(image: Image.Image, dest: Path, max_px: int = THUMB_PX) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    make_thumbnail(image.convert("RGB"), max_px).save(dest, format="PNG", optimize=True)
    return dest
