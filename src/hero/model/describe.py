"""Deterministic plain-language text for the model (spec/capabilities/street-description.md).

Everything here is a template over structured data - no LLM, no clock - so the same input always yields the same
sentence and every number in a sentence equals the number in the structured field it came from. Perception's own
per-item ``description`` always wins; these templates fill the gaps (``strip_text``, ``interval_text``,
``entity_text``) and write the aggregate texts (``about_summary``, ``version_summary``, ``improve_hint``).

Interpretation notes
--------------------
* ``about_summary`` pluralises "photo(s)" by count, so a one-photo street says "1 photo".
* Low-confidence parts are computed per fidelity bin (5 m aligned) and merged when adjacent: a bin is flagged when its
  fidelity is L0/L1, when a present/disputed entity inside it has confidence < 0.6, or when an open contradiction
  touches it (via its entities or its interval).
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from hero.model.fidelity import LEVEL_LABELS
from hero.model.scene_v2 import LowConfidencePart
from hero.model.types import (
    CanonicalModel,
    Entity,
    EntityState,
    EvidenceRole,
    FidelityMap,
    ModelDiff,
    ModelKind,
    Observation,
    ObservationKind,
    ProfileInterval,
    Strip,
    StripKind,
)

LOW_CONFIDENCE = 0.6
LEGACY_NOTE = "legacy, placement unverified"
MAP_ONLY_NOTE = "map-only sketch"


def _num(x: float) -> str:
    return f"{x:.0f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _words(name: str) -> str:
    return name.replace("_", " ")


def fidelity_label(level: int) -> str:
    return LEVEL_LABELS[level]


# ------------------------------------------------------------------ strips, intervals


def strip_text(strip: Strip, s_from: float, s_to: float) -> str:
    """e.g. ``Paver footpath about 1.5 m wide, broken from 22 to 31 m``."""
    material = "" if strip.material.value == "unknown" else strip.material.value + " "
    base = f"{material}{_words(strip.kind.value)}".strip()
    text = f"{base[:1].upper()}{base[1:]} about {strip.width_mean:.1f} m wide"
    if strip.condition.value != "unknown":
        text += f", {strip.condition.value}"
    return f"{text} from {_num(s_from)} to {_num(s_to)} m"


def interval_text(iv: ProfileInterval, side: str) -> str:
    """e.g. ``22-31 m: broken footpath on the left side; carriageway about 8.5 m wide.``"""
    head = f"{_num(iv.s_from)}-{_num(iv.s_to)} m: "
    state = iv.footpath_state.value
    if state == "missing":
        fp = f"no footpath on the {side} side"
    elif state == "unknown":
        fp = f"footpath on the {side} side not yet traced"
    else:
        fp = f"{state} footpath on the {side} side"
    carr = [s for s in iv.strips if s.kind is StripKind.carriageway]
    tail = f"; carriageway about {max(c.width_mean for c in carr):.1f} m wide" if carr else ""
    return f"{head}{fp}{tail}."


# ------------------------------------------------------------------ entities


def entity_label(e: Entity) -> str:
    base = _words(e.type.value)
    return f"{e.subtype} ({base})" if e.subtype else base


def entity_text(e: Entity) -> str:
    """Fallback description from the catalogue type + attributes (used when perception gave none)."""
    parts = []
    colour = e.attributes.get("colour")
    label = e.subtype or _words(e.type.value)
    parts.append(f"{str(colour) + ' ' if isinstance(colour, str) else ''}{label}".strip())
    text = f"{parts[0][:1].upper()}{parts[0][1:]} about {_num(e.position.s)} m from the start"
    notes = [_words(k) for k in sorted(e.attributes) if e.attributes[k] is True]
    if notes:
        text += " (" + ", ".join(notes) + ")"
    return text + "."


def _source_bucket(kind: ObservationKind | None) -> str:
    if kind is ObservationKind.user_photo:
        return "upload"
    if kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop):
        return "satellite"
    return "street_view"


def evidence_counts(e: Entity, observations: Mapping[str, Observation] | None = None) -> dict[str, int]:
    """Distinct supporting observations by source bucket (unknown observations count as Street View)."""
    seen = sorted({p.observation_id for p in e.provenance if p.role is EvidenceRole.supports})
    out = {"street_view": 0, "satellite": 0, "upload": 0}
    for oid in seen:
        obs = (observations or {}).get(oid)
        out[_source_bucket(obs.kind if obs else None)] += 1
    return out


def evidence_summary(e: Entity, observations: Mapping[str, Observation] | None = None, *, legacy: bool = False) -> str:
    counts = evidence_counts(e, observations)
    bits = []
    if counts["street_view"]:
        bits.append(_plural(counts["street_view"], "Street View frame"))
    if counts["satellite"]:
        bits.append(_plural(counts["satellite"], "satellite image"))
    if counts["upload"]:
        bits.append(_plural(counts["upload"], "uploaded photo"))
    if bits:
        text = "Seen in " + ", ".join(bits)
    elif legacy:
        text = "From the first-generation scene; no supporting photos (" + LEGACY_NOTE + ")"
    else:
        text = "No supporting photos yet"
    against = len({p.observation_id for p in e.provenance if p.role is EvidenceRole.contradicts})
    if against:
        text += f"; not visible in {_plural(against, 'later view')}"
    if e.state is EntityState.disputed:
        text += "; disputed"
    return text + "."


# ------------------------------------------------------------------ low-confidence parts


def _bin_index(fidelity: FidelityMap, s: float) -> int:
    if not fidelity.bins:
        return 0
    for b in fidelity.bins:
        if s < b.s_to:
            return b.index
    return fidelity.bins[-1].index


def low_confidence_parts(model: CanonicalModel, fidelity: FidelityMap) -> list[LowConfidencePart]:
    n = len(fidelity.bins)
    level: dict[int, int] = {b.index: b.level for b in fidelity.bins}
    ents: dict[int, set[str]] = {i: set() for i in range(n)}
    ctrs: dict[int, set[str]] = {i: set() for i in range(n)}
    by_id = {e.id: e for e in model.entities}
    for e in model.entities:
        if e.state is not EntityState.removed and e.confidence < LOW_CONFIDENCE:
            ents[_bin_index(fidelity, e.position.s)].add(e.id)
    for c in model.contradictions:
        if not c.is_open:
            continue
        touched: set[int] = set()
        for eid in c.entity_ids:
            if eid in by_id:
                touched.add(_bin_index(fidelity, by_id[eid].position.s))
        if c.interval is not None:
            for b in fidelity.bins:
                if b.s_from < c.interval[1] and b.s_to > c.interval[0]:
                    touched.add(b.index)
        for i in touched:
            ctrs[i].add(c.id)
    flagged = [i for i in range(n) if level.get(i, 0) <= 1 or ents[i] or ctrs[i]]
    parts: list[LowConfidencePart] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        lv = {level.get(i, 0) for i in run if level.get(i, 0) <= 1}
        e_ids = set().union(*(ents[i] for i in run))
        c_ids = set().union(*(ctrs[i] for i in run))
        why = []
        if lv:
            why.append("no street photos yet (map only)" if 0 in lv else "seen from one viewpoint only")
        if e_ids:
            why.append(f"{_plural(len(e_ids), 'object')} below {LOW_CONFIDENCE * 100:.0f} % confidence")
        if c_ids:
            why.append(f"{_plural(len(c_ids), 'unresolved conflict')}")
        parts.append(LowConfidencePart(s_from=fidelity.bins[run[0]].s_from, s_to=fidelity.bins[run[-1]].s_to,
                                       reason="; ".join(why)))

    for i in flagged:
        if run and i != run[-1] + 1:
            flush()
            run = []
        run.append(i)
    flush()
    return parts


def _span(parts: Sequence[LowConfidencePart]) -> str:
    return ", ".join(f"{_num(p.s_from)}-{_num(p.s_to)} m" for p in parts)


def improve_hint(fidelity: FidelityMap) -> str:
    """Where one more photo would help most: the first run of bins at the lowest level."""
    if not fidelity.bins:
        return ""
    low = min(b.level for b in fidelity.bins)
    if low >= 3:
        return "Every part of the street is already at the highest fidelity."
    run = []
    for b in fidelity.bins:
        if b.level == low:
            run.append(b)
        elif run:
            break
    a, z = run[0].s_from, run[-1].s_to
    where = f"{_num(a)}-{_num(z)} m"
    if low == 0:
        return f"A Street View photo facing forward around {where} would give it a first street-level view (L1)."
    if low == 1:
        return f"A photo facing left or right from another spot around {where} would raise it to L2."
    return f"A close-up photo around {where} would raise it to L3."


# ------------------------------------------------------------------ aggregate summaries


def photo_count(model: CanonicalModel) -> int:
    return len(set(model.fused_observation_ids))


def about_summary(model: CanonicalModel, fidelity: FidelityMap, *, name: str | None = None,
                  photos: int | None = None, parts: Sequence[LowConfidencePart] | None = None) -> str:
    """Plain-language 'About this street' text (required phrases: ``fidelity L{n}``, the label, ``{N} photos``,
    ``model v{version}``, the street name and length; low-confidence parts; legacy / map-only notes)."""
    n = photo_count(model) if photos is None else photos
    level = fidelity.overall.level
    street = name or model.reference_line.name or model.segment_slug
    parts = low_confidence_parts(model, fidelity) if parts is None else parts
    length = _num(model.reference_line.length_s)
    if model.legacy:
        head = (f"This street was imported from the first-generation scene ({LEGACY_NOTE}): fidelity L{level} "
                f"({fidelity_label(level)}), {_plural(n, 'photo')}, model v{model.version}. "
                f"It covers {length} m of {street}.")
    elif level == 0:
        head = (f"This street is a {MAP_ONLY_NOTE} at fidelity L0 ({fidelity_label(0)}), built from the map and "
                f"{_plural(n, 'photo')} (model v{model.version}). It covers {length} m of {street}.")
    else:
        head = (f"This street is modelled at fidelity L{level} ({fidelity_label(level)}) from "
                f"{_plural(n, 'photo')} (model v{model.version}). It covers {length} m of {street}.")
    out = [head]
    if parts:
        out.append(f"Least certain: {_span(parts)}.")
    removed = [e for e in model.entities if e.state is EntityState.removed]
    if removed:
        out.append(f"Previously seen but no longer supported: {_plural(len(removed), 'object')}.")
    return " ".join(out)


def version_summary(model: CanonicalModel, diff: ModelDiff | None, fidelity: FidelityMap) -> str:
    """One paragraph for the version history: what this version is and what changed since its parent."""
    live = model.computed_stats().entity_count
    kind = {ModelKind.prior: "map-only prior", ModelKind.legacy: LEGACY_NOTE, ModelKind.fused: "fused from photos",
            ModelKind.edit: "admin edit"}[model.kind]
    text = (f"Model v{model.version} ({kind}): {_plural(live, 'object')}, {_plural(len(model.buildings), 'building')}, "
            f"fidelity L{fidelity.overall.level} (score {fidelity.overall.score:.2f}) from "
            f"{_plural(photo_count(model), 'photo')}.")
    if diff is not None and diff.from_version is not None:
        if diff.is_empty:
            text += f" No change since v{diff.from_version}."
        elif diff.summary_lines:
            text += f" Since v{diff.from_version}: " + "; ".join(diff.summary_lines) + "."
    return text
