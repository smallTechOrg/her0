"""Root-cause audit of a compiled street: where are the holes?  (slice S1d; pure, no I/O besides ``load_audit_input``.)

``audit_scene(scene)`` accepts, duck-typed (dict, or any object with ``model_dump()``):

* a **Scene v2** (the pre-realism renderer's input; ``corridor.stations[].strips/ground_uv`` + ``buildings`` + wall/gate
  ``objects``) - reconstructs what the legacy renderer could actually draw;
* a **Scene v3 / StreetPlan** (has ``frontage`` / ``ground_tex``) - reads the plan's own ground-source bins and the
  frontage segments.  Both ``StreetPlan`` (``stations``) and ``SceneV3`` (``corridor.stations``) are tolerated.

Frames: game lateral ``lat`` is positive toward the modelled footpath side.  Report sides are ``"modelled"`` (lat > 0)
and ``"opposite"`` (lat < 0); ``side_map`` says which physical side each is.

Definitions (all per 5 m bin, last bin shorter):

* ``covered_m``  - length of the bin (along s) in which some building footprint / wall / gate lies within
  ``FRONTAGE_RANGE_M`` (35 m) of the reference line on that side.
* v2 frontage ``evidence_hole``: ``covered_m < min(2.5, bin_len)`` - the spec's evidence rule (a bin is backed when a
  footprint or wall/gate covers >= 2.5 m of it).  v2 frontage ``hole`` (the headline, what the player sees): fewer than
  2.5 m of the bin has a facade/wall whose near edge is within ``STREET_REACH_M`` (3 m) of the outermost strip on that
  side - a detached block set back from the street leaves the strip between it and the footpath empty.
* plan frontage ``hole``: ``covered_m < bin_len - 1 mm`` (segments must tile the street).
* ground (v2): the legacy renderer paints every strip flat on top of one satellite tile, so the footpath/carriageway
  surface (``SURFACE_KINDS``) is never photo-textured.  Per side-band ``[0, GROUND_BAND_M]``: ``tile_m`` (tile visible),
  ``flat_m`` (under any flat strip), ``flat_surface_m`` (under a footpath/carriageway/kerb strip), ``none_m`` (neither).
  v2 ground ``hole``: ``flat_surface_m`` or ``none_m`` > 0.5 m at any station of the bin.  Plan ground ``hole``: ``bin_source`` is missing / ``none``; interpolated/procedural bins are painted
  and therefore not holes.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BIN_M = 5.0
FRONTAGE_RANGE_M = 35.0
GROUND_BAND_M = 14.0  # the band the playable/baked ground covers on each side of the reference line
MIN_COVER_M = 2.5
GROUND_HOLE_M = 0.5
SAMPLE_M = 0.25
LAT_STEP_M = 0.5
SIDES = ("modelled", "opposite")
_NO_SOURCE = (None, "", "none", "blank")


@dataclass
class AuditReport:
    kind: str  # "scene_v2" | "scene_v3" | "street_plan"
    slug: str
    length_m: float
    bin_m: float
    footpath_side: str
    side_map: dict[str, str]
    footpath_intervals: list[dict[str, Any]]
    footpath_state_m: dict[str, float]
    strips_without_texture: list[dict[str, Any]]
    ground_coverage: dict[str, list[dict[str, Any]]]
    frontage_coverage: dict[str, list[dict[str, Any]]]
    hole_bins: dict[str, list[int]]  # frontage hole bins per side
    ground_hole_bins: dict[str, list[int]]
    totals: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)  # filled by the CLI (file name + sha256); empty for in-memory audits

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        t = self.totals
        lines = [f"audit {self.kind} {self.slug}: {self.length_m:g} m, {t['n_bins']} bins of {self.bin_m:g} m"]
        for iv in self.footpath_intervals:
            lines.append(f"  footpath {iv['from_m']:6.1f}-{iv['to_m']:6.1f} m  {iv['state']:<10} conf {iv.get('confidence', 0):.2f}")
        for side in SIDES:
            lines.append(f"  {side:<8} frontage holes {t['frontage_hole_bins'][side]:>2}/{t['n_bins']}  "
                         f"ground holes {t['ground_hole_bins'][side]:>2}/{t['n_bins']}  bins {self.hole_bins[side]}")
        lines.append(f"  strips without texture: {len(self.strips_without_texture)}")
        return "\n".join(lines)


# ----------------------------------------------------------------------------- helpers


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"cannot audit a {type(obj).__name__}: need a dict or an object with model_dump()")


def _r(x: float, n: int = 3) -> float:
    return round(float(x) + 0.0, n)


def make_bins(length: float, bin_m: float = BIN_M) -> list[tuple[float, float]]:
    n = max(1, math.ceil(length / bin_m - 1e-9))
    return [(i * bin_m, min(length, (i + 1) * bin_m)) for i in range(n)]


def _union_len(intervals: list[tuple[float, float]]) -> float:
    total, cur_hi = 0.0, -math.inf
    for lo, hi in sorted(intervals):
        if hi <= lo:
            continue
        lo = max(lo, cur_hi)
        if hi > lo:
            total += hi - lo
            cur_hi = hi
    return total


def _poly_lat_intervals(poly: list[tuple[float, float]], s: float) -> list[tuple[float, float]]:
    """Lateral intervals of the polygon [s, lat] cut by the line s = const (even-odd)."""
    ys: list[float] = []
    n = len(poly)
    for i in range(n):
        (s1, l1), (s2, l2) = poly[i], poly[(i + 1) % n]
        if s1 == s2:
            continue
        if (s1 <= s < s2) or (s2 <= s < s1):
            ys.append(l1 + (l2 - l1) * (s - s1) / (s2 - s1))
    ys.sort()
    return [(ys[i], ys[i + 1]) for i in range(0, len(ys) - 1, 2)]


def _clip_side(iv: tuple[float, float], side: str) -> tuple[float, float] | None:
    """Clip a lateral interval to the side band, returning |lat| from/to (near, far)."""
    lo, hi = (iv if side == "modelled" else (-iv[1], -iv[0]))
    lo, hi = max(lo, 0.0), min(hi, FRONTAGE_RANGE_M)
    return (lo, hi) if hi > lo else None


def _samples(lo: float, hi: float) -> list[float]:
    n = max(1, round((hi - lo) / SAMPLE_M))
    return [lo + (i + 0.5) * (hi - lo) / n for i in range(n)]


# ----------------------------------------------------------------------------- footpath / stations


def _stations(d: dict[str, Any]) -> list[dict[str, Any]]:
    corridor = d.get("corridor")
    if isinstance(corridor, dict) and corridor.get("stations"):
        return corridor["stations"]
    return d.get("stations") or []


def _footpath_intervals(d: dict[str, Any], length: float) -> tuple[list[dict[str, Any]], dict[str, float]]:
    ivs: list[dict[str, Any]] = []
    cov = (d.get("cross_section") or {}).get("footpath_coverage")
    if cov:
        for c in cov:
            ivs.append({"from_m": _r(c["from_m"]), "to_m": _r(c["to_m"]), "state": c["state"],
                        "confidence": _r(c.get("confidence", 0.0), 3)})
    else:  # StreetPlan: derive from per-station footpath (None = missing)
        sts = _stations(d)
        for i, st in enumerate(sts):
            fp = st.get("footpath")
            state = "missing" if fp is None else fp.get("state", "unknown")
            conf = 0.0 if fp is None else fp.get("confidence", 0.0)
            lo = 0.0 if i == 0 else (sts[i - 1]["s"] + st["s"]) / 2
            hi = length if i == len(sts) - 1 else (st["s"] + sts[i + 1]["s"]) / 2
            if ivs and ivs[-1]["state"] == state:
                ivs[-1]["to_m"] = _r(hi)
                ivs[-1]["confidence"] = min(ivs[-1]["confidence"], _r(conf))
            else:
                ivs.append({"from_m": _r(lo), "to_m": _r(hi), "state": state, "confidence": _r(conf)})
    by_state: dict[str, float] = {}
    for iv in ivs:
        by_state[iv["state"]] = _r(by_state.get(iv["state"], 0.0) + iv["to_m"] - iv["from_m"])
    return ivs, dict(sorted(by_state.items()))


# ----------------------------------------------------------------------------- v2 ground

# The legacy renderer (frontend/js/game/scene_builder.js: buildGround / buildStrips) draws ONE satellite tile under
# the whole corridor and then paints every strip (footpath, kerb rib, carriageway, verge, forecourt ...) on top as a
# flat vertex-coloured mesh.  So no strip is photographically textured; the tile is only visible where no strip lies.
TEXTURED_STRIP_KINDS: tuple[str, ...] = ()
SURFACE_KINDS = ("footpath", "carriageway", "kerb", "median")  # the walkable/drivable surface: must carry texture


def _uv_at(uv: list[list[float]], lat: float, half: float) -> tuple[float, float]:
    k = (lat + half) / (2 * half)
    return (uv[0][0] + (uv[1][0] - uv[0][0]) * k, uv[0][1] + (uv[1][1] - uv[0][1]) * k)


def _strips_of(st: dict[str, Any]) -> list[dict[str, Any]]:
    out = list(st.get("strips", []))
    fp = st.get("footpath")
    if fp and not any(sp["kind"] == "footpath" for sp in out):
        out.append({**fp, "kind": "footpath"})
    return out


def _v2_ground(d: dict[str, Any], bins: list[tuple[float, float]]):
    ground = d.get("ground") or {}
    tile = bool(ground.get("tile_url"))
    corridor = d.get("corridor") or {}
    half = float(corridor.get("ground_half_m", 30.0))
    step = float(corridor.get("step_m", 2.5))
    sts = corridor.get("stations") or []
    # ---- strips with no texture: runs of stations per strip kind
    runs: list[dict[str, Any]] = []
    open_run: dict[str, dict[str, Any]] = {}
    for st in sts:
        kinds = set()
        for sp in _strips_of(st):
            if sp["kind"] in TEXTURED_STRIP_KINDS:
                continue
            kinds.add(sp["kind"])
            r = open_run.get(sp["kind"])
            if r is None:
                r = open_run[sp["kind"]] = {"kind": sp["kind"], "from_m": st["s"], "to_m": st["s"], "stations": 0,
                                           "reason": "flat vertex colour: the legacy renderer never textures strips"
                                                     + ("" if tile else " and there is no ground tile"),
                                           "lat_lo": sp["lo"], "lat_hi": sp["hi"]}
                runs.append(r)
            r["to_m"] = st["s"]
            r["stations"] += 1
            r["lat_lo"], r["lat_hi"] = min(r["lat_lo"], sp["lo"]), max(r["lat_hi"], sp["hi"])
        if st.get("footpath") is None:  # missing footpath: the renderer shows bare earth (flat) - a run of its own
            r = open_run.get("footpath_missing")
            if r is None:
                r = open_run["footpath_missing"] = {"kind": "footpath_missing", "from_m": st["s"], "to_m": st["s"],
                                                   "stations": 0, "reason": "footpath absent: flat earth gap",
                                                   "lat_lo": 0.0, "lat_hi": 0.0}
                runs.append(r)
            r["to_m"] = st["s"]
            r["stations"] += 1
            kinds.add("footpath_missing")
        for k in [k for k in open_run if k not in kinds]:
            del open_run[k]
    for r in runs:
        r["to_m"] = min(_r(r["to_m"] + step / 2), d.get("segment", {}).get("length_m", r["to_m"] + step / 2)) \
            if r["stations"] else r["to_m"]
        r["from_m"] = _r(max(0.0, r["from_m"] - step / 2)) if r["from_m"] > 0 else 0.0
        r["lat_lo"], r["lat_hi"] = _r(r["lat_lo"]), _r(r["lat_hi"])
    runs.sort(key=lambda r: (r["from_m"], r["kind"]))

    # ---- per bin and side: how much of the [0, band] half is photo tile / flat strip / nothing
    cov: dict[str, list[dict[str, Any]]] = {s: [] for s in SIDES}
    for b_i, (lo, hi) in enumerate(bins):
        last = b_i == len(bins) - 1
        in_bin = [st for st in sts if lo - 1e-9 <= st["s"] < hi - 1e-9 or (last and lo <= st["s"] <= hi + 1e-9)]
        for side in SIDES:
            sign = 1.0 if side == "modelled" else -1.0
            worst = {"tile_m": math.inf, "flat_m": 0.0, "flat_surface_m": 0.0, "none_m": 0.0}
            for st in in_bin:
                cur = dict.fromkeys(worst, 0.0)
                for k in range(int(GROUND_BAND_M / LAT_STEP_M)):
                    lat = sign * (k + 0.5) * LAT_STEP_M
                    strip = next((sp for sp in _strips_of(st) if sp["lo"] <= lat <= sp["hi"]
                                  and sp["kind"] not in TEXTURED_STRIP_KINDS), None)
                    if strip is not None:
                        cur["flat_m"] += LAT_STEP_M
                        if strip["kind"] in SURFACE_KINDS:
                            cur["flat_surface_m"] += LAT_STEP_M
                        continue
                    uv = st.get("ground_uv")
                    if tile and uv:
                        u, v = _uv_at(uv, lat, half)
                        if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                            cur["tile_m"] += LAT_STEP_M
                            continue
                    cur["none_m"] += LAT_STEP_M
                for key, val in cur.items():
                    worst[key] = min(worst[key], val) if key == "tile_m" else max(worst[key], val)
            worst["tile_m"] = 0.0 if not in_bin else worst["tile_m"]
            cov[side].append({"bin": b_i, "s_from": _r(lo), "s_to": _r(hi), "stations": len(in_bin),
                              **{k: _r(v) for k, v in worst.items()},
                              "hole": (not in_bin) or worst["flat_surface_m"] > GROUND_HOLE_M or worst["none_m"] > GROUND_HOLE_M})
    return runs, cov


# ----------------------------------------------------------------------------- v2 frontage

STREET_REACH_M = 3.0  # a facade/wall counts as "at the street" when its near edge is within this of the outermost strip


def _edge_at(sts: list[dict[str, Any]], s: float, side: str) -> float:
    """|lat| of the outermost strip edge on a side at the station nearest to s (0 if none)."""
    if not sts:
        return 0.0
    st = min(sts, key=lambda x: abs(x["s"] - s))
    vals = [sp["hi"] for sp in _strips_of(st) if sp["hi"] > 0] if side == "modelled" else \
        [-sp["lo"] for sp in _strips_of(st) if sp["lo"] < 0]
    return max(vals, default=0.0)


def _v2_frontage(d: dict[str, Any], bins: list[tuple[float, float]]):
    cov: dict[str, list[dict[str, Any]]] = {}
    sts = (d.get("corridor") or {}).get("stations") or []
    buildings = d.get("buildings") or []
    polys = [[(float(p[0]), float(p[1])) for p in b["footprint"]] for b in buildings]
    walls = [o for o in (d.get("objects") or []) if o.get("type") in ("wall", "gate")]
    for side in SIDES:
        rows = []
        for b_i, (lo, hi) in enumerate(bins):
            samples = _samples(lo, hi)
            covered = reach = 0
            depth_sum = gap_sum = edge_sum = 0.0
            by_kind = {"building": 0, "wall": 0}
            for s in samples:
                ivs: list[tuple[float, float]] = []
                kinds: set[str] = set()
                for poly in polys:
                    for iv in _poly_lat_intervals(poly, s):
                        c = _clip_side(iv, side)
                        if c:
                            ivs.append(c)
                            kinds.add("building")
                for o in walls:
                    if o["x_m"] <= s < o["x_m"] + o.get("length_m", 0.0):
                        w = o.get("size_m", {}).get("w", 0.3)
                        c = _clip_side((o["lateral_m"] - w / 2, o["lateral_m"] + w / 2), side)
                        if c:
                            ivs.append(c)
                            kinds.add("wall")
                edge = _edge_at(sts, s, side)
                edge_sum += edge
                if ivs:
                    covered += 1
                    depth_sum += _union_len(ivs)
                    gap = max(0.0, min(a for a, _ in ivs) - edge)
                    gap_sum += gap
                    reach += gap <= STREET_REACH_M
                    for k in kinds:
                        by_kind[k] += 1
            blen = hi - lo
            step = blen / len(samples)
            covered_m, reach_m = covered * step, reach * step
            thr = min(MIN_COVER_M, blen)
            rows.append({"bin": b_i, "s_from": _r(lo), "s_to": _r(hi), "covered_m": _r(covered_m),
                         "street_facing_m": _r(reach_m),
                         "building_m": _r(by_kind["building"] * step), "wall_m": _r(by_kind["wall"] * step),
                         "lateral_depth_m": _r(depth_sum / covered if covered else 0.0),
                         "street_edge_lat_m": _r(edge_sum / len(samples)),
                         "gap_to_street_m": _r(gap_sum / covered if covered else 0.0),
                         "partial": covered_m < blen - 1e-3,
                         "evidence_hole": covered_m < thr - 1e-9,
                         "hole": reach_m < thr - 1e-9})
        cov[side] = rows
    return cov


# ----------------------------------------------------------------------------- plan / v3


def _plan_frontage(d: dict[str, Any], bins, length: float):
    fr = d.get("frontage") or {}
    sides = fr.get("sides", []) if isinstance(fr, dict) else fr
    by_side = {s.get("side"): s for s in sides}
    cov: dict[str, list[dict[str, Any]]] = {}
    for side in SIDES:
        segs = sorted(((float(g["s_from"]), float(g["s_to"]), g) for g in (by_side.get(side) or {}).get("segments", [])),
                      key=lambda t: t[:2])
        rows = []
        for b_i, (lo, hi) in enumerate(bins):
            ivs = [(max(a, lo), min(b, hi)) for a, b, _ in segs if b > lo and a < hi]
            covered = _union_len(ivs)
            blen = hi - lo
            hits = [g for a, b, g in segs if b > lo and a < hi]
            rows.append({"bin": b_i, "s_from": _r(lo), "s_to": _r(hi), "covered_m": _r(covered),
                         "building_m": _r(_union_len([(max(a, lo), min(b, hi)) for a, b, g in segs
                                                      if g.get("kind") == "building" and b > lo and a < hi])),
                         "wall_m": _r(_union_len([(max(a, lo), min(b, hi)) for a, b, g in segs
                                                  if g.get("kind") != "building" and b > lo and a < hi])),
                         "interpolated_m": _r(_union_len([(max(a, lo), min(b, hi)) for a, b, g in segs
                                                          if g.get("interpolated") and b > lo and a < hi])),
                         "lateral_depth_m": _r(max((abs(g["lat_to"]) for g in hits), default=0.0)),
                         "partial": covered < blen - 1e-3, "hole": covered < blen - 1e-3})
        cov[side] = rows
    return cov


def _plan_ground(d: dict[str, Any], bins):
    src = list((d.get("ground_tex") or {}).get("bin_source") or [])
    sts = _stations(d)
    cov: dict[str, list[dict[str, Any]]] = {s: [] for s in SIDES}
    for b_i, (lo, hi) in enumerate(bins):
        if b_i < len(src):
            source = src[b_i]
        else:  # fall back to the stations' own ground_src
            in_bin = [st.get("ground_src") for st in sts if lo <= st["s"] < hi]
            source = in_bin[0] if in_bin else None
        for side in SIDES:
            cov[side].append({"bin": b_i, "s_from": _r(lo), "s_to": _r(hi), "source": source,
                              "hole": source in _NO_SOURCE})
    return [], cov


def _plan_strips_without_texture(d: dict[str, Any], ground_cov) -> list[dict[str, Any]]:
    out = []
    mode = (d.get("ground_tex") or {}).get("mode")
    run = None
    for row in ground_cov["modelled"]:
        if row["source"] in ("procedural", "interpolated") or row["hole"]:
            if run and run["source"] == row["source"]:
                run["to_m"] = row["s_to"]
            else:
                run = {"kind": "ground", "from_m": row["s_from"], "to_m": row["s_to"], "source": row["source"],
                       "reason": "painted, not photographed" if not row["hole"] else "blank"}
                out.append(run)
        else:
            run = None
    if mode == "procedural" and not out:
        out.append({"kind": "ground", "from_m": 0.0, "to_m": _r(ground_cov["modelled"][-1]["s_to"]), "source": "procedural",
                    "reason": "painted, not photographed"})
    return out


# ----------------------------------------------------------------------------- entry points


def audit_scene(scene: Any, *, bin_m: float = BIN_M) -> AuditReport:
    d = _as_dict(scene)
    is_plan = "frontage" in d or "ground_tex" in d
    if is_plan:
        kind = "scene_v3" if "schema_version" in d else "street_plan"
    elif d.get("schema_version") == 2 or "corridor" in d:
        kind = "scene_v2"
    else:
        raise ValueError("not a Scene v2/v3 or StreetPlan: no corridor/stations/frontage found")
    seg = d.get("segment") or {}
    slug = d.get("slug") or seg.get("slug") or ""
    side = d.get("footpath_side") or seg.get("footpath_side") or "left"
    length = d.get("length_m") or seg.get("length_m") or (d.get("path") or {}).get("length_m")
    if not length:
        sts = _stations(d)
        length = sts[-1]["s"] if sts else 0.0
    length = float(length)
    bins = make_bins(length, bin_m)
    notes: list[str] = []
    ivs, state_m = _footpath_intervals(d, length)
    if is_plan:
        front = _plan_frontage(d, bins, length)
        strips, ground = _plan_ground(d, bins)
        strips = _plan_strips_without_texture(d, ground)
        if not (d.get("ground_tex") or {}).get("bin_source"):
            notes.append("no ground_tex.bin_source: ground bins read from stations[].ground_src")
    else:
        front = _v2_frontage(d, bins)
        strips, ground = _v2_ground(d, bins)
    hole = {s: [r["bin"] for r in front[s] if r["hole"]] for s in SIDES}
    ghole = {s: [r["bin"] for r in ground[s] if r["hole"]] for s in SIDES}
    totals = {
        "n_bins": len(bins),
        "frontage_hole_bins": {s: len(hole[s]) for s in SIDES},
        "frontage_partial_bins": {s: sum(r["partial"] for r in front[s]) for s in SIDES},
        "frontage_covered_m": {s: _r(sum(r["covered_m"] for r in front[s])) for s in SIDES},
        "ground_hole_bins": {s: len(ghole[s]) for s in SIDES},
        "strips_without_texture": len(strips),
        "footpath_intervals": len(ivs),
        "footpath_non_intact_m": _r(sum(v for k, v in state_m.items() if k != "intact")),
        "blank_bins": {s: len(set(hole[s]) | set(ghole[s])) for s in SIDES},
    }
    other = "right" if side == "left" else "left"
    return AuditReport(kind=kind, slug=slug, length_m=_r(length), bin_m=bin_m, footpath_side=side,
                       side_map={"modelled": side, "opposite": other}, footpath_intervals=ivs,
                       footpath_state_m=state_m, strips_without_texture=strips, ground_coverage=ground,
                       frontage_coverage=front, hole_bins=hole, ground_hole_bins=ghole, totals=totals, notes=notes)


def load_audit_input(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
