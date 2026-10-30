"""Extraction -> ``ProjectedBatch``: Claude perceives (pixels), code places (``hero.model.projection``).

Pure given the injected projection functions: no network, DB or clock. ``hero.model.projection`` (S1b) and
``hero.model.coverage`` (S1d) are imported lazily, so this module imports even before they land.

Conventions
-----------
* Detection key = ``"<observation_id>:<local_id>"`` (``#2``, ``#3`` ... appended for a repeated local_id inside one
  observation) so evidence is unique per ``(observation_id, detection_key)`` even when two batches reuse ``d1``.
* A detection that cannot be projected (beyond range, no estimate, bad ``image_index``, frame marked
  ``visibility_ok=false``) is DROPPED and reported in the second element returned by ``convert_*``.
* Footprint: a ``parallel`` object spans ``d`` along ``s`` and ``w`` across ``t``; ``perpendicular`` swaps them;
  ``unknown`` is treated as parallel. ``orientation_deg`` is 0 / 90 relative to ``s`` (None when unknown).
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from typing import Sequence

from hero.model.batch import (
    FacadeDetection,
    ProjectedBatch,
    ProjectedDetection,
    StateSpan,
    StripSample,
    VisibleRegion,
)
from hero.model.catalogue import default_size_m
from hero.model.refline import ReferenceLine
from hero.model.types import EntityType, FootpathState, Observation

from hero.fusion.schemas import OverheadExtraction, StreetExtraction
from hero.fusion.views import OverheadWindow, StreetBatch

# frame-level footpath-state evidence: ground span around the camera station per view, and its weight
FRAME_STATE_SPAN_M = {"forward": (0.0, 12.0), "back": (-12.0, 0.0), "left": (-6.0, 6.0), "right": (-6.0, 6.0),
                      "oblique": (-4.0, 8.0)}
FRAME_STATE_WEIGHT = {"forward": 0.5, "back": 0.5, "left": 0.7, "right": 0.7, "oblique": 0.4}
FACADE_FALLBACK_HALF_M = 5.0
FACADE_MIN_SPAN_M = 2.0
FACADE_FALLBACK_WEIGHT = 0.25
OVERHEAD_OBJECT_SIGMA_M = 0.5
OVERHEAD_WEIGHT = 0.8
OVERHEAD_MIN_WEIGHT = 0.05
HIRES_ZOOM = 20


@dataclass(frozen=True)
class Dropped:
    key: str      # "<observation_id or image_index>:<local_id>" or "facade:<n>"
    reason: str


def _clean_attrs(attrs: dict) -> dict[str, str | float | bool]:
    return {str(k): v for k, v in sorted(attrs.items()) if isinstance(v, (str, float, int, bool))}


def _unique(key: str, seen: set[str]) -> str:
    cand, n = key, 1
    while cand in seen:
        n += 1
        cand = f"{key}#{n}"
    seen.add(cand)
    return cand


def _clamp_s(ref: ReferenceLine, s: float) -> float:
    return min(max(s, 0.0), ref.length_s)


def _footprint(det_size, etype: EntityType, subtype: str | None, orientation: str) -> tuple[float, float, float | None, float | None]:
    """-> (extent_s, extent_t, height_m, orientation_deg)."""
    if det_size is not None:
        w, h, d = det_size.w, det_size.h, det_size.d
    else:
        w, h, d = default_size_m(etype, subtype, extent_length_s=1.0)
    if orientation == "perpendicular":
        return w, d, h, 90.0
    return d, w, h, (0.0 if orientation == "parallel" else None)


# ------------------------------------------------------------------ street level


def convert_street_batch(batch: StreetBatch, extraction: StreetExtraction, ref: ReferenceLine
                         ) -> tuple[ProjectedBatch, list[Dropped]]:
    from hero.model import projection as proj

    obs_by_index = {i: o for i, o in enumerate(batch.observations, 1)}
    frames = {f.image_index: f for f in extraction.frames}
    dropped: list[Dropped] = []
    seen: set[str] = set()

    def usable(index: int) -> Observation | None:
        o = obs_by_index.get(index)
        f = frames.get(index)
        return None if o is None or (f is not None and not f.visibility_ok) else o

    detections: list[ProjectedDetection] = []
    for det in extraction.detections:
        obs = usable(det.image_index)
        if obs is None:
            dropped.append(Dropped(f"{det.image_index}:{det.local_id}",
                                   "image_index out of range" if det.image_index not in obs_by_index
                                   else "frame marked visibility_ok=false"))
            continue
        try:
            pt = proj.project_street_detection(obs, ref, det.box_px, touches_ground=det.touches_ground,
                                               est_distance_m=det.est_distance_m)
        except ValueError as exc:  # e.g. an upload without heading/fov
            dropped.append(Dropped(f"{obs.id}:{det.local_id}", f"projection failed: {exc}"))
            continue
        if pt is None:
            dropped.append(Dropped(f"{obs.id}:{det.local_id}", "could not be projected (out of range or no estimate)"))
            continue
        ext_s, ext_t, height, orient = _footprint(det.size_m, det.type, det.subtype, det.orientation)
        flags = tuple(getattr(pt, "flags", ()) or ())
        if getattr(pt, "method", "") == "estimated_distance" and "estimated_range" not in flags:
            flags += ("estimated_range",)
        detections.append(ProjectedDetection(
            key=_unique(f"{obs.id}:{det.local_id}", seen), observation_id=obs.id, type=det.type, subtype=det.subtype,
            s=_clamp_s(ref, pt.s), t=pt.t, sigma_m=pt.sigma_m, extent_s=ext_s, extent_t=ext_t, height_m=height,
            orientation_deg=orient, attributes=_clean_attrs(det.attributes), description=det.description.strip(),
            raw_confidence=det.confidence, crop_px=det.box_px, weight=pt.weight, flags=flags))

    facades: list[FacadeDetection] = []
    for n, fac in enumerate(extraction.facades, 1):
        obs = usable(fac.image_index)
        if obs is None:
            dropped.append(Dropped(f"facade:{n}", "image_index out of range or frame not visible"))
            continue
        facades.append(_facade(proj, obs, ref, fac))

    states: list[StateSpan] = []
    for index, f in sorted(frames.items()):
        obs = usable(index)
        if obs is None or obs.station_s is None or not f.footpath_visible or f.footpath_state is FootpathState.unknown:
            continue
        view = obs.view.value if obs.view is not None else "oblique"
        lo, hi = FRAME_STATE_SPAN_M[view]
        s_from, s_to = _clamp_s(ref, obs.station_s + lo), _clamp_s(ref, obs.station_s + hi)
        if s_to - s_from < 1e-6:
            continue
        sides = {"left": ["left"], "right": ["right"], "both": ["left", "right"]}.get(f.footpath_side, [])
        for side in sides:
            states.append(StateSpan(observation_id=obs.id, side=side, s_from=s_from, s_to=s_to, state=f.footpath_state,
                                    weight=FRAME_STATE_WEIGHT[view]))

    visible = _street_visibility([o for i, o in obs_by_index.items() if usable(i) is not None], ref)
    out = ProjectedBatch(batch_id=batch.batch_id, observation_ids=tuple(o.id for o in batch.observations),
                         detections=tuple(detections), strips=(), states=tuple(states), facades=tuple(facades),
                         visible=tuple(visible))
    return out, dropped


def _street_visibility(observations: Sequence[Observation], ref: ReferenceLine) -> list[VisibleRegion]:
    from hero.model import coverage

    out: list[VisibleRegion] = []
    for o in observations:
        if o.station_s is not None:
            out.extend(coverage.visible_regions(o, ref))
    return out


def _facade(proj, obs: Observation, ref: ReferenceLine, fac) -> FacadeDetection:
    x, y, w, h = fac.box_px
    try:
        pts = [proj.project_street_detection(obs, ref, (cx, y + h - 1, 1, 1), touches_ground=True, est_distance_m=None)
               for cx in (x, x + w - 1)]
    except ValueError:
        pts = [None, None]
    if all(p is not None for p in pts):
        s_vals = sorted(p.s for p in pts)
        s_from, s_to = s_vals
        weight = sum(p.weight for p in pts) / 2.0
    else:
        centre = obs.station_s if obs.station_s is not None else 0.0
        s_from, s_to, weight = centre - FACADE_FALLBACK_HALF_M, centre + FACADE_FALLBACK_HALF_M, FACADE_FALLBACK_WEIGHT
    if s_to - s_from < FACADE_MIN_SPAN_M:
        mid = (s_from + s_to) / 2.0
        s_from, s_to = mid - FACADE_MIN_SPAN_M / 2.0, mid + FACADE_MIN_SPAN_M / 2.0
    s_from, s_to = _clamp_s(ref, s_from), _clamp_s(ref, s_to)
    return FacadeDetection(observation_id=obs.id, side=fac.side, s_from=s_from, s_to=max(s_to, s_from + 1e-3),
                           colour_hex=fac.colour_hex, storeys=fac.storeys,
                           description=(fac.description or (fac.use or "")).strip(), crop_px=fac.box_px,
                           weight=round(weight * max(fac.confidence, 0.1), 4))


def street_to_batch(batch: StreetBatch, extraction: StreetExtraction, ref: ReferenceLine) -> ProjectedBatch:
    """Spec signature; use ``convert_street_batch`` when the dropped list is needed."""
    return convert_street_batch(batch, extraction, ref)[0]


# ------------------------------------------------------------------ overhead


def convert_overhead(windows: Sequence[OverheadWindow], extraction: OverheadExtraction, ref: ReferenceLine
                     ) -> tuple[ProjectedBatch, list[Dropped]]:
    from hero.model import projection as proj

    by_index = {w.index: w for w in windows}
    dropped: list[Dropped] = []
    strips: list[StripSample] = []
    states: list[StateSpan] = []
    dets: list[ProjectedDetection] = []
    seen: set[str] = set()

    for win_out in extraction.windows:
        w = by_index.get(win_out.image_index)
        if w is None:
            dropped.append(Dropped(f"window:{win_out.image_index}", "image_index out of range"))
            continue
        oid, grid = w.observation.id, w.grid
        for strip in win_out.strips:
            weight = max(OVERHEAD_MIN_WEIGHT, round(OVERHEAD_WEIGHT * strip.confidence, 4))
            try:
                samples = proj.strip_samples_from_traces(
                    grid, strip.edge_a_px, strip.edge_b_px, kind=strip.kind, material=strip.material,
                    condition=strip.condition, side=strip.side, weight=weight)
            except ValueError as exc:
                dropped.append(Dropped(f"{oid}:strip:{strip.kind.value}:{strip.side}", str(exc)))
                continue
            strips.extend(dataclasses.replace(sm, observation_id=oid) for sm in samples)
        if win_out.footpath_runs:
            spans = proj.footpath_runs_to_states(grid, win_out.footpath_runs)
            states.extend(dataclasses.replace(sp, observation_id=oid) for sp in spans)
        for n, o in enumerate(win_out.objects, 1):
            x, y, bw, bh = o.box_px
            s, t = grid.px_to_st(x + bw / 2.0, y + bh / 2.0)
            if not (-1e-6 <= s <= ref.length_s + 1e-6):
                dropped.append(Dropped(f"{oid}:w{w.index}o{n}", "object outside the street's s range"))
                continue
            dets.append(ProjectedDetection(
                key=_unique(f"{oid}:w{w.index}o{n}", seen), observation_id=oid, type=o.type, subtype=None,
                s=_clamp_s(ref, s), t=t, sigma_m=OVERHEAD_OBJECT_SIGMA_M, extent_s=bh * grid.m_per_px,
                extent_t=bw * grid.m_per_px, height_m=None, orientation_deg=None,
                attributes=_clean_attrs(o.attributes), description=o.description.strip(), raw_confidence=o.confidence,
                crop_px=o.box_px, weight=OVERHEAD_WEIGHT, flags=("overhead",)))

    visible = []
    for w in windows:
        s_from, s_to = _clamp_s(ref, w.grid.s_bottom), _clamp_s(ref, w.grid.s_top)
        if s_to > s_from:
            hires = (w.observation.zoom or 0) >= HIRES_ZOOM
            visible.append(VisibleRegion(observation_id=w.observation.id, s_from=s_from, s_to=s_to, side="both",
                                         weight=1.0 if hires else 0.7))
    obs_ids = tuple(dict.fromkeys(w.observation.id for w in windows))
    batch_id = "ov-" + hashlib.sha1("|".join(f"{w.observation.id}@{w.grid.s_bottom:.1f}" for w in windows).encode()).hexdigest()[:10]
    return ProjectedBatch(batch_id=batch_id, observation_ids=obs_ids, detections=tuple(dets), strips=tuple(strips),
                          states=tuple(states), facades=(), visible=tuple(visible)), dropped


def overhead_to_batch(windows: Sequence[OverheadWindow], extraction: OverheadExtraction, ref: ReferenceLine) -> ProjectedBatch:
    """Spec signature; use ``convert_overhead`` when the dropped list is needed."""
    return convert_overhead(windows, extraction, ref)[0]
