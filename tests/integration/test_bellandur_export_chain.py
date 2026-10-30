"""compile -> mesh -> glb -> manifest on the recorded Bellandur model with the REAL builders (tmp dir, no network):
the dynamic types (parked_vehicle, vendor_stall, speed_breaker) never reach the static glb extras or the manifest,
and the recorded model JSON is left untouched."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pygltflib import GLTF2

from hero.export import store
from hero.export.pipeline import build_and_store_exports
from hero.model.compile import compile_scene
from hero.model.types import CanonicalModel, FidelityMap, Observation, ObservationKind

ROOT = Path(__file__).resolve().parents[2]
BELL = ROOT / "tests" / "fixtures" / "recorded" / "bellandur"
DYNAMIC = ("parked_vehicle", "vendor_stall", "speed_breaker")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


def test_recorded_bellandur_has_zero_dynamic_types_in_glb_extras_and_manifest(tmp_path):
    model_path = BELL / "model_v1.json"
    before = hashlib.sha256(model_path.read_bytes()).hexdigest()
    model = CanonicalModel.model_validate_json(model_path.read_text())
    assert any(e.type.value in DYNAMIC for e in model.entities), "the recorded street really contains dynamic entities"
    obs = {o["id"]: Observation.model_validate(o) for o in json.loads((BELL / "observations.json").read_text())}
    fid = FidelityMap.model_validate_json((BELL / "fidelity_v1.json").read_text())
    tile = next(o for o in obs.values() if o.kind is ObservationKind.satellite_overview)

    res = compile_scene(model, observations=obs, assets={}, tile=tile, fidelity=fid, slug_name="Bellandur Main Road",
                        city="Bengaluru")
    rec = build_and_store_exports(slug="bellandur-chain", version=model.version, model=model, scene=res.scene,
                                  plan=res.plan, observations=obs, data_dir=tmp_path)

    files = store.open_export(tmp_path, "bellandur-chain", rec.sha8)
    assert files is not None
    gltf = GLTF2.load_binary(str(files.glb))
    extras = [n.extras for n in gltf.nodes if n.extras] + [gltf.asset.extras]
    assert extras and len(gltf.nodes) > 3
    manifest = json.loads(files.manifest.read_bytes())

    for label, doc in (("glb extras", extras), ("manifest", manifest)):
        found = {s for s in _strings(doc) if s in DYNAMIC}
        assert not found, f"{label} carries dynamic types: {found}"
    assert not {e["type"] for e in manifest["entities"]} & set(DYNAMIC)
    assert not {c["type"] for c in manifest["colliders"]} & set(DYNAMIC)
    assert manifest["build"]["mesh_sha256"] == rec.mesh_sha256 == hashlib.sha256(files.glb.read_bytes()).hexdigest()
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == before, "the recorded model file is untouched"
    assert model.model_dump_json() == CanonicalModel.model_validate_json(model_path.read_text()).model_dump_json(), \
        "compiling and exporting did not mutate the in-memory model"
