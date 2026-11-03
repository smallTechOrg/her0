"""Seed the recorded fixture street (no network, no spend) into the configured database.

    uv run python -m hero.tools.load_fixture_street [--slug fixture-curved-street] [--up-to N] [--fixture-dir DIR]

Used by the ``seeded_fixture_segment`` conftest fixture (integration / e2e tiers) and by the human to see
the curved corridor in the gallery. Idempotent: re-running continues from the segment's current head version.

Fixture-directory contract (written by ``tests/fixtures/make_model_fixtures.py``, slice S1d; default
``tests/e2e/fixtures/model_fixture/``)::

    manifest.json                       optional; keys slug, name, city, start{lat,lon}, end{lat,lon}, footpath_side
                                        override what is derived from scene_v*.json / model_v0.json
    observations.json                   list of Observation dicts (``hero.model.types.Observation`` JSON; ``segment_id``
                                        and ``created_at`` optional - ids are kept so model evidence resolves)
    images/<basename of file_path>      copied to <data_dir>/<file_path> (missing source files are reported as warnings)
    model_v0..vN.json                   CanonicalModel          (REQUIRED: model_v0.json)
    scene_v0..vN.json                   compiled Scene v2 dict  (v>=1 required to publish; v0 optional)
    fidelity_v0..vN.json                FidelityMap             (optional: recomputed trivially as L0 if absent)
    diff_v1..vN.json                    ModelDiff               (optional)
    summary_v0..vN.txt                  version summary text    (optional)
    report_run1.json, report_run2.json  RunReport, one pipeline run each; ``result_version`` ties a run to a version
    extractions.json                    list of {run: 1-based run index (default 1), batch_index, kind, observation_ids,
                                        model, prompt_name, extraction}

Seeding under a slug other than ``fixture-curved-street`` re-keys the observation ids deterministically
(``uuid5``), everywhere they appear (models, scenes, reports, extractions), because observation ids are
globally unique primary keys and two seeded copies of the fixture must coexist in one DB.

Version 0 is stored with ``save_version_only``; versions >= 1 are published (``publish_version``), so the segment
ends ``ready`` with the head scene. Raises ``FixtureMissingError`` (a ``FileNotFoundError``) until the fixture exists.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import uuid
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hero.config import PROJECT_ROOT, get_settings
from hero.db import model_repo, repo
from hero.db.model_repo import ExtractionCreate, ModelVersionCreate, ObservationCreate
from hero.model.types import CanonicalModel, FidelityMap, FidelityBin, FidelityOverall, ModelDiff, RunReport
from hero.scene.schema import FootpathSide, LatLon, RunStatus

FIXTURE_SLUG = "fixture-curved-street"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "e2e" / "fixtures" / "model_fixture"
_VERSION_FILE = re.compile(r"^model_v(\d+)\.json$")


class FixtureMissingError(FileNotFoundError):
    """The fixture directory (or a required file in it) does not exist yet."""


@dataclass
class SeedReport:
    segment: repo.SegmentRecord
    head_version: int | None
    versions_loaded: list[int] = field(default_factory=list)
    observations: int = 0
    runs: int = 0
    warnings: list[str] = field(default_factory=list)


def fixture_available(fixture_dir: Path | None = None) -> bool:
    return ((fixture_dir or DEFAULT_FIXTURE_DIR) / "model_v0.json").is_file()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _l0_fidelity(length_s: float) -> FidelityMap:
    n = max(1, int(-(-length_s // 5.0)))
    bins = [FidelityBin(index=i, s_from=i * 5.0, s_to=min(length_s, (i + 1) * 5.0), level=0) for i in range(n)]
    return FidelityMap(bin_m=5.0, length_s=length_s, bins=bins,
                       overall=FidelityOverall(level=0, score=0.0,
                                               histogram_m={"L0": length_s, "L1": 0.0, "L2": 0.0, "L3": 0.0}))


def _segment_spec(fx: Path, manifest: dict, model0: CanonicalModel, versions: list[int]) -> dict[str, Any]:
    """name / city / endpoints / side from the manifest, else from the newest scene's ``segment``, else the model."""
    info: dict[str, Any] = {}
    for v in sorted(versions, reverse=True):
        scene_path = fx / f"scene_v{v}.json"
        if scene_path.is_file():
            scene = _read_json(scene_path)
            if isinstance(scene, dict) and scene.get("segment"):
                info = dict(scene["segment"])
                break
    pts = model0.reference_line.points
    spec = {
        "name": info.get("name", "Fixture curved street (test fixture)"),
        "city": info.get("city", "Bengaluru"),
        "start": info.get("start", {"lat": pts[0].lat, "lon": pts[0].lon}),
        "end": info.get("end", {"lat": pts[-1].lat, "lon": pts[-1].lon}),
        "footpath_side": info.get("footpath_side", model0.footpath_side),
    }
    for key in spec:
        if key in manifest:
            spec[key] = manifest[key]
    return spec


