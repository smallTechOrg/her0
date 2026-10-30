"""Fixed furniture merged per category (poles, trees, boxes, railings, decals) - small low-poly meshes.

Sizes come from the entity but are capped per type (``MAX_SIZE_M``) so no data error can produce a screen-filling
object. Poles are tapered 6-sided cylinders (+ cross-arm for ``variant == "utility"``); trees are trunk + 2 canopy
blobs; bins / bollards / bus stops / signboards are boxes; railings are two rails + posts; drains, manholes, kerb cuts,
ramps and crossings are flat decals 10 mm above the surface. Vehicles and other dynamic entities never appear here
(``FurniturePlan`` only admits fixed types).
"""

from __future__ import annotations

import math
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
            b.frustum(cx, cz, r, r * 0.7, h, surf, col)
            if f.variant == "utility":
                b.box(cx, cz, axf, axn, min(1.6, 0.4 + w * 3), 0.12, 0.12, surf + h - 0.5, col)
            elif f.variant == "streetlight":
                b.box(cx + axn[0] * 0.5, cz + axn[1] * 0.5, axf, axn, 1.0, 0.1, 0.12, surf + h - 0.1, col)
        elif f.type == "tree":
            w, h = _cap(w, cw), _cap(h, ch)
            b.frustum(cx, cz, min(0.2, w * 0.08), min(0.14, w * 0.05), h * 0.45, surf, (*hex_rgb("#6b4f3a"), 255))
            canopy = hex_rgb(f.canopy_hex or "#4f7d3a")
            b.blob(cx, surf + h * 0.62, cz, w / 2, (*canopy, 255))
            b.blob(cx + axf[0] * 0.5, surf + h * 0.82, cz + axf[1] * 0.5, w / 3.2, (*shade(canopy, 0.85), 255))
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
