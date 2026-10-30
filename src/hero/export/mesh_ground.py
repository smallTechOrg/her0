"""Ground strips along the curved reference line (spec/portability.md section 3).

``StreetProfile`` turns the plan's per-station cross-section (game lateral ``lat``, positive toward the modelled
footpath side) into exact lateral edges at any ``s``. The street is cut into *slices* ``[a, b]`` at every station and
every frontage-segment boundary, so ground, kerb, frontage, caps and furniture all share the same edges - no gaps by
construction: in every slice the nodes ``Footpath_Opposite / Kerb / Road / Footpath / Ground_L / Ground_R`` tile
``t`` from the left wall to the right wall.

Loop seam: over the last ``SEAM_BLEND_M`` metres every lateral edge (and wall) is blended linearly toward its station-0
value so copy k+1 joins copy k continuously.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hero.export.frame import UP, Builder, Colour, Frame, r3
from hero.model.street_plan_types import FrontageSegment, StreetPlan, t_of_lat

SEAM_BLEND_M = 6.0
KERB_FALLBACK_W = 0.2
BAND = 14.0  # ground atlas half width (t)
BIN_M = 5.0
EPS_S = 1e-6

# Vertex colours multiply the baked ground texture, so they are near-white tints (condition reads as a subtle shift,
# never a flat rectangle that cuts across the footpath at a slice boundary).
STATE_COLOUR: dict[str, Colour] = {
    "intact": (246, 243, 236, 255), "broken": (226, 216, 204, 255), "blocked": (232, 222, 216, 255),
    "encroached": (230, 222, 210, 255), "unknown": (240, 236, 228, 255), "missing": (218, 210, 194, 255),
}
ROAD_COLOUR: Colour = (196, 196, 204, 255)
OPP_COLOUR: Colour = (240, 236, 228, 255)
GROUND_COLOUR: Colour = (226, 220, 208, 255)
EARTH_COLOUR: Colour = (218, 210, 194, 255)


@dataclass(frozen=True)
class Slice:
    i: int  # lower station index
    a: float
    b: float


@dataclass(frozen=True)
class SV:
    """Station values in lat space."""

    car_lo: float
    car_hi: float
    kw: float
    hm: float  # modelled kerb / footpath height (0 = flush)
    fp_outer: float | None  # None = no modelled footpath here
    state: str
    ho: float  # opposite footpath height
    opp_outer: float | None  # None = no opposite footpath strip
    fp_conf: float
    fp_material: str


@dataclass(frozen=True)
class XS:
    """Cross-section at one ``s`` (lat space, ascending: wall_o < ... < wall_m)."""

    car_lo: float
    car_hi: float
    kw: float
    hm: float
    fp_outer: float | None
    ho: float
    opp_outer: float | None
    wall_m: float
    wall_o: float
    state: str

    @property
    def fp_inner(self) -> float:
        return self.car_hi + (self.kw if self.hm > 0 else 0.0)

    @property
    def opp_inner(self) -> float:
        return self.car_lo - (self.kw if self.ho > 0 else 0.0)


def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def _station_values(st: Any) -> SV:
    car = next((q for q in st.strips if q.kind.value == "carriageway"), None)
    car_hi = car.hi if car is not None else st.kerb.lat
    car_lo = car.lo if car is not None else car_hi - 9.0
    kw = st.kerb.width_m if st.kerb.width_m > 0 else KERB_FALLBACK_W
    fp = st.footpath
    hm, fp_outer, state, conf, mat = 0.0, None, "missing", 0.0, "unknown"
    if fp is not None:
        hm = st.kerb.height_m if st.kerb.height_m > 0 else max(fp.height_m, 0.0)
        fp_outer = max(fp.hi, car_hi + (kw if hm > 0 else 0.0) + 0.3)
        state, conf, mat = fp.state.value, float(fp.confidence), fp.material.value
    opp = None
    for q in st.strips:
        if q.kind.value == "footpath" and (q.lo + q.hi) / 2 < car_lo:
            opp = q
            break
    ho, opp_outer = 0.0, None
    if opp is not None:
        ho = max(opp.height_m, 0.0)
        opp_outer = min(opp.lo, car_lo - (kw if ho > 0 else 0.0) - 0.3)
    return SV(car_lo, car_hi, kw, hm, fp_outer, state, ho, opp_outer, conf, mat)


class StreetProfile:
    def __init__(self, plan: StreetPlan, frame: Frame):
        self.plan = plan
        self.frame = frame
        self.L = plan.length_m
        self.S = [st.s for st in plan.stations]
        self.sv = [_station_values(st) for st in plan.stations]
        self.fp_side = plan.footpath_side
        self.sides = {sd.side: sd.segments for sd in plan.frontage.sides}
        self.seam_m = min(SEAM_BLEND_M, self.L / 3.0)
        self.slices = self._slices()
        self._seg_cache: dict[tuple[str, int], FrontageSegment] = {}

    # ------------------------------------------------------------------ geometry helpers
    def t(self, lat: float) -> float:
        return t_of_lat(lat, self.fp_side)

    def physical(self, which: str) -> str:
        """``modelled`` / ``opposite`` -> ``left`` / ``right``."""
        if which == "modelled":
            return self.fp_side
        return "right" if self.fp_side == "left" else "left"

    def _slices(self) -> list[Slice]:
        pts = {round(s, 6) for s in self.S}
        for segs in self.sides.values():
            for sg in segs:
                pts.add(round(sg.s_from, 6))
                pts.add(round(sg.s_to, 6))
        pts.add(round(self.L - self.seam_m, 6))
        ordered = sorted(p for p in pts if 0.0 <= p <= self.L + 1e-9)
        out = []
        for a, b in zip(ordered, ordered[1:]):
            if b - a < 1e-4:
                continue
            mid = 0.5 * (a + b)
            i = max(k for k in range(len(self.S) - 1) if self.S[k] <= mid)
            out.append(Slice(i, a, b))
        return out

    def segment_at(self, which: str, s_mid: float) -> FrontageSegment:
        for sg in self.sides[which]:
            if sg.s_from - EPS_S <= s_mid <= sg.s_to + EPS_S:
                return sg
        return self.sides[which][-1]

    def blend_w(self, s: float) -> float:
        x = (s - (self.L - self.seam_m)) / self.seam_m
        return min(max(x, 0.0), 1.0)

    def wall_raw(self, which: str, sl: Slice, s: float) -> float:
        sg = self.segment_at(which, 0.5 * (sl.a + sl.b))
        u = (s - sg.s_from) / (sg.s_to - sg.s_from)
        lat = _lerp(sg.lat_from, sg.lat_to, min(max(u, 0.0), 1.0))
        w = self.blend_w(s)
        if w > 0.0:
            first = self.sides[which][0]
            lat = _lerp(lat, first.lat_from, w)
        return lat

    # ------------------------------------------------------------------ cross-section
    def xs(self, sl: Slice, s: float) -> XS:
        i = sl.i
        vi, vj = self.sv[i], self.sv[i + 1]
        u = (s - self.S[i]) / (self.S[i + 1] - self.S[i])
        u = min(max(u, 0.0), 1.0)
        car_lo, car_hi = _lerp(vi.car_lo, vj.car_lo, u), _lerp(vi.car_hi, vj.car_hi, u)
        kw = _lerp(vi.kw, vj.kw, u)
        fp_outer = hm = None
        if vi.fp_outer is not None:
            fo_j = vj.fp_outer if vj.fp_outer is not None else vi.fp_outer
            hm_j = vj.hm if vj.fp_outer is not None else vi.hm
            fp_outer, hm = _lerp(vi.fp_outer, fo_j, u), _lerp(vi.hm, hm_j, u)
        ho, opp_outer = vi.ho, None
        if vi.opp_outer is not None:
            oo_j = vj.opp_outer if vj.opp_outer is not None else vi.opp_outer
            opp_outer = _lerp(vi.opp_outer, oo_j, u)
            ho = _lerp(vi.ho, vj.ho if vj.opp_outer is not None else vi.ho, u)
        w = self.blend_w(s)
        if w > 0.0:
            v0 = self.sv[0]
            car_lo, car_hi, kw = _lerp(car_lo, v0.car_lo, w), _lerp(car_hi, v0.car_hi, w), _lerp(kw, v0.kw, w)
            if fp_outer is not None and v0.fp_outer is not None:
                fp_outer = _lerp(fp_outer, v0.fp_outer, w)
            if opp_outer is not None and v0.opp_outer is not None:
                opp_outer = _lerp(opp_outer, v0.opp_outer, w)
        has_fp = fp_outer is not None
        hm = hm if has_fp else 0.0
        inner = car_hi + (kw if (hm or 0) > 0 else 0.0) if has_fp else car_hi
        wall_m = max(self.wall_raw("modelled", sl, s), inner + 0.1)
        if has_fp:
            fp_outer = min(max(fp_outer, inner + 0.05), wall_m)
        o_inner = car_lo - (kw if (opp_outer is not None and ho > 0) else 0.0)
        wall_o = min(self.wall_raw("opposite", sl, s), o_inner - 0.1)
        if opp_outer is not None:
            opp_outer = max(min(opp_outer, o_inner - 0.05), wall_o)
        return XS(car_lo, car_hi, kw, hm or 0.0, fp_outer, ho if opp_outer is not None else 0.0, opp_outer,
                  wall_m, wall_o, vi.state if has_fp else "missing")

    def surface_y(self, s: float, lat: float) -> float:
        """Walking-surface height at ``(s, lat)`` (0 on the carriageway / forecourt)."""
        sl = self.slice_at(s)
        x = self.xs(sl, s)
        if x.fp_outer is not None and x.hm > 0 and x.car_hi <= lat <= x.fp_outer:
            return x.hm
        if x.opp_outer is not None and x.ho > 0 and x.opp_outer <= lat <= x.car_lo:
            return x.ho
        return 0.0

    def slice_at(self, s: float) -> Slice:
        for sl in self.slices:
            if sl.a - 1e-9 <= s <= sl.b + 1e-9:
                return sl
        return self.slices[-1] if s > self.L else self.slices[0]


# ---------------------------------------------------------------------- ground build


def uv_ground(length: float, s: float, t: float) -> tuple[float, float]:
    u = (BAND - t) / (2 * BAND)
    v = (length - s) / length
    return (min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0))


def _runs(items: list[tuple[float, float, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge consecutive slices with equal attributes into runs."""
    runs: list[dict[str, Any]] = []
    for a, b, attrs in items:
        if runs and abs(runs[-1]["s_to"] - a) < 1e-6 and all(runs[-1][k] == v for k, v in attrs.items()):
            runs[-1]["s_to"] = r3(b)
        else:
            runs.append({"s_from": r3(a), "s_to": r3(b), **attrs})
    return runs


