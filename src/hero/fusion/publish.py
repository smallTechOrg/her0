"""Publishing a fused model: consistency check, version bundle (fidelity, diff, scene, summary) and the DB write.

Everything here except ``publish_bundle`` / ``import_legacy_segment`` is pure (no network, DB or clock - callers pass
``now``), so it is unit-tested without services (``tests/unit/test_fusion_publish.py``).

* ``consistency_check`` - spec/agent.md rules 4a-d (e/f are handled where the evidence is created): returns the
  possibly-amended model plus warning strings; it never raises and never deletes anything.
* ``assemble_version`` - fidelity before/after, diff, compiled scene (v2 or v3-lite, with the run's ``generation``
  facts), summary. ``bundle.changed`` is False when the merge changed nothing (no new version is created then).
* ``derive_scene`` - the derived-artefact chain shared with ``recompile``: bake textures -> compile -> export bundle
  (glb, manifest, xodr; files first) -> v3 scene with ``assets``. Pure of network/LLM; IO is the data dir only.
* ``publish_bundle`` - ``derive_scene`` then ``model_repo.publish_version`` (one transaction: version row + segment
  pointer), so the export files exist before any DB row points at them.
* ``prior_fallback_scene`` - the labelled low-fidelity scene a failed first generation still leaves behind.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate, ModelVersionRecord
from hero.model import ids
from hero.logging import get_logger
from hero.model.compile import compile_scene
from hero.model.describe import version_summary
from hero.model.diff import diff_models
from hero.model.fidelity import compute_fidelity
from hero.model.legacy import import_legacy
from hero.model.scene_v2 import SceneV2
from hero.model.scene_v3 import Assets, SceneV3
from hero.model.types import (
    CanonicalModel,
    Contradiction,
    ContradictionKind,
    EntityState,
    EntityType,
    EvidenceRole,
    FidelityMap,
    FootpathState,
    ModelDiff,
    Observation,
    ObservationKind,
    StripKind,
)
from hero.scene.schema import Scene

FOOTPATH_BLOCKERS = frozenset({EntityType.parked_vehicle, EntityType.bin, EntityType.vendor_stall})
CONSISTENT_STATES = frozenset({FootpathState.encroached, FootpathState.blocked, FootpathState.broken,
                               FootpathState.missing})
MAX_ABS_T_M = 30.0
# strips that physically exclude each other; forecourt / verge / planting are backdrops that legitimately overlap them
CORE_STRIPS = frozenset({StripKind.carriageway, StripKind.kerb, StripKind.footpath, StripKind.parking,
                         StripKind.cycle_lane, StripKind.median, StripKind.shoulder})
STRIP_OVERLAP_TOL_M = 0.5
POSITION_TOL_M = 0.5
DUPLICATE_GATE_M = 0.5
FOOTPATH_OVERLAP_M = 0.2
VOTE_DAMPING = 0.85
PRIOR_SUMMARY = "Prior model from OSM geometry (no imagery yet)."

log = get_logger("hero.fusion.publish")


# ------------------------------------------------------------------ consistency check


def consistency_check(model: CanonicalModel) -> tuple[CanonicalModel, list[str]]:
    """Rules 4a-d. Warnings only; rule d additionally opens a ``footpath_state_conflict`` and damps that vote."""
    warnings: list[str] = []
    length = model.length_s
    live = [e for e in model.entities if e.state is not EntityState.removed]

    out_of_frame = [e.id for e in live if not (-POSITION_TOL_M <= e.position.s <= length + POSITION_TOL_M)
                    or abs(e.position.t) > MAX_ABS_T_M]
    if out_of_frame:
        warnings.append(f"consistency: {len(out_of_frame)} object(s) lie outside the street frame: "
                        + ", ".join(sorted(out_of_frame)[:5]))

    twins: list[str] = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if (a.type is b.type and a.state is EntityState.present and b.state is EntityState.present
                    and abs(a.position.s - b.position.s) < DUPLICATE_GATE_M
                    and abs(a.position.t - b.position.t) < DUPLICATE_GATE_M):
                twins.append(f"{a.id}/{b.id}")
    if twins:
        warnings.append(f"consistency: {len(twins)} possible duplicate object(s): " + ", ".join(sorted(twins)[:5]))

    for iv in model.profile:
        spans = sorted((s for s in iv.strips if s.kind in CORE_STRIPS), key=lambda s: s.t_lo[0])
        if any(a.t_hi[0] > b.t_lo[0] + STRIP_OVERLAP_TOL_M or a.t_hi[1] > b.t_lo[1] + STRIP_OVERLAP_TOL_M
               for a, b in zip(spans, spans[1:])):
            warnings.append(f"consistency: strips overlap in {iv.s_from:g}-{iv.s_to:g} m")

    contradictions = list(model.contradictions)
    known = {c.id for c in contradictions}
    profile = list(model.profile)
    changed_profile = False
    for e in sorted(live, key=lambda x: x.id):
        if e.type not in FOOTPATH_BLOCKERS or e.state is not EntityState.present:
            continue
        idx = next((i for i, iv in enumerate(profile) if e.position.s < iv.s_to), len(profile) - 1)
        iv = profile[idx]
        strip = iv.footpath_strip(model.footpath_side)
        if strip is None:
            continue
        f = min(max((e.position.s - iv.s_from) / max(iv.length, 1e-9), 0.0), 1.0)
        lo, hi = strip.t_lo_at(f), strip.t_hi_at(f)
        half = max(e.extent.width_t, 0.4) / 2.0
        overlap = min(hi, e.position.t + half) - max(lo, e.position.t - half)
        if overlap < FOOTPATH_OVERLAP_M or iv.footpath_state in CONSISTENT_STATES:
            continue
        cid = ids.contradiction_id("footpath_state_conflict", e.id, iv.id)
        warnings.append(f"consistency: {e.type.value} {e.id} sits on the footpath at {e.position.s:.0f} m "
                        f"but the footpath is '{iv.footpath_state.value}'")
        if cid not in known:
            known.add(cid)
            contradictions.append(Contradiction(
                id=cid, kind=ContradictionKind.footpath_state_conflict, entity_ids=[e.id],
                interval=(iv.s_from, iv.s_to),
                description=(f"A {e.type.value} blocks the footpath at {e.position.s:.0f} m, which is recorded as "
                             f"'{iv.footpath_state.value}'."),
                observation_ids=sorted({p.observation_id for p in e.provenance if p.role is EvidenceRole.supports}),
                opened_in_version=model.version))
        votes = dict(iv.state_votes)
        key = iv.footpath_state.value
        if key in votes:
            votes[key] = round(votes[key] * VOTE_DAMPING, 6)
            profile[idx] = iv.model_copy(update={"state_votes": votes})
            changed_profile = True
    if len(contradictions) == len(model.contradictions) and not changed_profile:
        return model, warnings
    return model.model_copy(update={"contradictions": contradictions, "profile": profile}), warnings


# ------------------------------------------------------------------ derived artefacts (bake -> compile -> export)


def _default_bake(model: CanonicalModel, plan: Any, observations: Any, data_dir: Path) -> Any:
    from hero.imagery.bake import bake_textures

    return bake_textures(model, plan, observations, data_dir)


def _default_export(**kwargs: Any) -> Any:
    from hero.export.pipeline import build_and_store_exports

    return build_and_store_exports(**kwargs)


@dataclass
class Toolchain:
    """The three derived-artefact steps, injectable so tests (and slices still landing) can substitute them.

    ``bake`` / ``export`` may be ``None`` (the module is not available): textures are then procedural and the scene
    is not exported (a v3 scene without ``assets`` is not served; v2 scenes play on the legacy path).
    """

    compile: Callable[..., Any] = compile_scene
    bake: Callable[..., Any] | None = None
    export: Callable[..., Any] | None = None

    @classmethod
    def default(cls) -> "Toolchain":
        bake: Callable[..., Any] | None = _default_bake
        export: Callable[..., Any] | None = _default_export
        try:
            import hero.imagery.bake  # noqa: F401
        except ImportError:
            bake = None
        try:
            import hero.export.pipeline  # noqa: F401
        except ImportError:
            export = None
        return cls(compile=compile_scene, bake=bake, export=export)


@dataclass
class Derived:
    scene: SceneV2 | SceneV3
    scene_schema: int
    compiled_by: str | None
    gaps: dict[str, Any] | None
    export: dict[str, Any] | None
    interpolated_m: float = 0.0
    dynamic_excluded: int = 0
    blank_bins: int = 0
    warnings: list[str] = field(default_factory=list)


def union_m(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for lo, hi in sorted(intervals):
        if end is None or lo > end:
            total += hi - lo
            end = hi
        elif hi > end:
            total += hi - end
            end = hi
    return round(total, 3)


def call_compile(compile_fn: Callable[..., Any], model: CanonicalModel, *, observations: Mapping[str, Observation],
                 assets: Mapping[str, str], fidelity: FidelityMap, segment_name: str, city: str,
                 textures: Any = None) -> tuple[Any, Any, Any]:
    """Run the compiler and normalise its result to ``(scene, plan, gaps)``.

    Passes ``textures`` only when the compiler accepts it, and accepts both a ``CompileResult`` (v3) and a bare scene.
    """
    params = inspect.signature(compile_fn).parameters
    kwargs: dict[str, Any] = dict(observations=observations, assets=assets, tile=best_tile(observations),
                                  fidelity=fidelity, slug_name=segment_name, city=city)
    if "textures" in params:
        kwargs["textures"] = textures
    out = compile_fn(model, **kwargs)
    if hasattr(out, "scene") and hasattr(out, "plan"):
        return out.scene, out.plan, getattr(out, "gaps", None)
    return out, None, None


def _bake(tc: Toolchain, model: CanonicalModel, observations: Mapping[str, Observation], data_dir: Path,
          warnings: list[str]) -> Any:
    if tc.bake is None:
        warnings.append("textures are procedural: the texture bake is unavailable")
        return None
    started = time.perf_counter()
    try:
        refs = tc.bake(model, None, list(observations.values()), data_dir)  # plan None: stations from the model
    except Exception as exc:  # noqa: BLE001 - a failed bake degrades to procedural, never blocks the street
        warnings.append(f"textures are procedural: the bake failed ({str(exc)[:120]})")
        log.warning("texture.bake_failed", slug=model.segment_slug, error=repr(exc)[:200])
        return None
    for w in getattr(refs, "warnings", None) or []:
        warnings.append(str(w))
    log.info("texture.baked", slug=model.segment_slug, version=model.version,
             latency_ms=round((time.perf_counter() - started) * 1000, 1))
    return refs


def assets_for(slug: str, rec: Any) -> Assets:
    """``scene.assets`` from an ``ExportRecord`` (URLs carry ``?v=<sha8>``, so a changed bundle busts the cache)."""
    v = f"?v={rec.sha8}"
    base = f"/api/segments/{slug}"
    return Assets(mesh_sha256=rec.mesh_sha256, sha8=rec.sha8, glb_url=f"{base}/model.glb{v}",
                  manifest_url=f"{base}/manifest.json{v}", xodr_url=f"{base}/street.xodr{v}",
                  glb_bytes=rec.glb_bytes, triangles=rec.triangles, primitives=rec.primitives)


def derive_scene(model: CanonicalModel, *, observations: Mapping[str, Observation], assets: Mapping[str, str],
                 fidelity: FidelityMap, segment_name: str, city: str, data_dir: Path,
                 generation: Mapping[str, Any] | None = None, toolchain: Toolchain | None = None,
                 export: bool = True) -> Derived:
    """bake -> compile -> (export) for one model version. No network, no LLM, deterministic for equal inputs.

    ``generation`` overrides fields of ``scene.generation`` (models, review_iterations, warnings). With
    ``export=False`` (dry runs) nothing is written. Export errors (``ExportTooLarge`` ...) propagate: the caller
    has not touched any DB row yet, so the previous scene stays.
    """
    tc = toolchain or Toolchain.default()
    warnings: list[str] = []
    textures = _bake(tc, model, observations, data_dir, warnings) if export else None
    started = time.perf_counter()
    scene, plan, gaps = call_compile(tc.compile, model, observations=observations, assets=assets, fidelity=fidelity,
                                     segment_name=segment_name, city=city, textures=textures)
    if generation:
        merged = scene.generation.model_copy(update=dict(generation))
        scene = scene.model_copy(update={"generation": merged})
    export_json: dict[str, Any] | None = None
    if isinstance(scene, SceneV3):
        if not export:
            pass
        elif tc.export is None or plan is None:
            warnings.append("the street was compiled but not exported (the export step is unavailable)")
        else:
            rec = tc.export(slug=model.segment_slug, version=model.version, model=model, scene=scene, plan=plan,
                            observations=observations, data_dir=data_dir)
            export_json = rec.to_json()
            scene = scene.model_copy(update={"assets": assets_for(model.segment_slug, rec)})
    gaps_json = gaps.model_dump(mode="json") if gaps is not None else None
    interpolated = union_m([(i.s_from, i.s_to) for i in gaps.interpolations]) if gaps is not None else 0.0
    dynamic = scene.dynamic_context.excluded_count if isinstance(scene, SceneV3) else 0
    blank = gaps.blank_total() if gaps is not None else 0
    schema = scene.schema_version
    compiled_by = scene.model.compiled_by if isinstance(scene, (SceneV2, SceneV3)) and getattr(scene, "model", None) else None
    log.info("scene.compiled", slug=model.segment_slug, version=model.version, schema=schema,
             interpolated_m=interpolated, dynamic_excluded=dynamic, blank_bins=blank,
             latency_ms=round((time.perf_counter() - started) * 1000, 1))
    return Derived(scene=scene, scene_schema=int(schema), compiled_by=compiled_by, gaps=gaps_json, export=export_json,
                   interpolated_m=interpolated, dynamic_excluded=dynamic, blank_bins=blank, warnings=warnings)


# ------------------------------------------------------------------ version bundle


@dataclass
class VersionBundle:
    model: CanonicalModel
    scene: SceneV2 | SceneV3
    fidelity_before: FidelityMap
    fidelity_after: FidelityMap
    diff: ModelDiff
    summary: str
    quality: str
    changed: bool
    # inputs ``publish_bundle`` needs to bake + export (set by ``assemble_version``)
    observations: Mapping[str, Observation] = field(default_factory=dict)
    assets: Mapping[str, str] = field(default_factory=dict)
    segment_name: str = ""
    city: str = ""
    generation: dict[str, Any] = field(default_factory=dict)
    derived: "Derived | None" = None

    def to_create(self) -> ModelVersionCreate:
        d = self.derived
        return ModelVersionCreate(
            version=self.model.version, kind="fused", parent_version=self.model.parent_version,
            run_id=self.model.run_id, model=self.model, scene=self.scene.model_dump(mode="json"),
            fidelity=self.fidelity_after, diff=self.diff, summary=self.summary,
            scene_schema=d.scene_schema if d else None, compiled_by=d.compiled_by if d else None,
            gaps=d.gaps if d else None, export=d.export if d else None)


def best_tile(observations: Mapping[str, Observation]) -> Observation | None:
    """The satellite overview used as the scene's ground (oldest overview wins, so the ground never jumps)."""
    tiles = sorted((o for o in observations.values() if o.kind is ObservationKind.satellite_overview),
                   key=lambda o: (o.created_at, o.id))
    return tiles[0] if tiles else None


