"""Evidence merge: ``ProjectedBatch`` -> next ``CanonicalModel`` (spec/data.md -> "Evidence, confidence and
state rules" + "Profile rules"; spec/architecture.md -> ``hero.model.merge``).

Guarantees (all covered by ``tests/unit/test_merge*.py``):

* **Deterministic** - batches are sorted by ``(min station in the batch, batch_id)``, observations by id,
  detections by key; ids come from ``hero.model.ids``; no clock, no randomness.
* **Idempotent** - evidence is unique per ``(observation_id, detection_key)``; profile / absence / facade
  evidence from an observation already in ``fused_observation_ids`` is never re-counted; a batch whose
  observations are all fused is reported in ``ignored_batches``. ``merge(merge(m, B), B)`` therefore has the
  same content hash as ``merge(m, B)``.
* **Never deletes** - entities, buildings and contradictions are only ever added or changed; an entity the
  imagery no longer supports becomes ``state="removed"`` and can be revived under the same id.
* **Contradictions stay visible** - disagreements are recorded as ``Contradiction`` rows (opened, and
  marked resolved only when the cause disappears); they are never silently dropped.

Interpretations / simplifications (see the slice report): the "min observation station" of a batch is the
minimum ``s`` found in its own content (``ProjectedBatch`` carries no observation objects); attribute
accumulated weight is approximated by the entity's summed support weight (entities store no per-attribute
weights); material/condition of a strip are first-seen-wins once known (no per-value vote store);
``position_conflict`` is never opened (matching is by proximity so no pair of detections "disagrees" on
position beyond the type-conflict case).
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from hero.model.batch import FacadeDetection, ProjectedBatch, ProjectedDetection, StateSpan, StripSample
from hero.model.calibrate import (
    calibrate_confidence,
    clamp_logit,
    confidence_from_logit,
    logit,
)
from hero.model.ids import building_id, contradiction_id, entity_id
from hero.model.types import (
    Billboard,
    Building,
    BuildingFacade,
    CanonicalModel,
    Condition,
    Contradiction,
    ContradictionKind,
    CritiqueOp,
    Entity,
    EntityState,
    EntityType,
    Evidence,
    EvidenceRole,
    Extent,
    FlagEntityOp,
    FootpathState,
    Material,
    ModelKind,
    NudgeEdgeOp,
    NudgeEntityOp,
    ProfileInterval,
    Position,
    SetFootpathStateOp,
    Strip,
    StripKind,
)

# ------------------------------------------------------------------ constants

GRID_M = 2.5
MIN_INTERVAL_M = 2.5
MAX_INTERVAL_M = 30.0
COLLINEAR_M = 0.15
PRIOR_WEIGHT_MAX = 0.5          # a strip lighter than this is a map prior and is replaced by any trace >= this
SUPPORT_DECAY = 0.75
ABSENCE_FACTOR = 0.5
ABSENCE_MIN_VISIBLE = 0.5
GATE_MIN, GATE_MAX = 1.2, 4.0
REMOVE_CONF, REVIVE_CONF, DISPUTE_CONF = 0.15, 0.35, 0.5
CRITIQUE_WEIGHT = 0.3
CRITIQUE_MAX_CONF_DELTA = 0.15
CRITIQUE_MAX_NUDGE_M = 3.0
CRITIQUE_MAX_EDGE_M = 1.0
STATE_CONFLICT_RATIO = 0.8
BUILDING_DEPTH_M = 8.0
STOREY_M = 3.2
DEFAULT_BUILDING_HEIGHT_M = 9.0
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

_REAL_STATES = (FootpathState.intact, FootpathState.broken, FootpathState.missing, FootpathState.blocked,
                FootpathState.encroached)


@dataclass
class ChangeRecord:
    kind: str
    id: str
    before: dict | None
    after: dict | None
    reason: str


@dataclass
class MergeResult:
    model: CanonicalModel
    changes: list[ChangeRecord]
    ignored_batches: list[str]


# ------------------------------------------------------------------ small helpers


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _ent_extent(e: Entity) -> float:
    return max(e.extent.length_s, e.extent.width_t)


def _gate(sig1: float, sig2: float, ext1: float, ext2: float) -> float:
    return _clip(2.5 * math.sqrt(sig1 ** 2 + sig2 ** 2) + 0.25 * max(ext1, ext2), GATE_MIN, GATE_MAX)


def _compatible(a: EntityType, b: EntityType) -> bool:
    return a == b or ((a is EntityType.other) != (b is EntityType.other))


def _in_side(t: float, side: str) -> bool:
    return side == "both" or (side == "left" and t >= 0.0) or (side == "right" and t < 0.0)


def _ent_brief(e: Entity) -> dict:
    return {"type": str(e.type), "subtype": e.subtype, "state": str(e.state), "confidence": round(e.confidence, 4),
            "position": [round(e.position.s, 3), round(e.position.t, 3)], "description": e.description,
            "attributes": dict(e.attributes)}


def _batch_min_station(b: ProjectedBatch) -> float:
    vals = [d.s for d in b.detections] + [x.s for x in b.strips] + [x.s_from for x in b.states] \
        + [x.s_from for x in b.facades] + [x.s_from for x in b.visible]
    return min(vals) if vals else math.inf


def _batch_key(b: ProjectedBatch) -> tuple:
    return (_batch_min_station(b), b.batch_id)


def _derive_state(e: Entity, prev: EntityState, version: int) -> EntityState:
    """State rules from spec/data.md (removed / disputed / present, with revive hysteresis)."""
    contra_obs = {x.observation_id for x in e.provenance if x.role is EvidenceRole.contradicts}
    n_support = sum(1 for x in e.provenance if x.role is EvidenceRole.supports)
    if e.confidence < REMOVE_CONF and len(contra_obs) >= 2:
        return EntityState.removed
    if prev is EntityState.removed and e.confidence < REVIVE_CONF:
        return EntityState.removed
    recent_contra = any(x.role is EvidenceRole.contradicts and x.added_in_version >= version - 1 for x in e.provenance)
    if n_support >= 1 and recent_contra and REMOVE_CONF <= e.confidence < DISPUTE_CONF:
        return EntityState.disputed
    return EntityState.present


# ------------------------------------------------------------------ profile working representation


@dataclass
class _CS:  # a strip within one grid cell
    kind: StripKind
    side: str
    ordinal: int
    material: Material
    condition: Condition
    height_m: float
    t_lo: float
    t_hi: float
    weight: float
    description: str
    prov: set[str] = field(default_factory=set)

    def key(self) -> tuple:
        return (self.kind.value, self.side, self.ordinal)


@dataclass
class _Cell:
    s0: float
    s1: float
    strips: list[_CS]
    votes: dict[str, float]
    state: FootpathState
    touched: bool = False

    @property
    def mid(self) -> float:
        return (self.s0 + self.s1) / 2


def _decide_state(votes: dict[str, float], current: FootpathState) -> tuple[FootpathState, bool]:
    real = {k: v for k, v in votes.items() if k != FootpathState.unknown.value and v > 0}
    if not real:
        return current, False
    ordered = sorted(real.items(), key=lambda kv: (-kv[1], kv[0]))
    leader = ordered[0]
    conflict = len(ordered) > 1 and ordered[1][1] >= STATE_CONFLICT_RATIO * leader[1]
    return FootpathState(leader[0]), conflict


def _build_cells(model: CanonicalModel) -> list[_Cell]:
    length = model.length_s
    n = max(1, int(length // GRID_M))
    g = length / n
    cells: list[_Cell] = []
    for i in range(n):
        s0, s1 = i * g, (length if i == n - 1 else (i + 1) * g)
        c = (s0 + s1) / 2
        iv = model.interval_at(c)
        f = _clip((c - iv.s_from) / iv.length, 0.0, 1.0)
        strips: list[_CS] = []
        ords: dict[tuple, int] = {}
        for st in iv.strips:
            k = (st.kind.value, st.side)
            o = ords.get(k, 0)
            ords[k] = o + 1
            strips.append(_CS(st.kind, st.side, o, st.material, st.condition, st.height_m, st.t_lo_at(f),
                              st.t_hi_at(f), st.weight, st.description, set(st.provenance)))
        cells.append(_Cell(s0, s1, strips, dict(iv.state_votes), iv.footpath_state))
    return cells


def _cell_index(cells: list[_Cell], s: float) -> int:
    g = cells[0].s1 - cells[0].s0
    return int(_clip(s // g if g > 0 else 0, 0, len(cells) - 1))


def _fold_strip(cell: _Cell, smp: StripSample) -> None:
    cands = [c for c in cell.strips if c.kind == smp.kind and c.side == smp.side]
    centre = (smp.t_lo + smp.t_hi) / 2
    cs = min(cands, key=lambda c: (abs((c.t_lo + c.t_hi) / 2 - centre), c.ordinal)) if cands else None
    w = smp.weight
    if cs is None:
        ordinal = max((c.ordinal for c in cell.strips if c.kind == smp.kind and c.side == smp.side), default=-1) + 1
        cell.strips.append(_CS(smp.kind, smp.side, ordinal, smp.material, smp.condition, 0.0, smp.t_lo, smp.t_hi, w,
                               "", {smp.observation_id}))
    elif cs.weight < PRIOR_WEIGHT_MAX and w >= PRIOR_WEIGHT_MAX:  # traced evidence replaces the map prior
        cs.t_lo, cs.t_hi, cs.weight = smp.t_lo, smp.t_hi, w
        if smp.material is not Material.unknown:
            cs.material = smp.material
        if smp.condition is not Condition.unknown:
            cs.condition = smp.condition
        cs.prov.add(smp.observation_id)
    else:
        W = cs.weight
        cs.t_lo = (W * cs.t_lo + w * smp.t_lo) / (W + w)
        cs.t_hi = (W * cs.t_hi + w * smp.t_hi) / (W + w)
        cs.weight = W + w
        if cs.material is Material.unknown:
            cs.material = smp.material
        if cs.condition is Condition.unknown:
            cs.condition = smp.condition
        cs.prov.add(smp.observation_id)
    cell.touched = True


def _fold_state(cells: list[_Cell], span: StateSpan) -> None:
    idx = [i for i, c in enumerate(cells)
           if min(c.s1, span.s_to) - max(c.s0, span.s_from) >= 0.5 * (c.s1 - c.s0) - 1e-9]
    if not idx:  # span shorter than half a cell: vote on the cell holding its middle
        idx = [_cell_index(cells, (span.s_from + span.s_to) / 2)]
    for i in idx:
        c = cells[i]
        if not c.touched:
            if c.state is not FootpathState.unknown and c.votes.get(c.state.value, 0.0) <= 0:
                c.votes[c.state.value] = 0.15
        c.touched = True
        c.votes.pop(FootpathState.unknown.value, None)  # the prior's "unknown" weight is replaced by traced votes
        c.votes[span.state.value] = c.votes.get(span.state.value, 0.0) + span.weight


def _settle_cells(cells: list[_Cell], side: str) -> None:
    """Decide each touched cell's state and keep ``missing`` <=> no modelled-side footpath strip."""
    for c in cells:
        if not c.touched:
            continue
        c.state, _ = _decide_state(c.votes, c.state)
    for i, c in enumerate(cells):
        if not c.touched:
            continue
        has = [s for s in c.strips if s.kind is StripKind.footpath and s.side == side]
        if c.state is FootpathState.missing:
            c.strips = [s for s in c.strips if not (s.kind is StripKind.footpath and s.side == side)]
        elif c.state in _REAL_STATES and not has:
            donors = sorted((abs(j - i), j) for j, o in enumerate(cells) if j != i and any(
                s.kind is StripKind.footpath and s.side == side for s in o.strips) and o.state is not FootpathState.missing)
            if donors:
                d = next(s for s in cells[donors[0][1]].strips if s.kind is StripKind.footpath and s.side == side)
                c.strips.append(_CS(d.kind, d.side, 0, d.material, d.condition, d.height_m, d.t_lo, d.t_hi, 0.15,
                                    d.description, set()))


