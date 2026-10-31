"""Improve-the-model planning (spec/api.md -> "Phase 2.1 - add-to-model endpoints").

Pure and deterministic: no network, DB or clock. Three jobs:

* ``interval_view``  - fidelity per bin folded into maximal intervals of equal ``(level, gaps)`` with the reason and the
  hint of what raises them, plus the ``weakest`` ranking;
* ``plan_improvements`` - the cheapest set of Street View frames / zoom-20 crops that would raise the weakest bins
  (denser stations, left/right close-up headings, hi-res ground crops), as exact ``ImageryPlan.targets``;
* ``estimate_cost`` - informational dollars (Google images + Claude tokens priced with the run's model). It never
  blocks anything; only the global ``HERO_CLAUDE_BUDGET_USD`` hard stop applies.

Interpretation notes
--------------------
* A bin below level 2 is planned straight to L2 (a left and a right Street View frame from two stations 8 m apart, which
  together satisfy "two stations + both sides"); a forward/back-only L1 bin with both sides already present gets one extra
  station instead. L3 needs the admin's own close-up photo, so ``target_level=3`` plans the same frames and says so in the
  ``note`` (the Street View frames cannot make an L3).
* A bin has a *gap* when the ground has no hi-res satellite evidence, when a frontage side has no evidence, when the
  footpath is prior-only or when no furniture evidence exists. Without a ``GapReport`` only the ground gap is known
  (from ``FidelityBin.overhead_hires``).
* A target is ``cached`` when a stored observation of the same kind/view lies within ``CACHED_RADIUS_M``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from hero.imagery.plan import HEADING_OFFSET_DEG, ImageryPlan, PlanTarget, SRange
from hero.model.fidelity import SIDE_RANGE_M, FWD_RANGE_M, MIN_STATION_GAP_M, count_stations
from hero.model.gap_types import GapReport
from hero.model.refline import ReferenceLine
from hero.model.types import FidelityBin, FidelityMap, Observation, ObservationKind
from hero.scene.geo import meters_per_px

SV_USD = 0.007
SAT_USD = 0.002
CLAUDE_IN_PER_IMAGE = 1500
CLAUDE_OUT_PER_IMAGE = 100
CLAUDE_IN_PER_RUN = 8000
CLAUDE_OUT_PER_RUN = 400
CLAUDE_IN_PER_REVIEW = 10_000    # one review-and-fix round: render + 2 crops + 2 frames + the model summary
CLAUDE_OUT_PER_REVIEW = 3_000
CACHED_RADIUS_M = 1.0
STATION_SPREAD_M = 4.0           # left frame at mid-4, right frame at mid+4 -> 8 m apart (>= MIN_STATION_GAP_M)
CROP_ZOOM = 20
CROP_REUSE_FRACTION = 0.8
MAX_WEAKEST = 5
GAP_NAMES = ("ground", "frontage_modelled", "frontage_opposite", "footpath", "furniture")

assert 2 * STATION_SPREAD_M >= MIN_STATION_GAP_M


# ------------------------------------------------------------------ cost


@dataclass(frozen=True)
class CostEstimate:
    google_usd: float
    claude_usd: float
    total_usd: float
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"google_usd": self.google_usd, "claude_usd": self.claude_usd, "total_usd": self.total_usd,
                "model": self.model}


def _price(model: str | None) -> tuple[str, float, float]:
    """``(model, input_usd_per_mtok, output_usd_per_mtok)`` for ``model`` (default: the configured vision model).

    Uses ``hero.llm.pricing.price_for`` (slice SM); until that exists, the price table loader is the same data."""
    from hero.config import get_settings
    from hero.llm import pricing

    name = model or get_settings().vision_model
    price_for = getattr(pricing, "price_for", None)
    price = price_for(name) if price_for is not None else pricing.load_price_table(get_settings()).require(name)
    return name, float(price.input_usd_per_mtok), float(price.output_usd_per_mtok)


def estimate_cost(new_sv: int, new_sat: int, model: str | None = None, *, fuse: bool = True, passes: int = 1,
                  review_rounds: int = 0) -> CostEstimate:
    """Google: $0.007 per new Street View frame, $0.002 per new satellite crop. Claude (``fuse``): per new image
    1 500 input + 100 output tokens, per run 8 000 + 400, priced with ``price_for(model)``; no new image, no run cost.

    Max quality: every Street View frame is extracted ``passes`` times (each extra pass re-sends the frames and repeats
    the per-run prompt), and each of ``review_rounds`` review-and-fix rounds adds ``CLAUDE_*_PER_REVIEW`` tokens. The
    figure is an upper bound: early stop skips rounds and stored answers are replayed for free."""
    if new_sv < 0 or new_sat < 0:
        raise ValueError("image counts cannot be negative")
    if passes < 1 or review_rounds < 0:
        raise ValueError("passes must be >= 1 and review_rounds >= 0")
    name, p_in, p_out = _price(model)
    google = new_sv * SV_USD + new_sat * SAT_USD
    claude = 0.0
    n_new = new_sv + new_sat
    if fuse and n_new:
        sv_calls = new_sv * passes
        runs = 1 + (passes - 1 if new_sv else 0)
        tok_in = (sv_calls + new_sat) * CLAUDE_IN_PER_IMAGE + runs * CLAUDE_IN_PER_RUN + review_rounds * CLAUDE_IN_PER_REVIEW
        tok_out = (sv_calls + new_sat) * CLAUDE_OUT_PER_IMAGE + runs * CLAUDE_OUT_PER_RUN + review_rounds * CLAUDE_OUT_PER_REVIEW
        claude = tok_in * p_in / 1e6 + tok_out * p_out / 1e6
    return CostEstimate(google_usd=round(google, 4), claude_usd=round(claude, 4),
                        total_usd=round(google + claude, 4), model=name)


# ------------------------------------------------------------------ intervals ("how good is each stretch, why")


def bin_gaps(b: FidelityBin, gaps: GapReport | None) -> list[str]:
    """Names (``GAP_NAMES`` order) of the evidence gaps of one bin."""
    out: list[str] = []
    gb = gaps.bins[b.index] if gaps is not None and b.index < len(gaps.bins) else None
    if gb is None:
        if not b.overhead_hires:
            out.append("ground")
        return out
    if gb.ground.evidence != "satellite_hires":
        out.append("ground")
    if gb.frontage.modelled.evidence == "none":
        out.append("frontage_modelled")
    if gb.frontage.opposite.evidence == "none":
        out.append("frontage_opposite")
    if gb.footpath.evidence == "prior":
        out.append("footpath")
    if gb.furniture.evidence == "none":
        out.append("furniture")
    return out


def weakest_reason(b: FidelityBin) -> str:
    if b.level == 0:
        return "no street-level photo covers this stretch"
    if b.level == 1:
        parts = []
        if b.stations < 2:
            parts.append("single viewpoint")
        if b.sides.get("left", 0) < 1:
            parts.append("no left-facing photo")
        if b.sides.get("right", 0) < 1:
            parts.append("no right-facing photo")
        return "; ".join(parts) or "needs another viewpoint"
    if b.level == 2:
        return "no close-up photo yet"
    return "fully covered"


def _actions(level: int, gaps: list[str]) -> list[str]:
    acts: list[str] = []
    if level < 2 or gaps:
        acts.append("fill_gaps")
    if level < 3:
        acts.append("upload_photo")
    return acts


def intervals_of(fidelity: FidelityMap, gaps: GapReport | None = None) -> list[dict[str, Any]]:
    """Maximal runs of bins with equal ``(level, gaps)``, ordered by ``s``."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for b in fidelity.bins:
        g = bin_gaps(b, gaps)
        key = (b.level, tuple(g))
        if cur is not None and cur["_key"] == key:
            cur["s_to"] = b.s_to
            continue
        cur = {"_key": key, "s_from": b.s_from, "s_to": b.s_to, "level": b.level, "gaps": g,
               "weakest_reason": weakest_reason(b), "hint": b.hint, "actions": _actions(b.level, g)}
        out.append(cur)
    for iv in out:
        del iv["_key"]
        iv["s_from"], iv["s_to"] = round(iv["s_from"], 3), round(iv["s_to"], 3)
    return out