def assemble_version(*, candidate: CanonicalModel, base: CanonicalModel, observations: Mapping[str, Observation],
                     assets: Mapping[str, str], segment_name: str, city: str, models: Mapping[str, str],
                     review_iterations: int, warnings: list[str]) -> VersionBundle:
    fid_before = compute_fidelity(base, observations)
    fid_after = compute_fidelity(candidate, observations)
    model = candidate.model_copy(update={"fidelity": fid_after, "stats": candidate.computed_stats()})
    diff = diff_models(base, model, fid_before, fid_after)
    scene, _plan, _gaps = call_compile(compile_scene, model, observations=observations, assets=assets,
                                       fidelity=fid_after, segment_name=segment_name, city=city)
    generation = {"models": dict(models), "review_iterations": review_iterations,
                  "warnings": list(dict.fromkeys([*scene.generation.warnings, *warnings]))}
    scene = scene.model_copy(update={"generation": scene.generation.model_copy(update=generation)})
    return VersionBundle(model=model, scene=scene, fidelity_before=fid_before, fidelity_after=fid_after, diff=diff,
                         summary=version_summary(model, diff, fid_after), quality=scene.generation.quality.value,
                         changed=not diff.is_empty, observations=observations, assets=assets,
                         segment_name=segment_name, city=city, generation=generation)


