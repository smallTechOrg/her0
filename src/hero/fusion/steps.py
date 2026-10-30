"""One function per fusion step (spec/agent.md -> ## Steps). Each takes ``(state, ctx)``, mutates the state and returns
a short detail string for the step record; exceptions propagate to ``hero.fusion.runner.run_step``, which decides
(per step) whether the run continues.

Interpretation notes (the spec leaves these open)
-------------------------------------------------
* The v0 ``prior`` model of a segment without versions is built and stored at the end of ``fetch_osm_context``
  (not in ``resolve_reference_line``) so the OSM buildings can be part of the immutable v0.
* Satellite observations that were not picked for any rectified window are still *consumed* by the overhead pass
  (added to the overhead batch's ``observation_ids``), otherwise a no-op re-run could never be free.
* ``BudgetExceeded`` inside a critique call only skips the critique (perception was already paid for); inside a
  perception call it ends the run ``failed_budget`` without publishing (extractions stay stored for the next run).
* The budget guard itself runs inside ``LLMClient.vision_json`` (check before / record after, as in Phase 1); the
  planner here only trims batches so that the typical failure never happens.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image

from hero.config import Settings
from hero.db import model_repo, repo
from hero.db.model_repo import ExtractionCreate, ModelVersionCreate
from hero.fusion import assets as assets_mod
from hero.fusion import publish as publish_mod
from hero.fusion.convert import convert_overhead, convert_street_batch
from hero.fusion.critique import drop_unknown_entity_ops, image_to_llm, run_critique
from hero.fusion.render import render_model_topdown
from hero.fusion.schemas import OverheadExtraction, StreetExtraction
from hero.fusion.state import CritiqueIteration, FusionState
from hero.fusion.views import (
    OverheadWindow,
    StreetBatch,
    _batch_id,
    _georef,
    build_overhead_windows,
    build_street_batches,
    overhead_prompt,
    street_prompt,
)
from hero.imagery.acquire import acquire
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.imagery.osm import OsmGeometry, fetch_osm
from hero.imagery.plan import default_plan, merge_plan, plan_images
from hero.imagery.roads import distance_to_polyline_m, fetch_road_polyline
from hero.llm.budget import BudgetGuard
from hero.llm.client import LLMClient, LLMImage
from hero.llm.errors import BudgetExceeded, SchemaValidationError, UpstreamError
from hero.llm.pricing import PriceTable, estimate_image_tokens, estimate_text_tokens
from hero.logging import get_logger
from hero.model.batch import ProjectedBatch
from hero.model.fidelity import compute_fidelity
from hero.model.merge import apply_critique, merge_batches
from hero.model.priors import OsmBuilding, OsmContext, build_prior_model
from hero.model.refline import ReferenceLine
from hero.model.roadclip import clip_road
from hero.model.report import build_run_report
from hero.model.types import (
    Observation,
    ObservationKind,
    ReportCost,
    ReportSkipped,
    ViewKind,
)
from hero.scene.geo import LocalFrame

log = get_logger("hero.fusion")

OBS_LIMIT = 5000
OVERHEAD_MAX_TOKENS = 5000
STREET_MAX_TOKENS = 6000
BUDGET_TRIM_FRACTION = 0.8
CRITIQUE_CROPS = 2
CRITIQUE_FRAMES = 2
CRITIQUE_IMAGE_PX = 1024
MAX_FOOTWAY_DISTANCE_M = 40.0
OSM_KEEP_T_M = 40.0
OSM_KEEP_S_MARGIN_M = 5.0


@dataclass
class FusionContext:
    settings: Settings
    run_id: str
    llm: LLMClient
    google: GoogleImagery
    guard: BudgetGuard
    prices: PriceTable
    osm_cache: ImageryCache
    data_dir: Path
    http: httpx.Client | None = None

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews" / self.run_id


StepFn = Callable[[FusionState, FusionContext], str]


def _now() -> datetime:
    return datetime.now(UTC)


def _is_satellite(o: Observation) -> bool:
    return o.kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop)


def _refresh_observations(state: FusionState) -> None:
    rows = model_repo.get_observations(state.segment.id, limit=OBS_LIMIT)
    state.observations = {o.id: o for o in rows}


# ------------------------------------------------------------------ 1. load_base_model


def load_base_model(state: FusionState, ctx: FusionContext) -> str:
    seg = state.segment
    state.legacy_imported = publish_mod.import_legacy_segment(seg, now=_now())
    state.head_version = model_repo.head_version(seg.id)
    _refresh_observations(state)
    if state.head_version is None:
        return f"no model yet (segment has {len(state.observations)} stored observation(s)); a prior will be built"
    rec = model_repo.get_version(seg.id, state.head_version)
    assert rec is not None
    state.head_record = rec
    if rec.model.legacy:
        state.fresh_lineage = True
        note = "legacy v0 (fresh lineage: nothing is merged from legacy objects)"
    else:
        state.base = rec.model
        note = f"{rec.kind} v{rec.version}, {rec.entity_count} object(s), {rec.observation_count} fused photo(s)"
    return (("imported Phase-1 scene as legacy v0; " if state.legacy_imported else "")
            + f"head {note}; {len(state.observations)} stored observation(s)")


# ------------------------------------------------------------------ 2. resolve_reference_line


def resolve_reference_line(state: FusionState, ctx: FusionContext) -> str:
    if state.base is not None:
        state.ref_model = state.base.reference_line
        state.ref = ReferenceLine.from_model(state.ref_model)
        state.ref_source = state.ref_model.source
        return f"inherited from v{state.base.version}: {state.ref_source}, {state.ref.length_s:.1f} m"

    seg = state.segment
    mid = seg.midpoint
    road = fetch_road_polyline(mid.lat, mid.lon, cache=ctx.osm_cache, overpass_url=ctx.settings.overpass_url,
                               http=ctx.http)
    ref: ReferenceLine | None = None
    detail = ""
    if road is not None:
        try:
            ref, info = clip_road(road.latlons, (mid.lat, mid.lon), seg.length_m, seg.heading_deg)
            state.ref_model = ref.to_model(source=road.source, osm_way_id=road.osm_way_id, name=road.name,
                                           extrapolated_m=(info.extrapolated_start_m, info.extrapolated_end_m))
            state.ref_source = road.source
            detail = (f"{road.source} way {road.osm_way_id} ({road.name or 'unnamed'}) clipped to {ref.length_s:.1f} m, "
                      f"snap {info.snap_m:.1f} m")
        except ValueError as exc:
            state.warn(f"Could not clip the OSM road to the segment ({exc}); using a straight chord.")
            ref = None
    else:
        state.warn("No OSM road geometry was available (Nominatim and Overpass); using a straight chord.")
    if ref is None:
        ref = ReferenceLine.from_latlons([(seg.start.lat, seg.start.lon), (seg.end.lat, seg.end.lon)])
        state.ref_model = ref.to_model(source="straight_chord", osm_way_id=None, name=None)
        state.ref_source = "straight_chord"
        state.warn("Placement is unverified: the reference line is a straight chord, not the real road "
                   "(placement_verified=false).")
        detail = f"straight chord {ref.length_s:.1f} m (placement unverified)"
    state.ref = ref
    return detail


# ------------------------------------------------------------------ 3. fetch_osm_context


def osm_context_from(osm: OsmGeometry, ref: ReferenceLine, road_name: str | None = None) -> OsmContext:
    """Adapt Overpass ways to the prior's ``OsmContext``: road tags, footway polylines and buildings in ``(s, t)``."""
    mid_lat, mid_lon = ref.point_at(ref.length_s / 2.0, 0.0)
    roads = [w for w in osm.ways if w.kind == "highway"]
    tags: dict[str, str] = {}
    if roads:
        def score(w) -> float:
            d = distance_to_polyline_m(mid_lat, mid_lon, w.latlons)
            return d - (15.0 if road_name and w.tags.get("name") == road_name else 0.0)

        tags = dict(min(roads, key=score).tags)
    footways = [list(w.latlons) for w in osm.ways if w.kind == "footway"
                and distance_to_polyline_m(mid_lat, mid_lon, w.latlons) <= MAX_FOOTWAY_DISTANCE_M + ref.length_s / 2.0]
    buildings: list[OsmBuilding] = []
    for w in osm.buildings:
        pts = [ref.to_st(lat, lon) for lat, lon in w.latlons]
        poly = [(round(p.s, 2), round(p.t, 2)) for p in pts]
        if len(poly) > 1 and w.latlons[0] == w.latlons[-1]:
            poly = poly[:-1]
        if len(poly) < 3:
            continue
        near = any(-OSM_KEEP_S_MARGIN_M <= s <= ref.length_s + OSM_KEEP_S_MARGIN_M and abs(t) <= OSM_KEEP_T_M
                   for s, t in poly)
        if not near:
            continue
        levels = w.tags.get("building:levels")
        try:
            storeys = int(float(levels)) if levels else None
        except ValueError:
            storeys = None
        buildings.append(OsmBuilding(osm_way_id=w.id, footprint=poly, height_m=w.height_m(), storeys=storeys))
    buildings.sort(key=lambda b: b.osm_way_id)
    return OsmContext(highway_tags=tags, footway_polylines=footways, buildings=buildings)