def _line_fit_ok(cells: list[_Cell], vals: list[float]) -> bool:
    if len(vals) <= 2:
        return True
    x0, x1 = cells[0].mid, cells[-1].mid
    if x1 - x0 < 1e-9:
        return True
    for c, v in zip(cells, vals):
        pred = vals[0] + (vals[-1] - vals[0]) * (c.mid - x0) / (x1 - x0)
        if abs(pred - v) > COLLINEAR_M + 1e-9:
            return False
    return True


def _can_join(group: list[_Cell], nxt: _Cell) -> bool:
    first = group[0]
    if nxt.state != first.state:
        return False
    if nxt.s1 - first.s0 > MAX_INTERVAL_M + 1e-9:
        return False
    cand = group + [nxt]
    keys = [s.key() for s in sorted(first.strips, key=lambda s: s.key())]
    if keys != [s.key() for s in sorted(nxt.strips, key=lambda s: s.key())]:
        return False
    for k in keys:
        per = [next(s for s in c.strips if s.key() == k) for c in cand]
        a = per[0]
        if any((p.material, p.condition, p.height_m) != (a.material, a.condition, a.height_m) for p in per):
            return False
        ws = [p.weight for p in per]
        if max(ws) - min(ws) > 0.25 * max(ws) + 0.05:
            return False
        if not _line_fit_ok(cand, [p.t_lo for p in per]) or not _line_fit_ok(cand, [p.t_hi for p in per]):
            return False
    return True