def prior_fallback_scene(prior: CanonicalModel, observations: Mapping[str, Observation], *, segment_name: str,
                         city: str, warnings: list[str]) -> SceneV2:
    """The v0 prior compiled as a clearly-labelled low-fidelity scene (``about.degraded``, quality ``generic``)."""
    fid = compute_fidelity(prior, observations)
    gen_warnings = list(dict.fromkeys(warnings))
    try:  # best effort: bake + export so the labelled prior is a servable v3 street (assets present)
        from hero.config import get_settings

        derived = derive_scene(prior, observations=observations, assets={}, fidelity=fid, segment_name=segment_name,
                               city=city, data_dir=get_settings().data_dir_abs)
        scene = derived.scene
    except Exception as exc:  # noqa: BLE001 - never lose the fallback street because the export failed
        log.warning("scene.prior_export_failed", slug=prior.segment_slug, error=repr(exc)[:200])
        scene, _plan, _gaps = call_compile(compile_scene, prior, observations=observations, assets={}, fidelity=fid,
                                           segment_name=segment_name, city=city)
    generation = scene.generation.model_copy(update={"warnings": list(dict.fromkeys([*scene.generation.warnings,
                                                                                      *gen_warnings]))})
    return scene.model_copy(update={"generation": generation})


# ------------------------------------------------------------------ DB writes


