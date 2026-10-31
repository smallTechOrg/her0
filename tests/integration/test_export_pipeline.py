"""build_and_store_exports end to end (S10).

Part 1 uses the fake builders (always runs). Part 2 runs the REAL mesh builder / glb writer / manifest / xodr on the
contract stand-in plan and is skipped, with a visible reason, only while one of those modules is not in the tree.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile

import pytest

from hero.config import PROJECT_ROOT
from hero.export import store
from hero.export.bundle import build_bundle
from hero.export.pipeline import build_and_store_exports
from tests.fixtures.export import fake_builders

REAL_MODULES = ("mesh_builder", "glb", "manifest", "xodr")


def test_pipeline_calls_stages_in_order_and_stores(test_db, tmp_path, monkeypatch):
    calls: list[str] = []
    fake_builders.install(monkeypatch, calls=calls)
    rec = build_and_store_exports(slug="pipe-street", version=4, model=object(), scene=object(), plan=object(),
                                  observations={}, data_dir=tmp_path)
    assert calls == ["mesh", "glb", "xodr", "manifest"]
    files = store.load_export("pipe-street", None, data_dir=tmp_path)
    assert files is not None and files.sha8 == rec.sha8
    assert store.load_export("pipe-street", rec.sha8, data_dir=tmp_path).glb == files.glb
    assert rec.to_json()["stats"] == {"primitives": 12, "triangles": 8420}
    assert store.ExportRecord.from_json(json.loads(json.dumps(rec.to_json()))) == rec


def test_failure_in_a_stage_leaves_no_files(tmp_path, monkeypatch):
    fake_builders.install(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr("hero.export.manifest.build_manifest", boom)
    with pytest.raises(RuntimeError):
        build_and_store_exports(slug="pipe-fail", version=1, model=object(), scene=object(), plan=object(),
                                observations={}, data_dir=tmp_path)
    assert not (tmp_path / "exports").exists()


def test_head_lookup_prefers_pointer_and_tolerates_a_corrupt_one(test_db, tmp_path, monkeypatch):
    fake_builders.install(monkeypatch)
    rec = build_and_store_exports(slug="pipe-head", version=1, model=object(), scene=object(), plan=object(),
                                  observations={}, data_dir=tmp_path)
    (tmp_path / "exports" / "pipe-head" / store.HEAD_POINTER).write_text("{not json")
    assert store.load_export("pipe-head", None, data_dir=tmp_path) is None
    store.write_head_pointer(tmp_path, "pipe-head", rec.sha8, 1)
    assert store.load_export("pipe-head", None, data_dir=tmp_path).sha8 == rec.sha8


def test_no_network_in_the_pipeline(tmp_path, monkeypatch):
    import socket

    def no_net(*a, **k):
        raise AssertionError("network used by the export pipeline")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    fake_builders.install(monkeypatch)
    build_and_store_exports(slug="pipe-net", version=1, model=object(), scene=object(), plan=object(),
                            observations={}, data_dir=tmp_path)


# ------------------------------------------------------------------ real builders

_missing = [m for m in REAL_MODULES if importlib.util.find_spec(f"hero.export.{m}") is None]


def real_inputs():
    from hero.model.scene_v3 import SceneV3
    from hero.model.street_plan_types import StreetPlan
    from hero.model.types import CanonicalModel

    fx = PROJECT_ROOT / "tests" / "fixtures"
    plan = StreetPlan.model_validate_json((fx / "export" / "contract_curved" / "plan.json").read_text())
    scene = SceneV3.model_validate_json((fx / "scene_v3" / "contract_curved.json").read_text())
    model = CanonicalModel.model_validate_json(
        (PROJECT_ROOT / "tests" / "e2e" / "fixtures" / "model_fixture" / "model_v2.json").read_text())
    return model, scene, plan


@pytest.mark.skipif(bool(_missing), reason=f"real export modules not in the tree yet: {_missing}")
def test_real_pipeline_on_the_contract_plan(tmp_path):
    from hero.export.glb import read_glb
    from hero.export.glb_validate import validate_glb

    model, scene, plan = real_inputs()
    kwargs = dict(slug="pipe-real", version=2, model=model, scene=scene, plan=plan, observations={})
    rec = build_and_store_exports(data_dir=tmp_path / "a", **kwargs)
    again = build_and_store_exports(data_dir=tmp_path / "b", **kwargs)
    assert rec == again  # deterministic
    files = store.load_export("pipe-real", None, data_dir=tmp_path / "a")
    glb = files.read("glb")
    assert glb[:4] == b"glTF"
    report = validate_glb(glb)
    assert report.ok, report
    read_glb(glb)
    manifest = json.loads(files.read("manifest"))
    assert manifest["schema"] == "hero-street-manifest/1"
    assert manifest["build"]["mesh_sha256"] == rec.mesh_sha256
    assert manifest["mesh_stats"]["triangles"] == rec.triangles
    assert b'revMinor="7"' in files.read("xodr")
    z = build_bundle(rec, tmp_path / "a", model_version=2)
    assert z == build_bundle(again, tmp_path / "b", model_version=2)
    assert zipfile.ZipFile(io.BytesIO(z)).namelist()[0] == "street.glb"
