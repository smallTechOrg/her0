"""``plan_street`` - the geometry half of the compile (spec/data.md r1 body, re-read by the r2 rule).

``plan_street(model, *, observations, fidelity, gaps, textures, gameplay) -> StreetPlan`` is pure: stations (lanes,
footpath, strips, kerb, ground source), the reference line, the ground texture plan, contiguous frontage on both sides,
fixed furniture, fixed hazard objects (plus the synthetic ``gap-`` / ``blocked-`` ones), tokens and the dynamic context
(spawn hints). Lane centres, kerb default, token and walkability rules come from ``gameplay.json``.

Interpretation notes
--------------------
* ``plan_street`` also accepts the optional keywords ``tile`` (the overview observation; only used for ``tile_px``
  values), ``name`` and ``city`` (default: the reference-line name / slug and ``""``) - the contract signature is a
  subset.
* ``objects`` holds only non-scenery fixed objects (scenery is drawn from furniture/frontage and is never a collider).
* ``kerb.lat`` is the carriageway edge nearest the footpath after strip overlap resolution (the footpath's ``lo`` when
  no carriageway strip is listed, the station edge when there is no footpath); its height is the footpath strip height
  (0.15 m prior, at most 0.4 m), 0.0 where the footpath is missing or a fixed ``kerb_cut`` / ``ramp`` / ``crossing``
  lowers it.
* ``fidelity`` is a copy of the fidelity map (overall level/score + bins).
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Callable, Mapping

from hero.export.gameplay_types import GameplayV1
from hero.model import dynamic, frontage, furniture, ground
from hero.model.compile import KERB_BASE_M, _Core, _core, _r, rules_from_gameplay
from hero.model.diff import model_hash
from hero.model.gap_types import GapReport
from hero.model.street_plan_types import (
    FidelityBin,
    FrontagePlan,
    FrontageSidePlan,
    Kerb,
    PlanFidelity,
    PlanFrame,
    PlanOrigin,
    PlanReferenceLine,
    PlanStation,
    RefPoint,
    StreetPlan,
)
from hero.model.texture_types import TextureRefs
from hero.model.types import CanonicalModel, EntityState, FidelityMap, Observation
from hero.scene.geo import LocalFrame
from hero.scene.schema import FootpathSide

MAX_KERB_M = 0.4
KERB_WIDTH_M = 0.2
LOWERING_TYPES = ("kerb_cut", "ramp", "crossing")


def _interp_fn(xs: list[float], ys: list[float]) -> Callable[[float], float]:
    def f(s: float) -> float:
        if s <= xs[0]:
            return ys[0]
        if s >= xs[-1]:
            return ys[-1]
        i = bisect_right(xs, s) - 1
        t = (s - xs[i]) / (xs[i + 1] - xs[i])
        return ys[i] * (1 - t) + ys[i + 1] * t

    return f


def _bounds(c: _Core, smooth: list | None = None) -> frontage.Bounds:
    xs = [st.s for st in c.stations]
    cs = c.corridor.stations
    mod_hi, opp_ceiling, opp_prior = [], [], []
    for k, (st, cst) in enumerate(zip(c.stations, cs)):
        fpk = smooth[k][1] if smooth is not None else cst.footpath
        mod_hi.append(fpk.hi if fpk is not None else dynamic.prior_band(st.edge)[1])
        near = [sp.lo for sp in cst.strips if sp.kind.value in ("carriageway", "footpath") and sp.lo < 0]
        any_lo = [sp.lo for sp in cst.strips if sp.lo < 0]
        carriage = [sp.lo for sp in cst.strips if sp.kind.value == "carriageway"]
        opp_ceiling.append((min(near) if near else (min(carriage) if carriage else -(st.edge + 4.0))) - frontage.OPPOSITE_CLEAR_M)
        if any_lo:
            opp_prior.append(min(any_lo) - frontage.PRIOR_OPPOSITE_OFFSET_M)
        elif carriage:
            opp_prior.append(min(carriage) - frontage.PRIOR_OPPOSITE_FALLBACK_M)
        else:
            opp_prior.append(-(st.edge * 2 + frontage.PRIOR_OPPOSITE_FALLBACK_M))
    hi = _interp_fn(xs, mod_hi)
    return frontage.Bounds(
        modelled_floor=lambda s: hi(s) + frontage.MODELLED_CLEAR_M,
        opposite_ceiling=_interp_fn(xs, opp_ceiling),
        modelled_prior=lambda s: hi(s) + frontage.PRIOR_MODELLED_OFFSET_M,
        opposite_prior=_interp_fn(xs, opp_prior))


SPIKE_M = 0.25           # a kerb / footpath-edge sample further than this from its 5-station median is a spike
SPIKE_HALF_WINDOW = 2


def _med(v: list[float]) -> float:
    x = sorted(v)
    return x[len(x) // 2]


def despike_stations(cstations: list) -> list[tuple[list, object]]:
    """``(strips, footpath)`` per corridor station with isolated spikes of the kerb line (carriageway ``hi``) and the
    footpath's outer edge replaced by their 5-station median (a single noisy sample draws a visible zig-zag kerb).
    The footpath's inner edge moves with the kerb so the strips stay edge-to-edge."""
    n = len(cstations)

    def car_hi(cst) -> float | None:
        v = [sp.hi for sp in cst.strips if sp.kind.value == "carriageway"]
        return max(v) if v else None

    kerb = [car_hi(c) for c in cstations]
    outer = [c.footpath.hi if c.footpath is not None else None for c in cstations]
    out = []
    for i, cst in enumerate(cstations):
        strips, fp = list(cst.strips), cst.footpath
        win = range(max(0, i - SPIKE_HALF_WINDOW), min(n, i + SPIKE_HALF_WINDOW + 1))
        k_old = kerb[i]
        if k_old is not None:
            med = _med([kerb[j] for j in win if kerb[j] is not None])
            if abs(k_old - med) > SPIKE_M:
                strips = [sp.model_copy(update={"hi": med}) if sp.kind.value == "carriageway" and abs(sp.hi - k_old) < 1e-9 else sp
                          for sp in strips]
                if fp is not None and abs(fp.lo - k_old) < 1e-6:
                    fp = fp.model_copy(update={"lo": med})
        if fp is not None and outer[i] is not None:
            med = _med([outer[j] for j in win if outer[j] is not None])
            if abs(outer[i] - med) > SPIKE_M and med > fp.lo + 0.3:
                fp = fp.model_copy(update={"hi": med})
        out.append((strips, fp))
    return out


