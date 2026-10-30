"""Fixed street furniture of the plan (poles, trees, bins, bollards, railings, signboards, bus stops, drains, manholes,
kerb cuts, crossings, ramps).

Dynamic entities (``hero.model.dynamic``) never become furniture. ``size_m`` = the entity extent / height (the
catalogue default when missing), then capped per type (``MAX_SIZE_M``) so no data error can produce a screen-filling
object. ``colour_hex`` / ``canopy_hex`` come from the per-entity tints of the bake (``TextureRefs.tints``); a tree uses
the tint for its canopy. ``object_id`` links a piece of furniture to the plan object that is its collision proxy.
Output is sorted by ``(s, id)``.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from hero.model import catalogue, describe
from hero.model.street_plan_types import FIXED_FURNITURE_TYPES, MAX_SIZE_M, FurniturePlan
from hero.model.types import CanonicalModel, EntityState, FidelityMap, EntityType
from hero.model.scene_v2 import SceneObjectV2

MIN_CONFIDENCE = 0.2
LENGTH_TYPES = ("railing", "crossing", "drain")


def _r(x: float) -> float:
    return round(x + 0.0, 3)


def plan_furniture(model: CanonicalModel, *, sign: int, dynamic_ids: set[str], objects: Sequence[SceneObjectV2],
                   fidelity: FidelityMap, tints: Mapping[str, str] | None = None) -> list[FurniturePlan]:
    tints = tints or {}
    length = model.reference_line.length_s
    proxies = {o.entity_id: o.id for o in objects if o.entity_id and not o.scenery and o.id == o.entity_id}
    out: list[FurniturePlan] = []
    for e in sorted(model.entities, key=lambda x: x.id):
        t = e.type.value
        if t not in FIXED_FURNITURE_TYPES or e.id in dynamic_ids:
            continue
        if e.state is EntityState.removed or e.confidence < MIN_CONFIDENCE:
            continue
        w0, h0, d0 = catalogue.default_size_m(e.type, e.subtype, e.extent.length_s)
        w = e.extent.width_t if e.extent.width_t > 0 else w0
        h = e.height_m if e.height_m is not None else h0
        d = e.extent.length_s if e.extent.length_s > 0 else d0
        if t in MAX_SIZE_M:
            cw, ch = MAX_SIZE_M[t]
            w, h = min(w, cw), min(h, ch)
        s = _r(min(max(e.position.s, 0.0), length - 1e-3))
        tint = tints.get(e.id)
        out.append(FurniturePlan(
            id=e.id, type=t, variant=e.subtype or "", s=s, lat=_r(sign * e.position.t), yaw_deg=_r(e.orientation_deg or 0.0),
            size_m={"w": _r(max(w, 0.0)), "h": _r(max(h, 0.0)), "d": _r(max(d, 0.0))},
            length_m=_r(max(d, 0.0)) if t in LENGTH_TYPES else 0.0,
            colour_hex=None if (tint is None or e.type is EntityType.tree) else tint,
            canopy_hex=tint if e.type is EntityType.tree else None,
            object_id=proxies.get(e.id), confidence=_r(e.confidence),
            fidelity_level=fidelity.level_at(s), description=e.description or describe.entity_text(e)))
    out.sort(key=lambda f: (f.s, f.id))
    return out