def fetch_osm_context(state: FusionState, ctx: FusionContext) -> str:
    seg = state.segment
    assert state.ref is not None and state.ref_model is not None
    detail = ""
    try:
        frame = LocalFrame.from_endpoints(seg.start.lat, seg.start.lon, seg.end.lat, seg.end.lon, seg.footpath_side)
        state.osm = fetch_osm(frame, ctx.osm_cache, ctx.settings.overpass_url, http=ctx.http)
        state.osm_context = osm_context_from(state.osm, state.ref, state.ref_model.name)
        detail = (f"OSM {'cached' if state.osm.cache_hit else 'fetched'}: {len(state.osm_context.buildings)} "
                  f"building(s), {len(state.osm_context.footway_polylines)} footway(s)")
    except Exception as exc:  # noqa: BLE001 - OSM is context, never a reason to fail the run
        state.osm, state.osm_context = None, None
        state.osm_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        state.warn("Overpass (OSM) unavailable; building footprints and road tags are missing from the prior.")
        detail = f"OSM unavailable ({state.osm_error[:100]})"
    detail += _ensure_base(state)
    return detail


def _ensure_base(state: FusionState) -> str:
    """Build (and, for a fresh segment, store) the v0 prior when there is no base to merge onto."""
    if state.base is not None:
        return ""
    seg = state.segment
    assert state.ref is not None and state.ref_model is not None
    prior = build_prior_model(segment_slug=seg.slug, footpath_side=seg.footpath_side.value, ref=state.ref,
                              ref_model=state.ref_model, osm=state.osm_context, now=_now())
    if state.fresh_lineage:
        head = state.head_version or 0
        state.base = prior.model_copy(update={"version": head})
        return f"; fresh-lineage prior (replaces legacy v{head} as the merge base)"
    model_repo.save_version_only(seg.id, ModelVersionCreate(
        version=0, kind="prior", parent_version=None, run_id=None, model=prior, scene=None,
        fidelity=compute_fidelity(prior, {}), diff=None, summary=publish_mod.PRIOR_SUMMARY))
    state.head_version = 0
    state.base = prior
    return "; stored v0 prior model"


