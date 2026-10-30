"""Publishing a fused model: consistency check, version bundle (fidelity, diff, scene, summary) and the DB write.

Everything here except ``publish_bundle`` / ``import_legacy_segment`` is pure (no network, DB or clock - callers pass
``now``), so it is unit-tested without services (``tests/unit/test_fusion_publish.py``).

* ``consistency_check`` - spec/agent.md rules 4a-d (e/f are handled where the evidence is created): returns the
  possibly-amended model plus warning strings; it never raises and never deletes anything.
* ``assemble_version`` - fidelity before/after, diff, compiled Scene v2 (with the run's ``generation`` facts), summary.
  ``bundle.changed`` is False when the merge changed nothing (no new version is created then).
* ``publish_bundle`` - ``model_repo.publish_version`` (one transaction: version row + segment pointer).
* ``prior_fallback_scene`` - the labelled low-fidelity scene a failed first generation still leaves behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate, ModelVersionRecord
from hero.model import ids
from hero.model.compile import compile_scene
from hero.model.describe import version_summary
from hero.model.diff import diff_models
from hero.model.fidelity import compute_fidelity
from hero.model.legacy import import_legacy
from hero.model.scene_v2 import SceneV2
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


# ------------------------------------------------------------------ version bundle


@dataclass
class VersionBundle:
    model: CanonicalModel
    scene: SceneV2
    fidelity_before: FidelityMap
    fidelity_after: FidelityMap
    diff: ModelDiff
    summary: str
    quality: str
    changed: bool

    def to_create(self) -> ModelVersionCreate:
        return ModelVersionCreate(
            version=self.model.version, kind="fused", parent_version=self.model.parent_version,
            run_id=self.model.run_id, model=self.model, scene=self.scene.model_dump(mode="json"),
            fidelity=self.fidelity_after, diff=self.diff, summary=self.summary)


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
    scene = compile_scene(model, observations=observations, assets=assets, tile=best_tile(observations),
                          fidelity=fid_after, slug_name=segment_name, city=city)
    generation = scene.generation.model_copy(update={
        "models": dict(models), "review_iterations": review_iterations,
        "warnings": list(dict.fromkeys([*scene.generation.warnings, *warnings]))})
    scene = scene.model_copy(update={"generation": generation})
    return VersionBundle(model=model, scene=scene, fidelity_before=fid_before, fidelity_after=fid_after, diff=diff,
                         summary=version_summary(model, diff, fid_after), quality=scene.generation.quality.value,
                         changed=not diff.is_empty)


def prior_fallback_scene(prior: CanonicalModel, observations: Mapping[str, Observation], *, segment_name: str,
                         city: str, warnings: list[str]) -> SceneV2:
    """The v0 prior compiled as a clearly-labelled low-fidelity scene (``about.degraded``, quality ``generic``)."""
    fid = compute_fidelity(prior, observations)
    scene = compile_scene(prior, observations=observations, assets={}, tile=best_tile(observations), fidelity=fid,
                          slug_name=segment_name, city=city)
    generation = scene.generation.model_copy(update={"warnings": list(dict.fromkeys([*scene.generation.warnings,
                                                                                      *warnings]))})
    return scene.model_copy(update={"generation": generation})


# ------------------------------------------------------------------ DB writes


def publish_bundle(segment_id: str, bundle: VersionBundle, *, thumbnail_path: str | None) -> ModelVersionRecord:
    """One transaction: ``model_versions`` row (must be head + 1) and the segment's scene pointer."""
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
