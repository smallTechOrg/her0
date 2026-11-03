"""Export orchestration: mesh -> glb -> xodr -> manifest -> content-addressed files (spec/architecture.md r2).

``build_and_store_exports`` is the only entry publish/recompile call. No network, no clock in the artefacts: the
bytes are a pure function of (model, scene, plan, observations, gameplay) in one pinned environment. The builder
modules (``mesh_builder``, ``glb``, ``manifest``, ``xodr``) are imported at call time through ``importlib`` so this
module imports cleanly while they are being written and so tests can substitute them.
"""

from __future__ import annotations

import importlib
import platform
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from hero.export import store
from hero.export.store import ExportRecord, bundle_sha256, sha256_hex
from hero.logging import get_logger

__all__ = ["ExportRecord", "ExportTooLarge", "build_and_store_exports", "built_with"]

DEFAULT_MAX_GLB_MB = 2.5


class ExportTooLarge(Exception):
    """The glb exceeds ``HERO_EXPORT_MAX_GLB_MB``; the previous scene/export stays untouched."""

    code = "export_too_large"


def built_with() -> dict[str, str]:
    def ver(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:  # pragma: no cover - both are hard dependencies
            return "unknown"

    return {"pygltflib": ver("pygltflib"), "pillow": ver("pillow"), "python": platform.python_version()}


def _mod(name: str) -> Any:
    return importlib.import_module(f"hero.export.{name}")


def _max_glb_bytes() -> int:
    from hero.config import get_settings

    try:
        mb = float(get_settings().export_max_glb_mb)
    except (AttributeError, TypeError, ValueError):  # a missing / malformed setting falls back to the default
        mb = DEFAULT_MAX_GLB_MB
    return int(mb * 1_000_000)


def build_and_store_exports(*, slug: str, version: int, model: Any, scene: Any, plan: Any, observations: Any,
                            data_dir: Path, gameplay: Any = None) -> ExportRecord:
    """Build glb + manifest + xodr for one model version and store them content-addressed.

    ``observations`` is the ``{obs_id: Observation}`` mapping the mesh builder bakes textures from.
    Raises ``ExportTooLarge`` (nothing written) when the glb is over the configured limit.
    """
    started = time.perf_counter()
    data_dir = Path(data_dir)
    mesh = _mod("mesh_builder").build_street_mesh(model, scene, observations=observations, data_dir=data_dir, plan=plan)
    glb_bytes = _mod("glb").write_glb(mesh)
    limit = _max_glb_bytes()
    if len(glb_bytes) > limit:
        raise ExportTooLarge(f"street.glb is {len(glb_bytes)} bytes, over the {limit}-byte limit (HERO_EXPORT_MAX_GLB_MB)")
    glb_sha = sha256_hex(glb_bytes)

    xodr_bytes = _mod("xodr").build_xodr(plan).encode("utf-8")
    xodr_sha = sha256_hex(xodr_bytes)

    manifest_mod = _mod("manifest")
    versions = built_with()
    gp = gameplay.model_dump(mode="json") if hasattr(gameplay, "model_dump") else gameplay  # dict | bytes | None
    manifest = manifest_mod.build_manifest(plan, mesh, glb_sha256=glb_sha, glb_bytes=len(glb_bytes),
                                           xodr_sha256=xodr_sha, gameplay=gp)
    gameplay_sha = _gameplay_sha(manifest)
    manifest_bytes = manifest_mod.manifest_bytes(manifest)
    manifest_sha = sha256_hex(manifest_bytes)

    bundle = bundle_sha256(glb_sha, manifest_sha, xodr_sha)
    sha8 = bundle[:8]
    files = store.write_export(data_dir, slug, sha8, glb=glb_bytes, manifest=manifest_bytes, xodr=xodr_bytes)
    store.write_head_pointer(data_dir, slug, sha8, version)

    stats = mesh.stats()
    rec = ExportRecord(sha8=sha8, mesh_sha256=glb_sha, manifest_sha256=manifest_sha, xodr_sha256=xodr_sha,
                       gameplay_sha256=gameplay_sha, bundle_sha256=bundle, glb_bytes=len(glb_bytes),
                       triangles=int(stats["triangles"]), primitives=int(stats["primitives"]),
                       files=files, built_with=versions)
    get_logger("hero.export").info(
        "export.built", slug=slug, version=version, sha8=sha8, glb_bytes=len(glb_bytes), triangles=rec.triangles,
        primitives=rec.primitives, textures=stats.get("textures"),
        latency_ms=round((time.perf_counter() - started) * 1000, 1))
    return rec


def _gameplay_sha(manifest: dict[str, Any]) -> str:
    return manifest["gameplay_sha256"]
