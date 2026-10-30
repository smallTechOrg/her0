"""Khronos glTF-Validator (reference implementation) on our glb files.

The npm package ``gltf-validator`` is a library (no CLI bin, which is why a bare ``npx gltf-validator`` fails with
"could not determine executable to run"), so ``hero.export.glb_validate.khronos_validate`` runs a tiny node driver through
``npx --yes -p gltf-validator@2.0.0-dev.3.10``. Skipped WITH A VISIBLE REASON when node/npx/network is unavailable.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_contract_export as mce  # noqa: E402

from hero.export.glb import write_glb  # noqa: E402
from hero.export.glb_validate import khronos_validate  # noqa: E402

pytestmark = pytest.mark.validator

STANDIN = ROOT / "tests" / "fixtures" / "export" / "contract_curved" / "street.glb"


@pytest.fixture(scope="module")
def written(tmp_path_factory) -> Path:
    plan, fr = mce.build_plan(json.loads(mce.SRC_SCENE.read_text()))
    mesh = mce.build_mesh(plan, fr, mce.load_gameplay())[0]
    p = tmp_path_factory.mktemp("glb") / "street.glb"
    p.write_bytes(write_glb(mesh))
    return p


@pytest.fixture(scope="module")
def standin_result():
    if not (shutil.which("npx") and shutil.which("node")):
        pytest.skip("Khronos validator not run: npx/node not on PATH")
    res = khronos_validate(STANDIN)
    if not res.ran:
        pytest.skip(f"Khronos validator not run: {res.reason}")
    return res


def test_standin_has_zero_errors(standin_result):
    assert standin_result.errors == 0, standin_result.messages
    assert standin_result.version


def test_written_glb_has_zero_errors(standin_result, written):
    res = khronos_validate(written)
    assert res.ran, res.reason
    assert res.errors == 0, res.messages
    assert res.warnings == 0, res.messages


def test_validator_really_rejects_a_broken_file(standin_result, tmp_path):
    """Guard against a validator that silently passes everything: a corrupted file must report errors."""
    data = bytearray(STANDIN.read_bytes())
    data[-5000:-4000] = b"\xff" * 1000  # damages the image / index data region
    data[12:16] = (len(data) + 100).to_bytes(4, "little")  # JSON chunk length now overruns the file
    bad = tmp_path / "bad.glb"
    bad.write_bytes(bytes(data))
    res = khronos_validate(bad)
    assert res.ran and res.errors > 0