def _simplify(model: CanonicalModel, cells: list[_Cell], side: str, version: int
              ) -> tuple[list[ProfileInterval], dict[str, tuple[float, float]]]:
    groups: list[list[_Cell]] = []
    for c in cells:
        if groups and _can_join(groups[-1], c):
            groups[-1].append(c)
        else:
            groups.append([c])
    used: set[str] = set()
    out: list[ProfileInterval] = []
    conflicts: dict[str, tuple[float, float]] = {}
    for grp in groups:
        x0, x1 = grp[0].s0, grp[-1].s1
        # id: the old interval overlapping most, if still free
        best = max(model.profile, key=lambda iv: (min(iv.s_to, x1) - max(iv.s_from, x0), -iv.s_from))
        iid = best.id if best.id not in used else "pi-" + _sha(f"{model.segment_slug}|{x0:.1f}|{x1:.1f}|{version}")[:8]
        while iid in used:
            iid += "x"
        used.add(iid)
        xa, xb = grp[0].mid, grp[-1].mid
        strips: list[Strip] = []
        for k in sorted({s.key() for c in grp for s in c.strips}):
            per = [next(s for s in c.strips if s.key() == k) for c in grp]
            lo_v, hi_v = [p.t_lo for p in per], [p.t_hi for p in per]

            def edge(vals: list[float], x: float) -> float:
                if len(vals) == 1 or xb - xa < 1e-9:
                    return sum(vals) / len(vals)
                return vals[0] + (vals[-1] - vals[0]) * (x - xa) / (xb - xa)

            lo = (edge(lo_v, x0), edge(lo_v, x1))
            hi = (edge(hi_v, x0), edge(hi_v, x1))
            hi = (max(hi[0], lo[0]), max(hi[1], lo[1]))
            lens = [c.s1 - c.s0 for c in grp]
            w = sum(p.weight * ln for p, ln in zip(per, lens)) / sum(lens)
            prov = sorted({o for p in per for o in p.prov})
            a = per[0]
            letter = {"left": "L", "right": "R", "centre": "C"}[a.side]
            sid = f"sp-{a.kind.value}-{letter}-{iid}" + (f"-{a.ordinal}" if a.ordinal else "")
            strips.append(Strip(id=sid, kind=a.kind, side=a.side, material=a.material, condition=a.condition,
                                height_m=a.height_m, t_lo=lo, t_hi=hi, weight=round(w, 4),
                                confidence=round(min(0.95, 1 - math.exp(-w)), 4),
                                description=next((p.description for p in per if p.description), ""), provenance=prov))
        strips.sort(key=lambda s: (s.t_lo[0], s.t_hi[0], s.kind.value, s.side))
        keys = sorted({k for c in grp for k in c.votes})
        votes = {k: round(sum(c.votes.get(k, 0.0) for c in grp) / len(grp), 4) for k in keys}
        state = grp[0].state
        _, conflict = _decide_state(votes, state)
        if conflict:
            conflicts[iid] = (x0, x1)
        n_obs = len({o for s in strips if s.kind is StripKind.footpath and s.side == side for o in s.provenance})
        out.append(ProfileInterval(
            id=iid, s_from=x0, s_to=x1, footpath_state=state, state_votes=votes, strips=strips,
            description=f"{x0:.0f}-{x1:.0f} m: {state.value} footpath.",
            evidence_summary=f"{n_obs} observation{'s' if n_obs != 1 else ''} fused." if n_obs else "Map prior only."))
    return out, conflicts


def _fold_profile(model: CanonicalModel, strips: Sequence[StripSample], states: Sequence[StateSpan], version: int
                  ) -> tuple[list[ProfileInterval], dict[str, tuple[float, float]]]:
    cells = _build_cells(model)
    for smp in sorted(strips, key=lambda x: (x.s, x.kind.value, x.side, x.observation_id, x.t_lo, x.t_hi)):
        if smp.weight <= 0 or smp.t_hi < smp.t_lo - 1e-9:
            continue
        _fold_strip(cells[_cell_index(cells, smp.s)], smp)
    side = model.footpath_side
    for span in sorted(states, key=lambda x: (x.s_from, x.s_to, x.observation_id, x.state.value)):
        if span.side != side or span.weight <= 0 or span.s_to <= span.s_from:
            continue
        _fold_state(cells, span)
    _settle_cells(cells, side)
    return _simplify(model, cells, side, version)


