"""Fixed furniture merged per category (poles, trees, boxes, railings, decals) - small low-poly meshes.

Sizes come from the entity but are capped per type (``MAX_SIZE_M``) so no data error can produce a screen-filling
object. Poles are Bengaluru spun-concrete poles: square, tapered, a grimy base, a painted black/white band, a
cross-arm with insulators for ``variant == "utility"``; streetlights are a steel tube with an outreach arm and a lamp
head. Trees are a tapered trunk that forks into 2-3 limbs under a wide canopy of 7-9 clustered blobs with per-tree
(deterministic, id-hashed) placement and leaf tones; bins / bollards / bus stops / signboards are boxes; railings are two rails + posts; drains, manholes, kerb cuts,
ramps and crossings are flat decals 10 mm above the surface. Vehicles and other dynamic entities never appear here
(``FurniturePlan`` only admits fixed types).
"""

from __future__ import annotations

import math
import zlib
from typing import Any

from hero.export.frame import UP, Builder, Colour, Frame, hex_rgb, r3, shade
from hero.export.mesh_ground import StreetProfile
from hero.model.street_plan_types import MAX_SIZE_M, StreetPlan

NODE_OF_TYPE = {
    "pole": "Furniture_Poles", "tree": "Furniture_Trees", "railing": "Furniture_Railings",
    "drain": "Furniture_Decals", "manhole": "Furniture_Decals", "crossing": "Furniture_Decals",
    "kerb_cut": "Furniture_Decals", "ramp": "Furniture_Decals",
}  # everything else (bin, bollard, bus_stop, signboard) -> Furniture_Boxes
DEFAULT_COLOUR = {
    "pole": "#9aa0a6", "bin": "#3f6b4a", "bollard": "#d8d8d0", "railing": "#5b6670", "signboard": "#c98b2b",
    "bus_stop": "#3d6fa5", "drain": "#4a4a4a", "manhole": "#55595e", "crossing": "#e8e8e8", "tree": "#6b4f3a",
    "kerb_cut": "#c9c4b8", "ramp": "#c9c4b8",
}
DECAL_MAX_M = 12.0
# per-type decal footprint caps (w across, d along): a drain grate / manhole is small, never a slab across the footpath
DECAL_CAP_M = {"drain": (1.4, 1.4), "manhole": (0.9, 0.9), "kerb_cut": (2.4, 2.4), "ramp": (2.4, 3.0)}
RAILING_MAX_LEN = 20.0
MIN_DIM = 0.05
POLE_MAX_DIAMETER_M = 0.26   # real poles are 0.15-0.25 m: never a screen-filling shaft
SIGN_HEIGHT_ABOVE_GROUND = 1.7


def node_of(type_: str) -> str:
    return NODE_OF_TYPE.get(type_, "Furniture_Boxes")


def _cap(value: float, cap: float) -> float:
    return max(MIN_DIM, min(float(value), cap))


