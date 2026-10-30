"""Stand-ins for the S6 (mesh_builder), S7 (glb), S8 (manifest, xodr) modules, built on the committed contract bundle.

``install(monkeypatch, salt=b"")`` registers fake ``hero.export.{mesh_builder,glb,xodr,manifest}`` modules in
``sys.modules`` (``hero.export.pipeline`` imports its builders at call time through ``importlib``). Deterministic:
the bytes are the contract fixture's, plus ``salt`` (so two "model versions" differ), with the manifest's ``build``
block carrying the real hashes the pipeline passes in.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

FIXTURE = Path(__file__).resolve().parent / "contract_curved"
SLUG = "contract-curved-street"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE / name).read_bytes()


def install(monkeypatch, *, salt: bytes = b"", calls: list | None = None) -> None:
    calls = calls if calls is not None else []
    base_manifest = json.loads(fixture_bytes("street.manifest.json"))

    mesh_mod = types.ModuleType("hero.export.mesh_builder")

    def build_street_mesh(model, scene, *, observations, data_dir, plan=None, textures=None):
        calls.append("mesh")
        return SimpleNamespace(salt=salt, stats=lambda: {"primitives": 12, "triangles": 8420, "vertices": 6100,
                                                          "materials": 4, "textures": 2, "nodes": []})

    mesh_mod.build_street_mesh = build_street_mesh

    glb_mod = types.ModuleType("hero.export.glb")

    def write_glb(mesh):
        calls.append("glb")
        return fixture_bytes("street.glb") + mesh.salt

    glb_mod.write_glb = write_glb

    xodr_mod = types.ModuleType("hero.export.xodr")

    def build_xodr(plan):  # real shape: (plan) -> str
        calls.append("xodr")
        return (fixture_bytes("street.xodr") + salt).decode("utf-8")

    xodr_mod.build_xodr = build_xodr

    man_mod = types.ModuleType("hero.export.manifest")

    def _doc(glb_sha256, glb_bytes, xodr_sha256, gameplay):
        calls.append("manifest")
        doc = json.loads(json.dumps(base_manifest))
        doc["build"].update(mesh_sha256=glb_sha256, glb_bytes=glb_bytes, xodr_sha256=xodr_sha256)
        if gameplay is not None:
            doc["gameplay_sha256"] = "feed" * 16
        return doc

    def build_manifest(plan, mesh, *, glb_sha256, glb_bytes, xodr_sha256, gameplay=None):  # real shape
        return _doc(glb_sha256, glb_bytes, xodr_sha256, gameplay)

    man_mod.build_manifest = build_manifest
    man_mod.manifest_bytes = lambda m: json.dumps(m, separators=(",", ":")).encode()

    for name, mod in (("mesh_builder", mesh_mod), ("glb", glb_mod), ("xodr", xodr_mod), ("manifest", man_mod)):
        monkeypatch.setitem(sys.modules, f"hero.export.{name}", mod)
        monkeypatch.setattr(sys.modules["hero.export"], name, mod, raising=False)