# ------------------------------------------------------------------ working context


class _Work:
    def __init__(self, base: CanonicalModel, version: int):
        self.base = base
        self.version = version
        self.slug = base.segment_slug
        self.entities: dict[str, Entity] = {e.id: e for e in base.model_copy(deep=True).entities}
        self.buildings: dict[str, Building] = {b.id: b for b in base.model_copy(deep=True).buildings}
        self.contradictions: dict[str, Contradiction] = {c.id: c for c in base.model_copy(deep=True).contradictions}
        self.profile: list[ProfileInterval] = base.model_copy(deep=True).profile
        self.touched: dict[str, dict[str, int]] = {}
        self.reopened: set[str] = set()
        self.new_ctr: set[str] = set()
        self.resolved_ctr: set[str] = set()
        self.profile_dirty = False
        self.seen_keys: set[tuple[str, str]] = {
            (x.observation_id, x.detection_key) for e in self.entities.values() for x in e.provenance
            if x.detection_key is not None}
        self.minted: dict[tuple[str, str], int] = {}
        for e in self.entities.values():
            first = next((x for x in e.provenance if x.role is EvidenceRole.supports), None) or (
                e.provenance[0] if e.provenance else None)
            if first is not None:
                k = (str(e.type), first.observation_id)
                self.minted[k] = self.minted.get(k, 0) + 1
        self.built_model = base  # for profile lookups while building facades

    # ---- entity helpers
    def touch(self, eid: str, what: str) -> None:
        d = self.touched.setdefault(eid, {})
        d[what] = d.get(what, 0) + 1

    def mint(self, det: ProjectedDetection) -> str:
        k = (str(det.type), det.observation_id)
        ordinal = self.minted.get(k, 0)
        eid = entity_id(self.slug, det.type, det.observation_id, det.s, det.t, ordinal)
        while eid in self.entities:
            ordinal += 1
            eid = entity_id(self.slug, det.type, det.observation_id, det.s, det.t, ordinal)
        self.minted[k] = ordinal + 1
        return eid

    def new_entity(self, det: ProjectedDetection) -> Entity:
        p = calibrate_confidence(det.raw_confidence)
        w = max(det.weight, 0.0)
        lg = clamp_logit(w * logit(p))
        eid = self.mint(det)
        e = Entity(
            id=eid, type=det.type, subtype=det.subtype, state=EntityState.present,
            position=Position(s=det.s, t=det.t), position_sigma_m=max(det.sigma_m, 0.05),
            extent=Extent(length_s=max(det.extent_s, 0.0), width_t=max(det.extent_t, 0.0)),
            height_m=det.height_m, orientation_deg=det.orientation_deg, attributes=dict(det.attributes),
            description=det.description, confidence=confidence_from_logit(lg), logit=lg,
            provenance=[Evidence(observation_id=det.observation_id, role=EvidenceRole.supports,
                                 detection_key=det.key, crop_px=det.crop_px, weight=w, added_in_version=self.version)],
            billboard=Billboard(observation_id=det.observation_id, crop_px=det.crop_px) if det.crop_px else None,
            first_seen_version=self.version, last_changed_version=self.version)
        self.entities[eid] = e
        self.seen_keys.add((det.observation_id, det.key))
        self.touch(eid, "new")
        return e

    def support(self, e: Entity, det: ProjectedDetection) -> None:
        w = max(det.weight, 0.0)
        sup = [x for x in e.provenance if x.role is EvidenceRole.supports]
        k = len(sup)
        top = max((x.weight for x in sup), default=0.0)
        # attributes (before the evidence list changes)
        w_old = sum(x.weight for x in sup)
        for key in sorted(det.attributes):
            val = det.attributes[key]
            if key not in e.attributes:
                e.attributes[key] = val
            elif e.attributes[key] != val:
                if w_old >= 0.5 and w >= 0.5:
                    cid = contradiction_id("attribute_conflict", e.id, key)
                    if cid not in self.contradictions:
                        self.contradictions[cid] = Contradiction(
                            id=cid, kind=ContradictionKind.attribute_conflict, entity_ids=[e.id],
                            interval=None, description=f"{e.type} {e.id}: attribute '{key}' is "
                                                       f"{e.attributes[key]!r} in some views and {val!r} in others.",
                            observation_ids=sorted({x.observation_id for x in sup} | {det.observation_id}),
                            opened_in_version=self.version)
                        self.new_ctr.add(cid)
                if w > w_old:
                    e.attributes[key] = val
        # position / size: precision-weighted running mean
        wo, wn = 1.0 / e.position_sigma_m ** 2, 1.0 / max(det.sigma_m, 0.05) ** 2
        tot = wo + wn
        e.position = Position(s=(e.position.s * wo + det.s * wn) / tot, t=(e.position.t * wo + det.t * wn) / tot)
        e.position_sigma_m = max(1.0 / math.sqrt(tot), 0.05)
        e.extent = Extent(length_s=max((e.extent.length_s * wo + max(det.extent_s, 0.0) * wn) / tot, 0.0),
                          width_t=max((e.extent.width_t * wo + max(det.extent_t, 0.0) * wn) / tot, 0.0))
        if det.height_m is not None:
            e.height_m = det.height_m if e.height_m is None else (e.height_m * wo + det.height_m * wn) / tot
        if det.orientation_deg is not None:
            if e.orientation_deg is None:
                e.orientation_deg = det.orientation_deg
            else:
                a, b = math.radians(e.orientation_deg), math.radians(det.orientation_deg)
                x, y = wo * math.cos(a) + wn * math.cos(b), wo * math.sin(a) + wn * math.sin(b)
                e.orientation_deg = math.degrees(math.atan2(y, x)) % 360.0
        if e.type is EntityType.other and det.type is not EntityType.other:
            e.type = det.type
        if e.subtype is None and det.subtype:
            e.subtype = det.subtype
        if det.description and w >= 1.25 * top:
            e.description = det.description
        best_crop = max((x.weight for x in sup if x.crop_px is not None), default=-1.0)
        if det.crop_px is not None and (e.billboard is None or w > best_crop):
            e.billboard = Billboard(observation_id=det.observation_id, crop_px=det.crop_px)
        # confidence
        p = calibrate_confidence(det.raw_confidence)
        e.logit = clamp_logit(e.logit + w * SUPPORT_DECAY ** k * logit(p))
        e.confidence = confidence_from_logit(e.logit)
        e.provenance.append(Evidence(observation_id=det.observation_id, role=EvidenceRole.supports,
                                     detection_key=det.key, crop_px=det.crop_px, weight=w,
                                     added_in_version=self.version))
        self.seen_keys.add((det.observation_id, det.key))
        e.last_changed_version = self.version
        self.touch(e.id, "support")

    def absence(self, e: Entity, obs_id: str, v: float) -> None:
        e.logit = clamp_logit(e.logit - ABSENCE_FACTOR * v)
        e.confidence = confidence_from_logit(e.logit)
        e.provenance.append(Evidence(observation_id=obs_id, role=EvidenceRole.contradicts, detection_key=None,
                                     crop_px=None, weight=v, added_in_version=self.version,
                                     note="inside the visible region but not detected"))
        e.last_changed_version = self.version
        self.touch(e.id, "absence")

    # ---- one observation's detections
    def merge_observation(self, obs_id: str, dets: list[ProjectedDetection]) -> set[str]:
        snapshot = list(self.entities.values())
        pairs: list[tuple[float, str, str]] = []
        for det in dets:
            for e in snapshot:
                if not _compatible(e.type, det.type):
                    continue
                if any(x.observation_id == obs_id and x.role is EvidenceRole.supports for x in e.provenance):
                    continue
                d = math.hypot(det.s - e.position.s, det.t - e.position.t)
                if d <= _gate(det.sigma_m, e.position_sigma_m, max(det.extent_s, det.extent_t), _ent_extent(e)):
                    pairs.append((d, det.key, e.id))
        pairs.sort()
        by_key = {d.key: d for d in dets}
        used_ent: set[str] = set()
        used_det: set[str] = set()
        involved: set[str] = set()
        for _, key, eid in pairs:
            if key in used_det or eid in used_ent:
                continue
            used_det.add(key)
            used_ent.add(eid)
            self.support(self.entities[eid], by_key[key])
            involved.add(eid)
        for det in dets:
            if det.key not in used_det:
                involved.add(self.new_entity(det).id)
        return involved

    def merge_absence(self, obs_id: str, regions: list) -> None:
        regs = [r for r in regions if r.observation_id == obs_id and r.weight >= ABSENCE_MIN_VISIBLE]
        if not regs:
            return
        for e in list(self.entities.values()):
            if any(x.observation_id == obs_id for x in e.provenance):
                continue  # supported by this observation (or already contradicted by it): unique per observation
            v = max((r.weight for r in regs if r.s_from <= e.position.s <= r.s_to and _in_side(e.position.t, r.side)),
                    default=0.0)
            if v >= ABSENCE_MIN_VISIBLE:
                self.absence(e, obs_id, v)

    # ---- contradictions
    def _sync(self, kinds: set[ContradictionKind], desired: dict[str, Contradiction]) -> None:
        for cid in sorted(desired):
            want = desired[cid]
            have = self.contradictions.get(cid)
            if have is None:
                self.contradictions[cid] = want
                self.new_ctr.add(cid)
            elif have.resolved_in_version is not None:
                have.resolved_in_version = None
                have.description = want.description
                have.observation_ids = want.observation_ids
                self.reopened.add(cid)
            else:
                have.description, have.interval, have.observation_ids = want.description, want.interval, want.observation_ids
        for cid, c in self.contradictions.items():
            if c.kind in kinds and c.resolved_in_version is None and cid not in desired:
                c.resolved_in_version = self.version
                self.resolved_ctr.add(cid)

    def refresh_entity_contradictions(self) -> None:
        ents = sorted(self.entities.values(), key=lambda e: e.id)
        desired: dict[str, Contradiction] = {}
        live = [e for e in ents if e.state is not EntityState.removed and e.confidence >= 0.5]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                if a.type == b.type or EntityType.other in (a.type, b.type):
                    continue
                d = math.hypot(a.position.s - b.position.s, a.position.t - b.position.t)
                if d <= _gate(a.position_sigma_m, b.position_sigma_m, _ent_extent(a), _ent_extent(b)):
                    cid = contradiction_id("type_conflict", a.id, b.id)
                    desired[cid] = Contradiction(
                        id=cid, kind=ContradictionKind.type_conflict, entity_ids=[a.id, b.id],
                        interval=None, description=f"{a.type} and {b.type} detected {d:.1f} m apart "
                                                   f"near s={(a.position.s + b.position.s) / 2:.1f} m.",
                        observation_ids=sorted({x.observation_id for x in a.supports() + b.supports()}),
                        opened_in_version=self.version)
        self._sync({ContradictionKind.type_conflict}, desired)
        pres: dict[str, Contradiction] = {}
        for e in ents:
            if e.state is EntityState.disputed:
                cid = contradiction_id("presence_conflict", e.id)
                pres[cid] = Contradiction(
                    id=cid, kind=ContradictionKind.presence_conflict, entity_ids=[e.id], interval=None,
                    description=f"{e.type} {e.id} was seen in {len(e.supports())} view(s) but not found in "
                                f"{len({x.observation_id for x in e.contradictions()})} others that should show it.",
                    observation_ids=sorted({x.observation_id for x in e.provenance
                                            if x.role in (EvidenceRole.supports, EvidenceRole.contradicts)}),
                    opened_in_version=self.version)
        self._sync({ContradictionKind.presence_conflict}, pres)

    def refresh_state_contradictions(self, conflicts: dict[str, tuple[float, float]]) -> None:
        desired = {}
        for iid, (a, b) in conflicts.items():
            cid = contradiction_id("footpath_state_conflict", iid)
            desired[cid] = Contradiction(
                id=cid, kind=ContradictionKind.footpath_state_conflict, entity_ids=[], interval=(a, b),
                description=f"Views disagree about the footpath between {a:.0f} m and {b:.0f} m "
                            f"(runner-up state has >= 80% of the leader's evidence).",
                observation_ids=[], opened_in_version=self.version)
        self._sync({ContradictionKind.footpath_state_conflict}, desired)

    # ---- buildings
    def add_facade(self, f: FacadeDetection) -> None:
        if f.s_to <= f.s_from:
            return
        for b in self.buildings.values():
            if any(x.observation_id == f.observation_id and x.crop_px == f.crop_px for x in b.facades):
                return
        best, best_ov = None, 0.0
        for b in sorted(self.buildings.values(), key=lambda b: b.id):
            if b.side != f.side:
                continue
            s_lo, s_hi = min(p[0] for p in b.footprint), max(p[0] for p in b.footprint)
            ov = min(s_hi, f.s_to) - max(s_lo, f.s_from)
            if ov >= 0.5 * min(s_hi - s_lo, f.s_to - f.s_from) and ov > best_ov:
                best, best_ov = b, ov
        fac = BuildingFacade(observation_id=f.observation_id, crop_px=f.crop_px, s_from=f.s_from, s_to=f.s_to,
                             weight=max(f.weight, 0.0))
        colour = f.colour_hex if f.colour_hex and _HEX.match(f.colour_hex) else None
        if best is None:
            mid = (f.s_from + f.s_to) / 2
            iv = self.base.interval_at(mid)
            if f.side == "left":
                near = max([s.t_hi[0] for s in iv.strips if s.side == "left"] + [6.0]) + 0.5
                fp = [(f.s_from, near), (f.s_to, near), (f.s_to, near + BUILDING_DEPTH_M), (f.s_from, near + BUILDING_DEPTH_M)]
            else:
                near = -(max([-s.t_lo[0] for s in iv.strips if s.side == "right"] + [6.0]) + 0.5)
                fp = [(f.s_from, near), (f.s_to, near), (f.s_to, near - BUILDING_DEPTH_M), (f.s_from, near - BUILDING_DEPTH_M)]
            bid = building_id(segment_slug=self.slug, side=f.side, s_center=mid)
            n = 0
            while bid in self.buildings:  # same id, but not matched above: keep ids unique and never overwrite
                n += 1
                bid = building_id(segment_slug=self.slug, side=f.side, s_center=mid) + f"-{n}"
            conf = min(0.95, 1 - math.exp(-fac.weight)) if fac.weight > 0 else 0.3
            self.buildings[bid] = Building(
                id=bid, source="vision", side=f.side, footprint=fp,
                height_m=f.storeys * STOREY_M if f.storeys else DEFAULT_BUILDING_HEIGHT_M,
                height_confidence=0.5 if f.storeys else 0.3, storeys=f.storeys, colour_hex=colour, facades=[fac],
                description=f.description, confidence=round(conf, 4), provenance=[f.observation_id])
            return
        b = best
        prev_top = max((x.weight for x in b.facades), default=0.0)
        b.facades.append(fac)
        if colour and (b.colour_hex is None or fac.weight > prev_top):
            b.colour_hex = colour
        if f.storeys and b.storeys is None:
            b.storeys = f.storeys
            if b.source != "osm":
                b.height_m = f.storeys * STOREY_M
                b.height_confidence = 0.5
        if f.description and (not b.description or fac.weight >= 1.25 * prev_top):
            b.description = f.description
        if b.source == "osm":
            b.source = "mixed"
        if f.observation_id not in b.provenance:
            b.provenance = sorted([*b.provenance, f.observation_id])
        total = sum(x.weight for x in b.facades)
        b.confidence = round(max(b.confidence, min(0.95, 1 - math.exp(-total))), 4)


