"""Entity catalogue (spec/data.md -> "Entity catalogue"): typed, versioned with the code.

``CATALOGUE[type]`` -> ``CatalogueEntry``; ``entry(type)`` accepts the enum or its string value.
``action_for`` applies the documented attribute overrides. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from hero.model.types import EntityType

CATALOGUE_VERSION = 1

EntityClass = Literal["obstacle", "hazard", "marking", "scenery"]
Action = Literal["jump", "slide", "dodge", "none"]
Runtime = Literal["fixed", "dynamic_context", "by_position"]


@dataclass(frozen=True)
class CatalogueEntry:
    type: EntityType
    klass: EntityClass
    action: Action                      # default game action
    size_m: tuple[float, float, float]  # default w x h x d; d == 0.0 means "use the entity's extent"
    note: str = ""
    emits_tokens: bool = False          # crossings / ramps generate tokens
    runtime: Runtime = "fixed"          # type default of the runtime classification (hero.model.dynamic refines "by_position")


# Runtime classification (spec/data.md -> "Runtime classification"): anything not listed is "fixed".
_RUNTIME: Mapping[str, Runtime] = {
    "parked_vehicle": "dynamic_context",
    "vendor_stall": "dynamic_context",
    "speed_breaker": "dynamic_context",
    "pothole": "by_position",   # fixed when its centre lies in the footpath band
    "debris": "by_position",
    "other": "by_position",     # dynamic unless attributes.blocks_footpath and in the footpath band
}


def _e(t: str, klass: EntityClass, action: Action, w: float, h: float, d: float, note: str = "",
       tokens: bool = False) -> tuple[EntityType, CatalogueEntry]:
    et = EntityType(t)
    return et, CatalogueEntry(et, klass, action, (w, h, d), note, tokens, _RUNTIME.get(t, "fixed"))


CATALOGUE: Mapping[EntityType, CatalogueEntry] = dict([
    _e("tree", "obstacle", "dodge", 0.8, 5.0, 0.8, "canopy_low attribute true also emits a low_branch hazard"),
    _e("pole", "obstacle", "dodge", 0.3, 7.0, 0.3, "utility/lamp; subtype"),
    _e("parked_vehicle", "obstacle", "dodge", 1.8, 1.5, 4.2,
       "two-wheeler 0.7x1.2x1.9; subtype auto-rickshaw/car/two-wheeler/bus/truck"),
    _e("signboard", "obstacle", "dodge", 0.8, 2.0, 0.2, "ground-standing boards; overhead signs are scenery"),
    _e("drain", "hazard", "jump", 0.8, 0.1, 1.0, "open/uncovered drain or broken slab (attributes.covered)"),
    _e("pothole", "hazard", "jump", 1.0, 0.1, 1.0),
    _e("debris", "hazard", "jump", 1.0, 0.4, 1.0, "rubble, garbage heaps"),
    _e("speed_breaker", "hazard", "jump", 3.0, 0.12, 0.6),
    _e("low_branch", "hazard", "slide", 1.8, 0.6, 1.0, "overhead clearance < 2 m"),
    _e("bollard", "obstacle", "dodge", 0.2, 0.8, 0.2),
    _e("bin", "obstacle", "dodge", 0.6, 1.0, 0.6),
    _e("vendor_stall", "obstacle", "dodge", 1.5, 2.0, 1.5),
    _e("railing", "obstacle", "dodge", 0.1, 1.0, 0.0, "depth = extent"),
    _e("bus_stop", "obstacle", "dodge", 1.5, 2.5, 3.0, "shelter/pole"),
    _e("gate", "obstacle", "dodge", 3.0, 2.0, 0.2),
    _e("wall", "scenery", "none", 0.3, 2.0, 0.0, "depth = extent; dodge when inside a lane band"),
    _e("manhole", "marking", "none", 0.7, 0.05, 0.7, "jump when attributes.open"),
    _e("crossing", "marking", "none", 9.0, 0.0, 3.0, "generates tokens", tokens=True),
    _e("ramp", "marking", "none", 1.5, 0.15, 1.5, "generates tokens", tokens=True),
    _e("kerb_cut", "marking", "none", 1.2, 0.1, 1.0),
    _e("shopfront", "scenery", "none", 0.0, 0.0, 0.0, "attached to a building, never a hazard"),
    _e("other", "obstacle", "none", 1.0, 1.0, 1.0, "dodge if attributes.blocks_footpath else none"),
])

TWO_WHEELER_SIZE_M = (0.7, 1.2, 1.9)

# Phase-1 object type -> catalogue type. ``footpath_gap`` is NOT an entity (a ``missing`` profile interval).
LEGACY_TYPE_MAP: Mapping[str, EntityType | None] = {
    "open_drain": EntityType.drain,
    "footpath_gap": None,
    "parked_vehicle": EntityType.parked_vehicle,
    "pothole": EntityType.pothole,
    "debris": EntityType.debris,
    "pole": EntityType.pole,
    "tree": EntityType.tree,
    "signboard": EntityType.signboard,
    "low_branch": EntityType.low_branch,
    "crossing": EntityType.crossing,
    "ramp": EntityType.ramp,
}


def entry(entity_type: EntityType | str) -> CatalogueEntry:
    """Catalogue entry for ``entity_type``; ``ValueError`` for an unknown type."""
    try:
        return CATALOGUE[EntityType(entity_type)]
    except ValueError as exc:
        raise ValueError(f"unknown entity type {entity_type!r}") from exc


def legacy_type(name: str) -> EntityType | None:
    """Map a Phase-1 object type name; ``None`` for ``footpath_gap``; ``ValueError`` if unknown."""
    if name not in LEGACY_TYPE_MAP:
        raise ValueError(f"unknown legacy object type {name!r}")
    return LEGACY_TYPE_MAP[name]


def default_size_m(entity_type: EntityType | str, subtype: str | None = None,
                   extent_length_s: float = 0.0) -> tuple[float, float, float]:
    """Default (w, h, d); a two-wheeler uses the smaller size; ``d == 0`` entries take ``extent_length_s``."""
    e = entry(entity_type)
    w, h, d = e.size_m
    if e.type is EntityType.parked_vehicle and subtype and "two" in subtype.lower():
        w, h, d = TWO_WHEELER_SIZE_M
    if d == 0.0:
        d = extent_length_s
    return (w, h, d)


def action_for(entity_type: EntityType | str, attributes: Mapping[str, object] | None = None, *,
               in_lane_band: bool = True) -> Action:
    """Game action for an entity, applying the catalogue's attribute overrides.

    * ``other`` -> dodge iff ``attributes.blocks_footpath`` else none.
    * ``manhole`` -> jump iff ``attributes.open``.
    * ``wall`` -> dodge iff it lies inside a lane band (``in_lane_band``).
    * ``drain`` with ``covered`` true -> none (a covered drain is not a hazard).
    * everything else: the catalogue default.
    """
    attrs = attributes or {}
    t = entry(entity_type).type
    if t is EntityType.other:
        return "dodge" if attrs.get("blocks_footpath") else "none"
    if t is EntityType.manhole:
        return "jump" if attrs.get("open") else "none"
    if t is EntityType.wall:
        return "dodge" if in_lane_band else "none"
    if t is EntityType.drain and attrs.get("covered") is True:
        return "none"
    return CATALOGUE[t].action


def emits_low_branch(entity_type: EntityType | str, attributes: Mapping[str, object] | None = None) -> bool:
    """A tree with ``canopy_low`` true also emits a ``low_branch`` hazard."""
    return entry(entity_type).type is EntityType.tree and bool((attributes or {}).get("canopy_low"))