def build_ground(plan: StreetPlan, frame: Frame, prof: StreetProfile) -> dict[str, Builder]:
    """Return the six ground builders (extras are added by the mesh builder)."""
    L = plan.length_m
    fp_phys, opp_phys = prof.physical("modelled"), prof.physical("opposite")
    b = {
        "Footpath": Builder("Footpath", colour=True), "Footpath_Opposite": Builder("Footpath_Opposite", colour=True),
        "Road": Builder("Road", colour=True), "Kerb": Builder("Kerb", colour=False),
        "Ground_L": Builder("Ground_L", colour=True), "Ground_R": Builder("Ground_R", colour=True),
    }
    ground_m = b["Ground_L" if fp_phys == "left" else "Ground_R"]
    ground_o = b["Ground_L" if opp_phys == "left" else "Ground_R"]
    T = prof.t

    def horiz(bld: Builder, s0: float, s1: float, l0a: float, l0b: float, l1a: float, l1b: float, y: float, col: Colour) -> None:
        """Quad between lateral intervals ``[l0a, l0b]`` at ``s0`` and ``[l1a, l1b]`` at ``s1`` (lat space)."""
        if l0b - l0a < 1e-6 and l1b - l1a < 1e-6:
            return
        pts = [(s0, T(l0a)), (s0, T(l0b)), (s1, T(l1b)), (s1, T(l1a))]
        bld.quad(*[frame.world(s, t, y) for s, t in pts], want=UP, uvs=[uv_ground(L, s, t) for s, t in pts], col=col)

    def vertical(bld: Builder, s0: float, s1: float, lat0: float, lat1: float, y0: float, y1: float, facing: float,
                 col: Colour | None = None, uv: bool = False) -> None:
        """Vertical face at lateral ``lat`` (slanted between s0 and s1) from y0 to y1, normal toward ``facing`` (lat dir)."""
        if abs(y1 - y0) < 1e-6:
            return
        p = [frame.world(s0, T(lat0), y0), frame.world(s1, T(lat1), y0), frame.world(s1, T(lat1), y1), frame.world(s0, T(lat0), y1)]
        dx, dz = frame.t_dir(0.5 * (s0 + s1))
        sg = 1.0 if prof.fp_side == "left" else -1.0  # lat increases along +sg * t_dir
        want = (dx * sg * facing, 0.0, dz * sg * facing)
        uvs = [uv_ground(L, s0, T(lat0)), uv_ground(L, s1, T(lat1)), uv_ground(L, s1, T(lat1)), uv_ground(L, s0, T(lat0))] if uv else None
        bld.quad(*p, want=want, uvs=uvs, col=col)

    for sl in prof.slices:
        x0, x1 = prof.xs(sl, sl.a), prof.xs(sl, sl.b)
        fp_here = x0.fp_outer is not None
        opp_here = x0.opp_outer is not None
        # carriageway
        horiz(b["Road"], sl.a, sl.b, x0.car_lo, x0.car_hi, x1.car_lo, x1.car_hi, 0.0, ROAD_COLOUR)
        # modelled side
        if fp_here:
            col = STATE_COLOUR.get(x0.state, STATE_COLOUR["unknown"])
            in0, in1 = x0.fp_inner, x1.fp_inner
            o1 = x1.fp_outer if x1.fp_outer is not None else x0.fp_outer
            if x0.hm > 0:
                horiz(b["Kerb"], sl.a, sl.b, x0.car_hi, in0, x1.car_hi, in1, x0.hm, None)
                vertical(b["Kerb"], sl.a, sl.b, x0.car_hi, x1.car_hi, 0.0, x0.hm, -1.0)
            horiz(b["Footpath"], sl.a, sl.b, in0, x0.fp_outer, in1, o1, x0.hm, col)
            if x0.hm > 0:
                vertical(b["Footpath"], sl.a, sl.b, x0.fp_outer, o1, 0.0, x0.hm, +1.0, col, uv=True)
            horiz(ground_m, sl.a, sl.b, x0.fp_outer, x0.wall_m, o1, x1.wall_m, 0.0, GROUND_COLOUR)
        else:
            horiz(ground_m, sl.a, sl.b, x0.car_hi, x0.wall_m, x1.car_hi, x1.wall_m, 0.0, EARTH_COLOUR)
        # opposite side
        if opp_here:
            oo1 = x1.opp_outer if x1.opp_outer is not None else x0.opp_outer
            ho = x0.ho
            if ho > 0:
                horiz(b["Kerb"], sl.a, sl.b, x0.opp_inner, x0.car_lo, x1.opp_inner, x1.car_lo, ho, None)
                vertical(b["Kerb"], sl.a, sl.b, x0.car_lo, x1.car_lo, 0.0, ho, +1.0)
            horiz(b["Footpath_Opposite"], sl.a, sl.b, x0.opp_outer, x0.opp_inner, oo1, x1.opp_inner, ho, OPP_COLOUR)
            if ho > 0:
                vertical(b["Footpath_Opposite"], sl.a, sl.b, x0.opp_outer, oo1, 0.0, ho, -1.0, OPP_COLOUR, uv=True)
            horiz(ground_o, sl.a, sl.b, x0.wall_o, x0.opp_outer, x1.wall_o, oo1, 0.0, GROUND_COLOUR)
        else:
            horiz(ground_o, sl.a, sl.b, x0.wall_o, x0.car_lo, x1.wall_o, x1.car_lo, 0.0, EARTH_COLOUR)
    return b