# ------------------------------------------------------------------ merge_batches


def merge_batches(base: CanonicalModel, batches: Sequence[ProjectedBatch], *, new_version: int,
                  run_id: str | None, now: datetime) -> MergeResult:
    w = _Work(base, new_version)
    fused_list = list(base.fused_observation_ids)
    fused = set(fused_list)
    ignored: list[str] = []
    all_conflicts: dict[str, tuple[float, float]] | None = None

    for batch in sorted(batches, key=_batch_key):
        if all(o in fused for o in batch.observation_ids):
            ignored.append(batch.batch_id)
            continue
        frozen = set(fused)  # observations already counted before this batch
        obs_ids = sorted(batch.observation_ids)
        by_obs: dict[str, list[ProjectedDetection]] = {}
        for d in sorted(batch.detections, key=lambda d: (d.observation_id, d.key)):
            if (d.observation_id, d.key) in w.seen_keys:
                continue
            by_obs.setdefault(d.observation_id, []).append(d)
        for o in sorted(set(by_obs) | {o for o in obs_ids if o not in frozen}):
            if o in frozen and o not in by_obs:
                continue
            w.merge_observation(o, by_obs.get(o, []))
            if o not in frozen:
                w.merge_absence(o, list(batch.visible))
        # profile evidence
        strips = [s for s in batch.strips if s.observation_id not in frozen]
        states = [s for s in batch.states if s.observation_id not in frozen]
        if strips or states:
            cur = CanonicalModel.model_construct(**{**dict(w.base), "profile": w.profile})
            w.profile, all_conflicts = _fold_profile(cur, strips, states, new_version)
            w.profile_dirty = True
            w.refresh_state_contradictions(all_conflicts)
        for f in sorted((f for f in batch.facades if f.observation_id not in frozen),
                        key=lambda f: (f.s_from, f.s_to, f.side, f.observation_id, f.crop_px)):
            w.add_facade(f)
        for o in batch.observation_ids:
            if o not in fused:
                fused.add(o)
                fused_list.append(o)

    # states of touched entities, then contradictions derived from entities
    for eid in sorted(w.touched):
        e = w.entities[eid]
        e.state = _derive_state(e, base.entity(eid).state if base.entity(eid) else EntityState.present, new_version)
    if w.touched:
        w.refresh_entity_contradictions()

    model = CanonicalModel(
        segment_slug=base.segment_slug, version=new_version, parent_version=base.version, kind=ModelKind.fused,
        run_id=run_id, created_at=now, legacy=base.legacy, placement_verified=base.placement_verified,
        footpath_side=base.footpath_side, reference_line=base.reference_line, profile=w.profile,
        entities=list(w.entities.values()), buildings=list(w.buildings.values()),
        contradictions=list(w.contradictions.values()), fused_observation_ids=fused_list, fidelity=None)
    model = model.model_copy(update={"stats": model.computed_stats()})
    return MergeResult(model=model, changes=_changes(base, model, w), ignored_batches=ignored)