def _remap(obj: Any, pattern: re.Pattern | None, mapping: dict[str, str]) -> Any:
    """Replace every observation id (whole-token match) in all string values of a JSON structure."""
    if pattern is None:
        return obj
    if isinstance(obj, str):
        return pattern.sub(lambda m: mapping[m.group(0)], obj)
    if isinstance(obj, list):
        return [_remap(x, pattern, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _remap(v, pattern, mapping) for k, v in obj.items()}
    return obj


def _retarget_slug(model: CanonicalModel, scene: dict | None, slug: str) -> tuple[CanonicalModel, dict | None]:
    model = model.model_copy(update={"segment_slug": slug})
    if scene is not None and isinstance(scene.get("segment"), dict):
        scene = {**scene, "segment": {**scene["segment"], "slug": slug}}
    return model, scene


def load_fixture(slug: str = FIXTURE_SLUG, *, up_to_version: int | None = None, fixture_dir: Path | None = None,
                 data_dir: Path | None = None) -> SeedReport:
    """Seed ``slug`` from the fixture directory; see the module docstring for the contract."""
    fx = Path(fixture_dir) if fixture_dir else DEFAULT_FIXTURE_DIR
    if not (fx / "model_v0.json").is_file():
        raise FixtureMissingError(
            f"Model fixture not found at {fx} (model_v0.json missing). It is generated by "
            "tests/fixtures/make_model_fixtures.py (slice S1d); run that first.")
    data_dir = Path(data_dir) if data_dir else get_settings().data_dir_abs
    manifest = _read_json(fx / "manifest.json") if (fx / "manifest.json").is_file() else {}
    versions = sorted(int(m.group(1)) for p in fx.iterdir() if (m := _VERSION_FILE.match(p.name)))
    if up_to_version is not None:
        versions = [v for v in versions if v <= up_to_version]
    report = SeedReport(segment=None, head_version=None)  # type: ignore[arg-type]

    obs_records = _read_json(fx / "observations.json") if (fx / "observations.json").is_file() else []
    mapping: dict[str, str] = {}
    if slug != FIXTURE_SLUG:
        mapping = {r["id"]: str(uuid.uuid5(uuid.NAMESPACE_URL, f"hero-fixture:{slug}:{r['id']}"))
                   for r in obs_records if r.get("id")}
    pattern = (re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")(?![\w.-])")
               if mapping else None)

    def load(name: str) -> Any:
        return _remap(_read_json(fx / name), pattern, mapping)

    model0 = CanonicalModel.model_validate(load("model_v0.json"))
    segment = repo.get_segment(slug)
    if segment is None:
        spec = _segment_spec(fx, manifest, model0, versions)
        segment = repo.create_segment(repo.SegmentCreate(
            name=spec["name"], city=spec["city"], start=LatLon(**spec["start"]), end=LatLon(**spec["end"]),
            footpath_side=FootpathSide(spec["footpath_side"]), slug=slug))
    seg_id = segment.id

    # ---- observations (ids preserved so model evidence resolves)
    for rec in _remap(obs_records, pattern, mapping):
        fields = {k: v for k, v in rec.items() if k in ObservationCreate.model_fields}
        fields["segment_id"] = seg_id
        create = ObservationCreate.model_validate(fields)
        _, created = model_repo.upsert_observation(create)
        report.observations += int(created)
        src = fx / "images" / Path(create.file_path).name
        dst = data_dir / create.file_path
        if not dst.exists():
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            else:
                report.warnings.append(f"image missing for observation {create.id or create.obs_key}: {src.name}")

    # ---- runs (one per report); result_version ties a run to a version
    run_for_version: dict[int, str] = {}
    run_ids: list[str] = []
    reports: list[RunReport] = []
    existing_runs = repo.list_runs_for_segment(seg_id, limit=100)
    for path in sorted(fx.glob("report_run*.json"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        rep = RunReport.model_validate(_remap(_read_json(path), pattern, mapping))
        if existing_runs and len(existing_runs) >= len(reports) + 1:
            run = sorted(existing_runs, key=lambda r: r.created_at)[len(reports)]
        else:
            run = repo.create_run(seg_id, {"mode": rep.mode, "fixture": True})
            repo.update_run(run.id, repo.RunUpdate(status=RunStatus.succeeded, started_at=datetime.now(UTC),
                                                   finished_at=datetime.now(UTC)))
            report.runs += 1
        run_ids.append(run.id)
        reports.append(rep.model_copy(update={"run_id": run.id, "segment_slug": slug}))
        if rep.version_created and rep.result_version is not None:
            run_for_version[rep.result_version] = run.id
    for rep in reports:
        model_repo.save_run_report(rep.run_id, seg_id, rep)

    # ---- versions
    head = model_repo.head_version(seg_id)
    for v in versions:
        if head is not None and v <= head:
            continue
        model = CanonicalModel.model_validate(load(f"model_v{v}.json"))
        scene_path = fx / f"scene_v{v}.json"
        scene = load(scene_path.name) if scene_path.is_file() else None
        model, scene = _retarget_slug(model, scene, slug)
        run_id = run_for_version.get(v)
        if run_id is not None:
            model = model.model_copy(update={"run_id": run_id})
            if scene is not None and isinstance(scene.get("generation"), dict):
                scene = {**scene, "generation": {**scene["generation"], "run_id": run_id}}
        fid_path = fx / f"fidelity_v{v}.json"
        fidelity = FidelityMap.model_validate(load(fid_path.name)) if fid_path.is_file() else (
            model.fidelity or _l0_fidelity(model.reference_line.length_s))
        diff_path = fx / f"diff_v{v}.json"
        diff = ModelDiff.model_validate(load(diff_path.name)) if diff_path.is_file() else None
        summ_path = fx / f"summary_v{v}.txt"
        rec = ModelVersionCreate(
            version=v, kind=model.kind.value, parent_version=model.parent_version, run_id=model.run_id,
            model=model, scene=scene, fidelity=fidelity, diff=diff,
            summary=summ_path.read_text(encoding="utf-8").strip() if summ_path.is_file() else "")
        if v == 0:
            model_repo.save_version_only(seg_id, rec)
        else:
            if scene is None:
                raise FixtureMissingError(f"scene_v{v}.json is required to publish version {v}.")
            quality = "full" if fidelity.overall.level >= 1 else "generic"
            model_repo.publish_version(seg_id, rec, thumbnail_path=None, quality=quality)
        report.versions_loaded.append(v)
        head = v

    # ---- recorded extractions (only when this call created the runs)
    ex_path = fx / "extractions.json"
    if ex_path.is_file() and report.runs and run_ids:
        for item in load(ex_path.name):
            idx = max(1, int(item.get("run", 1))) - 1
            model_repo.save_extraction(ExtractionCreate(
                segment_id=seg_id, run_id=run_ids[min(idx, len(run_ids) - 1)], batch_index=item["batch_index"],
                kind=item["kind"], observation_ids=item["observation_ids"], model=item["model"],
                prompt_name=item["prompt_name"], extraction=item["extraction"]))

    report.segment = repo.require_segment(slug)
    report.head_version = model_repo.head_version(seg_id)
    return report


def seed_fixture_segment(slug: str = FIXTURE_SLUG, *, up_to_version: int | None = None,
                         fixture_dir: Path | None = None) -> repo.SegmentRecord:
    """Seed the fixture street under ``slug`` and return its ``SegmentRecord`` (``ready`` once a version >= 1 loaded)."""
    return load_fixture(slug, up_to_version=up_to_version, fixture_dir=fixture_dir).segment


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load the recorded fixture street (no network, no spend).")
    ap.add_argument("--slug", default=FIXTURE_SLUG)
    ap.add_argument("--up-to", type=int, default=None, help="load versions 0..N only")
    ap.add_argument("--fixture-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    try:
        rep = load_fixture(args.slug, up_to_version=args.up_to, fixture_dir=args.fixture_dir)
    except FixtureMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for w in rep.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"loaded fixture street {rep.segment.slug!r}: head version {rep.head_version}, "
          f"{rep.observations} new observation(s), {rep.runs} new run(s), versions loaded {rep.versions_loaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