def weakest_of(intervals: Sequence[dict[str, Any]], n: int = MAX_WEAKEST) -> list[dict[str, Any]]:
    """Up to ``n`` intervals ranked by (level asc, gap count desc, length desc, s asc); fully fine ones are left out."""
    weak = [iv for iv in intervals if iv["level"] < 3 or iv["gaps"]]
    weak.sort(key=lambda iv: (iv["level"], -len(iv["gaps"]), -(iv["s_to"] - iv["s_from"]), iv["s_from"]))
    return list(weak[:n])


# ------------------------------------------------------------------ plan


@dataclass
class PlannedImageView:
    kind: str
    view: str | None
    station_s: float
    lat: float
    lon: float
    heading_deg: float | None
    zoom: int | None
    cached: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "view": self.view, "station_s": round(self.station_s, 3), "lat": round(self.lat, 6),
                "lon": round(self.lon, 6), "heading_deg": None if self.heading_deg is None else round(self.heading_deg, 2),
                "zoom": self.zoom, "cached": self.cached, "reason": self.reason}


@dataclass
class ImprovePlan:
    plan: ImageryPlan
    images: list[PlannedImageView]
    intervals_targeted: list[dict[str, Any]]
    est: CostEstimate
    trimmed: bool
    will_exceed_budget: bool
    note: str
    counts: dict[str, int] = field(default_factory=lambda: {"planned": 0, "new": 0, "cached": 0})

    def overrides(self) -> dict[str, Any]:
        """``plan_overrides`` for a run: exactly the targets (mutually exclusive with spacing/headings/s_range)."""
        return {"targets": [t.model_dump(exclude_none=True) for t in (self.plan.targets or [])]}


