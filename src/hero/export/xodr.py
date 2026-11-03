"""``street.xodr`` - OpenDRIVE 1.7, best effort (spec/portability.md section 8).

One ``<road>``; local metres from ``frame.origin`` (east, north, up); ``hdg = pi/2 - radians(bearing)``. The planView is
the dense 2.5 m polyline merged into ``<line/>`` pieces (arcs are not fitted); lanes are ``driving`` (carriageway half),
``curb`` and ``sidewalk`` (the footpath; absent where it is missing) on each side of the reference line, with widths per
``s``-interval; fixed furniture is exported as ``<object>``. No junctions, signals or elevation (flat z).

``parse_xodr`` reads the file back into plain dicts (used by the round-trip tests and by ports' sanity checks).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import quoteattr

from hero.export.manifest import StreetFrame
from hero.model.street_plan_types import StreetPlan, t_of_lat

MERGE_DEG = 0.02  # consecutive pieces whose heading differs by less than this are merged
MAX_CHORD_DEV_M = 0.01  # ... and only while every merged station stays this close to the chord
XODR_DATE = "2026-01-01T00:00:00"  # fixed: the file must be deterministic
OBJECT_TYPE = {"pole": "pole", "tree": "tree", "bollard": "pole", "railing": "barrier"}  # 'bollard' is not an OpenDRIVE 1.7 type
LANE_TYPES = ("driving", "curb", "sidewalk")


def _f(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _a(x: str) -> str:
    return quoteattr(x)


# ------------------------------------------------------------------------------------------------ profile per station


def _station_lanes(plan: StreetPlan, i: int) -> dict[str, list[tuple[str, float]]]:
    """Per side (``left``: t>0, ``right``: t<0) the outward lane list ``[(type, width)]`` at station ``i``."""
    st = plan.stations[i]
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    car = next(q for q in st.strips if q.kind.value == "carriageway")
    c_lo, c_hi = sorted((T(car.lo), T(car.hi)))
    edge = {"left": max(0.0, c_hi), "right": max(0.0, -c_lo)}
    mod_side = "left" if plan.footpath_side == "left" else "right"
    out: dict[str, list[tuple[str, float]]] = {"left": [("driving", edge["left"])], "right": [("driving", edge["right"])]}
    # footpath strips: modelled side from station.footpath, other strips of kind footpath from station.strips
    fps: dict[str, tuple[float, float]] = {}
    for q in st.strips:
        if q.kind.value == "footpath":
            lo, hi = sorted((T(q.lo), T(q.hi)))
            fps["left" if (lo + hi) / 2 > 0 else "right"] = (lo, hi) if (lo + hi) / 2 > 0 else (-hi, -lo)
    if st.footpath is not None:
        lo, hi = sorted((T(st.footpath.lo), T(st.footpath.hi)))
        fps[mod_side] = (lo, hi) if mod_side == "left" else (-hi, -lo)
    for side in ("left", "right"):
        if side in fps:
            inner, outer = fps[side]  # |t| of the strip's inner and outer edge
            gap = max(0.0, inner - edge[side])
            if gap > 0.0005:
                out[side].append(("curb", gap))
            out[side].append(("sidewalk", max(0.0, outer - max(inner, edge[side]))))
        elif side == mod_side and st.kerb.height_m > 0:
            out[side].append(("curb", st.kerb.width_m))
    return out


def _structure(per_station: list[dict[str, list[tuple[str, float]]]], i: int) -> tuple:
    """Lane-type sequence of interval ``i`` (a type present at either end is present in the interval)."""
    key = []
    for side in ("left", "right"):
        key.append(tuple(t for t in LANE_TYPES if any(t == lt for k in (i, i + 1) for lt, _ in per_station[k][side])))
    return tuple(key)


def _width(per_station, k: int, side: str, ltype: str) -> float:
    for lt, w in per_station[k][side]:
        if lt == ltype:
            return w
    return 0.0


# ------------------------------------------------------------------------------------------------------------ planView


def _geometries(plan: StreetPlan, fr: StreetFrame) -> list[dict[str, float]]:
    n = len(fr.s)
    h0 = math.radians(plan.frame.heading_deg)
    en = [fr.east_north(fr.px[i], fr.pz[i]) for i in range(n)]
    hdg = [math.pi / 2 - (h0 + fr.mid_heading(i)) for i in range(n - 1)]
    tol = math.radians(MERGE_DEG)
    out = []
    i = 0
    while i < n - 1:
        j = i + 1  # run covers intervals i .. j-1
        while j < n - 1 and abs(hdg[j] - hdg[i]) < tol:
            de, dn = en[j + 1][0] - en[i][0], en[j + 1][1] - en[i][1]
            ch = math.atan2(dn, de)
            ok = all(abs(-(en[k][0] - en[i][0]) * math.sin(ch) + (en[k][1] - en[i][1]) * math.cos(ch)) <= MAX_CHORD_DEV_M
                     for k in range(i + 1, j + 1))
            if not ok:
                break
            j += 1
        length = fr.s[j] - fr.s[i]
        if j == i + 1:
            h = hdg[i]
        else:  # chord heading keeps both run ends on the polyline (no position steps between pieces)
            h = math.atan2(en[j][1] - en[i][1], en[j][0] - en[i][0])
        out.append({"s": fr.s[i], "x": en[i][0], "y": en[i][1], "hdg": h, "length": length})
        i = j
    return out


# --------------------------------------------------------------------------------------------------------------- writer


def build_xodr(plan: StreetPlan) -> str:
    """OpenDRIVE 1.7 text for ``plan`` (deterministic)."""
    fr = StreetFrame(plan)
    n = len(fr.s)
    S = fr.s
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    geoms = _geometries(plan, fr)
    ens = [fr.east_north(fr.px[i], fr.pz[i]) for i in range(n)]
    east, north = [e for e, _ in ens], [v for _, v in ens]
    per = [_station_lanes(plan, i) for i in range(n)]
    keys = [_structure(per, i) for i in range(n - 1)]
    starts = [0] + [i for i in range(1, n - 1) if keys[i] != keys[i - 1]]

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<OpenDRIVE>",
        f'  <header revMajor="1" revMinor="7" name={_a(plan.slug)} version="1" date="{XODR_DATE}" '
        f'north="{_f(max(north))}" south="{_f(min(north))}" east="{_f(max(east))}" west="{_f(min(east))}" vendor="hero">',
        f"    <geoReference><![CDATA[+proj=tmerc +lat_0={plan.frame.origin.lat} +lon_0={plan.frame.origin.lon} "
        f"+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs]]></geoReference>",
        "  </header>",
        f'  <road name={_a(plan.name)} length="{_f(plan.length_m)}" id="1" junction="-1">',
        "    <planView>",
    ]
    for g in geoms:
        out.append(f'      <geometry s="{_f(g["s"])}" x="{g["x"]:.4f}" y="{g["y"]:.4f}" hdg="{g["hdg"]:.6f}" '
                   f'length="{_f(g["length"])}"><line/></geometry>')
    out += ["    </planView>", "    <lanes>"]
    for si, a in enumerate(starts):
        b = starts[si + 1] if si + 1 < len(starts) else n - 1
        out.append(f'      <laneSection s="{_f(S[a])}">')
        for side, tag, sign in (("left", "left", 1), ("right", "right", -1)):
            out.append(f"        <{tag}>")
            for lane_no, ltype in enumerate(keys[a][0 if side == "left" else 1], start=1):
                out.append(f'          <lane id="{sign * lane_no}" type="{ltype}" level="false">')
                for i in range(a, b):
                    w0, w1 = _width(per, i, side, ltype), _width(per, i + 1, side, ltype)
                    out.append(f'            <width sOffset="{_f(S[i] - S[a])}" a="{w0:.4f}" b="{(w1 - w0) / (S[i + 1] - S[i]):.6f}" c="0" d="0"/>')
                out.append("          </lane>")
            out.append(f"        </{tag}>")
            if side == "left":
                out.append('        <center><lane id="0" type="none" level="false"/></center>')
        out.append("      </laneSection>")
    out += ["    </lanes>", "    <objects>"]
    for f in plan.furniture:
        t = T(f.lat)
        out.append(
            f'      <object id={_a(f.id)} name={_a(f.type)} type="{OBJECT_TYPE.get(f.type, "obstacle")}" s="{_f(f.s)}" t="{_f(t)}" '
            f'zOffset="0" hdg="{math.radians(f.yaw_deg):.4f}" height="{_f(f.size_m.get("h", 0.0))}" '
            f'width="{_f(f.size_m.get("w", 0.0))}" length="{_f(f.size_m.get("d", 0.0))}" orientation="none">')
        out.append(f'        <userData code="hero:entity_id" value={_a(f.id)}/>')
        out.append("      </object>")
    out += ["    </objects>", "  </road>", "</OpenDRIVE>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------------------- reader


def parse_xodr(text: str | bytes) -> dict[str, Any]:
    """Parse ``street.xodr`` into plain dicts (raises ``ValueError`` on a structurally unusable file)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"xodr is not well-formed XML: {e}") from e
    if root.tag != "OpenDRIVE":
        raise ValueError(f"root element is {root.tag!r}, expected OpenDRIVE")
    hdr = root.find("header")
    roads = root.findall("road")
    if hdr is None or len(roads) != 1:
        raise ValueError("expected one <header> and one <road>")
    road = roads[0]
    geo = (hdr.findtext("geoReference") or "").strip()
    geoms = [{"s": float(g.get("s")), "x": float(g.get("x")), "y": float(g.get("y")), "hdg": float(g.get("hdg")),
              "length": float(g.get("length")), "kind": g[0].tag if len(g) else None}
             for g in road.findall("planView/geometry")]
    sections = []
    for sec in road.findall("lanes/laneSection"):
        d: dict[str, Any] = {"s": float(sec.get("s")), "left": [], "right": [], "center": []}
        for side in ("left", "center", "right"):
            for lane in sec.findall(f"{side}/lane"):
                d[side].append({
                    "id": int(lane.get("id")), "type": lane.get("type"),
                    "widths": [tuple(float(w.get(k)) for k in ("sOffset", "a", "b", "c", "d")) for w in lane.findall("width")]})
        sections.append(d)
    objects = []
    for o in road.findall("objects/object"):
        ud = {u.get("code"): u.get("value") for u in o.findall("userData")}
        objects.append({"id": o.get("id"), "name": o.get("name"), "type": o.get("type"), "s": float(o.get("s")),
                        "t": float(o.get("t")), "hdg": float(o.get("hdg", 0)), "height": float(o.get("height", 0)),
                        "width": float(o.get("width", 0)), "length": float(o.get("length", 0)),
                        "entity_id": ud.get("hero:entity_id")})
    return {
        "header": {"revMajor": int(hdr.get("revMajor")), "revMinor": int(hdr.get("revMinor")), "name": hdr.get("name"),
                   "vendor": hdr.get("vendor"), "geoReference": geo},
        "road": {"id": road.get("id"), "name": road.get("name"), "length": float(road.get("length")), "junction": road.get("junction")},
        "geometries": geoms, "sections": sections, "objects": objects,
    }