# ------------------------------------------------------------------ change records


def _ctr_brief(c: Contradiction) -> dict:
    return {"kind": str(c.kind), "description": c.description, "entity_ids": list(c.entity_ids),
            "interval": list(c.interval) if c.interval else None}


def _reason(t: dict[str, int]) -> str:
    bits = []
    if t.get("support"):
        bits.append(f"{t['support']} more supporting view{'s' if t['support'] != 1 else ''}")
    if t.get("absence"):
        bits.append(f"not seen in {t['absence']} view{'s' if t['absence'] != 1 else ''} that cover it")
    return ", ".join(bits) or "updated"


def _profile_changes(old: CanonicalModel, new: CanonicalModel, reason: str) -> list[ChangeRecord]:
    from hero.model.diff import _strip_changes  # local import: diff imports only types

    out: list[ChangeRecord] = []
    for iv in new.profile:
        mid = (iv.s_from + iv.s_to) / 2
        o = old.interval_at(mid)
        fo = _clip((mid - o.s_from) / o.length, 0.0, 1.0)
        fn = 0.5
        what = _strip_changes(o, iv, fo, fn)
        span = {"s_from": round(iv.s_from, 2), "s_to": round(iv.s_to, 2)}
        if o.footpath_state != iv.footpath_state:
            out.append(ChangeRecord("state_changed", iv.id, {**span, "state": str(o.footpath_state)},
                                    {**span, "state": str(iv.footpath_state)}, reason))
        rest = [x for x in what if not x.startswith("footpath_state")]
        if rest:
            out.append(ChangeRecord("strip_changed", iv.id, {**span}, {**span, "changes": rest}, reason))
    return out


