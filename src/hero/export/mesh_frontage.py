"""Continuous frontage ribbons (``Frontage_L/R``) + roof caps / returns (``Frontage_Caps``).

Walls are built per slice of ``StreetProfile`` (the same slices as the ground), so a wall's foot is exactly the outer
edge of the ground strip next to it and the ribbon is contiguous in ``s``. Where neighbouring segments sit at different
lateral positions or heights the step is closed by perpendicular faces (the wall ribbon) and returns (the caps node).

UV (facade atlas, spec 3): ``u = s / L``; ``v = v0 + (1 - min(h, 12) / 12) * 0.5`` with ``v0 = 0`` for ``Frontage_L``
and ``0.5`` for ``Frontage_R``.
"""

from __future__ import annotations

from typing import Any

from hero.export.frame import UP, Builder, Colour, Frame, hex_rgb, r3, shade
from hero.export.mesh_ground import StreetProfile
from hero.export.mesh_types import FACADE_MAX_H
from hero.model.street_plan_types import StreetPlan

CAP_DEPTH = {"building": 4.0, "wall": 0.4, "gate": 0.4}


def uv_facade(length: float, s: float, h: float, v0: float) -> tuple[float, float]:
    return (min(max(s / length, 0.0), 1.0), v0 + (1.0 - min(max(h, 0.0), FACADE_MAX_H) / FACADE_MAX_H) * 0.5)


def build_frontage(plan: StreetPlan, frame: Frame, prof: StreetProfile) -> tuple[dict[str, Builder], dict[str, dict[str, Any]]]:
    L = plan.length_m
    sg_sign = 1.0 if prof.fp_side == "left" else -1.0  # lat increases along sg_sign * t_dir
    builders = {"Frontage_L": Builder("Frontage_L", colour=False), "Frontage_R": Builder("Frontage_R", colour=False)}
    caps = Builder("Frontage_Caps", colour=True)
    seg_rows: dict[str, list[dict[str, Any]]] = {"Frontage_L": [], "Frontage_R": []}

    for which in ("modelled", "opposite"):
        phys = prof.physical(which)
        node = "Frontage_L" if phys == "left" else "Frontage_R"
        v0 = 0.0 if phys == "left" else 0.5
        bld = builders[node]
        inward = -1.0 if which == "modelled" else +1.0  # facing, in lat direction, toward the street
        segs = plan.frontage.sides[0 if which == "modelled" else 1].segments

        def want_vec(s: float, facing: float) -> tuple[float, float, float]:
            dx, dz = frame.t_dir(s)
            return (dx * sg_sign * facing, 0.0, dz * sg_sign * facing)

        def wall_lat(sl, s) -> float:
            x = prof.xs(sl, s)
            return x.wall_m if which == "modelled" else x.wall_o

        for k, sg in enumerate(segs):
            start = len(bld.idx)
            h = sg.height_m
            rgb = hex_rgb(sg.colour_hex)
            ccol: Colour = (*shade(rgb, 0.72), 255)
            depth = CAP_DEPTH[sg.kind]
            out_dir = -inward  # lat direction away from the street
            for sl in prof.slices:
                if sl.b <= sg.s_from + 1e-6 or sl.a >= sg.s_to - 1e-6:
                    continue
                la, lb = wall_lat(sl, sl.a), wall_lat(sl, sl.b)
                p = [frame.world(sl.a, prof.t(la), 0.0), frame.world(sl.b, prof.t(lb), 0.0),
                     frame.world(sl.b, prof.t(lb), h), frame.world(sl.a, prof.t(la), h)]
                hs = [0.0, 0.0, h, h]
                ss = [sl.a, sl.b, sl.b, sl.a]
                bld.quad(*p, want=want_vec(0.5 * (sl.a + sl.b), inward), uvs=[uv_facade(L, s_, h_, v0) for s_, h_ in zip(ss, hs)])
                la2, lb2 = la + out_dir * depth, lb + out_dir * depth
                caps.quad(frame.world(sl.a, prof.t(la), h), frame.world(sl.a, prof.t(la2), h),
                          frame.world(sl.b, prof.t(lb2), h), frame.world(sl.b, prof.t(lb), h), want=UP, col=ccol)
            # closing faces at both ends toward the neighbour's wall (closes lateral steps; never leaves a see-through)
            for nb, at, facing_s in ((segs[k - 1] if k > 0 else None, sg.s_from, -1.0),
                                     (segs[k + 1] if k + 1 < len(segs) else None, sg.s_to, +1.0)):
                if nb is None:
                    continue
                inside, outside = (at - 1e-4, at + 1e-4) if facing_s > 0 else (at + 1e-4, at - 1e-4)
                own = wall_lat(prof.slice_at(inside), at)
                other = wall_lat(prof.slice_at(outside), at)
                f = frame.forward(at)
                fwd = (f[0] * facing_s, 0.0, f[1] * facing_s)
                if abs(own - other) > 1e-4:
                    bld.quad(frame.world(at, prof.t(own), 0.0), frame.world(at, prof.t(other), 0.0),
                             frame.world(at, prof.t(other), h), frame.world(at, prof.t(own), h), want=fwd,
                             uvs=[uv_facade(L, at, 0.0, v0), uv_facade(L, at, 0.0, v0), uv_facade(L, at, h, v0), uv_facade(L, at, h, v0)])
                if nb.height_m < h - 0.05:  # return: roof-colour face above the lower neighbour
                    hi_lat = own + out_dir * depth
                    caps.quad(frame.world(at, prof.t(own), nb.height_m), frame.world(at, prof.t(hi_lat), nb.height_m),
                              frame.world(at, prof.t(hi_lat), h), frame.world(at, prof.t(own), h), want=fwd, col=ccol)
            fb = plan.fidelity.bins
            lvl = fb[min(int(sg.s_from // 5.0), len(fb) - 1)].level if fb else 0
            seg_rows[node].append({
                "s_from": r3(sg.s_from), "s_to": r3(sg.s_to), "type": sg.kind, "source": sg.source,
                "building_id": sg.building_id, "entity_id": sg.entity_id, "height_m": r3(h),
                "interpolated": sg.interpolated, "confidence": sg.confidence, "fidelity_level": lvl,
                "index_start": start, "index_count": len(bld.idx) - start})
    builders["Frontage_Caps"] = caps
    extras = {
        "Frontage_L": {"kind": "frontage", "side": "left", "segments": seg_rows["Frontage_L"]},
        "Frontage_R": {"kind": "frontage", "side": "right", "segments": seg_rows["Frontage_R"]},
        "Frontage_Caps": {"kind": "frontage_caps"},
    }
    return builders, extras