def reference_xy(parsed: dict[str, Any], s: float) -> tuple[float, float]:
    """Local (east, north) of the reference line at ``s`` (straight pieces)."""
    g = parsed["geometries"]
    cur = g[0]
    for geo in g:
        if geo["s"] <= s + 1e-9:
            cur = geo
    d = s - cur["s"]
    return (cur["x"] + d * math.cos(cur["hdg"]), cur["y"] + d * math.sin(cur["hdg"]))


def lane_edges(parsed: dict[str, Any], s: float) -> dict[str, list[tuple[str, float, float]]]:
    """Per side the outward lanes at ``s`` as ``(type, t_inner, t_outer)`` (left t>0, right t<0). The section containing ``s``
    is used; ``s`` equal to the road length uses the end of the last interval."""
    sec = parsed["sections"][0]
    for c in parsed["sections"]:
        if c["s"] <= s + 1e-9:
            sec = c
    out: dict[str, list[tuple[str, float, float]]] = {}
    for side, sign in (("left", 1.0), ("right", -1.0)):
        t = 0.0
        lanes = []
        for lane in sorted(sec[side], key=lambda ln: abs(ln["id"])):
            so = s - sec["s"]
            w = lane["widths"][0]
            for cand in lane["widths"]:
                if cand[0] <= so + 1e-9:
                    w = cand
            d = so - w[0]
            width = w[1] + w[2] * d + w[3] * d * d + w[4] * d ** 3
            lanes.append((lane["type"], sign * t, sign * (t + width)))
            t += width
        out[side] = lanes
    return out


