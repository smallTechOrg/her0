"""Admin model inspection API (spec/api.md ## Admin: model, versions, diff, coverage, entities, run report).

Read-only over ``hero.db.model_repo`` (r2: ``scene_schema`` and a gap summary on the head; ``GET .../gaps``). The only write is the lazy import of a Phase-1 scene as the legacy v0
model on the first ``GET .../model`` of a segment that has a scene and no versions.
Sibling modules (``hero.model.diff``, ``hero.model.legacy``, ``hero.model.coverage``) are imported lazily.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from hero.api.auth import require_admin
from hero.api.envelope import ApiError, ok
from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate, ModelVersionRecord, VersionConflictError
from hero.model.refline import ReferenceLine
from hero.model.types import (
    Entity,
    EntityState,
    EntityType,
    FidelityBin,
    FidelityMap,
    FidelityOverall,
    Observation,
)
from hero.scene.schema import Scene

router = APIRouter(prefix="/api/admin", tags=["admin-model"], dependencies=[Depends(require_admin)])

MAX_REF_POINTS = 60
MAX_OBS_PER_BIN = 12
MIN_OVERLAP_M = 2.5


# ------------------------------------------------------------------ helpers


def _segment(slug: str) -> repo.SegmentRecord:
    try:
        return repo.require_segment(slug)
    except repo.NotFoundError as exc:
        raise ApiError("not_found", str(exc)) from exc


def _version(segment_id: str, version: int | None, *, slug: str) -> ModelVersionRecord:
    """The requested version (default head); 404 when there is none."""
    n = model_repo.head_version(segment_id) if version is None else version
    rec = model_repo.get_version(segment_id, n) if n is not None and n >= 0 else None
    if rec is None:
        what = "has no model yet" if n is None else f"has no model version {n}"
        raise ApiError("not_found", f"Segment {slug!r} {what}.")
    return rec


def _l0_fidelity(length_s: float, bin_m: float = 5.0) -> FidelityMap:
    import math

    n = max(1, math.ceil(length_s / bin_m - 1e-9))
    bins = [FidelityBin(index=i, s_from=round(i * bin_m, 3), s_to=round(min(length_s, (i + 1) * bin_m), 3), level=0)
            for i in range(n)]
    return FidelityMap(bin_m=bin_m, length_s=length_s, bins=bins,
                       overall=FidelityOverall(level=0, score=0.0,
                                               histogram_m={"L0": round(length_s, 3), "L1": 0.0, "L2": 0.0, "L3": 0.0}))


def _maybe_import_legacy(seg: repo.SegmentRecord) -> None:
    """Import the Phase-1 scene as legacy v0 when the segment has a v1 scene and no versions."""
    if model_repo.head_version(seg.id) is not None or not isinstance(seg.scene, Scene):
        return
    try:
        from hero.model.legacy import import_legacy
    except ImportError as exc:  # S1d not landed: the segment simply has no model yet
        raise ApiError("internal_error", "Legacy import is unavailable (hero.model.legacy).") from exc
    model = import_legacy(seg.scene, segment_slug=seg.slug, footpath_side=seg.footpath_side.value,
                          now=datetime.now(UTC))
    rec = ModelVersionCreate(
        version=0, kind="legacy", parent_version=None, run_id=None, model=model, scene=seg.scene.model_dump(mode="json"),
        fidelity=model.fidelity or _l0_fidelity(model.reference_line.length_s), diff=None,
        summary="Imported Phase-1 scene (legacy, placement unverified).")
    try:
        model_repo.save_version_only(seg.id, rec)
    except VersionConflictError:  # a concurrent request imported it first
        pass


def _gap_summary(gaps: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{uncovered: {...}, interpolated_m}`` from a stored GapReport (``None`` until the version is recompiled)."""
    if not gaps:
        return None
    from hero.fusion.publish import union_m

    return {"uncovered": gaps.get("uncovered"),
            "interpolated_m": union_m([(i["s_from"], i["s_to"]) for i in gaps.get("interpolations", [])])}


def _simplify(points: list, limit: int = MAX_REF_POINTS) -> list[dict[str, float]]:
    """Uniformly decimate to at most ``limit`` points, always keeping the first and last."""
    n = len(points)
    if n <= limit:
        idx = list(range(n))
    else:
        idx = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    return [{"lat": points[i].lat, "lon": points[i].lon, "s": points[i].s} for i in idx]


def _dump(x: Any) -> Any:
    if isinstance(x, list):
        return [_dump(i) for i in x]
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


