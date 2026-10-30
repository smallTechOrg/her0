"""``street.manifest.json`` builder (spec/portability.md section 4): ``StreetPlan`` + ``StreetMesh`` -> ``ManifestV1``.

Pure and deterministic: no clock, no randomness, no host names. Everything geometric is expressed in the OpenDRIVE
``t`` (left-positive) via ``t_of_lat``; the street frame (x, z of the reference line) is integrated exactly as in
spec/portability.md section 2 (``StreetFrame``). The same ``StreetFrame`` is used by ``hero.export.xodr``.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from importlib import metadata
from typing import Any

from hero.export import GENERATOR, gameplay_bytes, load_gameplay
from hero.export.manifest_types import ATTRIBUTION_LINES, ATTRIBUTION_NOTICE, ManifestV1
from hero.export.mesh_types import FURNITURE_NODES, StreetMesh
from hero.model.street_plan_types import StreetPlan, t_of_lat

M_PER_DEG_LAT = 111_320.0  # the equirectangular scale shared with the contract fixture
LOOP_SEAM_BLEND_M = 6.0

# default glb node of a fixed-furniture type (used when the mesh does not say otherwise)
DEFAULT_NODE = {
    "pole": "Furniture_Poles", "tree": "Furniture_Trees", "railing": "Furniture_Railings",
    "drain": "Furniture_Decals", "manhole": "Furniture_Decals", "crossing": "Furniture_Decals",
    "kerb_cut": "Furniture_Decals", "ramp": "Furniture_Decals",
}


def r3(x: float) -> float:
    return round(x + 0.0, 3)


def r6(x: float) -> float:
    return round(x + 0.0, 6)


def wrap_pi(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class StreetFrame:
    """(s, t, y) -> street-frame world (X, Y, Z); the centreline P(s) is integrated on the station grid (midpoint heading)."""

    def __init__(self, plan: StreetPlan) -> None:
        pts = plan.reference_line.points
        self.s = [p.s for p in pts]
        self.heading_deg = plan.frame.heading_deg
        self.origin = (plan.frame.origin.lat, plan.frame.origin.lon)
        b0 = pts[0].bearing_deg
        self.phi = [wrap_pi(math.radians(p.bearing_deg - b0)) for p in pts]
        self.px, self.pz = [0.0], [0.0]
        for i in range(len(pts) - 1):
            ds = self.s[i + 1] - self.s[i]
            mid = 0.5 * (self.phi[i] + self.phi[i + 1])
            self.px.append(self.px[-1] + math.sin(mid) * ds)
            self.pz.append(self.pz[-1] - math.cos(mid) * ds)

    def world_i(self, i: int, t: float, y: float = 0.0) -> tuple[float, float, float]:
        return (self.px[i] - t * math.cos(self.phi[i]), y, self.pz[i] - t * math.sin(self.phi[i]))

    def east_north(self, x: float, z: float) -> tuple[float, float]:
        """Local (east, north) metres of a street-frame point, relative to ``origin``."""
        h = math.radians(self.heading_deg)
        fwd, right = -z, x
        return (fwd * math.sin(h) + right * math.cos(h), fwd * math.cos(h) - right * math.sin(h))

    def mid_heading(self, i: int) -> float:
        """Street-frame heading (radians, relative to heading_deg) of station interval i -> i+1."""
        return 0.5 * (self.phi[i] + self.phi[i + 1])


def _strip_side(t_lo: float, t_hi: float) -> str:
    return "left" if (t_lo + t_hi) / 2 > 0 else "right"


def _entity_nodes(mesh: StreetMesh | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if mesh is None:
        return out
    for n in mesh.nodes:
        for e in n.extras.get("entities", []) if n.extras.get("kind") == "furniture" else []:
            out[e["id"]] = n.name
    return out


def _default_node(ftype: str) -> str:
    return DEFAULT_NODE.get(ftype, "Furniture_Boxes")


def _lanes_and_cross(plan: StreetPlan, gp: dict) -> tuple[dict, dict]:
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    wd = gp["lanes"]["widths_m"]
    lanes: dict[str, Any] = {
        "defs": [
            {"index": 0, "name": "footpath", "kind": "footpath", "width_m": gp["lanes"]["default_footpath_width_m"]},
            {"index": 1, "name": "road_edge", "kind": "road_edge", "width_m": wd["road_edge"]},
            {"index": 2, "name": "road", "kind": "road", "width_m": wd["road"]},
        ],
        "stations": [],
    }
    cross: dict[str, Any] = {"stations": []}
    for st in plan.stations:
        fp = st.footpath
        lanes["stations"].append({
            "s": r3(st.s),
            "lane_t": [r3(T(v)) for v in st.lane_lat],
            "footpath": None if fp is None else {
                "t_lo": r3(min(T(fp.lo), T(fp.hi))), "t_hi": r3(max(T(fp.lo), T(fp.hi))),
                "state": fp.state.value, "material": fp.material.value, "height_m": r3(fp.height_m),
                "confidence": r3(fp.confidence)},
            "kerb": {"t": r3(T(st.kerb.lat)), "height_m": r3(st.kerb.height_m), "width_m": r3(st.kerb.width_m)},
        })
        strips: list[dict[str, Any]] = []
        for q in st.strips:
            lo, hi = sorted((T(q.lo), T(q.hi)))
            kind = q.kind.value
            strips.append({"kind": kind, "side": "centre" if kind == "carriageway" else _strip_side(lo, hi),
                           "t_lo": r3(lo), "t_hi": r3(hi), "material": q.material.value,
                           "condition": "fair" if kind == "carriageway" else "unknown",
                           "height_m": r3(q.height_m), "confidence": r3(q.confidence)})
        if fp is not None:
            lo, hi = sorted((T(fp.lo), T(fp.hi)))
            strips.append({"kind": "footpath", "side": _strip_side(lo, hi), "t_lo": r3(lo), "t_hi": r3(hi),
                           "material": fp.material.value, "condition": fp.state.value,
                           "height_m": r3(fp.height_m), "confidence": r3(fp.confidence)})
        if st.kerb.height_m > 0:
            kt = T(st.kerb.lat)
            outer = kt + st.kerb.width_m * (1 if kt > 0 else -1)
            strips.append({"kind": "kerb", "side": _strip_side(kt, outer), "t_lo": r3(min(kt, outer)), "t_hi": r3(max(kt, outer)),
                           "material": "concrete", "condition": "fair", "height_m": r3(st.kerb.height_m), "confidence": 0.7})
        seen: set[tuple] = set()
        uniq = []
        for q in sorted(strips, key=lambda q: (q["t_lo"], q["t_hi"], q["kind"])):
            key = (q["kind"], q["t_lo"], q["t_hi"])
            if key not in seen:  # a plan may already list the modelled footpath among its strips
                seen.add(key)
                uniq.append(q)
        cross["stations"].append({"s": r3(st.s), "strips": uniq})
    return lanes, cross


def _frontage(plan: StreetPlan) -> dict[str, list[dict[str, Any]]]:
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731

    def rows(side) -> list[dict[str, Any]]:
        return [{"s_from": r3(g.s_from), "s_to": r3(g.s_to), "type": g.kind, "t_from": r3(T(g.lat_from)), "t_to": r3(T(g.lat_to)),
                 "height_m": r3(g.height_m), "colour_hex": g.colour_hex, "source": g.source, "interpolated": g.interpolated}
                for g in side.segments]

    modelled, opposite = plan.frontage.sides
    if plan.footpath_side == "left":
        return {"left": rows(modelled), "right": rows(opposite)}
    return {"left": rows(opposite), "right": rows(modelled)}


def _size(d: dict) -> dict[str, float]:
    return {"w": r3(d.get("w", 0.0)), "h": r3(d.get("h", 0.0)), "d": r3(d.get("d", 0.0))}


def resolve_gameplay(gameplay: dict | bytes | None) -> tuple[dict, str]:
    """Return ``(gameplay dict, sha256)``; ``None`` = the committed ``gameplay.json`` and the sha of its bytes."""
    if gameplay is None:
        raw = gameplay_bytes()
        return load_gameplay(), hashlib.sha256(raw).hexdigest()
    if isinstance(gameplay, (bytes, bytearray)):
        return json.loads(gameplay), hashlib.sha256(bytes(gameplay)).hexdigest()
    canon = (json.dumps(gameplay, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return gameplay, hashlib.sha256(canon).hexdigest()


def build_manifest(
    plan: StreetPlan,
    mesh: StreetMesh,
    *,
    glb_sha256: str,
    glb_bytes: int,
    xodr_sha256: str,
    gameplay: dict | bytes | None = None,
) -> dict[str, Any]:
    """Build and validate the manifest; returns the JSON-ready dict (wire key ``schema``).

    ``glb_sha256`` / ``glb_bytes`` describe the serialised ``street.glb`` and ``xodr_sha256`` the ``street.xodr`` text
    (UTF-8) the manifest travels with.
    """
    gp, gp_sha = resolve_gameplay(gameplay)
    fr = StreetFrame(plan)
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    L = plan.length_m

    points = [{"s": r3(p.s), "lat": r6(p.lat), "lon": r6(p.lon), "x": r3(fr.px[i]), "z": r3(fr.pz[i]), "bearing_deg": r3(p.bearing_deg)}
              for i, p in enumerate(plan.reference_line.points)]
    lanes, cross = _lanes_and_cross(plan, gp)

    nodes = _entity_nodes(mesh)
    entities = [{
        "id": f.id, "type": f.type, "variant": f.variant, "s": r3(f.s), "t": r3(T(f.lat)), "yaw_deg": r3(f.yaw_deg),
        "size_m": _size(f.size_m), "length_m": r3(f.length_m), "colour_hex": f.colour_hex, "canopy_hex": f.canopy_hex,
        "confidence": r3(f.confidence), "fidelity_level": f.fidelity_level,
        "node": nodes.get(f.id) or _default_node(f.type), "description": f.description,
    } for f in plan.furniture]
    for e in entities:
        assert e["node"] in FURNITURE_NODES, e

    colliders = [{
        "id": o.id, "entity_id": o.entity_id, "type": o.type, "label": o.label, "s": r3(o.x_m), "length_m": r3(o.length_m),
        "lanes": list(o.lanes), "action": o.action.value,
        "size_m": {"w": r3(o.size_m.w), "h": r3(o.size_m.h), "d": r3(o.size_m.d)},
        "confidence": r3(o.confidence), "latlon": {"lat": r6(o.latlon.lat), "lon": r6(o.latlon.lon)},
        "tile_px": [r3(v) for v in o.tile_px] if o.tile_px else None,
    } for o in plan.objects]
    tokens = [{
        "id": t.id, "s": r3(t.x_m), "lane": int(t.lane), "reason": t.reason.value,
        "latlon": {"lat": r6(t.latlon.lat), "lon": r6(t.latlon.lon)}, "tile_px": [r3(v) for v in t.tile_px],
    } for t in plan.tokens]

    sh = plan.dynamic_context.spawn_hints
    end = len(fr.s) - 1
    try:
        pygltflib_v = metadata.version("pygltflib")
    except metadata.PackageNotFoundError:  # pragma: no cover
        pygltflib_v = "unknown"
    manifest = {
        "schema": "hero-street-manifest/1",
        "build": {
            "generator": GENERATOR, "segment_slug": plan.slug, "model_version": plan.model_version, "model_hash": plan.model_hash,
            "mesh_sha256": glb_sha256, "glb_bytes": glb_bytes, "xodr_sha256": xodr_sha256,
            "pygltflib": pygltflib_v, "pillow": metadata.version("pillow"), "python": platform.python_version(),
            "compiled_by": plan.compiled_by,
        },
        "segment": {
            "slug": plan.slug, "name": plan.name, "city": plan.city, "length_s": r3(L), "footpath_side": plan.footpath_side,
            "fidelity": {"overall_level": plan.fidelity.overall_level, "score": plan.fidelity.score,
                         "bins": [{"index": b.index, "s_from": r3(b.s_from), "s_to": r3(b.s_to), "level": b.level} for b in plan.fidelity.bins]},
        },
        "frame": {"units": "metres", "up_axis": "+Y", "handedness": "right", "travel_at_s0": "-Z",
                  "origin": {"lat": r6(plan.frame.origin.lat), "lon": r6(plan.frame.origin.lon)},
                  "heading_deg": r3(plan.frame.heading_deg), "world_from_st": "hero-st-world/1"},
        "reference_line": {"source": plan.reference_line.source, "osm_way_id": plan.reference_line.osm_way_id,
                           "name": plan.reference_line.name, "length_s": r3(plan.reference_line.length_s),
                           "step_m": plan.reference_line.step_m, "points": points},
        "lanes": lanes,
        "cross_section": cross,
        "frontage": _frontage(plan),
        "entities": entities,
        "colliders": colliders,
        "tokens": tokens,
        "spawn_hints": {"density": sh.density, "type_weights": dict(sh.type_weights)},
        "interpolated": [{"what": i.what, "side": i.side, "s_from": r3(i.s_from), "s_to": r3(i.s_to), "method": i.method, "note": i.note}
                         for i in plan.interpolated],
        "loop": {"seam_blend_m": LOOP_SEAM_BLEND_M,
                 "end_pose": {"x": r3(fr.px[end]), "z": r3(fr.pz[end]), "yaw_rad": r6(fr.phi[end])},
                 "chain": "rigid_end_to_start"},
        "mesh_stats": mesh.stats(),
        "attribution": {"lines": list(ATTRIBUTION_LINES), "notice": ATTRIBUTION_NOTICE},
        "gameplay": gp,
        "gameplay_sha256": gp_sha,
    }
    return ManifestV1.model_validate(manifest).dump()


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Deterministic serialisation: insertion key order, indent 1, UTF-8, trailing newline."""
    return (json.dumps(manifest, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