def ground_extras(plan: StreetPlan, prof: StreetProfile) -> dict[str, dict[str, Any]]:
    """Node extras (spec 3.1) for the six ground nodes."""
    L = plan.length_m
    bins = []
    for k, src in enumerate(plan.ground_tex.bin_source):
        fb = plan.fidelity.bins[min(k, len(plan.fidelity.bins) - 1)] if plan.fidelity.bins else None
        bins.append({"index": k, "s_from": r3(k * BIN_M), "s_to": r3(min(L, (k + 1) * BIN_M)), "ground_source": src,
                     "fidelity_level": fb.level if fb else 0, "interpolated": src == "interpolated"})
    fp_items, opp_items, road_items = [], [], []
    for sl in prof.slices:
        x = prof.xs(sl, 0.5 * (sl.a + sl.b))
        sv = prof.sv[sl.i]
        if x.fp_outer is not None:
            fp_items.append((sl.a, sl.b, {"state": x.state, "material": sv.fp_material, "height_m": r3(x.hm),
                                          "confidence": round(sv.fp_conf, 3)}))
        if x.opp_outer is not None:
            opp_items.append((sl.a, sl.b, {"state": "unknown", "material": "paver", "height_m": r3(x.ho), "confidence": 0.3}))
        road_items.append((sl.a, sl.b, {"state": "fair", "material": "asphalt", "height_m": 0.0, "confidence": 0.7}))
    fp_phys, opp_phys = prof.physical("modelled"), prof.physical("opposite")
    return {
        "Footpath": {"kind": "footpath", "side": fp_phys, "runs": _runs(fp_items), "bins": bins},
        "Footpath_Opposite": {"kind": "footpath", "side": opp_phys, "runs": _runs(opp_items), "bins": bins},
        "Road": {"kind": "road", "side": "both", "runs": _runs(road_items), "bins": bins},
        "Kerb": {"kind": "kerb", "side": "both", "runs": [], "bins": bins},
        "Ground_L": {"kind": "ground", "side": "left", "runs": [], "bins": bins},
        "Ground_R": {"kind": "ground", "side": "right", "runs": [], "bins": bins},
    }