# ------------------------------------------------------------------ 4. plan_imagery


def plan_imagery(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None
    plan = merge_plan(default_plan(ctx.settings), state.plan_overrides)
    items, info = plan_images(state.ref, plan)
    state.plan, state.planned, state.plan_info = plan, items, info
    for note in info.notes:
        state.warn(note)
    c = info.counts
    return (f"{len(items)} planned ({c.get('streetview', 0)} Street View, {c.get('satellite_crop', 0)} close-up, "
            f"{c.get('satellite_overview', 0)} overview), est ${info.est_cost_usd:.3f}"
            + (f", spacing widened to {info.effective_spacing_m:g} m" if info.trimmed else ""))


# ------------------------------------------------------------------ 5. acquire_imagery


def acquire_imagery(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None and state.plan is not None
    res = acquire(segment_id=state.segment.id, run_id=ctx.run_id, ref=state.ref, plan=state.plan, google=ctx.google,
                  data_dir=ctx.data_dir)
    state.acquired = res
    for w in res.warnings:
        state.warn(w)
    _refresh_observations(state)
    new = [o for o in res.observations if o.acquired_run_id == ctx.run_id and o.cost_usd > 0]
    new_ids = {o.id for o in new}
    state.new_observations = new
    state.cached_observations = [o for o in res.observations if o.id not in new_ids]
    return (f"{len(new)} new, {len(state.cached_observations)} cached, {len(res.skipped)} skipped, "
            f"{res.deduped} deduped; ${res.cost_usd:.3f} Google")


# ------------------------------------------------------------------ 6. select_unfused


def _eligible(o: Observation) -> bool:
    return _is_satellite(o) or (o.is_street_level and o.station_s is not None)


def select_unfused(state: FusionState, ctx: FusionContext) -> str:
    assert state.base is not None
    fused = set(state.base.fused_observation_ids)
    ordered = sorted(state.observations.values(), key=lambda o: (o.created_at, o.id))
    pending = [o for o in ordered if o.id not in fused]
    state.unfused = [o for o in pending if _eligible(o)]
    state.ineligible = [o for o in pending if not _eligible(o)]
    if state.ineligible:
        state.warn(f"{len(state.ineligible)} photo(s) have no position on the street and cannot be fused yet.")

    unfused_ids = {o.id for o in state.unfused}
    street_ids = {o.id for o in state.unfused if o.is_street_level}
    sat_ids = {o.id for o in state.unfused if _is_satellite(o)}
    used: set[str] = set()
    state.replay_street, state.replay_overhead = [], None
    for rec in reversed(model_repo.list_extractions(state.segment.id)):  # newest first: the latest paid answer wins
        ids = list(rec.observation_ids)
        if not ids:
            continue
        if rec.kind == "streetlevel" and set(ids) <= street_ids and not (set(ids) & used):
            state.replay_street.append(rec)
            used |= set(ids)
        elif rec.kind == "overhead" and state.replay_overhead is None and set(ids) <= sat_ids:
            state.replay_overhead = rec
    state.replay_street.sort(key=lambda r: (min((state.observations[i].station_s or 0.0) for i in r.observation_ids),
                                            r.id))
    if not unfused_ids:
        state.noop = True
        log.info("fusion.noop", run_id=ctx.run_id, reason="no unfused observations")
        return "nothing new to fuse: no-op (no model call, no new version)"
    return (f"{len(state.unfused)} unfused ({len(street_ids)} street-level, {len(sat_ids)} satellite); "
            f"{len(state.replay_street)} stored street extraction(s)"
            + (" and a stored overhead extraction" if state.replay_overhead else "") + " can be replayed")


# ------------------------------------------------------------------ 7. prepare_views


def estimate_call_usd(ctx: FusionContext, model: str, images: list[LLMImage], prompt: str, schema: type,
                      max_tokens: int) -> float:
    spec = schema.model_json_schema()
    spec.pop("title", None)
    text = prompt + json.dumps(spec, separators=(",", ":"))
    tokens_in = sum(estimate_image_tokens(i.width_px, i.height_px) for i in images) + estimate_text_tokens(text)
    return ctx.prices.cost_usd(model, tokens_in + 200, max_tokens)


def _replay_batch(state: FusionState, record) -> StreetBatch:
    obs = [state.observations[i] for i in record.observation_ids]
    return StreetBatch(batch_id=_batch_id("sv", [o.id for o in obs]), observations=obs, images=[], captions=[])


def prepare_views(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None and state.base is not None
    ref, side = state.ref, state.base.footpath_side
    replayed = {i for r in state.replay_street for i in r.observation_ids}
    street_obs = [o for o in state.unfused if o.is_street_level and o.id not in replayed]
    sat_obs = [o for o in state.unfused if _is_satellite(o)]

    windows: list[OverheadWindow] = []
    if sat_obs:
        windows = build_overhead_windows(sat_obs, ref, data_dir=ctx.data_dir)
        if state.replay_overhead is not None and [w.observation.id for w in windows] != list(
                state.replay_overhead.observation_ids):
            state.replay_overhead = None  # the windows would differ from the ones that were paid for
        if not windows:
            state.warn("No satellite image covers the street well enough for the overhead pass; "
                       "the satellite observations are marked as reviewed without evidence.")
    batches = build_street_batches(street_obs, ref, data_dir=ctx.data_dir,
                                   max_images=ctx.settings.perception_batch_images) if street_obs else []
    replay_batches = [_replay_batch(state, r) for r in state.replay_street]

    # ---- cost plan: priority = overhead, then street batches nearest the centre first
    jobs: list[tuple[str, object, float]] = []
    if windows and state.replay_overhead is None:
        prompt = overhead_prompt(windows, ref, footpath_side=side)
        jobs.append(("overhead", None, estimate_call_usd(ctx, ctx.settings.vision_model, [w.image for w in windows],
                                                         prompt, OverheadExtraction, OVERHEAD_MAX_TOKENS)))
    centre = ref.length_s / 2.0

    def distance(b: StreetBatch) -> float:
        stations = [o.station_s or 0.0 for o in b.observations]
        return abs(sum(stations) / len(stations) - centre)

    for b in sorted(batches, key=lambda b: (distance(b), b.batch_id)):
        prompt = street_prompt(b, ref, footpath_side=side)
        jobs.append(("street", b, estimate_call_usd(ctx, ctx.settings.vision_model, b.images, prompt,
                                                    StreetExtraction, STREET_MAX_TOKENS)))
    state.est_calls_usd = round(sum(j[2] for j in jobs), 4)
    keep: list[tuple[str, object, float]] = []
    if jobs:
        remaining = min(ctx.guard.budget_run_usd - repo.run_spend_usd(ctx.run_id), ctx.guard.remaining_usd())
        allowed = BUDGET_TRIM_FRACTION * max(0.0, remaining)
        total = 0.0
        for job in jobs:
            if total + job[2] <= allowed:
                keep.append(job)
                total += job[2]
        if not keep:
            ctx.guard.check_before_call(ctx.run_id, jobs[0][2])  # raises BudgetExceeded when even one call is too dear
            keep.append(jobs[0])  # fits the cap but not 80 % of it: keep the highest-priority call alone
        dropped = len(jobs) - len(keep)
        if dropped:
            state.budget_trimmed = dropped
            state.degraded = True
            state.warn(f"budget_trimmed: {dropped} perception batch(es) dropped to stay within "
                       f"{int(BUDGET_TRIM_FRACTION * 100)} % of the remaining run budget (${allowed:.2f} of "
                       f"${remaining:.2f}); their photos stay unfused for the next run.")
    kept_batches = {id(j[1]) for j in keep if j[0] == "street"}
    state.street_batches = [*replay_batches, *(b for b in batches if id(b) in kept_batches)]
    state.windows = windows if (windows and (state.replay_overhead is not None
                                             or any(j[0] == "overhead" for j in keep))) else []
    return (f"{len(state.windows)} overhead window(s), {len(state.street_batches)} street batch(es) "
            f"({len(replayed)} replayed), est ${state.est_calls_usd:.3f}"
            + (f", trimmed {state.budget_trimmed}" if state.budget_trimmed else ""))


# ------------------------------------------------------------------ 8-9. perception


def _call(state: FusionState, ctx: FusionContext, *, step: str, images: list[LLMImage], prompt: str, schema: type,
          model: str, prompt_name: str, max_tokens: int):
    state.calls_attempted += 1
    obj, usage = ctx.llm.vision_json(run_id=ctx.run_id, step=step, images=images, prompt=prompt, schema=schema,
                                     model=model, prompt_name=prompt_name, max_tokens=max_tokens)
    state.calls_succeeded += 1
    state.cost.add(usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.calls)
    return obj, usage


def _save_extraction(state: FusionState, ctx: FusionContext, *, batch_index: int, kind: str, ids: list[str],
                     model: str, prompt_name: str, obj) -> None:
    model_repo.save_extraction(ExtractionCreate(
        segment_id=state.segment.id, run_id=ctx.run_id, batch_index=batch_index, kind=kind, observation_ids=ids,
        model=model, prompt_name=prompt_name, extraction=obj.model_dump(mode="json")))
    state.extractions_saved += 1


def perceive_overhead(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None and state.base is not None
    windows = state.windows
    if state.replay_overhead is not None:
        state.overhead_result = OverheadExtraction.model_validate(state.replay_overhead.extraction)
        return f"replayed the stored overhead extraction ({len(windows)} window(s))"
    prompt = overhead_prompt(windows, state.ref, footpath_side=state.base.footpath_side)
    try:
        obj, usage = _call(state, ctx, step="perceive_overhead", images=[w.image for w in windows], prompt=prompt,
                           schema=OverheadExtraction, model=ctx.settings.vision_model,
                           prompt_name="perceive_overhead", max_tokens=OVERHEAD_MAX_TOKENS)
    except (SchemaValidationError, UpstreamError) as exc:
        state.overhead_result = None
        state.degraded = True
        state.warn(f"Overhead perception was skipped ({getattr(exc, 'message', str(exc))[:160]}); "
                   "the cross-section stays at the map prior.")
        return "skipped after an invalid / failed answer"
    state.overhead_result = obj
    _save_extraction(state, ctx, batch_index=0, kind="overhead", ids=[w.observation.id for w in windows],
                     model=ctx.settings.vision_model, prompt_name="perceive_overhead", obj=obj)
    return (f"{len(windows)} window(s): {len(obj.windows)} answered; {usage.input_tokens} in / "
            f"{usage.output_tokens} out tokens, ${usage.cost_usd:.4f}")


def perceive_streetlevel(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None and state.base is not None
    replay = {_batch_id("sv", list(r.observation_ids)): r for r in state.replay_street}
    done = replayed = skipped = 0
    for index, batch in enumerate(state.street_batches):
        record = replay.get(batch.batch_id)
        if record is not None:
            state.street_results.append((batch, StreetExtraction.model_validate(record.extraction)))
            replayed += 1
            continue
        prompt = street_prompt(batch, state.ref, footpath_side=state.base.footpath_side)
        try:
            obj, _usage = _call(state, ctx, step=f"perceive_streetlevel_{index + 1}", images=batch.images,
                                prompt=prompt, schema=StreetExtraction, model=ctx.settings.vision_model,
                                prompt_name="perceive_streetlevel", max_tokens=STREET_MAX_TOKENS)
        except (SchemaValidationError, UpstreamError) as exc:
            skipped += 1
            state.skipped_batches.append(batch.batch_id)
            state.degraded = True
            state.warn(f"Street-level batch {index + 1} was skipped ({getattr(exc, 'message', str(exc))[:140]}); "
                       f"its {len(batch.observations)} photo(s) stay unfused and are retried next run.")
            continue
        state.street_results.append((batch, obj))
        _save_extraction(state, ctx, batch_index=index, kind="streetlevel", ids=[o.id for o in batch.observations],
                         model=ctx.settings.vision_model, prompt_name="perceive_streetlevel", obj=obj)
        done += 1
    return f"{done} call(s), {replayed} replayed, {skipped} skipped"


# ------------------------------------------------------------------ 10. project_detections


def project_detections(state: FusionState, ctx: FusionContext) -> str:
    assert state.ref is not None
    ref = state.ref
    batches: list[ProjectedBatch] = []
    dropped = disagreements = 0
    sat_ids = tuple(sorted(o.id for o in state.unfused if _is_satellite(o)))
    if state.overhead_result is not None and state.windows:
        batch, drops = convert_overhead(state.windows, state.overhead_result, ref)
        merged_ids = tuple(sorted(set(batch.observation_ids) | set(sat_ids)))
        batches.append(dataclasses.replace(batch, observation_ids=merged_ids))
        dropped += len(drops)
        disagreements += sum("range_disagreement" in d.flags for d in batch.detections)
    elif sat_ids and not state.windows and state.budget_trimmed == 0:
        # satellite images nothing could be rectified from: consume them so a re-run is a free no-op
        batches.append(ProjectedBatch(batch_id="ov-consumed-" + _batch_id("x", list(sat_ids))[2:],
                                      observation_ids=sat_ids, detections=(), strips=(), states=(), facades=(),
                                      visible=()))
    for batch, extraction in state.street_results:
        projected, drops = convert_street_batch(batch, extraction, ref)
        batches.append(projected)
        dropped += len(drops)
        disagreements += sum("range_disagreement" in d.flags for d in projected.detections)
    state.projected, state.dropped_detections, state.range_disagreements = batches, dropped, disagreements
    if not batches:
        if state.calls_attempted and not state.calls_succeeded:
            raise UpstreamError("Every perception call failed or was invalid, so there is nothing to merge. "
                                "See the run warnings; nothing was published.", kind="anthropic")
        raise UpstreamError("There is nothing to merge after the perception step.", kind="anthropic")
    n_det = sum(len(b.detections) for b in batches)
    if disagreements:
        state.warn(f"{disagreements} detection(s) had a ground-plane range that disagrees with the model's estimate.")
    return f"{len(batches)} batch(es), {n_det} detection(s) placed, {dropped} dropped"


# ------------------------------------------------------------------ 11. merge


def merge(state: FusionState, ctx: FusionContext) -> str:
    assert state.base is not None
    new_version = (model_repo.head_version(state.segment.id) or 0) + 1
    result = merge_batches(state.base, state.projected, new_version=new_version, run_id=ctx.run_id, now=_now())
    state.candidate, state.merge_log = result.model, result.changes
    kinds: dict[str, int] = {}
    for c in result.changes:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    log.info("model.merge", run_id=ctx.run_id, segment_slug=state.segment.slug, base_version=state.base.version,
             batches=len(state.projected), entities_added=kinds.get("entity_added", 0),
             entities_updated=kinds.get("entity_updated", 0) + kinds.get("entity_state", 0),
             contradictions_opened=kinds.get("contradiction_opened", 0))
    return (f"v{state.base.version} -> v{new_version}: {kinds.get('entity_added', 0)} added, "
            f"{kinds.get('entity_updated', 0)} updated, {kinds.get('contradiction_opened', 0)} contradiction(s) opened")


# ------------------------------------------------------------------ 12. consistency_check


def consistency_check(state: FusionState, ctx: FusionContext) -> str:
    assert state.candidate is not None
    model, warnings = publish_mod.consistency_check(state.candidate)
    state.candidate = model
    for w in warnings:
        state.warn(w)
    return f"{len(warnings)} warning(s)"


# ------------------------------------------------------------------ 13. critique


def _tile_image(state: FusionState, ctx: FusionContext):
    tile_obs = publish_mod.best_tile(state.observations)
    if tile_obs is None:
        return None, None
    try:
        with Image.open(ctx.data_dir / tile_obs.file_path) as img:
            return img.convert("RGB"), _georef(tile_obs)
    except (OSError, ValueError):
        return None, None


def _downscaled(path: Path, caption: str) -> LLMImage | None:
    try:
        with Image.open(path) as img:
            copy = img.convert("RGB")
    except (OSError, ValueError):
        return None
    copy.thumbnail((CRITIQUE_IMAGE_PX, CRITIQUE_IMAGE_PX), Image.LANCZOS)
    return image_to_llm(copy, caption, media_type="image/jpeg")


def _critique_inputs(state: FusionState, ctx: FusionContext) -> tuple[list[LLMImage], list[LLMImage], list[str]]:
    """Up to 2 close-up crops nearest the centre and 2 forward Street View frames at 1/3 and 2/3 of the street."""
    assert state.ref is not None
    length = state.ref.length_s
    crops = sorted((o for o in state.observations.values() if o.kind is ObservationKind.satellite_crop),
                   key=lambda o: (abs((o.station_s or 0.0) - length / 2.0), o.id))[:CRITIQUE_CROPS]
    crop_imgs: list[LLMImage] = []
    ids: list[str] = []
    for o in crops:
        img = _downscaled(ctx.data_dir / o.file_path, f"Satellite close-up at s={o.station_s or 0.0:.0f} m, "
                                                     f"zoom {o.zoom}, north up (not straightened)")
        if img is not None:
            crop_imgs.append(img)
            ids.append(o.id)
    pool = sorted((o for o in state.observations.values() if o.kind is ObservationKind.streetview
                   and o.view is ViewKind.forward and o.station_s is not None), key=lambda o: (o.station_s, o.id))
    frames: list[LLMImage] = []
    for target in (length / 3.0, 2.0 * length / 3.0):
        if not pool or len(frames) >= CRITIQUE_FRAMES:
            break
        pick = min(pool, key=lambda o: (abs(o.station_s - target), o.id))
        pool = [o for o in pool if o.id != pick.id]
        img = _downscaled(ctx.data_dir / pick.file_path,
                          f"Street View frame, view forward, station s={pick.station_s:.0f} m")
        if img is not None:
            frames.append(img)
            ids.append(pick.id)
    return crop_imgs, frames, ids


def critique_once(n: int) -> StepFn:
    def run(state: FusionState, ctx: FusionContext) -> str:
        assert state.candidate is not None and state.ref is not None
        model = state.candidate
        tile, georef = _tile_image(state, ctx)
        render = render_model_topdown(model, tile=tile, georef=georef, ref=state.ref)
        ctx.previews_dir.mkdir(parents=True, exist_ok=True)
        render.save(ctx.previews_dir / f"critique{n}.png", format="PNG", optimize=True)
        crop_imgs, frames, used_ids = _critique_inputs(state, ctx)
        state.calls_attempted += 1
        try:
            result, usage = run_critique(ctx.llm, run_id=ctx.run_id, model=model, render=render, crops=crop_imgs,
                                         street_frames=frames, model_slug=ctx.settings.review_model,
                                         step=f"critique_{n}")
        except BudgetExceeded as exc:
            state.warn(f"Critique {n} was skipped: {exc.message[:160]}")
            return "skipped (budget)"
        except (SchemaValidationError, UpstreamError) as exc:
            state.warn(f"Critique {n} failed ({getattr(exc, 'message', str(exc))[:140]}); the candidate is kept.")
            return "skipped (the critique call failed)"
        state.calls_succeeded += 1
        state.cost.add(usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.calls)
        _save_extraction(state, ctx, batch_index=n - 1, kind="critique", ids=used_ids,
                         model=ctx.settings.review_model, prompt_name="critique_model", obj=result)
        cleaned, pre_skipped = drop_unknown_entity_ops(model, result)
        new_model, changes, skipped = apply_critique(model, cleaned.ops, run_id=ctx.run_id, now=_now())
        state.candidate = new_model
        all_skipped = [*pre_skipped, *skipped]
        state.critique_iterations.append(CritiqueIteration(
            n=n, accepted=result.accepted, critique=result.critique,
            ops=[op.model_dump(mode="json") for op in result.ops], applied=len(changes), skipped=all_skipped,
            preview_path=f"previews/{ctx.run_id}/critique{n}.png",
            preview_url=f"/media/previews/{ctx.run_id}/critique{n}.png",
            detail=f"{len(result.ops)} op(s), {len(changes)} change(s), {len(all_skipped)} skipped"))
        return (f"accepted={result.accepted}; {len(result.ops)} op(s), {len(changes)} applied, "
                f"{len(all_skipped)} skipped; ${usage.cost_usd:.4f}")

    return run


# ------------------------------------------------------------------ 14. publish


def publish(state: FusionState, ctx: FusionContext) -> str:
    assert state.candidate is not None and state.base is not None and state.ref is not None
    seg = state.segment
    candidate = state.candidate
    assets = assets_mod.cut_model_crops(candidate, state.observations, data_dir=ctx.data_dir, version=candidate.version)
    bundle = publish_mod.assemble_version(
        candidate=candidate, base=state.base, observations=state.observations, assets=assets,
        segment_name=seg.name, city=seg.city,
        models={"vision": ctx.settings.vision_model, "review": ctx.settings.review_model},
        review_iterations=len(state.critique_iterations), warnings=list(state.warnings))
    state.fidelity_before, state.fidelity_after, state.diff = bundle.fidelity_before, bundle.fidelity_after, bundle.diff
    if not bundle.changed:
        state.version, state.version_created = state.base.version, False
        state.warn("The merged model did not change, so no new version was created.")
        return f"no change vs v{state.base.version}: no new version"
    tile, georef = _tile_image(state, ctx)
    thumb_rel = f"previews/{ctx.run_id}/thumb.png"
    assets_mod.save_thumbnail(render_model_topdown(bundle.model, tile=tile, georef=georef, ref=state.ref),
                              ctx.data_dir / thumb_rel)
    rec = publish_mod.publish_bundle(seg.id, bundle, thumbnail_path=thumb_rel)
    state.published, state.scene, state.version, state.version_created = bundle.model, bundle.scene, rec.version, True
    state.thumbnail_path = thumb_rel
    log.info("model.published", run_id=ctx.run_id, segment_slug=seg.slug, version=rec.version,
             entity_count=rec.entity_count, fidelity_overall=bundle.fidelity_after.overall.score,
             quality=bundle.quality)
    return (f"published v{rec.version} ({rec.entity_count} object(s), fidelity L{bundle.fidelity_after.overall.level}, "
            f"quality {bundle.quality})")


# ------------------------------------------------------------------ 15. report


def report(state: FusionState, ctx: FusionContext) -> str:
    rep = build_report(state, ctx)
    if rep is None:
        return "no model to report on"
    state.report = rep
    model_repo.save_run_report(ctx.run_id, state.segment.id, rep)
    log.info("fusion.report", run_id=ctx.run_id, images_new=rep.images.new, images_cached=rep.images.cached,
             cost_usd=rep.cost.total_usd)
    return f"{rep.summary[:180]}"


@dataclass
class _RunView:
    id: str
    started_at: datetime | None
    finished_at: datetime | None


def build_report(state: FusionState, ctx: FusionContext, *, failed: bool = False):
    """The deterministic ``RunReport`` for the current state (``None`` when there is no base model to describe)."""
    base = state.base
    if base is None:
        return None
    now = _now()
    totals = repo.run_llm_totals(ctx.run_id)
    res = state.acquired
    anthropic = round(totals.cost_usd, 6)
    google_est = round(res.cost_usd, 4) if res else 0.0
    costs = ReportCost(
        anthropic_usd=anthropic, anthropic_calls=len(totals.calls), input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens, google_new_requests=res.paid_requests if res else 0,
        google_cached=res.cached if res else 0, google_est_usd=google_est, total_usd=round(anthropic + google_est, 4),
        run_cap_usd=ctx.guard.budget_run_usd, budget_remaining_usd=round(ctx.guard.remaining_usd(), 4))
    skipped = [ReportSkipped(station_s=s.get("station_s"), view=s.get("view"), reason=str(s.get("reason", "")))
               for s in (res.skipped if res else [])]
    result = None if failed else (state.published or base)
    fid_before = state.fidelity_before or compute_fidelity(base, state.observations)
    return build_run_report(
        run=_RunView(ctx.run_id, state.started_at, now), mode=state.mode, base=base, result=result,
        diff=state.diff if state.version_created else None, fid_before=fid_before,
        fid_after=state.fidelity_after, new_observations=state.new_observations,
        cached_count=len(state.cached_observations), costs=costs, warnings=list(state.warnings), now=now,
        cached_observations=state.cached_observations, skipped=skipped,
        degraded=state.degraded or failed or state.error is not None)
