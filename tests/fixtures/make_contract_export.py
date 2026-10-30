"""Generate the STAND-IN export bundle that other slices code against (Phase 2.1 r2, slice S1a-contract).

    uv run python tests/fixtures/make_contract_export.py            # (re)write the committed files
    uv run python tests/fixtures/make_contract_export.py --check    # exit 1 when the committed files are stale

Writes (all deterministic - no clock, no randomness):
  tests/fixtures/export/contract_curved/{plan.json, street.glb, street.manifest.json, street.xodr}
  tests/fixtures/scene_v3/contract_curved.json
  src/hero/export/schemas/{manifest,gameplay,scene_v3,gap_report}.schema.json

The street is the curved reference street ``tests/e2e/fixtures/s4_scene_curved_v2.json`` (60 m, radius 80 m, turns
RIGHT, footpath on the LEFT) re-expressed in the true street frame of spec/portability.md section 2. The glb has the
REAL contract: node names, materials, extras, UV rules, vertex colours - but is built from simple boxes / quads and a
tiny placeholder texture, with a small self-contained glb writer (the real writer is slice S7, the real geometry is
slice S6). Do NOT treat the geometry here as the production look.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import platform
import struct
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from hero.export import GENERATOR, SCHEMAS_DIR, gameplay_bytes, gameplay_sha256, load_gameplay  # noqa: E402
from hero.export.gameplay_types import GameplayV1  # noqa: E402
from hero.export.manifest_types import ManifestV1  # noqa: E402
from hero.export.mesh_types import (  # noqa: E402
    ASSET_SCHEMA, FURNITURE_CATEGORY, KERB_COLOUR, NODE_MATERIAL, NODE_NAMES, ROOT_NODE, SCENE_NAME,
    MeshMaterial, MeshNode, StreetMesh,
)
from hero.model.gap_types import GapReport  # noqa: E402
from hero.model.scene_v3 import SceneV3  # noqa: E402
from hero.model.street_plan_types import StreetPlan, t_of_lat  # noqa: E402

SRC_SCENE = ROOT / "tests" / "e2e" / "fixtures" / "s4_scene_curved_v2.json"
OUT_DIR = ROOT / "tests" / "fixtures" / "export" / "contract_curved"
SCENE_OUT = ROOT / "tests" / "fixtures" / "scene_v3" / "contract_curved.json"

SLUG = "contract-curved-street"
NAME = "Contract Curved Road, Indiranagar (stand-in export)"
MODEL_VERSION = 2
MODEL_HASH = hashlib.sha256(b"hero-contract-curved-v2").hexdigest()
COMPILED_BY = "hero.model.compile/2"
STEP = 2.5
BIN_M = 5.0
KERB_H = 0.15
KERB_W = 0.2
BAND = 14.0  # ground atlas half width
M_LAT = 111_320.0
MPP = 0.1455  # satellite overview metres per pixel (zoom 19, scale 2)


def r3(x: float) -> float:
    return round(x + 0.0, 3)


def r6(x: float) -> float:
    return round(x + 0.0, 6)


def hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def srgb_to_linear(c: int) -> float:
    x = c / 255.0
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


# ====================================================================== street frame


class Frame:
    """spec/portability.md section 2: (s, t, y) -> world (X, Y, Z); P(s) integrated on the station grid."""

    def __init__(self, s: list[float], bearing_deg: list[float], origin: tuple[float, float], heading: float):
        self.s, self.origin, self.heading = s, origin, heading
        self.phi = []
        for b in bearing_deg:
            d = math.radians(b - bearing_deg[0])
            self.phi.append((d + math.pi) % (2 * math.pi) - math.pi)
        self.px, self.pz = [0.0], [0.0]
        for i in range(len(s) - 1):
            ds = s[i + 1] - s[i]
            mid = 0.5 * (self.phi[i] + self.phi[i + 1])
            self.px.append(self.px[-1] + math.sin(mid) * ds)
            self.pz.append(self.pz[-1] - math.cos(mid) * ds)

    def world_i(self, i: int, t: float, y: float) -> tuple[float, float, float]:
        return (self.px[i] - t * math.cos(self.phi[i]), y, self.pz[i] - t * math.sin(self.phi[i]))

    def _seg(self, s: float) -> tuple[int, float]:
        n = len(self.s)
        i = min(max(max(k for k in range(n - 1) if self.s[k] <= s + 1e-9), 0), n - 2)
        return i, (s - self.s[i])

    def phi_at(self, s: float) -> float:
        i, d = self._seg(s)
        u = d / (self.s[i + 1] - self.s[i])
        return self.phi[i] + (self.phi[i + 1] - self.phi[i]) * u

    def p_at(self, s: float) -> tuple[float, float]:
        i, d = self._seg(s)
        mid = 0.5 * (self.phi[i] + self.phi[i + 1])
        return (self.px[i] + math.sin(mid) * d, self.pz[i] - math.cos(mid) * d)

    def world(self, s: float, t: float, y: float) -> tuple[float, float, float]:
        px, pz = self.p_at(s)
        ph = self.phi_at(s)
        return (px - t * math.cos(ph), y, pz - t * math.sin(ph))

    def east_north(self, x: float, z: float) -> tuple[float, float]:
        h = math.radians(self.heading)
        fwd, right = -z, x
        return (fwd * math.sin(h) + right * math.cos(h), fwd * math.cos(h) - right * math.sin(h))

    def latlon(self, x: float, z: float) -> tuple[float, float]:
        e, n = self.east_north(x, z)
        lat0, lon0 = self.origin
        return (lat0 + n / M_LAT, lon0 + e / (M_LAT * math.cos(math.radians(lat0))))


# ====================================================================== plan (StreetPlan) from the v2 fixture


def hash_pick(key: str, n: int) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) % n


def build_plan(v2: dict[str, Any]) -> tuple[StreetPlan, Frame]:
    seg = v2["segment"]
    L = seg["length_m"]
    samples = v2["path"]["samples"]
    s_list = [p["s"] for p in samples]
    # footpath_side == "left": theta = -(bearing - bearing0) (spec/data.md Scene v2); heading_deg == 0
    bearing = [math.degrees(-p["theta"]) % 360.0 for p in samples]
    origin = (seg["start"]["lat"], seg["start"]["lon"])
    fr = Frame(s_list, bearing, origin, seg["heading_deg"])

    n_bins = math.ceil(L / BIN_M)
    # ground sources per 5 m bin: real hi-res satellite, one interpolated bin, a tail of overview (stand-in variety)
    bin_source = ["satellite_hires"] * 8 + ["interpolated"] + ["satellite_overview"] * 3
    assert len(bin_source) == n_bins
    levels = [1] * 4 + [2] * (n_bins - 4)

    pts, stations = [], []
    for i, p in enumerate(samples):
        x, _, z = fr.world_i(i, 0.0, 0.0)
        lat, lon = fr.latlon(x, z)
        pts.append({"s": p["s"], "lat": r6(lat), "lon": r6(lon), "bearing_deg": r3(bearing[i]), "tile_px": None})
    st_v2 = v2["corridor"]["stations"]
    for i, st in enumerate(st_v2):
        car = st["strips"][0]
        fp = st["footpath"]
        strips = [
            {"kind": "footpath", "lo": -6.1, "hi": -4.5, "material": "paver", "height_m": KERB_H, "confidence": 0.3},
            dict(car),
        ]
        stations.append({
            "s": st["s"], "lane_lat": st["lane_lat"], "footpath": fp, "strips": strips,
            "kerb": {"lat": car["hi"], "height_m": KERB_H if fp else 0.0, "width_m": KERB_W},
            "ground_src": bin_source[min(int(st["s"] // BIN_M), n_bins - 1)],
        })

    def fseg(s0, s1, lat0, lat1, h, kind, colour, source, **kw):
        return {"s_from": s0, "s_to": s1, "lat_from": lat0, "lat_to": lat1, "height_m": h, "kind": kind,
                "colour_hex": colour, "storeys": kw.pop("storeys", 0), "source": source,
                "building_id": kw.pop("building_id", None), "entity_id": kw.pop("entity_id", None),
                "interpolated": source in ("interpolated", "prior"), "confidence": kw.pop("confidence", 0.2)}

    modelled = [
        fseg(0.0, 2.5, 8.2, 8.2, 7.0, "building", "#b8b0a2", "prior"),
        fseg(2.5, 22.5, 10.0, 10.0, 9.6, "building", "#c9b79c", "osm", building_id="bld-osm-1", storeys=3, confidence=0.9),
        fseg(22.5, 25.0, 9.0, 9.0, 2.0, "wall", "#8c8479", "entity", entity_id="ent-0000000011", confidence=0.7),
        fseg(25.0, 52.5, 9.5, 9.5, 7.0, "building", "#a98f7a", "osm", building_id="bld-osm-2", storeys=2, confidence=0.9),
        fseg(52.5, 60.0, 8.2, 8.2, 8.0, "building", "#b8b0a2", "prior"),
    ]
    opposite = [
        fseg(0.0, 5.0, -7.6, -7.6, 8.5, "building", "#b0a89a", "prior"),
        fseg(5.0, 30.0, -14.0, -14.0, 12.0, "building", "#8b97a6", "osm", building_id="bld-osm-3", storeys=4, confidence=0.9),
        fseg(30.0, 35.0, -7.6, -7.6, 2.2, "gate", "#6f6f6f", "entity", entity_id="ent-0000000012", confidence=0.6),
        fseg(35.0, 60.0, -7.6, -7.6, 9.0, "building", "#b0a89a", "prior"),
    ]

    def furn(id_, type_, variant, s, lat, size, length=0.0, colour=None, canopy=None, obj=None, conf=0.8, lvl=1, desc=""):
        return {"id": id_, "type": type_, "variant": variant, "s": s, "lat": lat, "yaw_deg": 0.0,
                "size_m": size, "length_m": length, "colour_hex": colour, "canopy_hex": canopy, "object_id": obj,
                "confidence": conf, "fidelity_level": lvl, "description": desc}

    furniture = [
        furn("ent-0000000013", "pole", "utility", 5.0, 6.9, {"w": 0.3, "h": 7.0, "d": 0.3}, colour="#9aa0a6", lvl=1,
             desc="Utility pole at the back edge of the footpath."),
        furn("ent-0000000007", "bin", "", 10.0, 6.4, {"w": 0.6, "h": 0.9, "d": 0.6}, colour="#3f6b4a", conf=0.7, lvl=1,
             desc="Green dustbin on the footpath."),
        furn("ent-0000000006", "tree", "", 12.0, 8.4, {"w": 3.0, "h": 8.0, "d": 3.0}, colour="#6b4f3a", canopy="#4f7d3a",
             conf=0.9, lvl=2, desc="Large shade tree in the forecourt."),
        furn("ent-0000000002", "pole", "streetlight", 22.2, 6.24, {"w": 0.4, "h": 6.0, "d": 0.4}, length=0.0,
             colour="#9aa0a6", obj="ent-0000000002", conf=0.9, lvl=2, desc="Concrete street-light pole in the middle of the walkway."),
        furn("ent-0000000008", "bollard", "", 26.0, 4.95, {"w": 0.25, "h": 0.9, "d": 0.25}, colour="#d8d8d0", conf=0.6, lvl=2,
             desc="Short bollard at the kerb."),
        furn("ent-0000000014", "drain", "", 26.0, 4.3, {"w": 0.5, "h": 0.01, "d": 1.2}, length=1.2, colour="#4a4a4a",
             conf=0.7, lvl=2, desc="Storm drain grating at the kerb."),
        furn("ent-0000000009", "railing", "", 44.0, 6.55, {"w": 0.1, "h": 1.0, "d": 6.0}, length=6.0, colour="#5b6670",
             conf=0.7, lvl=2, desc="Metal railing along the footpath."),
        furn("ent-0000000003", "signboard", "", 40.3, 5.27, {"w": 1.2, "h": 1.1, "d": 0.4}, colour="#c98b2b",
             obj="ent-0000000003", conf=0.45, lvl=2, desc="Hanging shop sign, partly hidden."),
        furn("ent-0000000015", "manhole", "", 10.0, 1.0, {"w": 0.7, "h": 0.01, "d": 0.7}, colour="#55595e", conf=0.7, lvl=1,
             desc="Manhole cover in the carriageway."),
        furn("ent-0000000010", "crossing", "zebra", 56.0, 0.2, {"w": 9.0, "h": 0.01, "d": 2.0}, length=2.0, colour="#e8e8e8",
             obj="ent-0000000010", conf=0.8, lvl=2, desc="Zebra crossing."),
    ]

    def obj(id_, ent, typ, label, x, length, lanes, action, lateral, size, conf, desc, scenery=False, yaw=0.0):
        cx, cz = fr.world(x + length / 2, lateral, 0.0)[0], fr.world(x + length / 2, lateral, 0.0)[2]
        lat, lon = fr.latlon(cx, cz)
        return {"id": id_, "entity_id": ent, "type": typ, "label": label, "x_m": x, "length_m": length, "lanes": lanes,
                "action": action, "lateral_m": lateral, "yaw_deg": yaw, "size_m": size, "confidence": conf,
                "scenery": scenery, "description": desc, "evidence": None, "source": None, "billboard_url": None,
                "latlon": {"lat": r6(lat), "lon": r6(lon)}, "tile_px": None}

    objects = [
        obj("blocked-int-0002", None, "debris", "footpath blocked", 15.0, 5.0, [0], "dodge", 5.8,
            {"w": 1.8, "h": 0.8, "d": 5.0}, 0.8, "The footpath is blocked here."),
        obj("ent-0000000002", "ent-0000000002", "pole", "street-light pole", 22.0, 0.4, [0], "dodge", 6.24,
            {"w": 0.4, "h": 6.0, "d": 0.4}, 0.9, "Concrete street-light pole in the middle of the walkway."),
        obj("gap-int-0004", None, "footpath_gap", "footpath missing", 30.0, 7.5, [0], "dodge", 5.8,
            {"w": 1.8, "h": 0.1, "d": 7.5}, 0.8, "No footpath for 7.5 m."),
        obj("ent-0000000003", "ent-0000000003", "signboard", "shop signboard", 40.0, 0.6, [0], "slide", 5.27,
            {"w": 1.2, "h": 1.1, "d": 0.4}, 0.45, "Hanging shop sign, partly hidden."),
        obj("ent-0000000005", "ent-0000000005", "debris", "debris on footpath", 50.0, 1.4, [0], "dodge", 5.3,
            {"w": 1.2, "h": 0.6, "d": 1.4}, 0.8, "Rubble on the footpath."),
        obj("ent-0000000010", "ent-0000000010", "crossing", "zebra crossing", 55.0, 2.0, [1, 2], "none", 0.2,
            {"w": 9.0, "h": 0.01, "d": 2.0}, 0.8, "Zebra crossing."),
    ]
    objects.sort(key=lambda o: (o["x_m"], o["id"]))

    def tok(id_, x, lane, reason):
        w = fr.world(x, 5.8 if lane == 0 else 3.8, 0.0)
        lat, lon = fr.latlon(w[0], w[2])
        return {"id": id_, "x_m": x, "lane": lane, "reason": reason, "latlon": {"lat": r6(lat), "lon": r6(lon)}, "tile_px": [0.0, 0.0]}

    tokens = [tok("t1", 4.0, 0, "intact_footpath"), tok("t2", 14.0, 0, "intact_footpath"),
              tok("t3", 42.0, 0, "intact_footpath"), tok("t4", 58.5, 0, "intact_footpath"),
              tok("t5", 55.5, 1, "crossing")]
    tokens.sort(key=lambda t: t["x_m"])

    # tile pixels (overview satellite): centre = the s=30 centreline point; px = 640 + east/mpp, 640 - north/mpp
    cx, cz = fr.world(30.0, 0.0, 0.0)[0], fr.world(30.0, 0.0, 0.0)[2]
    ce, cn = fr.east_north(cx, cz)

    def tile_px(x: float, z: float) -> list[float]:
        e, n = fr.east_north(x, z)
        return [r3(640.0 + (e - ce) / MPP), r3(640.0 - (n - cn) / MPP)]

    for p, (x, z) in zip(pts, [(fr.px[i], fr.pz[i]) for i in range(len(pts))]):
        p["tile_px"] = tile_px(x, z)
    for o in objects:
        w = fr.world(o["x_m"] + o["length_m"] / 2, o["lateral_m"], 0.0)
        o["tile_px"] = tile_px(w[0], w[2])
    for t in tokens:
        w = fr.world(t["x_m"], 5.8 if t["lane"] == 0 else 3.8, 0.0)
        t["tile_px"] = tile_px(w[0], w[2])

    interp = [
        {"what": "frontage", "side": "left", "s_from": 0.0, "s_to": 2.5, "method": "prior", "from_s": [],
         "note": "No footprint or photo here; a neutral facade is used."},
        {"what": "frontage", "side": "left", "s_from": 52.5, "s_to": 60.0, "method": "prior", "from_s": [],
         "note": "No footprint or photo here; a neutral facade is used."},
        {"what": "frontage", "side": "right", "s_from": 0.0, "s_to": 5.0, "method": "prior", "from_s": [],
         "note": "No footprint or photo covers the right-hand side here; a neutral facade is used."},
        {"what": "frontage", "side": "right", "s_from": 35.0, "s_to": 60.0, "method": "prior", "from_s": [],
         "note": "No footprint or photo covers the right-hand side here; a neutral facade is used."},
        {"what": "ground_texture", "side": "both", "s_from": 40.0, "s_to": 45.0, "method": "neighbours", "from_s": [35.0, 45.0],
         "note": "No hi-res satellite crop covers 40-45 m; blended from the neighbouring stretches."},
    ]

    plan_dict = {
        "plan": "street-plan/1", "slug": SLUG, "name": NAME, "city": seg["city"], "model_version": MODEL_VERSION,
        "model_hash": MODEL_HASH, "compiled_by": COMPILED_BY, "footpath_side": "left", "length_m": L,
        "frame": {"origin": {"lat": origin[0], "lon": origin[1]}, "heading_deg": seg["heading_deg"]},
        "reference_line": {"source": "osm_nominatim", "osm_way_id": 25173540, "name": "Contract Curved Road",
                           "length_s": L, "step_m": STEP, "points": pts},
        "corridor_step_m": STEP, "stations": stations,
        "ground_tex": {"mode": "baked_strip", "procedural_seed": int(hashlib.sha1(SLUG.encode()).hexdigest()[:7], 16),
                       "bin_m": BIN_M, "bin_source": bin_source,
                       "strip": {"url": f"/media/textures/{SLUG}/ground_v{MODEL_VERSION}_00000000.jpg", "size_px": [56, 120],
                                 "s_from": 0.0, "s_to": L, "t_lo": -BAND, "t_hi": BAND, "m_per_px": 0.5,
                                 "bin_source": bin_source}},
        "frontage": {"step_m": STEP, "sides": [
            {"side": "modelled", "texture": {"side": "modelled", "url": f"/media/textures/{SLUG}/frontage_modelled_v2_00000000.jpg",
                                              "size_px": [120, 32], "s_from": 0.0, "s_to": L,
                                              "bin_source": ["interpolated"] + ["street_view"] * 4 + ["street_view", "street_view", "street_view", "street_view", "street_view", "interpolated", "interpolated"]},
             "segments": modelled},
            {"side": "opposite", "texture": None, "segments": opposite}]},
        "furniture": sorted(furniture, key=lambda f: (f["s"], f["id"])),
        "objects": objects, "tokens": tokens,
        "dynamic_context": {"excluded_count": 2, "entity_ids": ["ent-0000000001", "ent-0000000004"],
                            "spawn_hints": {"density": r3(min(1.0, (2 / L * 100.0) / 40.0)),
                                            "type_weights": dict(load_gameplay()["spawner"]["default_spawn_hints"]["type_weights"])}},
        "fidelity": {"overall_level": 2, "score": r3(sum(levels) / len(levels)),
                     "bins": [{"index": i, "s_from": i * BIN_M, "s_to": min(L, (i + 1) * BIN_M), "level": lv} for i, lv in enumerate(levels)]},
        "interpolated": interp,
    }
    plan = StreetPlan.model_validate(plan_dict)
    return plan, fr


# ====================================================================== mesh building (boxes / quads)


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(v):
    n = math.sqrt(dot(v, v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def mm(p):
    return (round(p[0], 3) + 0.0, round(p[1], 3) + 0.0, round(p[2], 3) + 0.0)


class Builder:
    """Flat-shaded, non-indexed triangle soup for one node (every triangle gets its own 3 vertices)."""

    def __init__(self, name: str, uv: bool, colour: bool):
        self.name, self.has_uv, self.has_colour = name, uv, colour
        self.pos, self.nrm, self.uvs, self.cols, self.idx = [], [], [], [], []

    def tri(self, p0, p1, p2, want=None, uvs=None, col=None, outward=None):
        """Emit one triangle; flip so the CCW normal points along ``want`` (or away from the point ``outward``)."""
        p0, p1, p2 = mm(p0), mm(p1), mm(p2)
        n = cross(sub(p1, p0), sub(p2, p0))
        if dot(n, n) < 1e-12:
            return  # degenerate
        if want is None and outward is not None:
            c = ((p0[0] + p1[0] + p2[0]) / 3, (p0[1] + p1[1] + p2[1]) / 3, (p0[2] + p1[2] + p2[2]) / 3)
            want = sub(c, outward)
        flip = want is not None and dot(n, want) < 0
        order = [0, 2, 1] if flip else [0, 1, 2]
        pts = [p0, p1, p2]
        nn = norm(cross(sub(pts[order[1]], pts[order[0]]), sub(pts[order[2]], pts[order[0]])))
        for k in order:
            self.idx.append(len(self.pos))
            self.pos.append(pts[k])
            self.nrm.append((round(nn[0], 4) + 0.0, round(nn[1], 4) + 0.0, round(nn[2], 4) + 0.0))
            if self.has_uv:
                self.uvs.append(tuple(round(c, 5) for c in uvs[k]))
            if self.has_colour:
                self.cols.append(col)

    def quad(self, a, b, c, d, want=None, uvs=None, col=None, outward=None):
        u = uvs or [None] * 4
        self.tri(a, b, c, want, [u[0], u[1], u[2]], col, outward)
        self.tri(a, c, d, want, [u[0], u[2], u[3]], col, outward)

    def box(self, centre, ax_f, ax_n, w, h, d, y0, col):
        """Box on the ground: ``d`` along ax_f, ``w`` along ax_n, from y0 to y0+h; centre = (x, z)."""
        cx, cz = centre
        pts = {}
        for a in (-1, 1):
            for b in (-1, 1):
                for yy in (0, 1):
                    x = cx + ax_f[0] * a * d / 2 + ax_n[0] * b * w / 2
                    z = cz + ax_f[1] * a * d / 2 + ax_n[1] * b * w / 2
                    pts[(a, b, yy)] = (x, y0 + yy * h, z)
        c3 = (cx, y0 + h / 2, cz)
        faces = [((-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)), ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)),
                 ((-1, -1, 0), (1, -1, 0), (1, -1, 1), (-1, -1, 1)), ((-1, 1, 0), (1, 1, 0), (1, 1, 1), (-1, 1, 1)),
                 ((-1, -1, 0), (-1, 1, 0), (-1, 1, 1), (-1, -1, 1)), ((1, -1, 0), (1, 1, 0), (1, 1, 1), (1, -1, 1))]
        for f in faces:
            self.quad(*[pts[k] for k in f], col=col, outward=c3)

    def frustum(self, centre, r0, r1, h, y0, col, sides=6):
        cx, cz = centre
        ring0 = [(cx + r0 * math.cos(2 * math.pi * k / sides), y0, cz + r0 * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
        ring1 = [(cx + r1 * math.cos(2 * math.pi * k / sides), y0 + h, cz + r1 * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
        c3 = (cx, y0 + h / 2, cz)
        for k in range(sides):
            k2 = (k + 1) % sides
            self.quad(ring0[k], ring0[k2], ring1[k2], ring1[k], col=col, outward=c3)
            self.tri((cx, y0 + h, cz), ring1[k], ring1[k2], want=(0, 1, 0), col=col)

    def blob(self, centre, r, col, y_scale=0.8):
        cx, cy, cz = centre
        top, bot = (cx, cy + r * y_scale, cz), (cx, cy - r * y_scale, cz)
        eq = [(cx + r, cy, cz), (cx, cy, cz + r), (cx - r, cy, cz), (cx, cy, cz - r)]
        for k in range(4):
            a, b = eq[k], eq[(k + 1) % 4]
            self.tri(top, a, b, col=col, outward=centre)
            self.tri(bot, a, b, col=col, outward=centre)

    def node(self, extras: dict[str, Any]) -> MeshNode | None:
        if not self.idx:
            return None
        return MeshNode(self.name, NODE_MATERIAL[self.name], self.pos, self.nrm, self.idx,
                        self.uvs if self.has_uv else None, self.cols if self.has_colour else None, extras)


def build_mesh(plan: StreetPlan, fr: Frame, gp: dict) -> tuple[StreetMesh, list[dict]]:
    L = plan.length_m
    n = len(plan.stations)
    side = plan.footpath_side
    T = lambda lat: t_of_lat(lat, side)  # noqa: E731
    S = [st.s for st in plan.stations]
    UP = (0.0, 1.0, 0.0)

    def n_dir(i, sign):  # direction of increasing |t| on the left (sign=+1) / right (-1), world XZ
        return (-math.cos(fr.phi[i]) * sign, 0.0, -math.sin(fr.phi[i]) * sign)

    def uv_g(s, t):
        return ((BAND - t) / (2 * BAND), (L - s) / L)

    def uv_f(s, h, v0):
        return (s / L, v0 + (1.0 - min(h, 12.0) / 12.0) * 0.5)

    WHITE = (255, 255, 255, 255)
    state_col = {"intact": (236, 232, 222, 255), "broken": (196, 186, 174, 255), "blocked": (214, 196, 190, 255),
                 "encroached": (210, 198, 184, 255), "unknown": (225, 220, 210, 255), "missing": (200, 190, 170, 255)}
    ROAD_COL = (178, 178, 184, 255)
    GROUND_COL = (212, 204, 188, 255)
    OPP_COL = (225, 220, 210, 255)

    car = [next(s for s in st.strips if s.kind.value == "carriageway") for st in plan.stations]
    opp = [next(s for s in st.strips if s.kind.value == "footpath") for st in plan.stations]
    fps = [st.footpath for st in plan.stations]

    def seg_at(sd, s_mid):
        for sg in sd.segments:
            if sg.s_from - 1e-9 <= s_mid <= sg.s_to + 1e-9:
                return sg
        raise AssertionError(s_mid)

    mod_side, opp_side = plan.frontage.sides

    # ---- ground nodes
    foot, foot_o, road, kerb, gl, gr = (Builder("Footpath", True, True), Builder("Footpath_Opposite", True, True),
                                         Builder("Road", True, True), Builder("Kerb", False, False),
                                         Builder("Ground_L", True, True), Builder("Ground_R", True, True))
    for i in range(n - 1):
        j = i + 1
        sm = 0.5 * (S[i] + S[j])

        def strip_quad(b, lo_i, hi_i, lo_j, hi_j, y, col):
            a_, b_, c_, d_ = (fr.world_i(i, lo_i, y), fr.world_i(i, hi_i, y), fr.world_i(j, hi_j, y), fr.world_i(j, lo_j, y))
            b.quad(a_, b_, c_, d_, want=UP, uvs=[uv_g(S[i], lo_i), uv_g(S[i], hi_i), uv_g(S[j], hi_j), uv_g(S[j], lo_j)], col=col)

        strip_quad(road, car[i].lo, car[i].hi, car[j].lo, car[j].hi, 0.0, ROAD_COL)
        strip_quad(foot_o, opp[i].lo, opp[i].hi, opp[j].lo, opp[j].hi, KERB_H, OPP_COL)
        fp_here = fps[i] is not None
        if fp_here:
            fj = fps[j] or fps[i]
            col = state_col[fps[i].state.value]
            strip_quad(foot, fps[i].lo, fps[i].hi, fj.lo, fj.hi, KERB_H, col)
            inner_i, inner_j = fps[i].hi, fj.hi
            # modelled kerb: face toward the road + 0.2 m top
            kt_i, kt_j = plan.stations[i].kerb.lat, plan.stations[j].kerb.lat
            kerb.quad(fr.world_i(i, kt_i, 0.0), fr.world_i(j, kt_j, 0.0), fr.world_i(j, kt_j, KERB_H), fr.world_i(i, kt_i, KERB_H),
                      want=(-n_dir(i, 1)[0], 0.0, -n_dir(i, 1)[2]))
            kerb.quad(fr.world_i(i, kt_i, KERB_H), fr.world_i(i, kt_i + KERB_W, KERB_H), fr.world_i(j, kt_j + KERB_W, KERB_H),
                      fr.world_i(j, kt_j, KERB_H), want=UP)
        else:
            inner_i, inner_j = car[i].hi, car[j].hi
        # opposite kerb (prior template): face toward the road + top
        ko_i, ko_j = car[i].lo, car[j].lo
        kerb.quad(fr.world_i(i, ko_i, 0.0), fr.world_i(j, ko_j, 0.0), fr.world_i(j, ko_j, KERB_H), fr.world_i(i, ko_i, KERB_H),
                  want=n_dir(i, 1))
        kerb.quad(fr.world_i(i, ko_i - KERB_W, KERB_H), fr.world_i(i, ko_i, KERB_H), fr.world_i(j, ko_j, KERB_H),
                  fr.world_i(j, ko_j - KERB_W, KERB_H), want=UP)
        # ground between footpath/road edge and the wall (no hole)
        wl = T(seg_at(mod_side, sm).lat_from)
        wr = T(seg_at(opp_side, sm).lat_from)
        strip_quad(gl, inner_i, wl, inner_j, wl, 0.0, GROUND_COL)
        strip_quad(gr, wr, opp[i].lo, wr, opp[j].lo, 0.0, GROUND_COL)

    # ---- frontage nodes (contiguous in s; faces closing lateral steps) + caps
    fl, fr_, caps = Builder("Frontage_L", True, False), Builder("Frontage_R", True, False), Builder("Frontage_Caps", False, True)
    fl_seg, fr_seg = [], []

    def toward_street(i, sign):
        d = n_dir(i, sign)
        return (-d[0], 0.0, -d[2])

    def idx_of(sg):
        return next(k for k in range(n) if abs(S[k] - sg) < 1e-6)

    def build_frontage(b: Builder, sd, sign: int, v0: float, out: list):
        segs = sd.segments
        for k, sg in enumerate(segs):
            start = len(b.idx)
            t = T(sg.lat_from)
            i0, i1 = idx_of(sg.s_from), idx_of(sg.s_to)
            for i in range(i0, i1):
                j = i + 1
                p = [fr.world_i(i, t, 0.0), fr.world_i(j, t, 0.0), fr.world_i(j, t, sg.height_m), fr.world_i(i, t, sg.height_m)]
                hs = [0.0, 0.0, sg.height_m, sg.height_m]
                ss = [S[i], S[j], S[j], S[i]]
                b.quad(*p, want=toward_street(i, sign), uvs=[uv_f(s_, h_, v0) for s_, h_ in zip(ss, hs)])
            # closing faces at the boundaries where this segment is the nearer one
            for nb, at, facing in ((segs[k - 1] if k > 0 else None, sg.s_from, -1), (segs[k + 1] if k + 1 < len(segs) else None, sg.s_to, +1)):
                if nb is None or abs(nb.lat_from - sg.lat_from) < 1e-9:
                    continue
                if abs(sg.lat_from) < abs(nb.lat_from):
                    i = idx_of(at)
                    ta, tb = T(sg.lat_from), T(nb.lat_from)
                    fw = (math.sin(fr.phi[i]) * facing, 0.0, -math.cos(fr.phi[i]) * facing)
                    b.quad(fr.world_i(i, ta, 0.0), fr.world_i(i, tb, 0.0), fr.world_i(i, tb, sg.height_m), fr.world_i(i, ta, sg.height_m),
                           want=fw, uvs=[uv_f(at, 0.0, v0), uv_f(at, 0.0, v0), uv_f(at, sg.height_m, v0), uv_f(at, sg.height_m, v0)])
            out.append({"s_from": sg.s_from, "s_to": sg.s_to, "type": sg.kind, "source": sg.source, "building_id": sg.building_id,
                        "entity_id": sg.entity_id, "height_m": sg.height_m, "interpolated": sg.interpolated,
                        "confidence": sg.confidence,
                        "fidelity_level": plan.fidelity.bins[min(int(sg.s_from // BIN_M), len(plan.fidelity.bins) - 1)].level,
                        "index_start": start, "index_count": len(b.idx) - start})
            # roof cap
            depth = {"building": 4.0, "wall": 0.4, "gate": 0.4}[sg.kind]
            rgb = hex_rgb(sg.colour_hex)
            ccol = (int(rgb[0] * 0.7), int(rgb[1] * 0.7), int(rgb[2] * 0.7), 255)
            for i in range(i0, i1):
                j = i + 1
                t0 = T(sg.lat_from)
                t1 = t0 + math.copysign(depth, t0)
                caps.quad(fr.world_i(i, t0, sg.height_m), fr.world_i(i, t1, sg.height_m), fr.world_i(j, t1, sg.height_m),
                          fr.world_i(j, t0, sg.height_m), want=UP, col=ccol)

    build_frontage(fl, mod_side, +1 if T(1.0) > 0 else -1, 0.0, fl_seg)
    build_frontage(fr_, opp_side, -1 if T(1.0) > 0 else +1, 0.5, fr_seg)

    # ---- furniture (boxes / frustums / blobs / decals); entity ranges recorded per node
    fb = {nm: Builder(nm, False, True) for nm in ("Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals")}
    fents: dict[str, list] = {k: [] for k in fb}
    ent_rows = []
    tint_default = {"pole": "#9aa0a6", "bin": "#3f6b4a", "bollard": "#d8d8d0", "railing": "#5b6670", "signboard": "#c98b2b",
                    "drain": "#4a4a4a", "manhole": "#55595e", "crossing": "#e8e8e8", "tree": "#6b4f3a"}
    for f in plan.furniture:
        node = {"pole": "Furniture_Poles", "tree": "Furniture_Trees", "railing": "Furniture_Railings", "drain": "Furniture_Decals",
                "manhole": "Furniture_Decals", "crossing": "Furniture_Decals", "kerb_cut": "Furniture_Decals", "ramp": "Furniture_Decals"
                }.get(f.type, "Furniture_Boxes")
        b = fb[node]
        start = len(b.idx)
        t = T(f.lat)
        cx, _, cz = fr.world(f.s, t, 0.0)
        ph = fr.phi_at(f.s) + math.radians(f.yaw_deg)
        axf, axn = (math.sin(ph), -math.cos(ph)), (math.cos(ph), math.sin(ph))
        rgb = hex_rgb(f.colour_hex or tint_default.get(f.type, "#999999"))
        col = (*rgb, 255)
        w, h, d = f.size_m["w"], f.size_m["h"], f.size_m["d"]
        surf = KERB_H if (abs(t) > car[0].hi - 1e-6 and abs(t) < 7.0 and f.type not in ("drain", "manhole", "crossing")) else 0.0
        if f.type == "pole":
            b.frustum((cx, cz), w / 2, w / 3.2, h, surf, col)
            if f.variant == "utility":
                b.box((cx, cz), axf, axn, 1.4, 0.12, 0.12, surf + h - 0.5, col)
        elif f.type == "tree":
            b.frustum((cx, cz), 0.18, 0.12, h * 0.45, surf, (107, 79, 58, 255))
            cn = hex_rgb(f.canopy_hex or "#4f7d3a")
            b.blob((cx, surf + h * 0.62, cz), w / 2, (*cn, 255))
            b.blob((cx + axf[0] * 0.5, surf + h * 0.82, cz + axf[1] * 0.5), w / 3.2, (int(cn[0] * 0.85), int(cn[1] * 0.85), int(cn[2] * 0.85), 255))
        elif f.type == "railing":
            b.box((cx, cz), axf, axn, w, h, d, surf, col)
        elif f.type in ("drain", "manhole", "crossing"):
            y = surf + 0.01
            q = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
            P = [(cx + axf[0] * a * d / 2 + axn[0] * c * w / 2, y, cz + axf[1] * a * d / 2 + axn[1] * c * w / 2) for a, c in q]
            b.quad(*P, want=UP, col=col)
        elif f.type == "signboard":
            b.box((cx, cz), axf, axn, w, h, d, 1.7, col)
        else:  # bin, bollard, bus_stop
            b.box((cx, cz), axf, axn, w, h, d, surf, col)
        fents[node].append({"id": f.id, "type": f.type, "variant": f.variant, "s": r3(f.s), "t": r3(t), "yaw_deg": f.yaw_deg,
                            "confidence": f.confidence, "fidelity_level": f.fidelity_level, "index_start": start,
                            "index_count": len(b.idx) - start})
        ent_rows.append({"id": f.id, "type": f.type, "variant": f.variant, "s": r3(f.s), "t": r3(t), "yaw_deg": f.yaw_deg,
                         "size_m": {"w": f.size_m["w"], "h": f.size_m["h"], "d": f.size_m["d"]}, "length_m": f.length_m,
                         "colour_hex": f.colour_hex, "canopy_hex": f.canopy_hex, "confidence": f.confidence,
                         "fidelity_level": f.fidelity_level, "node": node, "description": f.description})

    # ---- node extras
    bins = [{"index": k, "s_from": k * BIN_M, "s_to": min(L, (k + 1) * BIN_M), "ground_source": plan.ground_tex.bin_source[k],
             "fidelity_level": plan.fidelity.bins[k].level, "interpolated": plan.ground_tex.bin_source[k] == "interpolated"}
            for k in range(len(plan.ground_tex.bin_source))]

    def runs_footpath():
        runs, cur = [], None
        for i in range(n - 1):
            fp = fps[i]
            if fp is None:
                cur = None
                continue
            if cur and cur["state"] == fp.state.value and abs(cur["s_to"] - S[i]) < 1e-9:
                cur["s_to"] = S[i + 1]
            else:
                cur = {"s_from": S[i], "s_to": S[i + 1], "state": fp.state.value, "material": fp.material.value,
                       "height_m": fp.height_m, "confidence": fp.confidence}
                runs.append(cur)
        return runs

    side_name = lambda lat: "left" if T(lat) > 0 else "right"  # noqa: E731
    ex = {
        "Footpath": {"kind": "footpath", "side": side_name(1.0), "runs": runs_footpath(), "bins": bins},
        "Footpath_Opposite": {"kind": "footpath", "side": side_name(-1.0),
                              "runs": [{"s_from": 0.0, "s_to": L, "state": "unknown", "material": "paver", "height_m": KERB_H, "confidence": 0.3}],
                              "bins": bins},
        "Road": {"kind": "road", "side": "both",
                 "runs": [{"s_from": 0.0, "s_to": L, "state": "fair", "material": "asphalt", "height_m": 0.0, "confidence": 0.7}], "bins": bins},
        "Kerb": {"kind": "kerb", "side": "both", "runs": [], "bins": bins},
        "Ground_L": {"kind": "ground", "side": "left", "runs": [], "bins": bins},
        "Ground_R": {"kind": "ground", "side": "right", "runs": [], "bins": bins},
        "Frontage_L": {"kind": "frontage", "side": "left", "segments": fl_seg},
        "Frontage_R": {"kind": "frontage", "side": "right", "segments": fr_seg},
        "Frontage_Caps": {"kind": "frontage_caps"},
    }
    for nm, cat in FURNITURE_CATEGORY.items():
        ex[nm] = {"kind": "furniture", "category": cat, "entities": fents[nm]}

    builders = {"Footpath": foot, "Footpath_Opposite": foot_o, "Road": road, "Kerb": kerb, "Ground_L": gl, "Ground_R": gr,
                "Frontage_L": fl, "Frontage_R": fr_, "Frontage_Caps": caps, **fb}
    nodes = []
    for nm in NODE_NAMES:
        mn = builders[nm].node(ex[nm])
        if mn is not None:
            nodes.append(mn)

    gp_sha = gameplay_sha256()
    asset_extras = {
        "schema": ASSET_SCHEMA, "generator": GENERATOR, "segment_slug": SLUG, "model_version": MODEL_VERSION,
        "model_hash": MODEL_HASH, "footpath_side": side, "units": "metres", "up_axis": "+Y", "travel": "-Z at s=0",
        "right": "+X at s=0", "length_s": L,
        "frame": {"origin": {"lat": plan.frame.origin.lat, "lon": plan.frame.origin.lon}, "heading_deg": plan.frame.heading_deg},
        "manifest": "street.manifest.json", "gameplay_sha256": gp_sha,
    }
    materials = [MeshMaterial("mat_ground", "#ffffff", "atlas_ground", 0.0, 1.0), MeshMaterial("mat_kerb", KERB_COLOUR),
                 MeshMaterial("mat_frontage", "#ffffff", "atlas_facade"), MeshMaterial("mat_flat", "#ffffff")]
    textures = {"atlas_ground": ground_atlas(plan), "atlas_facade": facade_atlas(plan)}
    return StreetMesh(asset_extras, nodes, materials, textures, L), ent_rows


# ---------------------------------------------------------------------- placeholder textures


def _noise(x: int, y: int, k: int = 0) -> int:
    h = (x * 73856093 ^ y * 19349663 ^ k * 83492791) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 0x5BD1E995 & 0xFFFFFFFF
    return (h >> 16) & 0xFF


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, subsampling=2, optimize=False, progressive=False)
    return buf.getvalue()


def ground_atlas(plan: StreetPlan) -> bytes:
    """56 x 120 px: image up = travel, column 0 = t +14. Painted per (t, s) from the plan, so it matches the strips."""
    W, H = 56, 120
    L = plan.length_m
    img = Image.new("RGB", (W, H))
    px = img.load()
    S = [st.s for st in plan.stations]
    for y in range(H):
        s = L - (y + 0.5) * L / H
        i = min(max(int(s // STEP), 0), len(S) - 2)
        u = (s - S[i]) / STEP
        st, st2 = plan.stations[i], plan.stations[i + 1]
        car = lambda stn: next(q for q in stn.strips if q.kind.value == "carriageway")  # noqa: E731
        lo = car(st).lo + (car(st2).lo - car(st).lo) * u
        hi = car(st).hi + (car(st2).hi - car(st).hi) * u
        fp = st.footpath
        for x in range(W):
            t = BAND - (x + 0.5) * 2 * BAND / W
            nz = _noise(x, y) % 24 - 12
            if lo <= t <= hi:
                base = (70, 72, 78)
                if abs(t) < 0.08 or abs(t - (lo + hi) / 2) < 0.12:
                    base = (200, 200, 190)
            elif hi < t <= hi + KERB_W and fp:
                base = (185, 185, 185)
            elif fp and fp.lo <= t <= fp.hi:
                base = (158, 146, 128)
                if x % 3 == 0 or y % 3 == 0:
                    base = (140, 130, 114)
            elif -6.1 <= t <= -4.5:
                base = (150, 144, 134)
            else:
                base = (118, 104, 86)
            px[x, y] = tuple(max(0, min(255, c + nz)) for c in base)
    return _jpeg(img)


def facade_atlas(plan: StreetPlan) -> bytes:
    """120 x 64 px: top half = left ribbon, bottom half = right ribbon; u = s/L, v within a half = 1 - h/12."""
    W, H = 120, 64
    L = plan.length_m
    img = Image.new("RGB", (W, H))
    px = img.load()
    mod_side, opp_side = plan.frontage.sides
    for half, sd in ((0, mod_side), (1, opp_side)):
        for x in range(W):
            s = (x + 0.5) * L / W
            sg = next(g for g in sd.segments if g.s_from <= s <= g.s_to)
            base = hex_rgb(sg.colour_hex)
            for yy in range(H // 2):
                h = (1.0 - (yy + 0.5) / (H // 2)) * 12.0
                c = base
                if h > sg.height_m:
                    c = (150, 170, 200)  # above the wall (never sampled)
                elif sg.kind == "building" and sg.source != "prior":
                    fl_ = (h % 3.0)
                    if 1.0 < fl_ < 2.3 and (s % 3.2) < 1.4 and h > 1.0:
                        c = (58, 70, 90)
                px[x, half * (H // 2) + yy] = tuple(max(0, min(255, k + _noise(x, yy, half) % 10 - 5)) for k in c)
    return _jpeg(img)


# ====================================================================== glb writer (stand-in, self-contained)


def write_glb_standin(mesh: StreetMesh) -> bytes:
    bin_ = bytearray()
    views, accessors = [], []

    def add_view(data: bytes, target: int | None) -> int:
        while len(bin_) % 4:
            bin_.append(0)
        v = {"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)}
        if target:
            v["target"] = target
        bin_.extend(data)
        views.append(v)
        return len(views) - 1

    def add_acc(view, ctype, count, atype, **kw):
        a = {"bufferView": view, "componentType": ctype, "count": count, "type": atype}
        a.update(kw)
        accessors.append(a)
        return len(accessors) - 1

    meshes, nodes = [], [{"name": ROOT_NODE, "children": list(range(1, len(mesh.nodes) + 1))}]
    mat_index = {m.name: i for i, m in enumerate(mesh.materials)}
    for nd in mesh.nodes:
        attrs = {}
        f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]  # noqa: E731  (min/max must equal the stored float32 values)
        xs, ys, zs = ([f32(p[k]) for p in nd.positions] for k in range(3))
        xs, ys, zs = list(xs), list(ys), list(zs)
        v = add_view(b"".join(struct.pack("<fff", *p) for p in nd.positions), 34962)
        attrs["POSITION"] = add_acc(v, 5126, len(nd.positions), "VEC3", min=[min(xs), min(ys), min(zs)], max=[max(xs), max(ys), max(zs)])
        v = add_view(b"".join(struct.pack("<fff", *p) for p in nd.normals), 34962)
        attrs["NORMAL"] = add_acc(v, 5126, len(nd.normals), "VEC3")
        if nd.uvs is not None:
            v = add_view(b"".join(struct.pack("<ff", *p) for p in nd.uvs), 34962)
            attrs["TEXCOORD_0"] = add_acc(v, 5126, len(nd.uvs), "VEC2")
        if nd.colors is not None:
            v = add_view(b"".join(struct.pack("<BBBB", *p) for p in nd.colors), 34962)
            attrs["COLOR_0"] = add_acc(v, 5121, len(nd.colors), "VEC4", normalized=True)
        big = len(nd.positions) >= 65535
        fmt, ctype = ("<I", 5125) if big else ("<H", 5123)
        v = add_view(b"".join(struct.pack(fmt, i) for i in nd.indices), 34963)
        ia = add_acc(v, ctype, len(nd.indices), "SCALAR", min=[min(nd.indices)], max=[max(nd.indices)])
        meshes.append({"name": nd.name, "primitives": [{"attributes": attrs, "indices": ia, "material": mat_index[nd.material], "mode": 4}]})
        nodes.append({"name": nd.name, "mesh": len(meshes) - 1, "extras": nd.extras})

    images, textures = [], []
    tex_index = {}
    for name, data in mesh.textures.items():
        v = add_view(data, None)
        images.append({"name": name, "mimeType": "image/jpeg", "bufferView": v})
        textures.append({"sampler": 0, "source": len(images) - 1, "name": name})
        tex_index[name] = len(textures) - 1
    materials = []
    for m in mesh.materials:
        rgb = hex_rgb(m.base_color_hex)
        pbr: dict[str, Any] = {"baseColorFactor": [round(srgb_to_linear(c), 6) for c in rgb] + [1.0]}
        if m.texture:
            pbr["baseColorTexture"] = {"index": tex_index[m.texture]}
        pbr["metallicFactor"] = m.metallic
        pbr["roughnessFactor"] = m.roughness
        materials.append({"name": m.name, "pbrMetallicRoughness": pbr, "doubleSided": False, "alphaMode": "OPAQUE"})
    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": GENERATOR, "extras": mesh.asset_extras},
        "scene": 0, "scenes": [{"name": SCENE_NAME, "nodes": [0]}], "nodes": nodes, "meshes": meshes, "materials": materials,
        "textures": textures, "images": images,
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(bin_)}],
    }
    js = json.dumps(doc, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    js += b" " * (-len(js) % 4)
    body = bytes(bin_) + b"\x00" * (-len(bin_) % 4)
    total = 12 + 8 + len(js) + 8 + len(body)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(body), 0x004E4942) + body)


# ====================================================================== manifest + xodr + scene


def build_xodr(plan: StreetPlan, fr: Frame) -> str:
    """Best-effort OpenDRIVE 1.7 (stand-in): polyline planView, one laneSection per footpath/kerb structure change."""
    n = len(plan.stations)
    S = [st.s for st in plan.stations]
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    b0 = plan.reference_line.points[0].bearing_deg
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<OpenDRIVE>",
           f'  <header revMajor="1" revMinor="7" name="{SLUG}" version="1" date="2026-01-01T00:00:00" north="0" south="0" east="0" west="0" vendor="hero">',
           f'    <geoReference><![CDATA[+proj=tmerc +lat_0={plan.frame.origin.lat} +lon_0={plan.frame.origin.lon} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs]]></geoReference>',
           "  </header>", f'  <road name="{NAME}" length="{L_fmt(plan.length_m)}" id="1" junction="-1">', "    <planView>"]
    h0 = math.radians(plan.frame.heading_deg)

    def enu(x, z):
        e, nn = fr.east_north(x, z)
        return e, nn

    for i in range(n - 1):
        ds = S[i + 1] - S[i]
        mid = 0.5 * (fr.phi[i] + fr.phi[i + 1])
        e, nn = enu(fr.px[i], fr.pz[i])
        hdg = math.pi / 2 - (h0 + mid)
        out.append(f'      <geometry s="{L_fmt(S[i])}" x="{e:.4f}" y="{nn:.4f}" hdg="{hdg:.6f}" length="{L_fmt(ds)}"><line/></geometry>')
    out.append("    </planView>")
    out.append('    <elevationProfile><elevation s="0" a="0" b="0" c="0" d="0"/></elevationProfile>')
    out.append("    <lanes>")
    # section structure: (footpath present, kerb present) per station-interval
    keys = [(plan.stations[i].footpath is not None) for i in range(n - 1)]
    starts = [0] + [i for i in range(1, n - 1) if keys[i] != keys[i - 1]]
    for si, a in enumerate(starts):
        b = starts[si + 1] if si + 1 < len(starts) else n - 1
        fp_on = keys[a]

        def lane_xml(lid, typ, wfun):
            ws = []
            for i in range(a, b):
                w0, w1 = wfun(i), wfun(i + 1)
                ws.append(f'            <width sOffset="{L_fmt(S[i] - S[a])}" a="{w0:.4f}" b="{(w1 - w0) / (S[i + 1] - S[i]):.6f}" c="0" d="0"/>')
            return [f'          <lane id="{lid}" type="{typ}" level="false">', *ws, "          </lane>"]

        def car(i):
            return next(q for q in plan.stations[i].strips if q.kind.value == "carriageway")

        def oppfp(i):
            return next(q for q in plan.stations[i].strips if q.kind.value == "footpath")

        def fpw(i):
            f = plan.stations[min(i, b - 1) if i >= b else i].footpath or plan.stations[a].footpath
            return (f.hi - f.lo) if f else 0.0

        lw = lambda i: max(0.0, T(car(i).hi if plan.footpath_side == "left" else -car(i).lo))  # noqa: E731
        # left lanes are t > 0 (carriageway part left of the reference line)
        left_car = lambda i: max(0.0, car(i).hi)  # noqa: E731
        right_car = lambda i: max(0.0, -car(i).lo)  # noqa: E731
        out.append(f'      <laneSection s="{L_fmt(S[a])}">')
        out.append("        <left>")
        out += lane_xml(1, "driving", left_car)
        if fp_on:
            out += lane_xml(2, "curb", lambda i: KERB_W)
            out += lane_xml(3, "sidewalk", fpw)
        out.append("        </left>")
        out.append('        <center><lane id="0" type="none" level="false"/></center>')
        out.append("        <right>")
        out += lane_xml(-1, "driving", right_car)
        out += lane_xml(-2, "curb", lambda i: KERB_W)
        out += lane_xml(-3, "sidewalk", lambda i: oppfp(i).hi - oppfp(i).lo)
        out.append("        </right>")
        out.append("      </laneSection>")
    out.append("    </lanes>")
    out.append("    <objects>")
    otype = {"pole": "pole", "tree": "tree", "bollard": "bollard", "railing": "barrier"}
    for f in plan.furniture:
        t = T(f.lat)
        out.append(f'      <object id="{f.id}" name="{f.type}" type="{otype.get(f.type, "obstacle")}" s="{L_fmt(f.s)}" t="{t:.3f}" zOffset="0" '
                   f'hdg="{math.radians(f.yaw_deg):.4f}" height="{f.size_m["h"]}" width="{f.size_m["w"]}" length="{f.size_m["d"]}" orientation="none">')
        out.append(f'        <userData code="hero:entity_id" value="{f.id}"/>')
        out.append("      </object>")
    out += ["    </objects>", "  </road>", "</OpenDRIVE>", ""]
    return "\n".join(out)


def L_fmt(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") if "." in f"{x:.4f}" else f"{x:.4f}"


def build_manifest(plan: StreetPlan, fr: Frame, mesh: StreetMesh, glb: bytes, xodr: str, ent_rows: list[dict], gp: dict) -> dict:
    T = lambda lat: t_of_lat(lat, plan.footpath_side)  # noqa: E731
    S = [st.s for st in plan.stations]
    L = plan.length_m
    side_lr = lambda t: "left" if t > 0 else "right"  # noqa: E731
    points = []
    for i, p in enumerate(plan.reference_line.points):
        points.append({"s": r3(p.s), "lat": r6(p.lat), "lon": r6(p.lon), "x": r3(fr.px[i]), "z": r3(fr.pz[i]), "bearing_deg": r3(p.bearing_deg)})
    wd = gp["lanes"]["widths_m"]
    lanes = {"defs": [{"index": 0, "name": "footpath", "kind": "footpath", "width_m": gp["lanes"]["default_footpath_width_m"]},
                      {"index": 1, "name": "road_edge", "kind": "road_edge", "width_m": wd["road_edge"]},
                      {"index": 2, "name": "road", "kind": "road", "width_m": wd["road"]}], "stations": []}
    cross = {"stations": []}
    mod_side, opp_side = plan.frontage.sides
    for st in plan.stations:
        fp = st.footpath
        lanes["stations"].append({
            "s": r3(st.s), "lane_t": [r3(T(v)) for v in st.lane_lat],
            "footpath": None if fp is None else {"t_lo": r3(min(T(fp.lo), T(fp.hi))), "t_hi": r3(max(T(fp.lo), T(fp.hi))),
                                                 "state": fp.state.value, "material": fp.material.value, "height_m": fp.height_m,
                                                 "confidence": fp.confidence},
            "kerb": {"t": r3(T(st.kerb.lat)), "height_m": st.kerb.height_m, "width_m": st.kerb.width_m}})
        strips = []
        for q in st.strips:
            lo, hi = sorted((T(q.lo), T(q.hi)))
            kind = q.kind.value
            side_ = "centre" if kind == "carriageway" else side_lr((lo + hi) / 2)
            strips.append({"kind": kind, "side": side_, "t_lo": r3(lo), "t_hi": r3(hi), "material": q.material.value,
                           "condition": "fair" if kind == "carriageway" else "unknown", "height_m": q.height_m, "confidence": q.confidence})
        if fp is not None:
            lo, hi = sorted((T(fp.lo), T(fp.hi)))
            strips.append({"kind": "footpath", "side": side_lr((lo + hi) / 2), "t_lo": r3(lo), "t_hi": r3(hi), "material": fp.material.value,
                           "condition": fp.state.value, "height_m": fp.height_m, "confidence": fp.confidence})
        if st.kerb.height_m > 0:
            kt = T(st.kerb.lat)
            strips.append({"kind": "kerb", "side": side_lr(kt), "t_lo": r3(min(kt, kt + st.kerb.width_m * (1 if kt > 0 else -1))),
                           "t_hi": r3(max(kt, kt + st.kerb.width_m * (1 if kt > 0 else -1))), "material": "concrete",
                           "condition": "fair", "height_m": st.kerb.height_m, "confidence": 0.7})
        strips.sort(key=lambda q: (q["t_lo"], q["t_hi"], q["kind"]))
        cross["stations"].append({"s": r3(st.s), "strips": strips})

    def fr_list(sd, left: bool):
        out = []
        for g in sd.segments:
            t = T(g.lat_from)
            out.append({"s_from": g.s_from, "s_to": g.s_to, "type": g.kind, "t_from": r3(t), "t_to": r3(T(g.lat_to)), "height_m": g.height_m,
                        "colour_hex": g.colour_hex, "source": g.source, "interpolated": g.interpolated})
        return out

    mod_is_left = T(1.0) > 0
    frontage = {"left": fr_list(mod_side if mod_is_left else opp_side, True), "right": fr_list(opp_side if mod_is_left else mod_side, False)}
    ob = gp["obstacle_types"]

    colliders = []
    for o in plan.objects:
        w = fr.world(o.x_m + o.length_m / 2, T(o.lateral_m), 0.0)
        colliders.append({"id": o.id, "entity_id": o.entity_id, "type": o.type, "label": o.label, "s": r3(o.x_m), "length_m": r3(o.length_m),
                          "lanes": list(o.lanes), "action": o.action.value, "size_m": {"w": o.size_m.w, "h": o.size_m.h, "d": o.size_m.d},
                          "confidence": o.confidence, "latlon": {"lat": o.latlon.lat, "lon": o.latlon.lon},
                          "tile_px": list(o.tile_px) if o.tile_px else None})
    tokens = [{"id": t.id, "s": r3(t.x_m), "lane": int(t.lane), "reason": t.reason.value,
               "latlon": {"lat": t.latlon.lat, "lon": t.latlon.lon}, "tile_px": list(t.tile_px)} for t in plan.tokens]
    sh = plan.dynamic_context.spawn_hints
    end = len(S) - 1
    xodr_sha = hashlib.sha256(xodr.encode()).hexdigest()
    return {
        "schema": "hero-street-manifest/1",
        "build": {"generator": GENERATOR, "segment_slug": SLUG, "model_version": MODEL_VERSION, "model_hash": MODEL_HASH,
                  "mesh_sha256": hashlib.sha256(glb).hexdigest(), "glb_bytes": len(glb), "xodr_sha256": xodr_sha,
                  "pygltflib": metadata.version("pygltflib"), "pillow": metadata.version("pillow"),
                  "python": platform.python_version(), "compiled_by": COMPILED_BY},
        "segment": {"slug": SLUG, "name": NAME, "city": plan.city, "length_s": L, "footpath_side": plan.footpath_side,
                    "fidelity": {"overall_level": plan.fidelity.overall_level, "score": plan.fidelity.score,
                                 "bins": [b.model_dump() for b in plan.fidelity.bins]}},
        "frame": {"units": "metres", "up_axis": "+Y", "handedness": "right", "travel_at_s0": "-Z",
                  "origin": {"lat": plan.frame.origin.lat, "lon": plan.frame.origin.lon}, "heading_deg": plan.frame.heading_deg,
                  "world_from_st": "hero-st-world/1"},
        "reference_line": {"source": plan.reference_line.source, "osm_way_id": plan.reference_line.osm_way_id,
                           "name": plan.reference_line.name, "length_s": L, "step_m": STEP, "points": points},
        "lanes": lanes, "cross_section": cross, "frontage": frontage, "entities": ent_rows, "colliders": colliders, "tokens": tokens,
        "spawn_hints": {"density": sh.density, "type_weights": dict(sh.type_weights)},
        "interpolated": [{"what": i.what, "side": i.side, "s_from": i.s_from, "s_to": i.s_to, "method": i.method, "note": i.note}
                         for i in plan.interpolated],
        "loop": {"seam_blend_m": 6.0, "end_pose": {"x": r3(fr.px[end]), "z": r3(fr.pz[end]), "yaw_rad": round(fr.phi[end], 6)},
                 "chain": "rigid_end_to_start"},
        "mesh_stats": mesh.stats(),
        "attribution": {"lines": ["Satellite imagery © Google", "Street-level imagery © Google", "Map data © OpenStreetMap contributors (ODbL)"],
                        "notice": "Textures derive from Google imagery and are provided for evaluation; check the Google Maps Platform terms before redistributing."},
        "gameplay": gp, "gameplay_sha256": gameplay_sha256(),
    }


def build_scene(v2: dict, plan: StreetPlan, manifest: dict, glb: bytes, manifest_bytes: bytes, xodr: str) -> dict:
    mesh_sha = manifest["build"]["mesh_sha256"]
    bundle = hashlib.sha256((mesh_sha + hashlib.sha256(manifest_bytes).hexdigest() + manifest["build"]["xodr_sha256"]).encode()).hexdigest()
    sha8 = bundle[:8]
    base = f"/api/segments/{SLUG}"
    ms = manifest["mesh_stats"]
    about = dict(v2["about"])
    about["model_version"] = MODEL_VERSION
    about["fidelity"] = {"level": "L2", "score": plan.fidelity.score, "label": "Multi-view",
                         "histogram_m": {"L0": 0.0, "L1": 20.0, "L2": 40.0, "L3": 0.0}}
    about["summary"] = ("This street is modelled at fidelity L2 (multi-view) from 48 photos (model v2) - 60 m of "
                        "Contract Curved Road, Indiranagar.")
    about["interpolated"] = [i.model_dump(mode="json") for i in plan.interpolated]
    about["gap_summary"] = ("About 10 m of the left-hand frontage, 30 m of the right-hand frontage and 5 m of ground texture "
                            "are interpolated from neighbouring stretches (no photo covers them).")
    about["dynamic_summary"] = ("Seen here but spawned dynamically during play instead of fixed in place: "
                                "1 parked vehicle, 1 carriageway pothole.")
    seg = dict(v2["segment"])
    seg.update({"slug": SLUG, "name": NAME})
    ground = dict(v2["ground"])
    # pixel of the s=0 centreline point in the overview tile (consistent with the manifest tile_px)
    p0 = plan.reference_line.points[0].tile_px
    ground["center"] = {"lat": plan.reference_line.points[len(plan.reference_line.points) // 2].lat,
                        "lon": plan.reference_line.points[len(plan.reference_line.points) // 2].lon}
    ground["frame_to_px"] = {"origin_px": list(p0), "along_px_per_m": [0.0, -round(1 / MPP, 2)], "across_px_per_m": [-round(1 / MPP, 2), 0.0]}
    scene = {
        "schema_version": 3, "segment": seg, "ground": ground, "cross_section": v2["cross_section"],
        "walkability": v2["walkability"], "about": about,
        "dynamic_context": {"excluded_count": 2, "entity_ids": ["ent-0000000001", "ent-0000000004"], "by_type": {
            "parked_vehicle": {"count": 1, "per_100m": 1.67, "subtypes": {"auto-rickshaw": 1}, "lane_share": [0.5, 0.5, 0.0], "mean_confidence": 0.86},
            "pothole": {"count": 1, "per_100m": 1.67, "subtypes": {}, "lane_share": [0.0, 0.0, 1.0], "mean_confidence": 0.7}}},
        "generation": v2["generation"],
        "model": {"version": MODEL_VERSION, "model_hash": MODEL_HASH, "compiled_by": COMPILED_BY},
        "assets": {"mesh_sha256": mesh_sha, "sha8": sha8, "glb_url": f"{base}/model.glb?v={sha8}",
                   "manifest_url": f"{base}/manifest.json?v={sha8}", "xodr_url": f"{base}/street.xodr?v={sha8}",
                   "gameplay_url": "/api/gameplay.json", "glb_bytes": len(glb), "triangles": ms["triangles"], "primitives": ms["primitives"]},
    }
    return scene


# ====================================================================== orchestration


def sample_gap_report(plan: StreetPlan) -> dict:
    """A GapReport for the stand-in street (used by the schema test and as an example in the JSON schema folder)."""
    n = len(plan.ground_tex.bin_source)
    bins = []
    for k in range(n):
        s0, s1 = k * BIN_M, min(plan.length_m, (k + 1) * BIN_M)
        src = plan.ground_tex.bin_source[k]
        ev = {"satellite_hires": "satellite_hires", "satellite_overview": "satellite_overview"}.get(src, "none")
        mod_prior = any(g.source == "prior" and g.s_from < s1 and g.s_to > s0 for g in plan.frontage.sides[0].segments)
        opp_prior = any(g.source == "prior" and g.s_from < s1 and g.s_to > s0 for g in plan.frontage.sides[1].segments)
        bins.append({"index": k, "s_from": s0, "s_to": s1, "fidelity_level": plan.fidelity.bins[k].level,
                     "ground": {"evidence": ev, "rendered": src},
                     "frontage": {"modelled": {"evidence": "none" if mod_prior else "osm", "rendered": "prior" if mod_prior else "osm"},
                                  "opposite": {"evidence": "none" if opp_prior else "osm", "rendered": "prior" if opp_prior else "osm"}},
                     "footpath": {"evidence": "traced", "rendered": "traced"}, "furniture": {"evidence": "street_level"},
                     "hint": ""})
    return {"gap_report": 1, "model_version": MODEL_VERSION, "bin_m": BIN_M, "length_s": plan.length_m, "bins": bins,
            "uncovered": {"ground": 1, "frontage_modelled": 2, "frontage_opposite": 8, "footpath": 0, "furniture": 0},
            "blank_after_compile": {"ground": 0, "frontage_modelled": 0, "frontage_opposite": 0, "footpath": 0},
            "interpolations": [i.model_dump(mode="json") for i in plan.interpolated]}


def build_all() -> dict[Path, bytes]:
    v2 = json.loads(SRC_SCENE.read_text())
    gp = load_gameplay()
    GameplayV1.model_validate(gp)
    plan, fr = build_plan(v2)
    mesh, ent_rows = build_mesh(plan, fr, gp)
    glb = write_glb_standin(mesh)
    xodr = build_xodr(plan, fr)
    manifest = build_manifest(plan, fr, mesh, glb, xodr, ent_rows, gp)
    ManifestV1.model_validate(manifest)
    manifest_bytes = (json.dumps(manifest, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    scene = build_scene(v2, plan, manifest, glb, manifest_bytes, xodr)
    SceneV3.model_validate(scene)
    GapReport.model_validate(sample_gap_report(plan))
    sch = SCHEMAS_DIR
    dump = lambda d: (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")  # noqa: E731
    return {
        OUT_DIR / "plan.json": dump(plan.model_dump(mode="json")),
        OUT_DIR / "street.glb": glb,
        OUT_DIR / "street.manifest.json": manifest_bytes,
        OUT_DIR / "street.xodr": xodr.encode("utf-8"),
        SCENE_OUT: dump(scene),
        sch / "manifest.schema.json": dump(ManifestV1.model_json_schema(by_alias=True)),
        sch / "gameplay.schema.json": dump(GameplayV1.model_json_schema(by_alias=True)),
        sch / "scene_v3.schema.json": dump(SceneV3.model_json_schema()),
        sch / "gap_report.schema.json": dump(GapReport.model_json_schema()),
    }


def main() -> int:
    files = build_all()
    if "--check" in sys.argv:
        stale = [str(p.relative_to(ROOT)) for p, b in files.items() if not p.exists() or p.read_bytes() != b]
        if stale:
            print("STALE:", *stale, sep="\n  ")
            return 1
        print("up to date")
        return 0
    for p, b in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
        print(f"wrote {p.relative_to(ROOT)} ({len(b)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
