"""Compile one canonical model version into a Scene v2 (spec/data.md -> "Scene JSON v2" and "Compile rules").

``compile_scene`` is a pure function: no clock (``created_at`` is read from the model), no randomness, no network, no
dict-order dependence. The same model + observations + assets + tile + fidelity gives a byte-identical
``SceneV2.model_dump_json()``, also on a model that was round-tripped through JSON.

World frame (spec/data.md)::

    lat_sign  = +1 for footpath_side "left", else -1          (game lateral  lat = lat_sign * t)
    turn_sign = -1 for "left", else +1
    theta(s)  = turn_sign * wrap(bearing(s) - bearing(0))     forward f = (sin th, -cos th), normal n = (cos th, sin th)
    P(0) = (0, 0);  P(s + ds) = P(s) + f(s) * ds              (forward Euler on the sample grid)
    world(s, lat) = P(s) + lat * n(s)

Interpretation notes (the spec leaves these open)
-------------------------------------------------
* ``corridor.stations[].strips`` lists every strip except the modelled-side footpath (that one is ``footpath``), in game
  lateral order (lo ascending).
* ``carriageway_edge`` is the largest ``hi`` among the station's carriageway strips (default half of 8.5 m); with no
  footpath at all the prior centre ``edge + 0.9`` (0.15 kerb + 0.75 half path) is used.
* The loop seam blends station values linearly to the station-0 values over the last 6 m (``w = 0`` at ``L - 6`` to ``1``
  at ``L``): lane centres, footpath/strip edges and confidences. Entities are NOT moved (they only get clamped by the lane
  rule); a state/material label is not blended.
* An object overlaps a lane band when the overlap of ``lat +- width/2`` with ``lane_lat[i] +- band/2`` is
  ``>= min(0.15, width)``; band widths are (footpath width at the object, 1.6, 2.0) m.
* ``about.photos`` counts ``model.fused_observation_ids``; an id missing from ``observations`` counts as Street View so
  ``photos.total`` always equals the fused count.
* ``generation.models`` is ``{}`` and ``review_iterations`` 0 (compile knows nothing about the run); the publisher may
  override ``generation`` before storing. ``generation.run_id`` is ``model.run_id`` or ``"none"``.
* A tree with ``attributes.canopy_low`` also emits a ``low_branch`` object (id ``lb-<entity id>``, ``entity_id`` = the tree).
* Walkability uses the v1 formula via ``hero.scene.walkability`` constants (intact = state ``intact`` only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from hero.model import catalogue, describe
from hero.model.diff import model_hash
from hero.model.fidelity import LEVEL_LABELS
from hero.model.refline import ReferenceLine
from hero.model.scene_v2 import (
    COMPILED_BY,
    About,
    AboutFidelity,
    AboutPhotos,
    BuildingV2,
    CaptureDates,
    Corridor,
    CorridorFootpath,
    CorridorStation,
    CorridorStrip,
    CoverageRangeV2,
    CrossSectionV2,
    EvidenceSources,
    GroundV2,
    ModelRef,
    ObjectEvidence,
    Path,
    PathSample,
    SceneObjectV2,
    SceneV2,
)
from hero.model.types import (
    CanonicalModel,
    Entity,
    EntityState,
    EntityType,
    FidelityMap,
    FootpathState,
    Observation,
    ObservationKind,
    ProfileInterval,
    StripKind,
)
from hero.scene.geo import (
    LocalFrame,
    TileGeoref,
    compute_frame_to_px,
    latlon_to_tile_px,
    meters_per_px,
)
from hero.scene.schema import (
    MAX_TOKENS,
    Action,
    FootpathSide,
    Generation,
    Lane,
    LatLon,
    ObjectSource,
    Quality,
    SegmentInfo,
    SizeM,
    Token,
    TokenReason,
    Walkability,
)
from hero.scene.walkability import COVERAGE_WEIGHT, CROSSING_BONUS, HAZARD_SATURATION, HAZARD_WEIGHT, _round_half_up

STEP_M = 2.5
GROUND_HALF_M = 30.0
SEAM_M = 6.0
MIN_LANE_SEP_M = 1.4
DEFAULT_EDGE_M = 4.25  # half of the default 8.5 m carriageway
PRIOR_PATH_OFFSET_M = 0.9
LANE_BAND_M = (1.5, 1.6, 2.0)
MIN_OBJECT_CONFIDENCE = 0.2
LANE_OVERLAP_M = 0.15
TOKEN_EVERY_M = 10.0
DEFAULT_BUILDING_COLOUR = "#b8b0a2"
SYNTHETIC_TILE = (1280, 1280, 19, 2)


# ------------------------------------------------------------------ small helpers


def _r(x: float, nd: int = 3) -> float:
    return round(x, nd) + 0.0  # + 0.0 normalises -0.0


def _wrap_pi(a: float) -> float:
    w = (a + math.pi) % (2.0 * math.pi) - math.pi
    return math.pi if w == -math.pi else w


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def station_grid(length: float) -> list[float]:
    """Every 2.5 m plus the end (strictly increasing; a final remainder under 1 mm is absorbed)."""
    out = []
    i = 0
    while i * STEP_M < length - 1e-3:
        out.append(_r(i * STEP_M))
        i += 1
    out.append(length)
    return out


# ------------------------------------------------------------------ per-station evaluation


@dataclass
class _Fp:
    lo: float
    hi: float
    state: str
    material: str
    height: float
    confidence: float


@dataclass
class _Sp:
    kind: str
    lo: float
    hi: float
    material: str
    height: float
    confidence: float


@dataclass
class _Station:
    s: float
    interval: ProfileInterval
    fp: _Fp | None
    strips: list[_Sp]
    edge: float
    lane: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


def _interval_confidence(iv: ProfileInterval, side: str) -> float:
    strip = iv.footpath_strip(side)  # type: ignore[arg-type]
    if strip is not None:
        return _clamp01(strip.confidence)
    votes = iv.state_votes.get(iv.footpath_state.value, 0.0)
    return _clamp01(1.0 - math.exp(-votes)) if votes > 0 else 0.0


def _resolve_overlaps(strips: list[_Sp], fp: _Fp | None) -> list[_Sp]:
    """Make the listed strips non-overlapping: the more confident strip keeps its span, a weaker one is trimmed
    (the larger remaining piece is kept; nothing left drops it). The modelled-side footpath ranks first."""
    taken: list[tuple[float, float]] = [] if fp is None else [(fp.lo, fp.hi)]
    out: list[_Sp] = []
    for sp in sorted(strips, key=lambda x: (-x.confidence, x.kind, x.lo, x.hi)):
        pieces = [(sp.lo, sp.hi)]
        for a, b in taken:
            nxt = []
            for lo, hi in pieces:
                if hi <= a or lo >= b:
                    nxt.append((lo, hi))
                    continue
                if a - lo > 1e-9:
                    nxt.append((lo, a))
                if hi - b > 1e-9:
                    nxt.append((b, hi))
            pieces = nxt
        pieces = [p for p in pieces if p[1] - p[0] > 1e-6]
        if not pieces:
            continue
        lo, hi = max(pieces, key=lambda p: (p[1] - p[0], -p[0]))
        out.append(_Sp(sp.kind, lo, hi, sp.material, sp.height, sp.confidence))
        taken.append((lo, hi))
    out.sort(key=lambda x: (x.lo, x.hi, x.kind))
    return out


def _eval_station(model: CanonicalModel, s: float, sign: int) -> _Station:
    iv = model.interval_at(s)
    f = _clamp01((s - iv.s_from) / (iv.s_to - iv.s_from))
    side = model.footpath_side
    fp: _Fp | None = None
    strips: list[_Sp] = []
    edges: list[float] = []
    for st in iv.strips:
        a, b = sign * st.t_lo_at(f), sign * st.t_hi_at(f)
        lo, hi = min(a, b), max(a, b)
        is_fp = st.kind is StripKind.footpath and st.side == side
        if is_fp:
            if iv.footpath_state is not FootpathState.missing:
                fp = _Fp(lo, hi, iv.footpath_state.value, st.material.value, st.height_m, st.confidence)
            continue
        if st.kind is StripKind.carriageway:
            edges.append(hi)
        strips.append(_Sp(st.kind.value, lo, hi, st.material.value, st.height_m, st.confidence))
    strips = _resolve_overlaps(strips, fp)
    return _Station(s=s, interval=iv, fp=fp, strips=strips, edge=max(edges) if edges else DEFAULT_EDGE_M)


def _fill_centres(stations: Sequence[_Station]) -> list[float]:
    """Footpath centre per station; gaps (missing) take the nearest known centre, interpolated across the gap."""
    known = [(i, (st.fp.lo + st.fp.hi) / 2.0) for i, st in enumerate(stations) if st.fp is not None]
    out: list[float] = []
    for i, st in enumerate(stations):
        if st.fp is not None:
            out.append((st.fp.lo + st.fp.hi) / 2.0)
            continue
        prev = next(((j, c) for j, c in reversed(known) if j < i), None)
        nxt = next(((j, c) for j, c in known if j > i), None)
        if prev and nxt:
            sp, sn = stations[prev[0]].s, stations[nxt[0]].s
            out.append(prev[1] + (nxt[1] - prev[1]) * (st.s - sp) / (sn - sp))
        elif prev or nxt:
            out.append((prev or nxt)[1])  # type: ignore[index]
        else:
            out.append(st.edge + PRIOR_PATH_OFFSET_M)
    return out


def _blend_seam(stations: list[_Station], length: float) -> None:
    if len(stations) < 2:
        return
    first = stations[0]
    start = length - SEAM_M
    for st in stations[1:]:
        if st.s <= start:
            continue
        w = min(1.0, (st.s - start) / SEAM_M)
        st.lane = [(1 - w) * a + w * b for a, b in zip(st.lane, first.lane)]
        st.edge = (1 - w) * st.edge + w * first.edge
        if st.fp is not None and first.fp is not None:
            st.fp.lo = (1 - w) * st.fp.lo + w * first.fp.lo
            st.fp.hi = (1 - w) * st.fp.hi + w * first.fp.hi
            st.fp.confidence = (1 - w) * st.fp.confidence + w * first.fp.confidence
        used: dict[str, int] = {}
        for sp in st.strips:
            k = used.get(sp.kind, 0)
            used[sp.kind] = k + 1
            twins = [x for x in first.strips if x.kind == sp.kind]
            if k < len(twins):
                sp.lo = (1 - w) * sp.lo + w * twins[k].lo
                sp.hi = (1 - w) * sp.hi + w * twins[k].hi
                sp.confidence = (1 - w) * sp.confidence + w * twins[k].confidence


# ------------------------------------------------------------------ geometry


@dataclass(frozen=True)
class _Geo:
    ref: ReferenceLine
    georef: TileGeoref
    sign: int
    has_tile: bool

    def px(self, s: float, t: float) -> tuple[float, float]:
        lat, lon = self.ref.point_at(s, t)
        x, y = latlon_to_tile_px(lat, lon, self.georef)
        return _r(x, 2), _r(y, 2)

    def latlon(self, s: float, t: float) -> LatLon:
        lat, lon = self.ref.point_at(s, t)
        return LatLon(lat=_r(lat, 6), lon=_r(lon, 6))


def _georef(tile: Observation | None, ref: ReferenceLine) -> tuple[TileGeoref, float]:
    if tile is not None:
        g = TileGeoref(center_lat=tile.lat, center_lon=tile.lon, zoom=tile.zoom or SYNTHETIC_TILE[2],
                       scale=tile.scale or SYNTHETIC_TILE[3], size_px=(tile.width_px, tile.height_px))
        return g, tile.meters_per_px or meters_per_px(tile.lat, g.zoom, g.scale)
    lat, lon = ref.point_at(ref.length_s / 2.0, 0.0)
    w, h, z, sc = SYNTHETIC_TILE
    return TileGeoref(center_lat=lat, center_lon=lon, zoom=z, scale=sc, size_px=(w, h)), meters_per_px(lat, z, sc)


# ------------------------------------------------------------------ objects


def _interp(stations: Sequence[_Station], s: float, getter) -> float:
    if s <= stations[0].s:
        return getter(stations[0])
    for a, b in zip(stations, stations[1:]):
        if s <= b.s:
            f = (s - a.s) / (b.s - a.s)
            return getter(a) * (1 - f) + getter(b) * f
    return getter(stations[-1])


def _fp_width(st: _Station) -> float:
    return st.fp.hi - st.fp.lo if st.fp is not None else LANE_BAND_M[0]


def _lanes_for(stations: Sequence[_Station], s: float, lat: float, width: float) -> list[int]:
    lo, hi = lat - width / 2.0, lat + width / 2.0
    need = min(LANE_OVERLAP_M, width)
    fpw = _interp(stations, s, _fp_width)
    out = []
    for i in range(3):
        c = _interp(stations, s, lambda st, i=i: st.lane[i])
        band = fpw if i == 0 else LANE_BAND_M[i]
        overlap = min(hi, c + band / 2.0) - max(lo, c - band / 2.0)
        if overlap >= need - 1e-9:
            out.append(i)
    return out


def _entity_object(e: Entity, model: CanonicalModel, g: _Geo, stations: Sequence[_Station], length: float,
                   observations: Mapping[str, Observation], assets: Mapping[str, str]) -> list[SceneObjectV2]:
    cat = catalogue.entry(e.type)
    w0, h0, d0 = catalogue.default_size_m(e.type, e.subtype, e.extent.length_s)
    width = e.extent.width_t if e.extent.width_t > 0 else w0
    depth = e.extent.length_s if e.extent.length_s > 0 else d0
    height = e.height_m if e.height_m is not None else h0
    x = _r(min(max(e.position.s, 0.0), length - 1e-3))
    depth = max(0.0, min(depth, length + 5.0 - x))
    lat = g.sign * e.position.t
    lanes = _lanes_for(stations, x, lat, max(width, 0.05))
    scenery = not lanes
    action = Action.none if scenery else Action(catalogue.action_for(e.type, e.attributes, in_lane_band=True))
    counts = describe.evidence_counts(e, observations)
    support_ids = sorted({p.observation_id for p in e.supports()})
    src = None
    if e.billboard is not None and e.billboard.observation_id in observations:
        src = ObjectSource(image_url=observations[e.billboard.observation_id].url, crop_px=e.billboard.crop_px)
    desc = e.description or describe.entity_text(e)
    base = SceneObjectV2(
        id=e.id, entity_id=e.id, type=e.type.value, label=describe.entity_label(e), x_m=x, length_m=_r(depth),
        lanes=lanes, action=action, lateral_m=_r(lat), yaw_deg=_r(e.orientation_deg or 0.0, 1),
        size_m=SizeM(w=_r(width), h=_r(height), d=_r(depth)), confidence=_r(e.confidence, 3), scenery=scenery,
        description=desc,
        evidence=ObjectEvidence(observation_count=len(support_ids), sources=EvidenceSources(**counts),
                                summary=describe.evidence_summary(e, observations, legacy=model.legacy),
                                observation_ids=support_ids, state=e.state.value),
        source=src, billboard_url=assets.get(e.id), latlon=g.latlon(x, e.position.t), tile_px=g.px(x, e.position.t))
    out = [base]
    if cat.type is EntityType.tree and catalogue.emits_low_branch(e.type, e.attributes):
        lb_w, lb_h, lb_d = catalogue.default_size_m(EntityType.low_branch)
        lane0 = _interp(stations, x, lambda st: st.lane[0])  # the canopy hangs over the walkway
        lb_lanes = _lanes_for(stations, x, lane0, lb_w)
        out.append(SceneObjectV2(
            id=f"lb-{e.id}", entity_id=e.id, type="low_branch", label="low branch", x_m=x, length_m=_r(lb_d),
            lanes=lb_lanes, action=Action.none if not lb_lanes else Action.slide, lateral_m=_r(lane0), yaw_deg=0.0,
            size_m=SizeM(w=lb_w, h=lb_h, d=lb_d), confidence=_r(e.confidence, 3), scenery=not lb_lanes,
            description="Low branch hanging over the path from the tree "
                        f"about {describe._num(x)} m from the start.",
            evidence=base.evidence, source=src, billboard_url=None, latlon=g.latlon(x, g.sign * lane0),
            tile_px=g.px(x, g.sign * lane0)))
    return out


def _interval_objects(model: CanonicalModel, g: _Geo, stations: Sequence[_Station], length: float) -> list[SceneObjectV2]:
    out: list[SceneObjectV2] = []
    for iv in model.profile:
        if iv.footpath_state not in (FootpathState.missing, FootpathState.blocked):
            continue
        x = _r(min(iv.s_from, length - 1e-3))
        depth = _r(min(iv.s_to - iv.s_from, length + 5.0 - x))
        mid = (iv.s_from + iv.s_to) / 2.0
        lat = _interp(stations, mid, lambda st: st.lane[0])
        conf = _r(_interval_confidence(iv, model.footpath_side), 3)
        a, b = describe._num(iv.s_from), describe._num(iv.s_to)
        if iv.footpath_state is FootpathState.missing:
            oid, typ, label, h = f"gap-{iv.id}", "footpath_gap", "footpath missing", 0.2
            desc = f"The footpath is missing from {a} to {b} m; walkers have to step onto the road."
        else:
            oid, typ, label, h = f"blocked-{iv.id}", "debris", "footpath blocked", 1.0
            desc = f"The footpath is blocked from {a} to {b} m."
        out.append(SceneObjectV2(
            id=oid, entity_id=None, type=typ, label=label, x_m=x, length_m=depth, lanes=[0], action=Action.dodge,
            lateral_m=_r(lat), yaw_deg=0.0, size_m=SizeM(w=_r(_interp(stations, mid, _fp_width)), h=h, d=depth),
            confidence=conf, scenery=False, description=desc,
            evidence=ObjectEvidence(observation_count=0, sources=EvidenceSources(), summary=f"Footpath {iv.footpath_state.value} from {a} to {b} m "
                                    "(from the profile, not a detected object).", observation_ids=[],
                                    state="present"),
            source=None, billboard_url=None, latlon=g.latlon(x, g.sign * lat), tile_px=g.px(x, g.sign * lat)))
    return out


# ------------------------------------------------------------------ tokens / walkability


def _tokens(model: CanonicalModel, g: _Geo, stations: Sequence[_Station], objects: Sequence[SceneObjectV2],
            length: float) -> list[Token]:
    hazards = [o for o in objects if o.action != Action.none and 0 in o.lanes]

    def blocked(x: float) -> bool:
        return any(o.x_m - 0.5 <= x <= o.x_m + o.length_m + 0.5 for o in hazards)

    marks = [(o.x_m, 1, TokenReason.crossing if o.type == "crossing" else TokenReason.ramp)
             for o in objects if o.type in ("crossing", "ramp") and o.x_m < length]
    marks.sort(key=lambda m: (m[0], m[2].value))
    marks = marks[:MAX_TOKENS]
    runs: list[tuple[float, float]] = []
    for iv in model.profile:
        if iv.footpath_state is FootpathState.intact:
            if runs and abs(runs[-1][1] - iv.s_from) < 1e-6:
                runs[-1] = (runs[-1][0], iv.s_to)
            else:
                runs.append((iv.s_from, iv.s_to))
    cand: list[float] = []
    for a, b in runs:
        for k in range(int((b - a) // TOKEN_EVERY_M)):
            x0 = a + TOKEN_EVERY_M * (k + 0.5)
            for shift in (0.0, 1.0, 2.0, 3.0, -1.0, -2.0, -3.0):
                x = x0 + shift
                if a + 0.25 <= x <= b - 0.25 and x < length and not blocked(x):
                    cand.append(_r(x))
                    break
    room = MAX_TOKENS - len(marks)
    if len(cand) > room:
        cand = [cand[(i * len(cand)) // room] for i in range(room)] if room > 0 else []
    items = [(x, 0, TokenReason.intact_footpath) for x in cand] + marks
    items.sort(key=lambda m: (m[0], m[1]))
    out = []
    for n, (x, lane, reason) in enumerate(items, 1):
        lat = _interp(stations, x, lambda st, lane=lane: st.lane[lane])
        out.append(Token(id=f"t{n}", x_m=x, lane=lane, reason=reason, latlon=g.latlon(x, g.sign * lat),
                         tile_px=g.px(x, g.sign * lat)))
    return out


def _walkability(model: CanonicalModel, objects: Sequence[SceneObjectV2], ranges: Sequence[CoverageRangeV2],
                 length: float) -> Walkability:
    intact = sum(r.length_m for r in ranges if r.state is FootpathState.intact)
    pct = min(100.0, max(0.0, _round_half_up(100.0 * intact / length, 1)))
    hazards = sum(1 for o in objects if o.action != Action.none)
    crossing = any(o.type == "crossing" for o in objects)
    raw = (COVERAGE_WEIGHT * pct + (CROSSING_BONUS if crossing else 0.0)
           + HAZARD_WEIGHT * max(0.0, 1.0 - hazards / HAZARD_SATURATION))
    return Walkability(footpath_coverage_pct=pct, hazard_count=hazards, crossing_present=crossing,
                       score=int(min(100, max(0, _round_half_up(raw)))))


# ------------------------------------------------------------------ the compiler


def compile_scene(model: CanonicalModel, *, observations: Mapping[str, Observation], assets: Mapping[str, str],
                  tile: Observation | None, fidelity: FidelityMap, slug_name: str, city: str) -> SceneV2:
    ref = ReferenceLine.from_model(model.reference_line)
    length = model.reference_line.length_s
    side = model.footpath_side
    sign = 1 if side == "left" else -1
    georef, mpp = _georef(tile, ref)
    g = _Geo(ref=ref, georef=georef, sign=sign, has_tile=tile is not None)

    # ---- stations
    s_grid = station_grid(length)
    stations = [_eval_station(model, s, sign) for s in s_grid]
    centres = _fill_centres(stations)
    for st, c in zip(stations, centres):
        l1 = st.edge - PRIOR_PATH_OFFSET_M
        st.lane = [max(c, l1 + MIN_LANE_SEP_M), l1, l1 - 1.8]
    _blend_seam(stations, length)

    # ---- path (world frame)
    b0 = ref.bearing_deg(0.0)
    turn_sign = -1 if side == "left" else 1
    thetas = [_wrap_pi(turn_sign * math.radians(ref.bearing_deg(s) - b0)) for s in s_grid]
    xs, zs = [0.0], [0.0]
    for i in range(len(s_grid) - 1):
        ds = s_grid[i + 1] - s_grid[i]
        xs.append(xs[-1] + math.sin(thetas[i]) * ds)
        zs.append(zs[-1] - math.cos(thetas[i]) * ds)
    samples = [PathSample(s=_r(s), x=_r(x, 4), z=_r(z, 4), theta=_r(th, 5), tile_px=g.px(s, 0.0))
               for s, x, z, th in zip(s_grid, xs, zs, thetas)]
    path = Path(length_m=length, samples=samples)

    # ---- corridor
    w_px, h_px = georef.size_px
    cstations = []
    for st in stations:
        uv = None
        if tile is not None:
            uv = tuple((_r(px / w_px, 4), _r(py / h_px, 4))
                       for px, py in (g.px(st.s, sign * -GROUND_HALF_M), g.px(st.s, sign * GROUND_HALF_M)))
        cstations.append(CorridorStation(
            s=_r(st.s),
            lane_lat=(_r(st.lane[0]), _r(st.lane[1]), _r(st.lane[2])),
            footpath=None if st.fp is None else CorridorFootpath(
                lo=_r(st.fp.lo), hi=_r(st.fp.hi), state=FootpathState(st.fp.state), material=st.fp.material,
                height_m=_r(st.fp.height), confidence=_r(_clamp01(st.fp.confidence))),
            strips=[CorridorStrip(kind=sp.kind, lo=_r(sp.lo), hi=_r(sp.hi), material=sp.material,
                                  height_m=_r(sp.height), confidence=_r(_clamp01(sp.confidence)))
                    for sp in st.strips],
            ground_uv=uv))
    corridor = Corridor(step_m=STEP_M, ground_half_m=GROUND_HALF_M, stations=cstations)

    # ---- cross-section
    weights = [((s_grid[min(i + 1, len(s_grid) - 1)] - s_grid[max(i - 1, 0)]) / 2.0) for i in range(len(s_grid))]
    total_w = sum(weights) or 1.0
    lane_mean = [sum(w * st.lane[i] for w, st in zip(weights, stations)) / total_w for i in range(3)]
    lane_r = [_r(lane_mean[0]), _r(lane_mean[1]), _r(lane_mean[2])]
    lane_r[1] = min(lane_r[1], _r(lane_r[0] - MIN_LANE_SEP_M))
    lane_r[2] = min(lane_r[2], _r(lane_r[1] - MIN_LANE_SEP_M))
    widths = []
    for st in stations:
        cw = [sp.hi - sp.lo for sp in st.strips if sp.kind == "carriageway"]
        if cw:
            widths.append(max(cw))
    road_width = _r(sum(widths) / len(widths)) if widths else 8.5
    lanes = [Lane(index=0, name="footpath", kind="footpath", lat_m=lane_r[0], width_m=LANE_BAND_M[0]),
             Lane(index=1, name="road_edge", kind="road_edge", lat_m=lane_r[1], width_m=LANE_BAND_M[1]),
             Lane(index=2, name="road", kind="road", lat_m=lane_r[2], width_m=LANE_BAND_M[2])]
    ranges = []
    prev_to = 0.0
    for i, iv in enumerate(model.profile):
        to = length if i == len(model.profile) - 1 else _r(iv.s_to)
        if to <= prev_to:
            continue
        ranges.append(CoverageRangeV2(from_m=prev_to, to_m=to, state=iv.footpath_state,
                                      confidence=_r(_interval_confidence(iv, side))))
        prev_to = to
    cross = CrossSectionV2(road_width_m=max(road_width, 0.1), lanes=lanes, footpath_coverage=ranges)

    # ---- buildings
    buildings = []
    for b in sorted(model.buildings, key=lambda x: x.id):
        buildings.append(BuildingV2(
            id=b.id, source=b.source, footprint=[(_r(s), _r(sign * t)) for s, t in b.footprint], height_m=b.height_m,
            color_hex=b.colour_hex or DEFAULT_BUILDING_COLOUR, facade_url=assets.get(b.id),
            confidence=_r(b.confidence), side="modelled" if b.side == side else "opposite",
            description=b.description or f"Building about {b.height_m:.0f} m tall on the "
                                         f"{'modelled' if b.side == side else 'opposite'} side."))

    # ---- objects
    objects: list[SceneObjectV2] = []
    for e in sorted(model.entities, key=lambda x: x.id):
        if e.state is EntityState.removed or e.confidence < MIN_OBJECT_CONFIDENCE:
            continue
        objects.extend(_entity_object(e, model, g, stations, length, observations, assets))
    objects.extend(_interval_objects(model, g, stations, length))
    objects.sort(key=lambda o: (o.x_m, o.id))
    tokens = _tokens(model, g, stations, objects, length)
    walk = _walkability(model, objects, ranges, length)

    # ---- about / generation / model ref
    fused = sorted(set(model.fused_observation_ids))
    buckets = {"street_view": 0, "satellite": 0, "user": 0}
    dates = []
    overhead = False
    for oid in fused:
        o = observations.get(oid)
        if o is None:
            buckets["street_view"] += 1
            continue
        if o.kind is ObservationKind.user_photo:
            buckets["user"] += 1
        elif o.kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop):
            buckets["satellite"] += 1
            overhead = True
        else:
            buckets["street_view"] += 1
        if o.capture_date:
            dates.append(o.capture_date)
    level = fidelity.overall.level
    parts = describe.low_confidence_parts(model, fidelity)
    about = About(
        model_version=model.version,
        fidelity=AboutFidelity(level=f"L{level}", score=fidelity.overall.score, label=LEVEL_LABELS[level],
                               histogram_m={k: _r(v) for k, v in sorted(fidelity.overall.histogram_m.items())}),
        photos=AboutPhotos(total=len(fused), street_view=buckets["street_view"], satellite=buckets["satellite"],
                           user=buckets["user"]),
        last_updated=model.created_at.date().isoformat(),
        capture_dates=CaptureDates(oldest=min(dates) if dates else None, newest=max(dates) if dates else None),
        low_confidence_parts=parts,
        summary=describe.about_summary(model, fidelity, name=slug_name, photos=len(fused), parts=parts),
        improve_hint=describe.improve_hint(fidelity), legacy=model.legacy, placement_verified=model.placement_verified,
        degraded=(not model.legacy) and level == 0)
    quality = Quality.full if level >= 1 else (Quality.satellite_only if overhead else Quality.generic)
    confs = [o.confidence for o in objects] + [r.confidence for r in ranges]
    generation = Generation(run_id=model.run_id or "none", quality=quality,
                            confidence_overall=_clamp01(_r(sum(confs) / len(confs), 2)) if confs else 0.0,
                            review_iterations=0, models={}, warnings=[])

    first, last = model.reference_line.points[0], model.reference_line.points[-1]
    start, end = LatLon(lat=first.lat, lon=first.lon), LatLon(lat=last.lat, lon=last.lon)
    frame = LocalFrame.from_endpoints(first.lat, first.lon, last.lat, last.lon, FootpathSide(side))
    segment = SegmentInfo(slug=model.segment_slug, name=slug_name, city=city, start=start, end=end, length_m=length,
                          heading_deg=_r(frame.heading_deg % 360.0, 1) % 360.0, footpath_side=FootpathSide(side))
    ground = GroundV2(tile_url=tile.url if tile is not None else None, size_px=(w_px, h_px), zoom=georef.zoom,
                      scale=georef.scale, center=LatLon(lat=georef.center_lat, lon=georef.center_lon),
                      meters_per_px=_r(mpp, 5), frame_to_px=compute_frame_to_px(frame, georef))
    return SceneV2(schema_version=2, segment=segment, ground=ground, path=path, corridor=corridor, cross_section=cross,
                   buildings=buildings, objects=objects, tokens=tokens, walkability=walk, about=about,
                   generation=generation, model=ModelRef(version=model.version, model_hash=model_hash(model),
                                                         compiled_by=COMPILED_BY))