# ------------------------------------------------------------------ model head / versions / diff


@router.get("/segments/{slug}/model")
def model_head(slug: str):
    seg = _segment(slug)
    _maybe_import_legacy(seg)
    head_n = model_repo.head_version(seg.id)
    if head_n is None:
        return ok({"slug": slug, "head_version": None, "legacy": False, "placement_verified": False,
                   "scene_schema": None, "gaps": None,
                   "reference_line": None, "fidelity": None,
                   "counts": {"entities": 0, "removed": 0, "buildings": 0, "observations": 0, "fused_observations": 0},
                   "versions": []})
    head = model_repo.get_version(seg.id, head_n)
    assert head is not None
    m = head.model
    removed = sum(1 for e in m.entities if e.state is EntityState.removed)
    ref = m.reference_line
    return ok({
        "slug": slug, "head_version": head_n, "legacy": m.legacy, "placement_verified": m.placement_verified,
        "scene_schema": head.scene_schema if head.scene else None, "gaps": _gap_summary(head.gaps),
        "reference_line": {"source": ref.source, "osm_way_id": ref.osm_way_id, "name": ref.name,
                           "length_s": ref.length_s, "points": _simplify(ref.points)},
        "fidelity": head.fidelity.model_dump(mode="json"),
        "counts": {"entities": len(m.entities) - removed, "removed": removed, "buildings": len(m.buildings),
                   "observations": sum(model_repo.observation_counts(seg.id).values()),
                   "fused_observations": len(m.fused_observation_ids)},
        "versions": _dump(model_repo.list_versions(seg.id)),
    })


@router.get("/segments/{slug}/model/versions/{n}")
def model_version(slug: str, n: int):
    seg = _segment(slug)
    rec = _version(seg.id, n, slug=slug)
    return ok({"version": rec.version, "kind": rec.kind, "parent_version": rec.parent_version, "run_id": rec.run_id,
               "created_at": rec.created_at.isoformat(), "summary": rec.summary,
               "model": rec.model.model_dump(mode="json"), "fidelity": rec.fidelity.model_dump(mode="json"),
               "diff": rec.diff.model_dump(mode="json") if rec.diff else None})


@router.get("/segments/{slug}/model/diff")
def model_diff(slug: str, from_: int = Query(alias="from"), to: int = Query()):
    seg = _segment(slug)
    a = _version(seg.id, from_, slug=slug)
    b = _version(seg.id, to, slug=slug)
    try:
        from hero.model.diff import diff_models
    except ImportError as exc:
        raise ApiError("internal_error", "Model diff is unavailable (hero.model.diff).") from exc
    return ok(diff_models(a.model, b.model, a.fidelity, b.fidelity).model_dump(mode="json"))


# ------------------------------------------------------------------ gaps


@router.get("/segments/{slug}/gaps")
def model_gaps(slug: str, version: int | None = Query(default=None, ge=0)):
    """The ``GapReport`` of ``version`` (default head): the stored one, else analysed on the fly (nothing is written)."""
    seg = _segment(slug)
    rec = _version(seg.id, version, slug=slug)
    if rec.gaps is not None:
        return ok(rec.gaps)
    try:
        from hero.model.gaps import analyze_gaps
    except ImportError as exc:
        raise ApiError("internal_error", "Gap analysis is unavailable (hero.model.gaps).") from exc
    ids = rec.model.fused_observation_ids
    observations = {o.id: o for o in model_repo.get_observations(seg.id, ids=ids, limit=max(len(ids), 1))} if ids else {}
    return ok(analyze_gaps(rec.model, observations, rec.fidelity, None).model_dump(mode="json"))


# ------------------------------------------------------------------ coverage


def _cell_intervals(obs: Observation, ref: ReferenceLine) -> list[tuple[float, float]]:
    """``[s_from, s_to]`` of every coverage cell of ``obs`` (documented rules if S1d's module is absent)."""
    try:
        from hero.model.coverage import observation_cells
    except ImportError:
        s = obs.station_s
        if s is None or not obs.is_street_level:
            return []
        view = obs.view.value if obs.view else "oblique"
        if view == "forward":
            return [(s, s + 15.0)]
        if view == "back":
            return [(s - 15.0, s)]
        return [(s - 6.0, s + 6.0)]
    return [(c.s_from, c.s_to) for c in observation_cells(obs, ref)]