@dataclass
class _BinState:
    bin: FidelityBin
    left: int
    right: int
    stations: int
    extra: list[float] = field(default_factory=list)  # planned new street-level station coordinates covering the bin

    @property
    def mid(self) -> float:
        return (self.bin.s_from + self.bin.s_to) / 2.0

    def n_stations(self) -> int:
        return self.stations + count_stations(self.extra)


def _covers(a: float, b: float, lo: float, hi: float) -> bool:
    return min(b, hi) - max(a, lo) >= min(2.5, b - a) - 1e-9


def _cell_range(view: str, s: float) -> tuple[float, float]:
    if view == "forward":
        return s, s + FWD_RANGE_M
    if view == "back":
        return s - FWD_RANGE_M, s
    return s - SIDE_RANGE_M, s + SIDE_RANGE_M


def _stored_match(observations: Sequence[Observation], t: PlanTarget) -> bool:
    for o in observations:
        if o.station_s is None:
            continue
        if t.kind == "streetview":
            if o.kind in (ObservationKind.streetview, ObservationKind.user_photo) and o.view is not None \
                    and o.view.value == t.view and abs(o.station_s - t.station_s) <= CACHED_RADIUS_M:
                return True
        elif o.kind is ObservationKind.satellite_crop and o.zoom == t.zoom and abs(o.station_s - t.station_s) <= 5.0:
            return True
    return False


