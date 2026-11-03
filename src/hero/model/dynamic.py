"""Runtime classification (fixed vs dynamic_context) and the dynamic summary (spec/data.md -> "Runtime classification").

The canonical model is never changed: ``runtime_of(entity, footpath_band)`` is a pure function evaluated at compile
time. The catalogue column ``runtime`` holds the type default (``fixed`` | ``dynamic_context`` | ``by_position``); the
position rule refines ``by_position`` types:

* ``pothole`` / ``debris``: fixed when the entity's lateral centre lies in the footpath band, else dynamic_context.
* ``other``: dynamic_context unless ``attributes.blocks_footpath`` is true **and** the centre is in the band.

``footpath_band`` is ``(lo, hi)`` in game lateral (the modelled-side footpath at the entity's ``s`` widened by
``BAND_WIDEN_M``; the prior band ``[edge + 0.15, edge + 1.65]`` when the footpath is missing/unknown) - the caller
(``hero.model.compile``) computes it from its stations.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from hero.export.gameplay_types import GameplayV1
from hero.model import catalogue
from hero.model.scene_v3 import DynamicContext, DynamicTypeStats
from hero.model.street_plan_types import SPAWN_TYPES, PlanDynamicContext, SpawnHints
from hero.model.types import Entity

BAND_WIDEN_M = 0.3
PRIOR_KERB_M = 0.15
PRIOR_BAND_WIDTH_M = 1.5
DENSITY_FULL_PER_100M = 60.0   # dynamic entities seen per 100 m that saturate the spawn density (was 40: too busy)
OBSERVED_MIX = 0.7
MIN_EXCLUDED_FOR_OBSERVED = 3
Runtime = str  # "fixed" | "dynamic_context"


def prior_band(edge: float) -> tuple[float, float]:
    return (edge + PRIOR_KERB_M, edge + PRIOR_KERB_M + PRIOR_BAND_WIDTH_M)


def widen(band: tuple[float, float]) -> tuple[float, float]:
    return (band[0] - BAND_WIDEN_M, band[1] + BAND_WIDEN_M)


def runtime_of(entity: Entity, footpath_band: tuple[float, float], *, lateral: float | None = None) -> Runtime:
    """``"fixed"`` or ``"dynamic_context"``. ``lateral`` is the entity's game-lateral centre (default ``position.t``;
    the caller passes ``sign * t`` for a right-footpath model); ``footpath_band`` is in the same frame."""
    default = catalogue.entry(entity.type).runtime
    if default != "by_position":
        return default
    lat = entity.position.t if lateral is None else lateral
    in_band = footpath_band[0] <= lat <= footpath_band[1]
    if entity.type.value == "other":
        return "fixed" if (entity.attributes.get("blocks_footpath") is True and in_band) else "dynamic_context"
    return "fixed" if in_band else "dynamic_context"


# ------------------------------------------------------------------ spawn hints / summaries


def _subtype(e: Entity) -> str:
    return (e.subtype or "").strip().lower().replace("_", "-")


def _spawn_type_counts(entities: Sequence[Entity]) -> dict[str, float]:
    out = dict.fromkeys(SPAWN_TYPES, 0.0)
    for e in entities:
        t = e.type.value
        if t == "parked_vehicle":
            out["two_wheeler" if "two" in _subtype(e) else "parked_car"] += 1.0
        elif t == "vendor_stall":
            out["stall"] += 1.0
        elif t in ("pothole", "speed_breaker"):
            out["pothole"] += 1.0
        elif t == "debris":
            out["cone"] += 0.5
            out["barricade"] += 0.5
    return out


def _normalise4(w: Mapping[str, float]) -> dict[str, float]:
    """Round to 4 dp keeping the sum at exactly 1.0 (the largest weight absorbs the remainder)."""
    total = sum(w.values())
    out = {k: round(w[k] / total, 4) for k in SPAWN_TYPES}
    big = max(SPAWN_TYPES, key=lambda k: (out[k], -SPAWN_TYPES.index(k)))
    out[big] = round(out[big] + (1.0 - sum(out.values())), 4)
    return out


def _lane_weights(lanes: Sequence[int], gameplay: GameplayV1) -> tuple[float, float, float]:
    """Share of dynamic obstacles per lane (3 dp, sum exactly 1.0); the gameplay base tables when none were seen."""
    if lanes:
        raw = [sum(1 for ln in lanes if ln == i) / len(lanes) for i in range(3)]
    else:
        tab = [sum(gameplay.spawner.lane_weights[str(i)].values()) for i in range(3)]
        raw = [t / sum(tab) for t in tab]
    out = [round(r, 3) for r in raw]
    big = max(range(3), key=lambda i: (out[i], -i))
    out[big] = round(out[big] + (1.0 - sum(out)), 3)
    return (out[0], out[1], out[2])


def spawn_hints(entities: Sequence[Entity], length_m: float, gameplay: GameplayV1,
                lanes: Sequence[int] = ()) -> SpawnHints:
    """Density = min(1, excluded per 100 m / 60); weights = 0.7 observed + 0.3 prior when >= 3 entities else the prior."""
    prior = gameplay.spawner.default_spawn_hints.type_weights
    prior = {k: prior[k] for k in SPAWN_TYPES}
    counts = _spawn_type_counts(entities)
    total = sum(counts.values())
    if len(entities) >= MIN_EXCLUDED_FOR_OBSERVED and total > 0:
        mixed = {k: OBSERVED_MIX * counts[k] / total + (1.0 - OBSERVED_MIX) * prior[k] for k in SPAWN_TYPES}
    else:
        mixed = prior
    density = min(1.0, (len(entities) / length_m * 100.0) / DENSITY_FULL_PER_100M) if len(entities) else 0.0
    if not entities:
        density = gameplay.spawner.default_spawn_hints.density
    return SpawnHints(density=round(density, 3), type_weights=_normalise4(mixed),
                      lane_weights=_lane_weights(lanes, gameplay))


def dynamic_context(entities: Sequence[tuple[Entity, int]], length_m: float,
                    gameplay: GameplayV1) -> tuple[DynamicContext, PlanDynamicContext]:
    """``entities`` = (entity, lane index 0..2) of every excluded dynamic entity. Returns the lite-scene summary and
    the plan's context (with spawn hints)."""
    ents = sorted(entities, key=lambda x: x[0].id)
    ids = [e.id for e, _ in ents]
    by_type: dict[str, DynamicTypeStats] = {}
    for t in sorted({e.type.value for e, _ in ents}):
        group = [(e, ln) for e, ln in ents if e.type.value == t]
        subs: dict[str, int] = {}
        for e, _ in group:
            if e.subtype:
                subs[_subtype(e)] = subs.get(_subtype(e), 0) + 1
        lanes = [sum(1 for _, ln in group if ln == i) / len(group) for i in range(3)]
        by_type[t] = DynamicTypeStats(
            count=len(group), per_100m=round(len(group) / length_m * 100.0, 1), subtypes=dict(sorted(subs.items())),
            lane_share=(round(lanes[0], 3), round(lanes[1], 3), round(lanes[2], 3)),
            mean_confidence=round(sum(e.confidence for e, _ in group) / len(group), 3))
    lite = DynamicContext(excluded_count=len(ids), entity_ids=ids, by_type=by_type)
    plan = PlanDynamicContext(excluded_count=len(ids), entity_ids=ids,
                              spawn_hints=spawn_hints([e for e, _ in ents], length_m, gameplay,
                                                      [ln for _, ln in ents]))
    return lite, plan
