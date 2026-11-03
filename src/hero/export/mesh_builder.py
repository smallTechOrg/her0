"""``build_street_mesh`` - the deterministic Python mesh builder (spec/portability.md section 3).

Pure function of ``(StreetPlan, baked texture files)``: no clock, no randomness, no network, no dict/set-order
dependence (every iteration is over a list or a sorted sequence). Geometry is in the true street frame (metres, +Y up,
travel -Z at s = 0, +X right), every node transform identity, one primitive per node, nodes without geometry omitted.

``observations`` is accepted for signature stability (the bake step already folded pixels into the textures named in
the plan); ``data_dir`` is where those baked files live (``<data_dir>/textures/<slug>/``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from hero.export import GENERATOR, gameplay_sha256
from hero.export.atlas import facade_atlas, ground_atlas
from hero.export.frame import Frame, r3
from hero.export.mesh_frontage import build_frontage
from hero.export.mesh_furniture import build_furniture
from hero.export.mesh_ground import StreetProfile, build_ground, ground_extras
from hero.export.mesh_types import (
    ASSET_SCHEMA, KERB_COLOUR, NODE_NAMES, MeshMaterial, StreetMesh,
)
from hero.model.street_plan_types import StreetPlan


class MeshBuildError(RuntimeError):
    pass


def _plan_from_model(model: Any, scene: Any, observations: Mapping[str, Any], data_dir: Path) -> StreetPlan:
    try:
        from hero.model.street_plan import plan_street  # S1b
    except ImportError as exc:  # pragma: no cover - until S1b lands
        raise MeshBuildError("no StreetPlan given and hero.model.street_plan.plan_street is not available") from exc
    from hero.export import load_gameplay
    from hero.export.gameplay_types import GameplayV1
    from hero.model.fidelity import compute_fidelity
    from hero.model.gaps import analyze_gaps

    fidelity = compute_fidelity(model, observations)
    gaps = analyze_gaps(model, observations, fidelity, None)
    return plan_street(model, observations=observations, fidelity=fidelity, gaps=gaps, textures=None,
                       gameplay=GameplayV1.model_validate(load_gameplay()))


def build_street_mesh(model: Any, scene: Any, *, observations: Mapping[str, Any], data_dir: Path,
                      plan: StreetPlan | None = None, textures: Any = None) -> StreetMesh:
    data_dir = Path(data_dir)
    if plan is None:
        plan = _plan_from_model(model, scene, observations, data_dir)
    frame = Frame.from_plan(plan)
    prof = StreetProfile(plan, frame)

    ground = build_ground(plan, frame, prof)
    g_extras = ground_extras(plan, prof)
    front, f_extras = build_frontage(plan, frame, prof)
    furn, u_extras, _rows = build_furniture(plan, frame, prof)

    builders = {**ground, **front, **furn}
    extras = {**g_extras, **f_extras, **u_extras}
    nodes = []
    for name in NODE_NAMES:  # contract order
        node = builders[name].node(extras[name])
        if node is not None:
            nodes.append(node)

    asset_extras: dict[str, Any] = {
        "schema": ASSET_SCHEMA, "generator": GENERATOR, "segment_slug": plan.slug, "model_version": plan.model_version,
        "model_hash": plan.model_hash, "footpath_side": plan.footpath_side, "units": "metres", "up_axis": "+Y",
        "travel": "-Z at s=0", "right": "+X at s=0", "length_s": r3(plan.length_m),
        "frame": {"origin": {"lat": plan.frame.origin.lat, "lon": plan.frame.origin.lon}, "heading_deg": plan.frame.heading_deg},
        "manifest": "street.manifest.json", "gameplay_sha256": gameplay_sha256(),
    }
    materials = [
        MeshMaterial("mat_ground", "#ffffff", "atlas_ground", 0.0, 1.0),
        MeshMaterial("mat_kerb", KERB_COLOUR),
        MeshMaterial("mat_frontage", "#ffffff", "atlas_facade"),
        MeshMaterial("mat_flat", "#ffffff"),
    ]
    tex = {"atlas_ground": ground_atlas(plan, prof, data_dir), "atlas_facade": facade_atlas(plan, prof, data_dir)}
    return StreetMesh(asset_extras, nodes, materials, tex, plan.length_m)