def _kerb_spans(c: _Core, dynamic_ids: set[str]) -> list[tuple[float, float]]:
    spans = []
    for e in c.model.entities:
        if e.type.value in LOWERING_TYPES and e.id not in dynamic_ids and e.state is not EntityState.removed \
                and e.confidence >= 0.2:
            d = max(e.extent.length_s, 1.0)
            spans.append((e.position.s - 0.25, e.position.s + d + 0.25))
    return spans


def plan_from_core(c: _Core, *, fidelity: FidelityMap, gaps: GapReport, textures: TextureRefs | None,
                   gameplay: GameplayV1, name: str | None = None, city: str = "") -> StreetPlan:
    model = c.model
    length = c.length
    slug = model.segment_slug
    gtex = ground.plan_ground(slug, length, textures)
    dyn_ids = {e.id for e, _ in c.dynamic}
    spans = _kerb_spans(c, dyn_ids)

    pts: list[RefPoint] = []
    stations: list[PlanStation] = []
    smooth = despike_stations(list(c.corridor.stations))
    for st, cst, (cstrips, cfp) in zip(c.stations, c.corridor.stations, smooth):
        lat, lon = c.ref.point_at(st.s, 0.0)
        px = c.g.px(st.s, 0.0) if c.tile is not None else None
        pts.append(RefPoint(s=st.s, lat=round(lat, 6), lon=round(lon, 6), bearing_deg=_r(c.ref.bearing_deg(st.s), 2),
                            tile_px=px))
        carriage = [sp.hi for sp in cstrips if sp.kind.value == "carriageway"]
        if carriage:
            klat = max(carriage)
        elif cfp is not None:
            klat = cfp.lo
        else:
            klat = st.edge
        if cfp is None or any(a <= st.s <= b for a, b in spans):
            kh = 0.0
        else:
            kh = min(MAX_KERB_M, cfp.height_m if cfp.height_m > 0 else KERB_BASE_M)
        src = gtex.bin_source[min(int(st.s // gtex.bin_m + 1e-9), len(gtex.bin_source) - 1)]
        stations.append(PlanStation(s=cst.s, lane_lat=cst.lane_lat, footpath=cfp, strips=cstrips,
                                    kerb=Kerb(lat=_r(klat), height_m=_r(kh), width_m=KERB_WIDTH_M), ground_src=src))
    rl = model.reference_line
    reference = PlanReferenceLine(source=rl.source, osm_way_id=rl.osm_way_id, name=rl.name or "", length_s=length,
                                  step_m=2.5, points=pts)
    first, last = rl.points[0], rl.points[-1]
    lframe = LocalFrame.from_endpoints(first.lat, first.lon, last.lat, last.lon, FootpathSide(c.side))
    frame = PlanFrame(origin=PlanOrigin(lat=first.lat, lon=first.lon), heading_deg=_r(lframe.heading_deg % 360.0, 1) % 360.0)

    bounds = _bounds(c, smooth)
    sides = []
    for side in frontage.SIDES:
        ev = frontage.evidence(model, side)
        dec = frontage.decisions(ev)
        segs = frontage.plan_side(model, side, ev, dec, bounds, slug=slug)
        sides.append(FrontageSidePlan(side=side, texture=(textures.frontage.get(side) if textures else None),  # type: ignore[arg-type]
                                      segments=segs))

    objects = [o.model_copy(update={"tile_px": None}) if c.tile is None else o for o in c.objects if not o.scenery]
    fur = furniture.plan_furniture(model, sign=c.sign, dynamic_ids=dyn_ids, objects=c.objects, fidelity=fidelity,
                                   tints=textures.tints if textures else None)
    _, dyn_ctx = dynamic.dynamic_context(c.dynamic, length, gameplay)
    fid = PlanFidelity(overall_level=fidelity.overall.level, score=fidelity.overall.score,
                       bins=[FidelityBin(index=b.index, s_from=b.s_from, s_to=b.s_to, level=b.level) for b in fidelity.bins])
    return StreetPlan(
        slug=slug, name=name or rl.name or slug, city=city, model_version=model.version, model_hash=model_hash(model),
        footpath_side=c.side, length_m=length, frame=frame, reference_line=reference, corridor_step_m=2.5,  # type: ignore[arg-type]
        stations=stations, ground_tex=gtex, frontage=FrontagePlan(step_m=frontage.CELL_M, sides=sides), furniture=fur,
        objects=objects, tokens=c.tokens, dynamic_context=dyn_ctx, fidelity=fid,
        interpolated=[i for i in gaps.interpolations if i.what != "furniture_unknown"])


def plan_street(model: CanonicalModel, *, observations: Mapping[str, Observation], fidelity: FidelityMap,
                gaps: GapReport, textures: TextureRefs | None, gameplay: GameplayV1,
                tile: Observation | None = None, name: str | None = None, city: str = "",
                assets: Mapping[str, str] | None = None) -> StreetPlan:
    c = _core(model, observations=observations, assets=assets or {}, tile=tile, rules=rules_from_gameplay(gameplay))
    return plan_from_core(c, fidelity=fidelity, gaps=gaps, textures=textures, gameplay=gameplay, name=name, city=city)