def plan_improvements(*, fidelity: FidelityMap, gaps: GapReport | None, observations: Sequence[Observation],
                      ref: ReferenceLine, target_level: int = 2, max_images: int = 40, max_usd: float = 1.0,
                      s_range: SRange | None = None, model: str | None = None,
                      remaining_usd: float | None = None, fuse: bool = True, passes: int = 1,
                      review_rounds: int = 0) -> ImprovePlan:
    """Cheapest targets that raise the weakest bins to ``min(target_level, 2)`` and close ground evidence gaps.

    ``max_images`` caps the planned frames and ``max_usd`` the estimated total; when either bites the tail of the
    priority order is dropped and ``trimmed`` is true. ``will_exceed_budget`` is true only when the Claude estimate
    exceeds ``remaining_usd`` (the global budget left; ``None`` = unknown -> false)."""
    if target_level not in (2, 3):
        raise ValueError("target_level must be 2 or 3")
    lo = max(0.0, s_range.s_from) if s_range else 0.0
    hi = min(ref.length_s, s_range.s_to) if s_range else ref.length_s
    if s_range is not None and not hi > lo:
        raise ValueError(f"s_range [{s_range.s_from}, {s_range.s_to}] lies outside the line (0..{ref.length_s:.1f} m)")

    states = [_BinState(b, b.sides.get("left", 0), b.sides.get("right", 0), b.stations) for b in fidelity.bins]
    gap_names = {b.index: bin_gaps(b, gaps) for b in fidelity.bins}
    need_level = min(target_level, 2)

    def wanted(st: _BinState) -> bool:
        mid = st.mid
        if not (lo - 1e-9 <= mid <= hi + 1e-9):
            return False
        return st.bin.level < need_level or "ground" in gap_names[st.bin.index]

    order = sorted((st for st in states if wanted(st)),
                   key=lambda st: (st.bin.level, -len(gap_names[st.bin.index]), st.bin.index))

    targets: list[PlanTarget] = []
    reasons: dict[tuple, str] = {}
    crop_stations: list[float] = []
    trimmed = False
    targeted_bins: dict[int, tuple[int, int]] = {}
    crop_radius = meters_per_px(ref.point_at(ref.length_s / 2.0, 0.0)[0], CROP_ZOOM, 2) * 640 * CROP_REUSE_FRACTION

    def clamp(s: float) -> float:
        return round(min(max(s, 0.0), ref.length_s), 3)

    def add_view(view: str, s: float, why: str) -> PlanTarget:
        s = clamp(s)
        t = PlanTarget(kind="streetview", station_s=s, view=view)  # type: ignore[arg-type]
        reasons[("streetview", view, s)] = why
        for st in states:
            a, b = _cell_range(view, s)
            if _covers(st.bin.s_from, st.bin.s_to, a, b):
                if view == "left":
                    st.left += 1
                elif view == "right":
                    st.right += 1
                st.extra.append(s)
        return t

    def cost_of(ts: Sequence[PlanTarget]) -> float:
        sv = sum(1 for t in ts if t.kind == "streetview" and not _stored_match(observations, t))
        sat = sum(1 for t in ts if t.kind == "satellite_crop" and not _stored_match(observations, t))
        return estimate_cost(sv, sat, model, fuse=fuse, passes=passes, review_rounds=review_rounds).total_usd

    for st in order:
        b = st.bin
        proposed: list[PlanTarget] = []
        mid = st.mid
        if b.level < need_level:
            snapshot = [(s_, s_.left, s_.right, list(s_.extra)) for s_ in states]
            if st.left < 1 or st.right < 1 or st.n_stations() < 2:
                if st.left < 1:
                    proposed.append(add_view("left", mid - STATION_SPREAD_M,
                                             f"left-facing view for {b.s_from:g}-{b.s_to:g} m ({b.hint or 'raises the level'})"))
                if st.right < 1:
                    proposed.append(add_view("right", mid + STATION_SPREAD_M,
                                             f"right-facing view for {b.s_from:g}-{b.s_to:g} m"))
                if st.n_stations() < 2:
                    far = mid - 8.0 if mid - 8.0 >= 0 else mid + 8.0
                    proposed.append(add_view("forward", far, f"second viewpoint for {b.s_from:g}-{b.s_to:g} m"))
            if b.level < need_level and not proposed and b.level == 0:
                proposed.append(add_view("forward", mid, f"first street-level view of {b.s_from:g}-{b.s_to:g} m"))
        if "ground" in gap_names[b.index] and not any(abs(mid - c) <= crop_radius for c in crop_stations):
            t = PlanTarget(kind="satellite_crop", station_s=clamp(mid), zoom=CROP_ZOOM)
            reasons[("satellite_crop", None, t.station_s)] = "zoom-20 satellite crop: no hi-res ground evidence here"
            crop_stations.append(mid)
            proposed.append(t)
        if not proposed:
            continue
        new_total = [*targets, *proposed]
        if len(new_total) > max_images or cost_of(new_total) > max_usd + 1e-9:
            trimmed = True
            # roll the virtual coverage back; later (lower priority) bins are dropped too
            for s_, left, right, extra in snapshot if b.level < need_level else []:
                s_.left, s_.right, s_.extra = left, right, extra
            if any(p.kind == "satellite_crop" for p in proposed):
                crop_stations.pop()
            break
        targets.extend(proposed)
        targeted_bins[b.index] = (b.level, need_level if b.level < need_level else b.level)

    plan = ImageryPlan(sv_spacing_m=10.0, headings=["forward", "back", "left", "right"], sat_crop_zoom=CROP_ZOOM,
                       sat_crop_spacing_m=20.0, include_overview=True, max_images=max(1, min(2000, len(targets) + 1)),
                       targets=targets or None)
    images: list[PlannedImageView] = []
    for t in sorted(targets, key=lambda t: (t.station_s, t.kind, t.view or "")):
        lat, lon = ref.point_at(t.station_s, 0.0)
        heading = (ref.bearing_deg(t.station_s) + HEADING_OFFSET_DEG[t.view]) % 360.0 if t.view else None
        images.append(PlannedImageView(
            kind=t.kind, view=t.view, station_s=t.station_s, lat=lat, lon=lon, heading_deg=heading,
            zoom=t.zoom, cached=_stored_match(observations, t),
            reason=reasons.get((t.kind, t.view, t.station_s), "")))
    n_cached = sum(1 for i in images if i.cached)
    new_sv = sum(1 for i in images if i.kind == "streetview" and not i.cached)
    new_sat = sum(1 for i in images if i.kind == "satellite_crop" and not i.cached)
    est = estimate_cost(new_sv, new_sat, model, fuse=fuse, passes=passes, review_rounds=review_rounds)

    targeted = _fold_targeted(fidelity, targeted_bins)
    if not images:
        if target_level == 3 and all(st.bin.level >= 2 for st in order or states):
            note = "Level 3 needs your own close-up photos: use Upload photos on the stretches you want to lift."
        else:
            note = "Nothing to fill: every stretch is at the target level."
    else:
        note = f"{len(images)} images planned ({len(images) - n_cached} new, {n_cached} already stored)."
        if target_level == 3:
            note += " Street View frames lift stretches to L2; L3 needs your own close-up photos (Upload)."
        if trimmed:
            note += " The plan was trimmed to fit max_images / max_usd; run Fill gaps again afterwards for the rest."
    return ImprovePlan(
        plan=plan, images=images, intervals_targeted=targeted, est=est, trimmed=trimmed,
        will_exceed_budget=bool(remaining_usd is not None and est.claude_usd > remaining_usd + 1e-9), note=note,
        counts={"planned": len(images), "new": len(images) - n_cached, "cached": n_cached})