def check_structure(text: str | bytes) -> list[str]:
    """Structural problems of an xodr file (empty list = OK): header, geometry continuity and total length, lane ids,
    non-negative widths."""
    try:
        p = parse_xodr(text)
    except ValueError as e:
        return [str(e)]
    issues: list[str] = []
    h = p["header"]
    if (h["revMajor"], h["revMinor"]) != (1, 7):
        issues.append(f"header revision {h['revMajor']}.{h['revMinor']} != 1.7")
    if "+proj=tmerc" not in h["geoReference"]:
        issues.append("geoReference missing +proj=tmerc")
    g = p["geometries"]
    if not g:
        return issues + ["no planView geometry"]
    if abs(g[0]["s"]) > 1e-9:
        issues.append("first geometry s != 0")
    for a, b in zip(g, g[1:]):
        if not b["s"] > a["s"]:
            issues.append(f"geometry s not strictly increasing at {b['s']}")
        if abs(a["s"] + a["length"] - b["s"]) > 1e-3:
            issues.append(f"geometry gap/overlap at s={b['s']}")
    if abs(sum(x["length"] for x in g) - p["road"]["length"]) > 1e-3:
        issues.append("sum of geometry lengths != road length")
    secs = p["sections"]
    if not secs or abs(secs[0]["s"]) > 1e-9:
        issues.append("laneSections must start at s=0")
    for a, b in zip(secs, secs[1:]):
        if not b["s"] > a["s"]:
            issues.append("laneSection s not strictly increasing")
    for sec in secs:
        if [ln["id"] for ln in sec["center"]] != [0]:
            issues.append(f"laneSection {sec['s']}: centre lane must be id 0")
        for side, sign in (("left", 1), ("right", -1)):
            ids = sorted(abs(ln["id"]) for ln in sec[side])
            if any(ln["id"] * sign <= 0 for ln in sec[side]):
                issues.append(f"laneSection {sec['s']}: wrong sign of lane id on {side}")
            if ids != list(range(1, len(ids) + 1)):
                issues.append(f"laneSection {sec['s']}: {side} lane ids not contiguous from the centre: {ids}")
            for ln in sec[side]:
                if not ln["widths"]:
                    issues.append(f"lane {ln['id']} has no width")
                for w in ln["widths"]:
                    if w[1] < 0:
                        issues.append(f"lane {ln['id']} negative width {w[1]}")
    ids = [o["id"] for o in p["objects"]]
    if len(set(ids)) != len(ids):
        issues.append("duplicate object ids")
    return issues
