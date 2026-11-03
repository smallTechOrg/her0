"""Import a Phase-1 (scene v1) street as canonical model v0 (spec/capabilities/street-model.md -> Legacy).

``import_legacy(scene, segment_slug=..., footpath_side=..., now=...)`` returns a ``CanonicalModel`` with ``kind="legacy"``,
``legacy=True``, ``placement_verified=False``, version 0 and every fidelity bin L0. Its scene stays v1 and keeps playing;
a first fusion run starts a fresh lineage (nothing here is merged forward).

Interpretation notes
--------------------
* The reference line is the straight chord ``segment.start -> segment.end`` (``source="legacy_straight"``). Its length is
  the chord length computed by ``ReferenceLine`` (the v1 ``length_m`` can differ by centimetres to decimetres); profile
  intervals keep the v1 metres and the last one is stretched/clipped to the chord length.
* One entity per v1 object **except** ``footpath_gap`` (``catalogue.LEGACY_TYPE_MAP`` / data.md: a gap is a ``missing``
  profile interval, which the coverage ranges already carry). Entity ``s = x_m`` and ``t`` is the mean lateral of the
  object's lanes (``lat`` toward the footpath side, so ``t = lat`` for ``left``, ``-lat`` for ``right``); sigma 1.5 m;
  ``provenance == []``; description ends with "(legacy, placement unverified)".
* Strips: carriageway centred on the line (width ``road_width_m``) and, unless ``missing``, the footpath beside it
  (edges ``R/2`` to ``R/2 + F`` on the footpath side, ``F`` = the footpath lane width). Weight from the coverage confidence.
* Buildings keep their v1 source and get vision-form ids ``bld-v-...`` (a v1 id carries no OSM way id).
"""

from __future__ import annotations

import math
from datetime import datetime

from hero.model import catalogue, describe, ids
from hero.model.fidelity import compute_fidelity
from hero.model.refline import ReferenceLine
from hero.model.types import (
    Building,
    CanonicalModel,
    Condition,
    Entity,
    EntityState,
    Extent,
    FootpathState,
    Material,
    ModelKind,
    Position,
    ProfileInterval,
    Strip,
    StripKind,
)
from hero.scene.geo import mean_lane_lat_m
from hero.scene.schema import Scene

LEGACY_NOTE = describe.LEGACY_NOTE
LEGACY_SIGMA_M = 1.5
LEGACY_OBS = "legacy"


def _logit(p: float) -> float:
    p = min(max(p, 0.02), 0.98)
    return math.log(p / (1.0 - p))


def _strip_weight(conf: float) -> float:
    return round(-math.log(1.0 - min(max(conf, 0.0), 0.95)), 3)