def publish_bundle(segment_id: str, bundle: VersionBundle, *, thumbnail_path: str | None,
                   data_dir: Path | None = None, toolchain: Toolchain | None = None) -> ModelVersionRecord:
    """Bake + compile + export (files first), then ONE transaction: ``model_versions`` row (must be head + 1) and the
    segment's scene pointer. ``bundle.scene`` is replaced by the exported scene (v3 carries ``assets``)."""
    if data_dir is None:
        from hero.config import get_settings

        data_dir = get_settings().data_dir_abs
    derived = derive_scene(bundle.model, observations=bundle.observations, assets=bundle.assets,
                           fidelity=bundle.fidelity_after, segment_name=bundle.segment_name, city=bundle.city,
                           data_dir=data_dir, generation=bundle.generation or None, toolchain=toolchain)
    for w in derived.warnings:  # not part of the scene: the scene must equal what `recompile` would produce
        log.warning("scene.derive_warning", slug=bundle.model.segment_slug, warning=w[:200])
    bundle.scene, bundle.derived = derived.scene, derived
    return model_repo.publish_version(segment_id, bundle.to_create(), thumbnail_path=thumbnail_path,
                                      quality=bundle.quality)


def import_legacy_segment(seg: repo.SegmentRecord, *, now: datetime) -> bool:
    """Import a Phase-1 (scene v1) segment as legacy model v0. ``False`` when there is nothing to import."""
    if model_repo.head_version(seg.id) is not None or not isinstance(seg.scene, Scene):
        return False
    model = import_legacy(seg.scene, segment_slug=seg.slug, footpath_side=seg.footpath_side.value, now=now)
    fidelity = model.fidelity or compute_fidelity(model, {})
    rec = ModelVersionCreate(
        version=0, kind="legacy", parent_version=None, run_id=None, model=model,
        scene=seg.scene.model_dump(mode="json"), fidelity=fidelity, diff=None,
        summary="Imported Phase-1 scene (legacy, placement unverified).")
    try:
        model_repo.save_version_only(seg.id, rec)
    except model_repo.VersionConflictError:  # a concurrent request imported it first
        return False
    return True
