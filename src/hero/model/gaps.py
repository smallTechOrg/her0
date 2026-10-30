"""Gap analysis (spec/data.md -> "Gap analysis"): per 5 m bin, what the data supports and what the compile renders.

``analyze_gaps(model, observations, fidelity, textures) -> GapReport`` is pure. For every bin and layer it records the
*evidence* (what the data really supports) and the *rendered* source (what the compiled street shows); the compile
guarantees ``rendered`` is never blank - whatever is not evidence-backed is interpolated (from neighbours) or taken from a
neutral prior, and listed in ``interpolations``.

Evidence rules (bin = 5 m, last shorter)
----------------------------------------
* ground:    ``satellite_hires`` when the fidelity bin is ``overhead_hires``; ``satellite_overview`` when a fused overview
  tile contains ``(s, 0)`` and ``(s, +-8)``; else ``none``. Rendered = the bake's ``bin_source`` (``procedural`` for
  every bin without baked textures, see ``hero.model.ground``).
* frontage (per side): ``osm`` when a footprint / wall / gate covers >= min(2.5 m, bin) of the bin within 35 m; ``photo``
  when a street-level observation looking at that side covers the bin (direct left/right cell, or a forward / back /
  oblique cell whose camera range weight is >= 0.5); ``osm+photo`` both; ``none``. Rendered comes from the same decisions
  ``hero.model.frontage.plan_side`` uses (``osm`` | ``vision`` | ``interpolated`` | ``prior``).
* footpath:  ``traced`` (a modelled-side footpath strip with weight >= 0.5), ``state_only`` (a weak strip or state
  votes), ``prior`` (neither). Rendered: ``traced`` / ``state_only`` / ``interpolated`` (an ``unknown`` state painted
  from the nearest known neighbour) / ``prior``.
* furniture: ``street_level`` when any street-level observation covers the bin (fidelity ``views > 0``), else ``none``
  (informational: no objects are invented).

Interpretation notes: interpolation entries use the physical side (``left`` / ``right``) for frontage and the
footpath-side for footpath/kerb; ground and furniture use ``both``.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from hero.model import frontage as fr
from hero.model import ground as gr
from hero.model.calibrate import range_weight
from hero.model.coverage import STREET_KINDS, observation_cells, tile_covers
from hero.model.gap_types import (
    BIN_M,
    BlankCounts,
    FootpathGap,
    FrontageGap,
    FrontageSideGap,
    GapBin,
    GapCounts,
    GapReport,
    GroundGap,
    FurnitureGap,
    Interpolation,
)
from hero.model.refline import ReferenceLine
from hero.model.texture_types import TextureRefs
from hero.model.types import CanonicalModel, FidelityMap, FootpathState, Observation, ObservationKind

MIN_COVER_M = 2.5
PHOTO_RANGE_WEIGHT = 0.5
DEFAULT_KERB_NOTE = "Kerb height is not measured here; the default 0.15 m kerb is used."


def _r(x: float) -> float:
    return round(x + 0.0, 3)


def bins_of(length: float) -> list[tuple[float, float]]:
    n = gr.n_bins(length)
    return [(_r(i * BIN_M), _r(min(length, (i + 1) * BIN_M))) for i in range(n)]


def _covers(c_from: float, c_to: float, a: float, b: float) -> bool:
    return min(c_to, b) - max(c_from, a) >= min(MIN_COVER_M, b - a) - 1e-9


def _photo_bins(model: CanonicalModel, observations: Mapping[str, Observation], ref: ReferenceLine,
                bins: Sequence[tuple[float, float]]) -> dict[str, list[bool]]:
    """{'left': [...], 'right': [...]} per bin: a street-level observation looks at that physical side here."""
    out = {"left": [False] * len(bins), "right": [False] * len(bins)}
    for oid in sorted(set(model.fused_observation_ids)):
        obs = observations.get(oid)
        if obs is None or obs.kind not in (ObservationKind.streetview, ObservationKind.user_photo):
            continue
        for c in observation_cells(obs, ref):
            if c.kind not in STREET_KINDS and c.kind != "closeup":
                continue
            for i, (a, b) in enumerate(bins):
                if not _covers(c.s_from, c.s_to, a, b):
                    continue
                if c.kind in ("left", "right"):
                    out[c.kind][i] = True
                elif c.kind == "closeup" and c.side in ("left", "right"):
                    out[c.side][i] = True
                elif range_weight(abs((a + b) / 2.0 - c.station_s)) >= PHOTO_RANGE_WEIGHT:
                    if c.side == "both":
                        out["left"][i] = out["right"][i] = True
                    else:
                        out[c.side][i] = True
    return out


def _footpath_bin(model: CanonicalModel, a: float, b: float) -> tuple[str, str, bool]:
    """(evidence, rendered, kerb_defaulted) for the bin."""
    iv = model.interval_at((a + b) / 2.0)
    strip = iv.footpath_strip(model.footpath_side)
    state = iv.footpath_state
    votes = sum(iv.state_votes.values()) > 0
    if state is FootpathState.unknown and strip is None:
        known = any(x.footpath_state is not FootpathState.unknown for x in model.profile)
        return "prior", "interpolated" if known else "prior", False
    if strip is not None and strip.weight >= 0.5 and state is not FootpathState.missing:
        return "traced", "traced", strip.height_m <= 0.0
    if strip is not None or votes or state is FootpathState.missing:
        return "state_only", "state_only", bool(strip is not None and strip.height_m <= 0.0)
    return "prior", "prior", False


def _runs(flags: Sequence[object], bins: Sequence[tuple[float, float]]) -> list[tuple[int, int, object]]:
    """Consecutive runs (first bin, last bin, value) of non-None values."""
    out: list[tuple[int, int, object]] = []
    for i, f in enumerate(flags):
        if f is None:
            continue
        if out and out[-1][1] == i - 1 and out[-1][2] == f:
            out[-1] = (out[-1][0], i, f)
        else:
            out.append((i, i, f))
    return out


def analyze_gaps(model: CanonicalModel, observations: Mapping[str, Observation], fidelity: FidelityMap,
                 textures: TextureRefs | None) -> GapReport:
    length = model.reference_line.length_s
    ref = ReferenceLine.from_model(model.reference_line)
    bins = bins_of(length)
    n = len(bins)
    if len(fidelity.bins) < n:
        raise ValueError(f"the fidelity map has {len(fidelity.bins)} bin(s) but the {length:g} m street has {n} "
                         f"5 m bins; recompute the fidelity map for this model version before analysing gaps")
    phys = {"modelled": model.footpath_side, "opposite": "right" if model.footpath_side == "left" else "left"}

    # ---- ground
    overview = [observations[o] for o in sorted(set(model.fused_observation_ids))
                if o in observations and observations[o].kind is ObservationKind.satellite_overview]
    g_evidence: list[str] = []
    for i, (a, b) in enumerate(bins):
        fb = fidelity.bins[i]
        if fb.overhead_hires:
            g_evidence.append("satellite_hires")
        elif any(tile_covers(o, ref, a, b) for o in overview):
            g_evidence.append("satellite_overview")
        else:
            g_evidence.append("none")
    g_rendered = gr.bin_sources(length, textures)

    # ---- frontage
    photo = _photo_bins(model, observations, ref, bins)
    side_ev = {s: fr.evidence(model, s) for s in fr.SIDES}
    side_dec = {s: fr.decisions(side_ev[s]) for s in fr.SIDES}
    f_gap: dict[str, list[FrontageSideGap]] = {}
    for s in fr.SIDES:
        rows = []
        for i, (a, b) in enumerate(bins):
            has_geo, rendered = fr.rendered_label(side_ev[s], side_dec[s], a, b)
            has_photo = photo[phys[s]][i]
            ev = "osm+photo" if has_geo == "osm" and has_photo else "osm" if has_geo == "osm" else "photo" if has_photo else "none"
            rows.append(FrontageSideGap(evidence=ev, rendered=rendered))  # type: ignore[arg-type]
        f_gap[s] = rows

    # ---- footpath
    fp = [_footpath_bin(model, a, b) for a, b in bins]

    out_bins: list[GapBin] = []
    for i, (a, b) in enumerate(bins):
        fb = fidelity.bins[i]
        level = fb.level
        street = fb.views > 0
        hint = ""
        for s in ("opposite", "modelled"):
            if f_gap[s][i].evidence == "none":
                side = phys[s]
                hint = (f"No footprint or photo covers the {side}-hand frontage at {_r(a):g}-{_r(b):g} m: add a Street View "
                        f"frame facing {side}, or upload a photo taken from the {side} footpath.")
                break
        if not hint and g_evidence[i] == "none":
            hint = f"No satellite tile covers {_r(a):g}-{_r(b):g} m: add a close-up satellite crop of this stretch."
        if not hint:
            hint = fb.hint
        out_bins.append(GapBin(
            index=i, s_from=a, s_to=b, fidelity_level=level,
            ground=GroundGap(evidence=g_evidence[i], rendered=g_rendered[i]),  # type: ignore[arg-type]
            frontage=FrontageGap(modelled=f_gap["modelled"][i], opposite=f_gap["opposite"][i]),
            footpath=FootpathGap(evidence=fp[i][0], rendered=fp[i][1]),  # type: ignore[arg-type]
            furniture=FurnitureGap(evidence="street_level" if street else "none"), hint=hint))

    uncovered = GapCounts(
        ground=sum(1 for x in g_evidence if x == "none"),
        frontage_modelled=sum(1 for x in f_gap["modelled"] if x.evidence == "none"),
        frontage_opposite=sum(1 for x in f_gap["opposite"] if x.evidence == "none"),
        footpath=sum(1 for x in fp if x[0] == "prior"),
        furniture=sum(1 for bn in out_bins if bn.furniture.evidence == "none"))
    # PROVISIONAL (label-based, before any plan exists): ``compile_scene`` replaces it with ``blank_from_plan`` - the
    # count taken from the compiled plan's own stations and frontage segments.
    blank = BlankCounts(
        ground=sum(1 for x in g_rendered if not x),
        frontage_modelled=sum(1 for i in range(n) if not f_gap["modelled"][i].rendered),
        frontage_opposite=sum(1 for i in range(n) if not f_gap["opposite"][i].rendered),
        footpath=sum(1 for x in fp if not x[1]))

    interps = _interpolations(model, bins, g_rendered, side_ev, side_dec, phys, fp, out_bins, length)
    return GapReport(model_version=model.version, bin_m=BIN_M, length_s=length, bins=out_bins, uncovered=uncovered,
                     blank_after_compile=blank, interpolations=interps)


# ------------------------------------------------------------------ blank bins, from the compiled plan

_NO_SOURCE = (None, "", "none", "blank")
_TILE_EPS_M = 1e-3


def blank_from_plan(plan) -> BlankCounts:
    """Blank 5 m bins counted on the compiled ``StreetPlan`` itself (not on the analysis' own label lists).

    * ground: a bin is blank when no plan station lies in it or any station in it has no ground source;
    * frontage (per side): a bin is blank when that side's segments do not tile it (covered length < bin - 1 mm);
    * footpath: a bin is blank when no plan station lies in it (stations carry the footpath cross-section).
    """
    length = plan.length_m
    bins = bins_of(length)
    last = len(bins) - 1

    def stations_in(i: int, a: float, b: float):
        return [st for st in plan.stations if a - 1e-9 <= st.s < b - 1e-9 or (i == last and st.s <= b + 1e-9 and st.s >= a)]

    ground = footpath = 0
    for i, (a, b) in enumerate(bins):
        sts = stations_in(i, a, b)
        if not sts:
            ground += 1
            footpath += 1
        elif any(st.ground_src in _NO_SOURCE for st in sts):
            ground += 1

    def side_blank(side: str) -> int:
        segs = next(sd.segments for sd in plan.frontage.sides if sd.side == side)
        return sum(1 for a, b in bins
                   if sum(max(0.0, min(g.s_to, b) - max(g.s_from, a)) for g in segs) < (b - a) - _TILE_EPS_M)

    return BlankCounts(ground=ground, frontage_modelled=side_blank("modelled"),
                       frontage_opposite=side_blank("opposite"), footpath=footpath)


# ------------------------------------------------------------------ interpolation entries


def _interpolations(model, bins, g_rendered, side_ev, side_dec, phys, fp, out_bins, length) -> list[Interpolation]:
    out: list[Interpolation] = []
    # ground texture
    flags = [x if x in ("interpolated", "procedural") else None for x in g_rendered]
    for i0, i1, kind in _runs(flags, bins):
        a, b = bins[i0][0], bins[i1][1]
        if kind == "interpolated":
            frm = [bins[i0 - 1][1]] if i0 > 0 else []
            frm += [bins[i1 + 1][0]] if i1 + 1 < len(bins) else []
            out.append(Interpolation(what="ground_texture", side="both", s_from=a, s_to=b, method="neighbours",
                                     from_s=[_r(x) for x in frm],
                                     note="No satellite imagery covers this stretch; the ground is feathered from the nearest covered bins."))
        else:
            out.append(Interpolation(what="ground_texture", side="both", s_from=a, s_to=b, method="procedural",
                                     note="No usable imagery here; the ground is a procedural pavement/asphalt pattern."))
    # frontage, per cell runs
    for s in fr.SIDES:
        ev, dec = side_ev[s], side_dec[s]
        run: list[int] = []

        def flush() -> None:
            if not run:
                return
            d0 = dec[run[0]]
            a, b = ev.bounds[run[0]][0], ev.bounds[run[-1]][1]
            if d0.kind == "neighbours":
                anchors = []
                for i in run:
                    d = dec[i]
                    if d.left is not None:
                        anchors.append(ev.bounds[d.left][1])
                    if d.right is not None:
                        anchors.append(ev.bounds[d.right][0])
                frm = sorted({_r(x) for x in anchors})
                one = all((dec[i].left is None) != (dec[i].right is None) for i in run)
                note = (f"No photo or footprint covers the {phys[s]} side here; the frontage is copied from the nearest "
                        f"covered stretch." if one else
                        f"No photo or footprint covers the {phys[s]} side here; frontage blended between "
                        f"{frm[0]:g} m and {frm[-1]:g} m.")
                out.append(Interpolation(what="frontage", side=phys[s], s_from=_r(a), s_to=_r(b), method="neighbours",
                                         from_s=[frm[0], frm[-1]] if len(frm) > 1 else frm, note=note))
            else:
                out.append(Interpolation(what="frontage", side=phys[s], s_from=_r(a), s_to=_r(b), method="prior",
                                         note=f"No footprint or photo covers the {phys[s]} side here; a neutral facade is used."))

        for i, d in enumerate(dec):
            if d.kind == "evidence":
                flush()
                run = []
                continue
            if run and dec[run[-1]].kind != d.kind:
                flush()
                run = []
            run.append(i)
        flush()
    # footpath state, kerb
    for i0, i1, kind in _runs([x[1] if x[1] in ("interpolated", "prior") else None for x in fp], bins):
        out.append(Interpolation(what="footpath_state", side=phys["modelled"], s_from=bins[i0][0], s_to=bins[i1][1],
                                 method="neighbours" if kind == "interpolated" else "prior",
                                 note="The footpath state is unknown here; it is drawn from the nearest known stretch."
                                 if kind == "interpolated" else "No footpath evidence here; a prior footpath is drawn."))
    for i0, i1, _ in _runs([True if x[2] else None for x in fp], bins):
        out.append(Interpolation(what="kerb", side=phys["modelled"], s_from=bins[i0][0], s_to=bins[i1][1],
                                 method="prior", note=DEFAULT_KERB_NOTE))
    for i0, i1, _ in _runs([True if b.furniture.evidence == "none" else None for b in out_bins], bins):
        out.append(Interpolation(what="furniture_unknown", side="both", s_from=bins[i0][0], s_to=bins[i1][1],
                                 method="prior",
                                 note="No street-level photo covers this stretch, so street furniture here is unknown (none invented)."))
    out.sort(key=lambda x: (x.s_from, x.what, x.side, x.s_to))
    return out