def _changes(base: CanonicalModel, model: CanonicalModel, w: _Work) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    bmap = {e.id: e for e in base.entities}
    for e in model.entities:
        o = bmap.get(e.id)
        if o is None:
            out.append(ChangeRecord("entity_added", e.id, None, _ent_brief(e), "first detected"))
            continue
        if o.state != e.state:
            out.append(ChangeRecord("entity_state", e.id, {"state": str(o.state), "confidence": round(o.confidence, 4)},
                                    {"state": str(e.state), "confidence": round(e.confidence, 4)}, _reason(w.touched.get(e.id, {}))))
        ex = {"state", "last_changed_version"}
        if o.model_dump(exclude=ex) != e.model_dump(exclude=ex):
            out.append(ChangeRecord("entity_updated", e.id, _ent_brief(o), _ent_brief(e), _reason(w.touched.get(e.id, {}))))
    bb = {b.id: b for b in base.buildings}
    for b in model.buildings:
        o = bb.get(b.id)
        if o is None:
            out.append(ChangeRecord("building_added", b.id, None, {"side": b.side, "source": b.source,
                                                                      "height_m": b.height_m, "confidence": b.confidence},
                                    "facade detected"))
        elif o != b:
            out.append(ChangeRecord("building_updated", b.id, {"facades": len(o.facades), "confidence": o.confidence},
                                    {"facades": len(b.facades), "confidence": b.confidence}, "more facade evidence"))
    if w.profile_dirty:
        out.extend(_profile_changes(base, model, "new footpath/profile evidence"))
    bc = {c.id: c for c in base.contradictions}
    for c in model.contradictions:
        o = bc.get(c.id)
        if (o is None and c.is_open) or (o is not None and not o.is_open and c.is_open):
            out.append(ChangeRecord("contradiction_opened", c.id, None, _ctr_brief(c), c.description))
        elif o is not None and o.is_open and not c.is_open:
            out.append(ChangeRecord("contradiction_resolved", c.id, _ctr_brief(o), _ctr_brief(c),
                                    f"resolved in version {c.resolved_in_version}"))
    return out


# ------------------------------------------------------------------ critique