def _covers(b: FidelityBin, lo: float, hi: float) -> bool:
    overlap = min(b.s_to, hi) - max(b.s_from, lo)
    return overlap >= min(MIN_OVERLAP_M, b.s_to - b.s_from) - 1e-9 and overlap > 0


@router.get("/segments/{slug}/model/coverage")
def model_coverage(slug: str, version: int | None = Query(default=None, ge=0)):
    seg = _segment(slug)
    rec = _version(seg.id, version, slug=slug)
    ids = rec.model.fused_observation_ids
    observations = model_repo.get_observations(seg.id, ids=ids, limit=max(len(ids), 1)) if ids else []
    order = {oid: i for i, oid in enumerate(ids)}
    observations.sort(key=lambda o: order.get(o.id, 0))
    ref = ReferenceLine.from_model(rec.model.reference_line)
    intervals = {o.id: _cell_intervals(o, ref) for o in observations}
    by_bin = []
    for b in rec.fidelity.bins:
        hit = [o.id for o in observations if any(_covers(b, lo, hi) for lo, hi in intervals[o.id])]
        by_bin.append({"index": b.index, "observation_ids": hit[:MAX_OBS_PER_BIN]})
    return ok({**rec.fidelity.model_dump(mode="json"), "observations_by_bin": by_bin})


# ------------------------------------------------------------------ entities


def _entity_row(e: Entity) -> dict[str, Any]:
    return {"id": e.id, "type": e.type.value, "subtype": e.subtype, "state": e.state.value, "s": e.position.s,
            "t": e.position.t, "confidence": e.confidence, "description": e.description,
            "evidence_count": len(e.provenance), "first_seen_version": e.first_seen_version,
            "last_changed_version": e.last_changed_version}


@router.get("/segments/{slug}/model/entities")
def model_entities(slug: str, version: int | None = Query(default=None, ge=0), type: EntityType | None = None,
                   state: EntityState | None = None, min_confidence: float | None = Query(default=None, ge=0, le=1)):
    seg = _segment(slug)
    rec = _version(seg.id, version, slug=slug)
    items = [e for e in rec.model.entities
             if (type is None or e.type is type) and (state is None or e.state is state)
             and (min_confidence is None or e.confidence >= min_confidence)]
    items.sort(key=lambda e: (e.position.s, e.id))
    return ok([_entity_row(e) for e in items])


@router.get("/segments/{slug}/model/entities/{entity_id}")
def model_entity(slug: str, entity_id: str, version: int | None = Query(default=None, ge=0)):
    seg = _segment(slug)
    rec = _version(seg.id, version, slug=slug)
    entity = next((e for e in rec.model.entities if e.id == entity_id), None)
    if entity is None:
        raise ApiError("not_found", f"Entity {entity_id!r} is not in model version {rec.version}.")
    obs_ids = list({p.observation_id for p in entity.provenance})
    obs = {o.id: o for o in model_repo.get_observations(seg.id, ids=obs_ids, limit=max(len(obs_ids), 1))} if obs_ids else {}
    evidence = []
    for p in entity.provenance:
        o = obs.get(p.observation_id)
        evidence.append({
            "role": p.role.value, "weight": p.weight, "crop_px": list(p.crop_px) if p.crop_px else None,
            "note": p.note, "added_in_version": p.added_in_version,
            "observation": None if o is None else {
                "id": o.id, "kind": o.kind.value, "view": o.view.value if o.view else None, "url": o.url,
                "station_s": o.station_s, "heading_deg": o.heading_deg, "capture_date": o.capture_date,
                "width_px": o.width_px, "height_px": o.height_px}})
    history = []
    for summary in model_repo.list_versions(seg.id):
        if summary.version > rec.version:
            break
        past = model_repo.get_version(seg.id, summary.version)
        e = next((x for x in past.model.entities if x.id == entity_id), None) if past else None
        if e is not None:
            history.append({"version": summary.version, "confidence": e.confidence, "state": e.state.value,
                            "position": e.position.model_dump(mode="json")})
    contradictions = [c.model_dump(mode="json") for c in rec.model.contradictions if entity_id in c.entity_ids]
    return ok({"entity": entity.model_dump(mode="json"), "evidence": evidence, "contradictions": contradictions,
               "history": history})


# ------------------------------------------------------------------ run report


@router.get("/runs/{run_id}/report")
def run_report(run_id: str):
    report = model_repo.get_run_report(run_id)
    if report is None:
        raise ApiError("not_found", "This run has no report (Phase-1 run, or it ended before the report step).")
    return ok(report.model_dump(mode="json"))