def import_legacy(scene: Scene, *, segment_slug: str, footpath_side: str, now: datetime) -> CanonicalModel:
    if footpath_side not in ("left", "right"):
        raise ValueError(f"footpath_side must be 'left' or 'right', got {footpath_side!r}")
    seg = scene.segment
    ref = ReferenceLine.from_latlons([(seg.start.lat, seg.start.lon), (seg.end.lat, seg.end.lon)])
    length = round(ref.length_s, 3)
    sign = 1.0 if footpath_side == "left" else -1.0
    cs = scene.cross_section
    half_road = cs.road_width_m / 2.0
    fp_width = cs.lanes[0].width_m
    tag = "L" if footpath_side == "left" else "R"

    profile: list[ProfileInterval] = []
    cov = list(cs.footpath_coverage)
    prev = 0.0
    for i, r in enumerate(cov):
        a = prev
        b = length if (i == len(cov) - 1 or r.to_m >= length) else round(r.to_m, 3)
        if b - a < 1e-6:
            continue
        state = FootpathState(r.state.value)
        weight = _strip_weight(r.confidence)
        strips = [Strip(id=f"sp-carriageway-C-{len(profile)}", kind=StripKind.carriageway, side="centre",
                        material=Material.asphalt, condition=Condition.unknown, height_m=0.0,
                        t_lo=(-half_road, -half_road), t_hi=(half_road, half_road), weight=weight,
                        confidence=round(r.confidence, 3), description="", provenance=[])]
        if state is not FootpathState.missing:
            lo, hi = (half_road, half_road + fp_width) if sign > 0 else (-half_road - fp_width, -half_road)
            strips.append(Strip(
                id=f"sp-footpath-{tag}-{len(profile)}", kind=StripKind.footpath, side=footpath_side,  # type: ignore[arg-type]
                material=Material.unknown,
                condition=Condition.broken if state is FootpathState.broken else Condition.unknown, height_m=0.15,
                t_lo=(lo, lo), t_hi=(hi, hi), weight=weight, confidence=round(r.confidence, 3), description="",
                provenance=[]))
        strips.sort(key=lambda s: s.t_lo[0])
        iv = ProfileInterval(id=f"pi-{len(profile):04d}", s_from=a, s_to=b, footpath_state=state,
                             state_votes={state.value: weight}, strips=strips, description="",
                             evidence_summary=f"From the first-generation scene ({LEGACY_NOTE}).")
        strips = [s.model_copy(update={"description": describe.strip_text(s, a, b)}) for s in strips]
        iv = iv.model_copy(update={"strips": strips})
        iv = iv.model_copy(update={"description": describe.interval_text(iv, footpath_side).rstrip(".")
                                   + f" ({LEGACY_NOTE})."})
        profile.append(iv)
        prev = b
        if b >= length:
            break
    if not profile or profile[-1].s_to < length - 1e-6:
        # coverage ended short of the chord: stretch the last interval
        if profile:
            profile[-1] = profile[-1].model_copy(update={"s_to": length})
        else:
            raise ValueError("scene has no usable footpath coverage")

    entities: list[Entity] = []
    ordinals: dict[str, int] = {}
    for obj in sorted(scene.objects, key=lambda o: o.id):
        etype = catalogue.legacy_type(obj.type.value)
        if etype is None:
            continue
        lat = mean_lane_lat_m(obj.lanes, cs.lanes)
        s, t = float(obj.x_m), round(lat * sign, 3)
        n = ordinals.get(etype.value, 0)
        ordinals[etype.value] = n + 1
        attrs: dict[str, str | float | bool] = {"legacy_id": obj.id}
        if etype.value == "drain":
            attrs["covered"] = False
        label = obj.label or etype.value.replace("_", " ")
        entities.append(Entity(
            id=ids.entity_id(segment_slug, etype.value, LEGACY_OBS, s, t, n), type=etype, state=EntityState.present,
            position=Position(s=s, t=t), position_sigma_m=LEGACY_SIGMA_M,
            extent=Extent(length_s=obj.length_m, width_t=obj.size_m.w), height_m=obj.size_m.h or None,
            orientation_deg=None, attributes=attrs, description=f"{label[:1].upper()}{label[1:]} ({LEGACY_NOTE}).",
            confidence=round(obj.confidence, 3), logit=round(_logit(obj.confidence), 4), provenance=[],
            billboard=None, first_seen_version=0, last_changed_version=0))

    buildings: list[Building] = []
    for b in scene.buildings:
        fp = [(float(x), round(float(lat) * sign, 3)) for x, lat in b.footprint]
        s_c = sum(p[0] for p in fp) / len(fp)
        mean_lat = sum(lat for _, lat in b.footprint) / len(b.footprint)
        bid = ids.building_id(segment_slug=segment_slug, side=footpath_side if mean_lat >= 0 else
                              ("right" if footpath_side == "left" else "left"), s_center=s_c)
        side = footpath_side if mean_lat >= 0 else ("right" if footpath_side == "left" else "left")
        buildings.append(Building(
            id=bid, source=b.source, side=side, footprint=fp, height_m=b.height_m, height_confidence=0.2,  # type: ignore[arg-type]
            colour_hex=b.color_hex, description=f"Building about {b.height_m:.0f} m tall ({LEGACY_NOTE}).",
            confidence=round(b.confidence, 3), provenance=[]))
    seen: set[str] = set()
    buildings = [b for b in buildings if not (b.id in seen or seen.add(b.id))]  # type: ignore[func-returns-value]

    model = CanonicalModel(
        segment_slug=segment_slug, version=0, parent_version=None, kind=ModelKind.legacy, run_id=None, created_at=now,
        legacy=True, placement_verified=False, footpath_side=footpath_side,  # type: ignore[arg-type]
        reference_line=ref.to_model(source="legacy_straight", osm_way_id=None, name=seg.name),
        profile=profile, entities=entities, buildings=buildings, contradictions=[], fused_observation_ids=[])
    model = model.model_copy(update={"stats": model.computed_stats()})
    return model.model_copy(update={"fidelity": compute_fidelity(model, {})})
