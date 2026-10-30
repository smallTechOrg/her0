"""Shared helpers for the test_mesh_* tests (S6): plan loading, cached builds, (s, t) triangle projection."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from hero.export.frame import Frame
from hero.export.mesh_builder import build_street_mesh
from hero.export.mesh_types import StreetMesh
from hero.model.street_plan_types import StreetPlan

ROOT = Path(__file__).resolve().parents[2]
CURVED = ROOT / "tests" / "fixtures" / "export" / "contract_curved" / "plan.json"
PLAN_DIR = ROOT / "tests" / "fixtures" / "plan"


def plan_paths() -> list[Path]:
    """The stand-in curved plan always; the S1b-generated ``tests/fixtures/plan/*.plan.json`` once they exist."""
    return [CURVED, *sorted(PLAN_DIR.glob("*.plan.json"))]


def plan_ids() -> list[str]:
    return [p.parent.name if p == CURVED else p.name.removesuffix(".plan.json") for p in plan_paths()]


@lru_cache(maxsize=None)
def load_plan(path: str) -> StreetPlan:
    return StreetPlan.model_validate_json(Path(path).read_text())


@lru_cache(maxsize=None)
def build(path: str, data_dir: str = "/nonexistent-hero-data") -> StreetMesh:
    return build_street_mesh(None, None, observations={}, data_dir=Path(data_dir), plan=load_plan(path))


_ST_CACHE: dict[tuple[int, str], list] = {}


def tri_st(mesh: StreetMesh, node: str, frame: Frame) -> list:
    """``[[(s, t), ...3], ...]`` for every triangle of ``node`` (cached per mesh object)."""
    key = (id(mesh), node)
    if key not in _ST_CACHE:
        nd = mesh.node(node)
        out = []
        if nd is not None:
            st = [frame.inverse(p[0], p[2]) for p in nd.positions]
            for k in range(0, len(nd.indices), 3):
                out.append([st[nd.indices[k + j]] for j in range(3)])
        _ST_CACHE[key] = (out, mesh)  # keep the mesh alive so id() is never reused
    return _ST_CACHE[key][0]


def t_interval_at(tri, s: float):
    """Intersection of the triangle (in the s-t plane) with the line s = const -> (t_lo, t_hi) or None."""
    ts = []
    for a in range(3):
        (s0, t0), (s1, t1) = tri[a], tri[(a + 1) % 3]
        if abs(s1 - s0) < 1e-12:
            if abs(s0 - s) < 1e-9:
                ts += [t0, t1]
            continue
        if min(s0, s1) - 1e-12 <= s <= max(s0, s1) + 1e-12:
            ts.append(t0 + (t1 - t0) * (s - s0) / (s1 - s0))
    if len(ts) < 2:
        return None
    return min(ts), max(ts)


def union(intervals, tol=1e-3):
    out = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def synthetic_plan(length: float = 200.0, side: str = "left", baked: bool = False) -> StreetPlan:
    """A straight synthetic street (bearing 0) with every feature kind, for budget / side-symmetry tests."""
    step = 2.5
    n = int(round(length / step))
    s_list = [round(i * step, 3) for i in range(n + 1)]
    if s_list[-1] < length - 1e-9:
        s_list.append(length)
    stations, pts = [], []
    for s in s_list:
        missing = 60.0 <= s < 75.0
        fp = None if missing else {"lo": 4.9, "hi": 6.7, "state": "broken" if 30 <= s < 40 else "intact", "material": "paver",
                                   "height_m": 0.15, "confidence": 0.8}
        stations.append({"s": s, "lane_lat": [5.8, 3.8, 2.0], "footpath": fp,
                         "strips": [{"kind": "footpath", "lo": -6.1, "hi": -4.5, "material": "paver", "height_m": 0.15, "confidence": 0.3},
                                    {"kind": "carriageway", "lo": -4.3, "hi": 4.7, "material": "asphalt", "height_m": 0.0, "confidence": 0.7}],
                         "kerb": {"lat": 4.7, "height_m": 0.0 if missing else 0.15, "width_m": 0.2}, "ground_src": "procedural"})
        pts.append({"s": s, "lat": 12.93 + s * 9e-6, "lon": 77.67, "bearing_deg": 0.0, "tile_px": None})

    def segs(start_lat, kinds):
        out, a, k = [], 0.0, 0
        while a < length - 1e-9:
            b = min(length, a + (17.5 if k % 2 == 0 else 12.5))
            kind = kinds[k % len(kinds)]
            out.append({"s_from": a, "s_to": b, "lat_from": start_lat + (0.5 if k % 3 == 0 else 0.0) * (1 if start_lat > 0 else -1),
                        "lat_to": start_lat + (0.5 if k % 3 == 0 else 0.0) * (1 if start_lat > 0 else -1),
                        "height_m": {"building": 8.0 + k % 4, "wall": 2.0, "gate": 2.2}[kind], "kind": kind,
                        "colour_hex": ["#c9b79c", "#a98f7a", "#b0a89a"][k % 3], "storeys": 3, "source": "osm" if kind == "building" else "entity",
                        "building_id": None, "entity_id": None, "interpolated": False, "confidence": 0.7})
            a, k = b, k + 1
        return out

    furniture = []
    for k, s in enumerate(range(5, int(length) - 3, 9)):
        typ = ("pole", "tree", "bin", "railing", "bollard", "drain")[k % 6]
        size = {"pole": {"w": 0.3, "h": 7.0, "d": 0.3}, "tree": {"w": 3.0, "h": 8.0, "d": 3.0}, "bin": {"w": 0.6, "h": 0.9, "d": 0.6},
                "railing": {"w": 0.1, "h": 1.0, "d": 6.0}, "bollard": {"w": 0.25, "h": 0.9, "d": 0.25}, "drain": {"w": 0.5, "h": 0.01, "d": 1.2}}[typ]
        furniture.append({"id": f"ent-{k:010d}", "type": typ, "variant": "utility" if typ == "pole" else "", "s": float(s),
                          "lat": 6.9 if typ in ("pole", "tree") else 6.2 if typ != "drain" else 4.3, "yaw_deg": 0.0, "size_m": size,
                          "length_m": 6.0 if typ == "railing" else 0.0, "colour_hex": None, "canopy_hex": None, "object_id": None,
                          "confidence": 0.8, "fidelity_level": 1, "description": ""})
    n_bins = -(-int(length) // 5)
    plan = {
        "plan": "street-plan/1", "slug": "synthetic", "name": "Synthetic", "city": "X", "model_version": 1, "model_hash": "0" * 64,
        "compiled_by": "hero.model.compile/2", "footpath_side": side, "length_m": length,
        "frame": {"origin": {"lat": 12.93, "lon": 77.67}, "heading_deg": 0.0},
        "reference_line": {"source": "osm_nominatim", "osm_way_id": 1, "name": "S", "length_s": length, "step_m": step, "points": pts},
        "corridor_step_m": step, "stations": stations,
        "ground_tex": {"mode": "procedural", "procedural_seed": 7, "bin_m": 5.0, "bin_source": ["procedural"] * n_bins, "strip": None},
        "frontage": {"step_m": step, "sides": [{"side": "modelled", "texture": None, "segments": segs(9.0, ["building", "wall", "building", "gate"])},
                                               {"side": "opposite", "texture": None, "segments": segs(-9.0, ["building", "building", "wall"])}]},
        "furniture": furniture, "objects": [], "tokens": [],
        "dynamic_context": {"excluded_count": 0, "entity_ids": [], "spawn_hints": {"density": 0.5, "type_weights": {
            "parked_car": 0.2, "two_wheeler": 0.2, "cone": 0.15, "barricade": 0.1, "pothole": 0.15, "stall": 0.1, "low_branch": 0.1},
            "lane_weights": [0.2, 0.5, 0.3]}},
        "fidelity": {"overall_level": 1, "score": 1.0, "bins": [{"index": i, "s_from": i * 5.0, "s_to": min(length, (i + 1) * 5.0), "level": 1} for i in range(n_bins)]},
        "interpolated": [],
    }
    return StreetPlan.model_validate(plan)


def build_plan(plan: StreetPlan, data_dir="/nonexistent-hero-data") -> StreetMesh:
    return build_street_mesh(None, None, observations={}, data_dir=Path(data_dir), plan=plan)
