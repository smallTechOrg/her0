"""Model diff and content hash (spec/data.md -> "Model diff"; spec/architecture.md -> Contracts).

``diff_models(a, b, fid_a, fid_b) -> ModelDiff`` is a pure, deterministic comparison of two canonical
models (``a`` may be ``None`` for the first version). ``model_hash(m)`` is the sha256 of the canonical JSON of
the model *content*, excluding ``version, parent_version, run_id, created_at, kind`` so two lineages with
identical content hash alike (the no-op / idempotence check).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from hero.model.types import (
    CanonicalModel,
    DiffContradictions,
    DiffEntities,
    DiffEntityChanged,
    DiffEntityRef,
    DiffEntityRemoved,
    DiffFidelity,
    DiffIntervalChange,
    DiffObservations,
    DiffProfile,
    DiffBuildings,
    Entity,
    EntityState,
    FidelityImproved,
    FidelityMap,
    ModelDiff,
    ProfileInterval,
    Strip,
)

HASH_EXCLUDE = {"version", "parent_version", "run_id", "created_at", "kind"}
CONF_DELTA = 0.05
MOVE_M = 0.4
EDGE_MOVE_M = 0.15
EPS = 1e-9


def model_hash(m: CanonicalModel) -> str:
    data = m.model_dump(mode="json", exclude=HASH_EXCLUDE)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ entities


def _ref(e: Entity) -> DiffEntityRef:
    return DiffEntityRef(id=e.id, type=str(e.type), s=round(e.position.s, 2), t=round(e.position.t, 2),
                         confidence=round(e.confidence, 3), description=e.description)


def _entity_fields(ea: Entity, eb: Entity) -> dict:
    f: dict = {}
    if abs(ea.confidence - eb.confidence) >= CONF_DELTA - EPS:
        f["confidence"] = [round(ea.confidence, 3), round(eb.confidence, 3)]
    if math.hypot(ea.position.s - eb.position.s, ea.position.t - eb.position.t) >= MOVE_M - EPS:
        f["position"] = [[round(ea.position.s, 2), round(ea.position.t, 2)],
                         [round(eb.position.s, 2), round(eb.position.t, 2)]]
    for name in ("type", "subtype", "state", "description"):
        va, vb = getattr(ea, name), getattr(eb, name)
        if va != vb:
            f[name] = [str(va) if va is not None else None, str(vb) if vb is not None else None]
    if ea.attributes != eb.attributes:
        keys = sorted(set(ea.attributes) | set(eb.attributes))
        f["attributes"] = {k: [ea.attributes.get(k), eb.attributes.get(k)] for k in keys
                           if ea.attributes.get(k) != eb.attributes.get(k)}
    return f


def _change_reason(ea: Entity, eb: Entity) -> str:
    sa = {(e.observation_id, e.detection_key, e.role) for e in ea.provenance}
    new = [e for e in eb.provenance if (e.observation_id, e.detection_key, e.role) not in sa]
    sup = sum(1 for e in new if e.role.value == "supports")
    con = sum(1 for e in new if e.role.value == "contradicts")
    other = len(new) - sup - con
    bits = []
    if sup:
        bits.append(f"{sup} more supporting view{'s' if sup != 1 else ''}")
    if con:
        bits.append(f"{con} view{'s' if con != 1 else ''} did not see it")
    if other:
        bits.append(f"{other} review note{'s' if other != 1 else ''}")
    return ", ".join(bits) or "updated"


def _diff_entities(a: CanonicalModel | None, b: CanonicalModel) -> DiffEntities:
    amap = {e.id: e for e in a.entities} if a else {}
    out = DiffEntities()
    unchanged = 0
    for eb in sorted(b.entities, key=lambda e: e.id):
        ea = amap.get(eb.id)
        removed_b = eb.state is EntityState.removed
        if ea is None:
            if not removed_b:
                out.added.append(_ref(eb))
            else:
                out.removed.append(DiffEntityRemoved(id=eb.id, type=str(eb.type), s=round(eb.position.s, 2),
                                                     t=round(eb.position.t, 2), reason="first recorded as removed"))
            continue
        removed_a = ea.state is EntityState.removed
        if removed_b and not removed_a:
            out.removed.append(DiffEntityRemoved(
                id=eb.id, type=str(eb.type), s=round(eb.position.s, 2), t=round(eb.position.t, 2),
                reason=f"not seen in {len(eb.contradictions())} later views; confidence "
                       f"{ea.confidence:.2f} → {eb.confidence:.2f}"))
        elif removed_a and not removed_b:
            out.revived.append(_ref(eb))
        else:
            fields = _entity_fields(ea, eb)
            if fields:
                out.changed.append(DiffEntityChanged(id=eb.id, fields=fields, reason=_change_reason(ea, eb)))
            else:
                unchanged += 1
    bmap = {e.id for e in b.entities}
    for ea in sorted(a.entities if a else [], key=lambda e: e.id):
        if ea.id not in bmap:  # should never happen (merge never deletes), but report honestly
            out.removed.append(DiffEntityRemoved(id=ea.id, type=str(ea.type), s=round(ea.position.s, 2),
                                                 t=round(ea.position.t, 2), reason="missing from the new snapshot"))
    out.unchanged = unchanged
    return out


# ------------------------------------------------------------------ profile


def _strip_key(s: Strip) -> tuple:
    return (str(s.kind), s.side)


def _strip_changes(ia: ProfileInterval, ib: ProfileInterval, fa: float, fb: float) -> list[str]:
    out: list[str] = []
    if ia.footpath_state != ib.footpath_state:
        out.append(f"footpath_state {ia.footpath_state} → {ib.footpath_state}")
    sa = {_strip_key(s): s for s in ia.strips}
    sb = {_strip_key(s): s for s in ib.strips}
    for k in sorted(set(sa) | set(sb)):
        label = f"{k[0]} ({k[1]})"
        if k not in sa:
            out.append(f"{label} added")
        elif k not in sb:
            out.append(f"{label} removed")
        else:
            x, y = sa[k], sb[k]
            moved = max(abs(x.t_lo_at(fa) - y.t_lo_at(fb)), abs(x.t_hi_at(fa) - y.t_hi_at(fb)))
            if moved >= EDGE_MOVE_M - EPS:
                out.append(f"{label} edge moved {moved:.1f} m")
            if x.material != y.material:
                out.append(f"{label} material {x.material} → {y.material}")
            if x.condition != y.condition:
                out.append(f"{label} condition {x.condition} → {y.condition}")
    return out


def _diff_profile(a: CanonicalModel | None, b: CanonicalModel) -> DiffProfile:
    if a is None:
        return DiffProfile()
    length = b.length_s
    cuts = {0.0, length}
    for m in (a, b):
        for iv in m.profile:
            cuts.add(round(iv.s_from, 6))
            cuts.add(round(iv.s_to, 6))
    xs = sorted(c for c in cuts if 0.0 <= c <= length + 1e-6)
    rows: list[tuple[float, float, str]] = []
    for x0, x1 in zip(xs, xs[1:]):
        if x1 - x0 < 1e-6:
            continue
        mid = (x0 + x1) / 2
        ia, ib = a.interval_at(mid), b.interval_at(mid)
        fa = (mid - ia.s_from) / ia.length
        fb = (mid - ib.s_from) / ib.length
        what = "; ".join(_strip_changes(ia, ib, fa, fb))
        if what:
            if rows and rows[-1][2] == what and abs(rows[-1][1] - x0) < 1e-6:
                rows[-1] = (rows[-1][0], x1, what)
            else:
                rows.append((x0, x1, what))
    return DiffProfile(changed_intervals=[DiffIntervalChange(s_from=round(r0, 2), s_to=round(r1, 2), what=w)
                                          for r0, r1, w in rows])


# ------------------------------------------------------------------ buildings / contradictions / fidelity


def _building_brief(bd) -> dict:
    return {"id": bd.id, "side": bd.side, "source": bd.source}


def _diff_buildings(a: CanonicalModel | None, b: CanonicalModel) -> DiffBuildings:
    amap = {x.id: x for x in a.buildings} if a else {}
    bmap = {x.id: x for x in b.buildings}
    out = DiffBuildings()
    for bid in sorted(bmap):
        nb = bmap[bid]
        oa = amap.get(bid)
        if oa is None:
            out.added.append(_building_brief(nb))
            continue
        fields: dict = {}
        if abs(oa.height_m - nb.height_m) >= 0.5:
            fields["height_m"] = [round(oa.height_m, 2), round(nb.height_m, 2)]
        if abs(oa.confidence - nb.confidence) >= CONF_DELTA - EPS:
            fields["confidence"] = [round(oa.confidence, 3), round(nb.confidence, 3)]
        for name in ("colour_hex", "storeys", "source", "description"):
            if getattr(oa, name) != getattr(nb, name):
                fields[name] = [getattr(oa, name), getattr(nb, name)]
        if len(oa.facades) != len(nb.facades):
            fields["facades"] = [len(oa.facades), len(nb.facades)]
        if fields:
            out.changed.append({"id": bid, "fields": fields})
    for bid in sorted(set(amap) - set(bmap)):
        out.removed.append(_building_brief(amap[bid]))
    return out


def _diff_contradictions(a: CanonicalModel | None, b: CanonicalModel) -> DiffContradictions:
    a_open = {c.id for c in a.contradictions if c.is_open} if a else set()
    out = DiffContradictions()
    for c in sorted(b.contradictions, key=lambda c: c.id):
        if c.is_open and c.id not in a_open:
            out.opened.append({"id": c.id, "kind": str(c.kind), "description": c.description})
        elif not c.is_open and c.id in a_open:
            out.resolved.append({"id": c.id, "kind": str(c.kind), "description": c.description})
    return out


def _diff_fidelity(fa: FidelityMap | None, fb: FidelityMap) -> DiffFidelity:
    improved: list[FidelityImproved] = []
    unchanged = 0.0
    for b in fb.bins:
        la = fa.level_at((b.s_from + b.s_to) / 2) if fa else 0
        span = b.s_to - b.s_from
        if b.level > la:
            if improved and improved[-1].from_ == f"L{la}" and improved[-1].to == f"L{b.level}" \
                    and abs(improved[-1].s_to - b.s_from) < 1e-6:
                improved[-1] = FidelityImproved(s_from=improved[-1].s_from, s_to=round(b.s_to, 2),
                                                from_=f"L{la}", to=f"L{b.level}")
            else:
                improved.append(FidelityImproved(s_from=round(b.s_from, 2), s_to=round(b.s_to, 2),
                                                 from_=f"L{la}", to=f"L{b.level}"))
        elif b.level == la:
            unchanged += span
    return DiffFidelity(improved=improved, unchanged_len_m=round(unchanged, 2),
                        overall=(round(fa.overall.score, 2) if fa else 0.0, round(fb.overall.score, 2)))


# ------------------------------------------------------------------ summary


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _summary_lines(a: CanonicalModel | None, b: CanonicalModel, d: ModelDiff) -> list[str]:
    lines: list[str] = []
    if d.observations.fused_added:
        lines.append(f"{_plural(d.observations.fused_added, 'new photo')} fused")
    if d.entities.added:
        counts = Counter(e.type for e in d.entities.added)
        parts = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
        lines.append(f"{_plural(len(d.entities.added), 'new object')}: {parts}")
    if d.entities.revived:
        lines.append(f"{_plural(len(d.entities.revived), 'object')} seen again")
    if d.entities.removed:
        lines.append(f"{_plural(len(d.entities.removed), 'object')} no longer supported by the imagery")
    if d.entities.changed:
        lines.append(f"{_plural(len(d.entities.changed), 'object')} updated")
    if d.profile.changed_intervals:
        lines.append(f"{_plural(len(d.profile.changed_intervals), 'stretch')} of the cross-section changed")
    if d.buildings.added or d.buildings.changed:
        lines.append(f"{len(d.buildings.added)} new / {len(d.buildings.changed)} updated buildings")
    if d.contradictions.opened:
        lines.append(f"{_plural(len(d.contradictions.opened), 'new disagreement')} between views")
    if d.contradictions.resolved:
        lines.append(f"{_plural(len(d.contradictions.resolved), 'disagreement')} resolved")
    for imp in d.fidelity.improved:
        lines.append(f"Fidelity {imp.from_} → {imp.to} over {imp.s_to - imp.s_from:.0f} m")
    return lines


def diff_models(a: CanonicalModel | None, b: CanonicalModel, fid_a: FidelityMap | None,
                fid_b: FidelityMap) -> ModelDiff:
    fused_added = len(set(b.fused_observation_ids) - set(a.fused_observation_ids)) if a else len(set(b.fused_observation_ids))
    d = ModelDiff(
        from_version=a.version if a else None, to_version=b.version,
        entities=_diff_entities(a, b), profile=_diff_profile(a, b), buildings=_diff_buildings(a, b),
        fidelity=_diff_fidelity(fid_a, fid_b), contradictions=_diff_contradictions(a, b),
        observations=DiffObservations(fused_added=fused_added))
    d.is_empty = not (
        d.entities.added or d.entities.removed or d.entities.revived or d.entities.changed
        or d.profile.changed_intervals or d.buildings.added or d.buildings.changed or d.buildings.removed
        or d.fidelity.improved or d.fidelity.overall[0] != d.fidelity.overall[1]
        or d.contradictions.opened or d.contradictions.resolved or fused_added)
    d.summary_lines = _summary_lines(a, b, d)
    return d