def apply_critique(model: CanonicalModel, ops: Sequence[CritiqueOp], *, run_id: str | None, now: datetime
                   ) -> tuple[CanonicalModel, list[ChangeRecord], list[str]]:
    """Apply review ops as low-weight ``critique`` evidence. Never deletes; out-of-bound / unknown ops are skipped
    (and listed). Per entity the total confidence change is capped at 0.15; nudges at 3 m (edges 1 m)."""
    base = model
    m = model.model_copy(deep=True)
    ents = {e.id: e for e in m.entities}
    changes: list[ChangeRecord] = []
    skipped: list[str] = []
    conf_used: dict[str, float] = {}
    v = m.version

    def add_evidence(e: Entity, op) -> bool:
        key = f"{op.op}:{_sha(op.model_dump_json())[:8]}"
        if any(x.role is EvidenceRole.critique and x.detection_key == key for x in e.provenance):
            skipped.append(f"{op.op} on {e.id}: duplicate of an op already applied")
            return False
        e.provenance.append(Evidence(observation_id="critique", role=EvidenceRole.critique, detection_key=key,
                                     weight=CRITIQUE_WEIGHT, added_in_version=v, note=op.reason or None))
        e.last_changed_version = v
        return True

    state_spans: list[StateSpan] = []
    state_reasons: list[str] = []
    for op in ops:
        if isinstance(op, FlagEntityOp):
            e = ents.get(op.entity_id)
            if e is None:
                skipped.append(f"flag_entity: unknown entity {op.entity_id}")
                continue
            remaining = CRITIQUE_MAX_CONF_DELTA - conf_used.get(e.id, 0.0)
            if remaining <= 1e-9:
                skipped.append(f"flag_entity on {e.id}: critique confidence cap ({CRITIQUE_MAX_CONF_DELTA}) reached")
                continue
            before = _ent_brief(e)
            if not add_evidence(e, op):
                continue
            delta = min(remaining, CRITIQUE_WEIGHT * 0.5, max(e.confidence - 0.02, 0.0))
            conf_used[e.id] = conf_used.get(e.id, 0.0) + delta
            e.confidence = _clip(e.confidence - delta, 0.02, 0.98)
            e.logit = clamp_logit(logit(e.confidence))
            if e.state is EntityState.present and REMOVE_CONF <= e.confidence < DISPUTE_CONF:
                e.state = EntityState.disputed
            changes.append(ChangeRecord("entity_updated", e.id, before, _ent_brief(e), op.reason or "flagged by review"))
        elif isinstance(op, NudgeEntityOp):
            e = ents.get(op.entity_id)
            if e is None:
                skipped.append(f"nudge_entity: unknown entity {op.entity_id}")
                continue
            if abs(op.ds_m) > CRITIQUE_MAX_NUDGE_M or abs(op.dt_m) > CRITIQUE_MAX_NUDGE_M:
                skipped.append(f"nudge_entity on {e.id}: |nudge| exceeds {CRITIQUE_MAX_NUDGE_M} m")
                continue
            before = _ent_brief(e)
            if not add_evidence(e, op):
                continue
            e.position = Position(s=_clip(e.position.s + op.ds_m, 0.0, m.length_s), t=e.position.t + op.dt_m)
            changes.append(ChangeRecord("entity_updated", e.id, before, _ent_brief(e), op.reason or "nudged by review"))
        elif isinstance(op, SetFootpathStateOp):
            if op.side != m.footpath_side:
                skipped.append(f"set_footpath_state: side {op.side} is not the modelled side ({m.footpath_side})")
            elif op.state is FootpathState.unknown:
                skipped.append("set_footpath_state: 'unknown' carries no evidence")
            elif op.s_to <= op.s_from or op.s_to <= 0 or op.s_from >= m.length_s:
                skipped.append(f"set_footpath_state: range {op.s_from}-{op.s_to} m outside the street")
            else:
                state_spans.append(StateSpan(observation_id="critique", side=op.side, s_from=max(op.s_from, 0.0),
                                             s_to=min(op.s_to, m.length_s), state=op.state, weight=CRITIQUE_WEIGHT))
                state_reasons.append(op.reason or "footpath state set by review")
        elif isinstance(op, NudgeEdgeOp):
            if abs(op.dt_m) > CRITIQUE_MAX_EDGE_M:
                skipped.append(f"nudge_edge: |dt| exceeds {CRITIQUE_MAX_EDGE_M} m")
                continue
            if op.s_to <= op.s_from:
                skipped.append("nudge_edge: empty range")
                continue
            hit = 0
            for iv in m.profile:
                ov = min(iv.s_to, op.s_to) - max(iv.s_from, op.s_from)
                if ov <= 0 or not (ov >= 0.5 * iv.length or (iv.s_from <= op.s_from and op.s_to <= iv.s_to)):
                    continue
                for i, st in enumerate(iv.strips):
                    if st.kind == op.kind and st.side == op.side:
                        if op.side == "right":
                            lo = (min(st.t_lo[0] + op.dt_m, st.t_hi[0]), min(st.t_lo[1] + op.dt_m, st.t_hi[1]))
                            hi = st.t_hi
                        else:
                            lo = st.t_lo
                            hi = (max(st.t_hi[0] + op.dt_m, st.t_lo[0]), max(st.t_hi[1] + op.dt_m, st.t_lo[1]))
                        iv.strips[i] = st.model_copy(update={"t_lo": lo, "t_hi": hi,
                                                              "provenance": sorted({*st.provenance, "critique"})})
                        hit += 1
                        changes.append(ChangeRecord("strip_changed", iv.id, {"t_lo": list(st.t_lo), "t_hi": list(st.t_hi)},
                                                    {"t_lo": list(lo), "t_hi": list(hi)}, op.reason or "edge nudged by review"))
            if not hit:
                skipped.append(f"nudge_edge: no {op.kind} strip on side {op.side} in {op.s_from}-{op.s_to} m")
        else:  # pragma: no cover - unreachable with the discriminated union
            skipped.append(f"unsupported op {op!r}")

    profile = m.profile
    if state_spans:
        profile, _ = _fold_profile(m, [], state_spans, v)
        tmp = CanonicalModel.model_construct(**{**dict(m), "profile": profile})
        changes.extend(_profile_changes(m, tmp, "; ".join(dict.fromkeys(state_reasons))))
    out = CanonicalModel(
        segment_slug=m.segment_slug, version=m.version, parent_version=m.parent_version, kind=m.kind,
        run_id=run_id if run_id is not None else m.run_id, created_at=now if changes else m.created_at,
        legacy=m.legacy, placement_verified=m.placement_verified, footpath_side=m.footpath_side,
        reference_line=m.reference_line, profile=profile, entities=m.entities, buildings=m.buildings,
        contradictions=m.contradictions, fused_observation_ids=m.fused_observation_ids, fidelity=None)
    out = out.model_copy(update={"stats": out.computed_stats()})
    return out, changes, skipped