def build_furniture(plan: StreetPlan, frame: Frame, prof: StreetProfile) -> tuple[dict[str, Builder], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    nodes = ("Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals")
    fb = {n: Builder(n, colour=True) for n in nodes}
    ents: dict[str, list[dict[str, Any]]] = {n: [] for n in nodes}
    rows: list[dict[str, Any]] = []
    for f in plan.furniture:  # already sorted by (s, id)
        node = node_of(f.type)
        b = fb[node]
        start = len(b.idx)
        t = prof.t(f.lat)
        s = min(max(f.s, 0.0), plan.length_m)
        cx, _, cz = frame.world(s, t, 0.0)
        ph = frame.phi_at(s) + math.radians(f.yaw_deg)
        axf, axn = (math.sin(ph), -math.cos(ph)), (math.cos(ph), math.sin(ph))
        surf = prof.surface_y(s, f.lat)
        col: Colour = (*hex_rgb(f.colour_hex or DEFAULT_COLOUR.get(f.type, "#999999")), 255)
        cw, ch = MAX_SIZE_M.get(f.type, (DECAL_MAX_M, DECAL_MAX_M))
        w, h, d = f.size_m["w"], f.size_m["h"], f.size_m["d"]
        if f.type == "pole":
            w, h = _cap(w, cw), _cap(h, ch)
            r = max(0.04, min(w, POLE_MAX_DIAMETER_M) / 2)
            if f.variant == "streetlight":
                _streetlight(b, cx, cz, axn, r, h, surf, col)
            else:
                _concrete_pole(b, cx, cz, axf, axn, r, h, surf, col, utility=f.variant == "utility")
        elif f.type == "tree":
            w, h = _cap(w, cw), _cap(h, ch)
            _tree(b, f.id, cx, cz, w, h, surf, hex_rgb(f.canopy_hex or "#4f7d3a"))
        elif f.type == "railing":
            w = _cap(w, cw)
            h = _cap(h, ch)
            length = _cap(f.length_m or d, RAILING_MAX_LEN)
            _railing(b, cx, cz, axf, axn, w, h, length, surf, col)
        elif f.type in ("drain", "manhole", "crossing", "kerb_cut", "ramp"):
            cap_w, cap_d = DECAL_CAP_M.get(f.type, (DECAL_MAX_M, DECAL_MAX_M))
            w, d = _cap(w, cap_w), _cap(d, cap_d)
            y = surf + 0.01
            if f.type in ("drain", "manhole"):       # grates are dark metal whatever the surrounding crop tint is
                col = (*hex_rgb(DEFAULT_COLOUR[f.type]), 255)
            if f.type == "crossing":
                n = max(1, min(12, int(w / 0.9)))
                bw = w / (2 * n - 1)
                for k in range(n):
                    off = -w / 2 + bw * (2 * k) + bw / 2
                    _flat(b, cx + axn[0] * off, cz + axn[1] * off, axf, axn, bw, d, y, col)
            else:
                _flat(b, cx, cz, axf, axn, w, d, y, col)
        elif f.type == "signboard":
            w, h = _cap(w, cw), _cap(h, ch)
            d = _cap(d, 0.6)
            b.box(cx, cz, axf, axn, w, h, d, surf + SIGN_HEIGHT_ABOVE_GROUND, col)
        else:  # bin, bollard, bus_stop
            w, h = _cap(w, cw), _cap(h, ch)
            d = _cap(d, cw)
            b.box(cx, cz, axf, axn, w, h, d, surf, col)
        row = {"id": f.id, "type": f.type, "variant": f.variant, "s": r3(f.s), "t": r3(t), "yaw_deg": f.yaw_deg}
        ents[node].append({**row, "confidence": f.confidence, "fidelity_level": f.fidelity_level,
                           "index_start": start, "index_count": len(b.idx) - start})
        rows.append({**row, "node": node})
    extras = {n: {"kind": "furniture", "category": cat, "entities": ents[n]}
              for n, cat in (("Furniture_Poles", "poles"), ("Furniture_Trees", "trees"), ("Furniture_Boxes", "boxes"),
                             ("Furniture_Railings", "railings"), ("Furniture_Decals", "decals"))}
    return fb, extras, rows


def _flat(b: Builder, cx: float, cz: float, axf, axn, w: float, d: float, y: float, col: Colour) -> None:
    q = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    pts = [(cx + axf[0] * a * d / 2 + axn[0] * c * w / 2, y, cz + axf[1] * a * d / 2 + axn[1] * c * w / 2) for a, c in q]
    b.quad(*pts, want=UP, col=col)


def _railing(b: Builder, cx: float, cz: float, axf, axn, w: float, h: float, length: float, y0: float, col: Colour) -> None:
    rail = max(0.04, min(w, 0.1))
    b.box(cx, cz, axf, axn, rail, 0.05, length, y0 + h - 0.05, col)  # top rail
    b.box(cx, cz, axf, axn, rail, 0.04, length, y0 + h * 0.4, col)  # mid rail
    n = max(2, min(11, int(length / 1.5) + 1))
    for k in range(n):
        off = -length / 2 + length * k / (n - 1)
        b.box(cx + axf[0] * off, cz + axf[1] * off, axf, axn, rail, h, rail, y0, col)


def _rng(key: str):
    """Deterministic 0..1 stream from an entity id (no global random state; same id -> same tree)."""
    state = [zlib.crc32(key.encode("utf-8")) or 1]

    def nxt() -> float:
        x = state[0]
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        state[0] = x & 0xFFFFFFFF
        return state[0] / 4294967296.0
    return nxt


CONCRETE = hex_rgb("#a29d92")
GRIME = hex_rgb("#5f5a52")
STEEL = hex_rgb("#7d838a")


def _concrete_pole(b: Builder, cx: float, cz: float, axf, axn, r: float, h: float, y0: float, col: Colour,
                   *, utility: bool) -> None:
    """Square spun-concrete pole (45 deg phase -> faces along the street), tapering to 60 %; grimy base, a painted
    black / white band at eye height, cross-arm(s) with three insulators on utility poles."""
    base = col if col[:3] != hex_rgb(DEFAULT_COLOUR["pole"]) else (*CONCRETE, 255)
    ph = math.atan2(axf[1], axf[0]) + math.pi / 4
    taper = lambda y: r * (1.0 - 0.4 * y / h)  # noqa: E731
    bands = [(0.0, 0.9, (*GRIME, 255)), (0.9, 1.35, (*shade(GRIME[:3], 1.25), 255)), (1.35, 1.6, (235, 235, 230, 255)),
             (1.6, 1.85, (25, 25, 25, 255)), (1.85, 2.1, (235, 235, 230, 255)), (2.1, h, base)]
    for a, z, c in bands:
        if a >= h:
            break
        z = min(z, h)
        b.frustum(cx, cz, taper(a), taper(z), z - a, y0 + a, c, sides=4, phase=ph)
    if utility:
        arm = min(1.6, 0.6 + r * 4)
        for k, dy in enumerate((0.45, 1.15)):
            y = y0 + h - dy
            b.box(cx, cz, axf, axn, arm * (1.0 - 0.25 * k), 0.09, 0.09, y, (*STEEL, 255))
            if k == 0:
                for off in (-arm * 0.42, 0.0, arm * 0.42):
                    ix, iz = cx + axn[0] * off, cz + axn[1] * off
                    b.frustum(ix, iz, 0.045, 0.03, 0.14, y + 0.09, (226, 226, 220, 255), sides=6)


def _streetlight(b: Builder, cx: float, cz: float, axn, r: float, h: float, y0: float, col: Colour) -> None:
    """Steel tube pole, an outreach arm towards the road (-axn is away from the footpath edge either way: the arm
    reaches 1.2 m along +axn like the old model) and a flat LED lamp head."""
    steel = (*STEEL, 255) if col[:3] == hex_rgb(DEFAULT_COLOUR["pole"]) else col
    rr = min(r, 0.09)
    b.frustum(cx, cz, rr * 1.4, rr * 1.4, 0.5, y0, (*shade(STEEL, 0.8), 255), sides=8)
    b.frustum(cx, cz, rr, rr * 0.65, h - 0.5, y0 + 0.5, steel, sides=8)
    top = (cx, y0 + h - 0.05, cz)
    end = (cx + axn[0] * 1.2, y0 + h + 0.25, cz + axn[1] * 1.2)
    b.limb(top, end, rr * 0.6, rr * 0.5, steel, sides=6)
    b.box(end[0] + axn[0] * 0.25, end[2] + axn[1] * 0.25, (axn[1], -axn[0]), axn, 0.6, 0.08, 0.25, end[1] - 0.06,
          (60, 63, 66, 255))
    b.box(end[0] + axn[0] * 0.25, end[2] + axn[1] * 0.25, (axn[1], -axn[0]), axn, 0.5, 0.02, 0.18, end[1] - 0.075,
          (240, 238, 220, 255))


def _tree(b: Builder, key: str, cx: float, cz: float, w: float, h: float, y0: float, canopy: tuple[int, int, int]) -> None:
    rnd = _rng(key)
    bark = (*hex_rgb("#5a4636"), 255)
    # entity w is often the trunk footprint (0.8 m default), not the crown: size the crown from the height too
    rt = max(0.12, min(0.3, h * 0.035))
    w = max(w, min(MAX_SIZE_M["tree"][0], h * 0.75))
    fork = h * (0.38 + 0.08 * rnd())
    b.frustum(cx, cz, rt * 1.25, rt, 0.35, y0, bark, sides=7)                 # root flare
    b.frustum(cx, cz, rt, rt * 0.8, fork - 0.35, y0 + 0.35, bark, sides=7)
    top = (cx, y0 + fork, cz)
    n_limbs = 2 + int(rnd() * 2)
    crown_y = y0 + h * 0.66
    tips = []
    for k in range(n_limbs):
        a = 2 * math.pi * (k / n_limbs + rnd() * 0.25)
        reach = w * (0.18 + 0.12 * rnd())
        tip = (cx + math.cos(a) * reach, crown_y + h * 0.08 * rnd(), cz + math.sin(a) * reach)
        b.limb(top, tip, rt * 0.75, rt * 0.3, bark, sides=5)
        tips.append(tip)
    # canopy: a wide dome of clustered blobs, darker underneath, lighter and smaller on top
    n = 7 + int(rnd() * 3)
    rad = w / 2
    for k in range(n):
        a = 2 * math.pi * (k / n) + rnd() * 0.6
        d = rad * (0.35 + 0.4 * rnd()) if k else 0.0
        y = crown_y + h * (0.04 + 0.12 * rnd()) + (0.0 if k else h * 0.1)
        r = rad * (0.42 + 0.18 * rnd())
        tone = 0.78 + 0.32 * rnd()
        c = (*shade(canopy, tone * (0.9 if y < crown_y + h * 0.08 else 1.05)), 255)
        b.blob(cx + math.cos(a) * d, y, cz + math.sin(a) * d, r, c, y_scale=0.62, sides=7)
