"""Pure re-compile of a stored street: bake textures -> compile -> export, from the head model and the cached
observations. **Zero Google, zero Anthropic** (cost is always 0.0): nothing here constructs a network client.

* ``recompile_segment`` - one street. Overwrites only the derived columns (``scene_json / scene_schema / compiled_by /
  gaps_json / export_json``, plus the segment pointer when the head is served); the knowledge columns are never written.
  Idempotent: a second run reproduces the same scene and the same content-addressed export (``unchanged=True``).
* ``recompile_all`` - every segment that has a canonical model version. Phase-1 legacy streets (scene only / legacy v0)
  are skipped unless ``include_legacy``; their stored scenes keep being served as stored.

Used by ``python -m hero.tools.recompile`` and by ``POST /api/admin/segments/{slug}/regenerate`` (compile mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from hero.config import get_settings
from hero.db import model_repo, repo
from hero.fusion import assets as assets_mod
from hero.fusion import publish as publish_mod
from hero.logging import get_logger
from hero.model.types import CanonicalModel, EntityState, Observation

log = get_logger("hero.fusion.recompile")

OBS_LIMIT = 100_000
NOTHING_TO_REGENERATE = "Nothing to regenerate: generate once first."


class RecompileError(Exception):
    """A recompile that cannot run. ``code`` is an API error code (``not_found`` | ``validation_error``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RecompileResult:
    slug: str
    version: int
    scene_schema_before: int | None
    scene_schema_after: int
    unchanged: bool
    version_created: bool = False
    interpolated_m: float = 0.0
    dynamic_excluded: int = 0
    blank_bins: int = 0
    missing_extractions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0  # always 0.0: no network, no LLM
    export_sha8: str | None = None
    triangles: int | None = None
    primitives: int | None = None
    mesh_sha256: str | None = None
    dry_run: bool = False

    def summary_line(self) -> str:
        glb = self.export_sha8 or "-"
        tri = "-" if self.triangles is None else str(self.triangles)
        facts = (f"glb {glb}, triangles {tri}, interpolated {self.interpolated_m:g} m, "
                 f"dynamic excluded {self.dynamic_excluded}, cost {self.cost_usd:.1f}")
        if self.unchanged:
            return f"{self.slug}: unchanged (scene {self.scene_schema_after}; {facts})"
        before = "none" if self.scene_schema_before is None else str(self.scene_schema_before)
        return f"{self.slug}: scene {before}->{self.scene_schema_after}, {facts}"


def _crop_urls_on_disk(model: CanonicalModel, data_dir: Path) -> dict[str, str]:
    """Crop URLs for a dry run: what ``cut_model_crops`` would return, restricted to files already on disk."""
    out: dict[str, str] = {}
    ids = [e.id for e in model.entities if e.state is not EntityState.removed and e.billboard is not None]
    ids += [b.id for b in model.buildings if b.facades]
    for item in sorted(ids):
        if (assets_mod.crops_dir(data_dir, model.segment_slug) / f"{item}.png").is_file():
            out[item] = assets_mod.crop_url(model.segment_slug, item, model.version)
    return out


def _generation_of(scene: Mapping[str, Any] | None) -> dict[str, Any]:
    """The facts of the original run that a recompile must carry over (it cannot rediscover them)."""
    gen = (scene or {}).get("generation")
    if not isinstance(gen, dict):
        return {}
    keep = {k: gen[k] for k in ("models", "review_iterations") if k in gen}
    return keep


def _ensure_model(seg: repo.SegmentRecord) -> None:
    if model_repo.head_version(seg.id) is None:
        publish_mod.import_legacy_segment(seg, now=datetime.now(UTC))
    if model_repo.head_version(seg.id) is None:
        raise RecompileError("validation_error", NOTHING_TO_REGENERATE)


def recompile_segment(slug: str, *, remerge: bool = False, dry_run: bool = False, data_dir: Path | None = None,
                      toolchain: publish_mod.Toolchain | None = None) -> RecompileResult:
    """Recompile the head model version of ``slug`` from stored data. See the module docstring."""
    if remerge:
        raise RecompileError("validation_error", "remerge not available: replaying stored extractions is not built; "
                                                 "use mode 'compile' (or Generate, which costs Claude).")
    seg = repo.get_segment(slug)
    if seg is None:
        raise RecompileError("not_found", f"No street with slug {slug!r}.")
    _ensure_model(seg)
    head = model_repo.head_version(seg.id)
    assert head is not None
    rec = model_repo.get_version(seg.id, head)
    assert rec is not None
    data_dir = Path(data_dir) if data_dir is not None else get_settings().data_dir_abs
    observations: dict[str, Observation] = {
        o.id: o for o in model_repo.get_observations(seg.id, limit=OBS_LIMIT)}
    model = rec.model
    crops = (_crop_urls_on_disk(model, data_dir) if dry_run else
             assets_mod.cut_model_crops(model, observations, data_dir=data_dir, version=model.version))
    derived = publish_mod.derive_scene(
        model, observations=observations, assets=crops, fidelity=rec.fidelity, segment_name=seg.name, city=seg.city,
        data_dir=data_dir, generation=_generation_of(rec.scene) or None, toolchain=toolchain, export=not dry_run)
    new_scene = derived.scene.model_dump(mode="json")
    if dry_run:
        # no export ran, so compare scenes without their assets block
        old = {k: v for k, v in (rec.scene or {}).items() if k != "assets"}
        unchanged = old == {k: v for k, v in new_scene.items() if k != "assets"} and rec.gaps == derived.gaps
    else:
        unchanged = (rec.scene == new_scene and rec.gaps == derived.gaps and rec.export == derived.export
                     and rec.scene_schema == derived.scene_schema and rec.compiled_by == derived.compiled_by)
    if not dry_run and not unchanged:
        model_repo.update_derived(seg.id, head, scene=new_scene, compiled_by=derived.compiled_by, gaps=derived.gaps,
                                  export=derived.export, scene_schema=derived.scene_schema)
    export = derived.export or (rec.export if dry_run else None) or {}
    result = RecompileResult(
        slug=slug, version=head, scene_schema_before=rec.scene_schema if rec.scene else None,
        scene_schema_after=derived.scene_schema, unchanged=unchanged, interpolated_m=derived.interpolated_m,
        dynamic_excluded=derived.dynamic_excluded, blank_bins=derived.blank_bins, warnings=list(derived.warnings),
        export_sha8=export.get("sha8"), triangles=(export.get("stats") or {}).get("triangles"),
        primitives=(export.get("stats") or {}).get("primitives"), mesh_sha256=export.get("mesh_sha256"),
        dry_run=dry_run)
    log.info("scene.recompiled", slug=slug, version=head, from_schema=result.scene_schema_before,
             to_schema=result.scene_schema_after, unchanged=unchanged, cost_usd=0.0)
    return result


def is_legacy(seg: repo.SegmentRecord) -> bool:
    """A Phase-1 import: no canonical model version (only a stored v1/v2 scene), or a head that is the legacy v0 import."""
    head = model_repo.head_version(seg.id)
    if head is None:
        return True
    rec = model_repo.get_version(seg.id, head)
    return rec is not None and rec.kind == "legacy"


def recompile_slugs(*, include_legacy: bool = False) -> list[str]:
    """Slugs ``recompile_all`` covers (sorted): every street with a canonical model version; Phase-1 legacy streets
    (scene only / legacy v0) only with ``include_legacy``."""
    out: list[str] = []
    for seg in repo.list_segments():
        if model_repo.head_version(seg.id) is None and seg.scene is None:
            continue
        if include_legacy or not is_legacy(seg):
            out.append(seg.slug)
    return sorted(out)


def recompile_all(*, remerge: bool = False, dry_run: bool = False, data_dir: Path | None = None,
                  toolchain: publish_mod.Toolchain | None = None) -> list[RecompileResult]:
    return [recompile_segment(slug, remerge=remerge, dry_run=dry_run, data_dir=data_dir, toolchain=toolchain)
            for slug in recompile_slugs()]
