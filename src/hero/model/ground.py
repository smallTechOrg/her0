"""Ground texture plan: which source each 5 m bin of the ground strip really holds.

* ``textures.ground`` present -> ``mode = "baked_strip"``; ``bin_source`` is the bake's own report (it painted every gap
  into the image: feathered extension of covered bins, otherwise procedural), resized to ``ceil(length / 5)`` entries.
* otherwise -> ``mode = "procedural"``; every bin is ``procedural`` (the mesh builder paints footpath/road from the strips
  and ``procedural_seed``) - nothing is blank.

``procedural_seed`` = ``textures.procedural_seed`` when set, else ``int32(sha1(slug))``.
"""

from __future__ import annotations

import hashlib
import math

from hero.model.street_plan_types import GroundTexPlan
from hero.model.texture_types import GroundBinSource, TextureRefs

BIN_M = 5.0


def n_bins(length: float, bin_m: float = BIN_M) -> int:
    return max(1, math.ceil(length / bin_m - 1e-9))


def procedural_seed(slug: str, textures: TextureRefs | None = None) -> int:
    if textures is not None and textures.procedural_seed:
        return int(textures.procedural_seed)
    return int.from_bytes(hashlib.sha1(slug.encode()).digest()[:4], "big") & 0x7FFFFFFF


def bin_sources(length: float, textures: TextureRefs | None) -> list[GroundBinSource]:
    n = n_bins(length)
    if textures is None or textures.ground is None:
        return ["procedural"] * n
    src = list(textures.ground.bin_source)[:n]
    return src + ["procedural"] * (n - len(src))  # type: ignore[return-value]


def plan_ground(slug: str, length: float, textures: TextureRefs | None) -> GroundTexPlan:
    src = bin_sources(length, textures)
    baked = textures is not None and textures.ground is not None
    return GroundTexPlan(mode="baked_strip" if baked else "procedural", procedural_seed=procedural_seed(slug, textures),
                         bin_m=BIN_M, bin_source=src, strip=textures.ground if baked else None)  # type: ignore[union-attr]