def _fold_targeted(fidelity: FidelityMap, targeted: Mapping[int, tuple[int, int]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in fidelity.bins:
        pair = targeted.get(b.index)
        if pair is None or pair[0] == pair[1]:
            continue
        if out and out[-1]["from_level"] == pair[0] and out[-1]["to_level"] == pair[1] \
                and abs(out[-1]["s_to"] - b.s_from) < 1e-6:
            out[-1]["s_to"] = b.s_to
        else:
            out.append({"s_from": b.s_from, "s_to": b.s_to, "from_level": pair[0], "to_level": pair[1]})
    return out


# ------------------------------------------------------------------ what one uploaded photo can do


def photo_effect(model, observations: Mapping[str, Observation], new_obs: Observation,
                 ref: ReferenceLine | None = None) -> dict[str, Any]:
    """``{s_from, s_to, can_reach_level, message}``: the bins the photo's cells cover and the level it lifts them to,
    from ``compute_fidelity`` on ``model`` plus this observation (informational; the photo is not fused yet)."""
    from hero.model.coverage import observation_cells
    from hero.model.fidelity import compute_fidelity

    ref = ref or ReferenceLine.from_model(model.reference_line)
    cells = observation_cells(new_obs, ref)
    spans = [(c.s_from, c.s_to) for c in cells if c.kind != "overhead_hires"]
    if not spans:
        return {"s_from": None, "s_to": None, "can_reach_level": 2,
                "message": "This photo covers no stretch of the street."}
    s_from, s_to = min(a for a, _ in spans), max(b for _, b in spans)
    ids = sorted({*model.fused_observation_ids, new_obs.id})
    with_photo = model.model_copy(update={"fused_observation_ids": ids})
    obs = {**observations, new_obs.id: new_obs}
    before = compute_fidelity(model, observations)
    after = compute_fidelity(with_photo, obs)
    covered = [(a, b) for a, b in zip(before.bins, after.bins) if b.s_to > s_from + 1e-9 and b.s_from < s_to - 1e-9]
    best = max((b.level for _, b in covered), default=0)
    lifted = [b for a, b in covered if b.level > a.level]
    reach = 3 if best >= 3 else 2
    if best >= 3:
        msg = f"Once fused, this photo lifts {s_from:.0f}-{s_to:.0f} m to L3 (close-up)."
    elif lifted:
        msg = f"Once fused, this photo raises {s_from:.0f}-{s_to:.0f} m to L{best}."
    else:
        hint = next((b.hint for _, b in covered if b.hint), "")
        msg = (f"This photo adds evidence for {s_from:.0f}-{s_to:.0f} m but does not change the level yet. {hint}").strip()
    return {"s_from": round(s_from, 3), "s_to": round(s_to, 3), "can_reach_level": reach, "message": msg}
