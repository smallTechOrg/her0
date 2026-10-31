"""Determinism of the export pipeline (S10): in-process twice and across PYTHONHASHSEED subprocesses, plus the
committed baseline hashes (tests/fixtures/export/baseline_hashes.json, regenerated deliberately)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from hero.export.bundle import build_bundle
from hero.export.pipeline import build_and_store_exports
from hero.config import PROJECT_ROOT
from tests.fixtures.export import fake_builders


def test_in_process_twice_identical(tmp_path, monkeypatch):
    fake_builders.install(monkeypatch)
    kwargs = dict(slug="s-one", version=1, model=object(), scene=object(), plan=object(), observations={})
    a = build_and_store_exports(data_dir=tmp_path / "a", **kwargs)
    b = build_and_store_exports(data_dir=tmp_path / "b", **kwargs)
    assert a == b
    assert build_bundle(a, tmp_path / "a", model_version=1) == build_bundle(b, tmp_path / "b", model_version=1)


_SCRIPT = textwrap.dedent("""
    import hashlib, json, sys, tempfile, pathlib
    sys.path.insert(0, {root!r})
    import pytest
    from tests.fixtures.export import fake_builders
    from hero.export.pipeline import build_and_store_exports
    from hero.export.bundle import build_bundle
    mp = pytest.MonkeyPatch()
    fake_builders.install(mp)
    d = pathlib.Path(tempfile.mkdtemp())
    rec = build_and_store_exports(slug="s-one", version=1, model=object(), scene=object(), plan=object(), observations={{}}, data_dir=d)
    print(json.dumps({{"sha8": rec.sha8, "bundle": hashlib.sha256(build_bundle(rec, d, model_version=1)).hexdigest()}}))
""")


def _run(seed: str) -> dict:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", _SCRIPT.format(root=str(PROJECT_ROOT))], env=env, check=True,
                         capture_output=True, text=True, cwd=PROJECT_ROOT).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_identical_across_hash_seeds():
    assert _run("0") == _run("1")


# ------------------------------------------------------------------ real builders + committed baseline

import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

BASELINE = PROJECT_ROOT / "tests" / "fixtures" / "export" / "baseline_hashes.json"
_REAL = ("mesh_builder", "glb", "manifest", "xodr")
_missing = [m for m in _REAL if importlib.util.find_spec(f"hero.export.{m}") is None]

_REAL_SCRIPT = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {root!r})
    from tests.unit.test_export_determinism import real_hashes
    print(json.dumps(real_hashes()))
""")


def real_hashes() -> dict:
    """Hashes of the REAL pipeline over the contract stand-in plan (the baseline's subject)."""
    from hero.export.bundle import build_bundle as _bundle
    from hero.export.pipeline import build_and_store_exports as _build
    from hero.model.scene_v3 import SceneV3
    from hero.model.street_plan_types import StreetPlan
    from hero.model.types import CanonicalModel

    fx = PROJECT_ROOT / "tests" / "fixtures"
    plan = StreetPlan.model_validate_json((fx / "export" / "contract_curved" / "plan.json").read_text())
    scene = SceneV3.model_validate_json((fx / "scene_v3" / "contract_curved.json").read_text())
    model = CanonicalModel.model_validate_json((fx.parent / "e2e" / "fixtures" / "model_fixture" / "model_v2.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        rec = _build(slug="contract-curved-street", version=2, model=model, scene=scene, plan=plan, observations={},
                     data_dir=Path(tmp))
        zip_sha = hashlib.sha256(_bundle(rec, Path(tmp), model_version=2)).hexdigest()
    return {"built_with": rec.built_with, "sha8": rec.sha8, "mesh_sha256": rec.mesh_sha256,
            "manifest_sha256": rec.manifest_sha256, "xodr_sha256": rec.xodr_sha256, "zip_sha256": zip_sha,
            "glb_bytes": rec.glb_bytes, "triangles": rec.triangles, "primitives": rec.primitives}


def _run_real(seed: str) -> dict:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", _REAL_SCRIPT.format(root=str(PROJECT_ROOT))], env=env, check=True,
                         capture_output=True, text=True, cwd=PROJECT_ROOT).stdout
    return json.loads(out.strip().splitlines()[-1])


needs_real = pytest.mark.skipif(bool(_missing), reason=f"real export modules not in the tree yet: {_missing}")


@needs_real
def test_real_pipeline_identical_in_process_and_across_hash_seeds():
    first = real_hashes()
    assert real_hashes() == first
    assert _run_real("0") == first
    assert _run_real("1") == first


@needs_real
def test_real_pipeline_matches_committed_baseline():
    """Regenerate DELIBERATELY with ``uv run python -m tests.unit.test_export_determinism --regenerate`` (in the
    same commit as the builder/library change that moved the bytes)."""
    assert BASELINE.is_file(), "tests/fixtures/export/baseline_hashes.json missing - run the --regenerate command"
    base = json.loads(BASELINE.read_text())
    now = real_hashes()
    if base["built_with"]["pygltflib"] != now["built_with"]["pygltflib"] or base["built_with"]["pillow"] != now["built_with"]["pillow"]:
        pytest.skip(f"baseline recorded under {base['built_with']}, running under {now['built_with']}: "
                    "library versions differ, regenerate deliberately")
    assert now == base, "export bytes changed: if intended, regenerate baseline_hashes.json in the same commit"


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        BASELINE.write_text(json.dumps(real_hashes(), indent=2) + "\n")
        print("wrote", BASELINE)
    else:
        print(json.dumps(real_hashes(), indent=2))
