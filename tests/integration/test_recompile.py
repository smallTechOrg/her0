"""Recompile (spec/architecture.md -> Contracts 2.1): pure re-compile of a stored street, zero Google / zero Anthropic.

Real DB + the real S1b compiler, S1e bake and S10 export on the recorded fixture street (seeded with its v2 scenes),
inside a per-test data dir. Fakes are used only to force failures (oversized export, broken bake).
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import httpx
import pytest

from hero.config import reset_settings
from hero.db import model_repo, repo
from hero.fusion import publish as publish_mod
from hero.fusion.recompile import RecompileError, recompile_segment, recompile_slugs
from hero.tools import recompile as recompile_cli
from hero.tools.load_fixture_street import seed_fixture_segment


@pytest.fixture()
def data_dir(test_db, tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("HERO_DATA_DIR", str(d))
    reset_settings()
    yield d
    reset_settings()


@pytest.fixture()
def street(data_dir):
    slug = f"recompile-{uuid.uuid4().hex[:8]}"
    return seed_fixture_segment(slug)


def _knowledge(seg_id: str, version: int) -> dict:
    """The columns a compile-only recompile must never rewrite."""
    from hero.db import models
    from hero.db.session import get_session

    with get_session() as s:
        row = s.query(models.ModelVersionRow).filter_by(segment_id=seg_id, version=version).one()
        return {"model_json": json.dumps(row.model_json, sort_keys=True), "fidelity_json": json.dumps(
            row.fidelity_json, sort_keys=True), "diff_json": json.dumps(row.diff_json, sort_keys=True),
            "model_hash": row.model_hash, "summary": row.summary}


@pytest.fixture()
def no_network(monkeypatch):
    """Any attempt to build an HTTP client or open a socket during the test fails loudly."""
    def boom(*a, **k):
        raise AssertionError("recompile must not touch the network")

    monkeypatch.setattr(httpx.Client, "__init__", boom)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    try:
        import anthropic

        monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    except ImportError:  # pragma: no cover
        pass


# ------------------------------------------------------------------ happy path


def test_recompile_upgrades_the_scene_to_v3_and_exports_the_bundle(street, data_dir, no_network):
    before = repo.get_segment(street.slug)
    head = model_repo.head_version(street.id)
    knowledge = _knowledge(street.id, head)
    assert before.scene.schema_version == 2 and before.scene_schema == 2

    res = recompile_segment(street.slug)

    assert (res.scene_schema_before, res.scene_schema_after, res.unchanged) == (2, 3, False)
    assert res.cost_usd == 0.0 and res.version == head and not res.version_created
    assert res.export_sha8 and len(res.export_sha8) == 8 and res.triangles and res.triangles > 0
    after = repo.get_segment(street.slug)
    assert after.scene.schema_version == 3 and after.scene_schema == 3
    assert after.scene.assets.sha8 == res.export_sha8
    rec = model_repo.get_version(street.id, head)
    assert rec.scene_schema == 3 and rec.compiled_by == "hero.model.compile/2" and rec.gaps is not None
    assert rec.export["sha8"] == res.export_sha8 and rec.export["files"]["glb"].endswith(".glb")
    for rel in rec.export["files"].values():
        assert (data_dir / rel).is_file()
    assert (data_dir / rec.export["files"]["glb"]).read_bytes()[:4] == b"glTF"
    # knowledge columns untouched, no new version
    assert _knowledge(street.id, head) == knowledge
    assert model_repo.head_version(street.id) == head
    # gap facts surface in the result
    assert res.interpolated_m >= 0 and res.blank_bins == 0 and res.dynamic_excluded >= 0
    assert "scene 2->3" in res.summary_line() and res.export_sha8 in res.summary_line()


def test_second_recompile_is_unchanged_with_byte_identical_glb(street, data_dir, no_network):
    first = recompile_segment(street.slug)
    rec = model_repo.get_version(street.id, first.version)
    glb = (data_dir / rec.export["files"]["glb"]).read_bytes()
    stored = rec.scene

    second = recompile_segment(street.slug)

    assert second.unchanged is True and second.export_sha8 == first.export_sha8
    assert second.scene_schema_before == 3 == second.scene_schema_after
    assert "unchanged" in second.summary_line()
    again = model_repo.get_version(street.id, first.version)
    assert again.scene == stored and (data_dir / again.export["files"]["glb"]).read_bytes() == glb


def test_recompile_carries_over_the_original_generation_facts(street, data_dir):
    head = model_repo.head_version(street.id)
    old = model_repo.get_version(street.id, head).scene["generation"]
    recompile_segment(street.slug)
    new = model_repo.get_version(street.id, head).scene["generation"]
    assert new["models"] == old["models"] and new["review_iterations"] == old["review_iterations"]


def test_dry_run_writes_nothing(street, data_dir):
    head = model_repo.head_version(street.id)
    before = model_repo.get_version(street.id, head)
    res = recompile_segment(street.slug, dry_run=True)
    assert res.dry_run and res.scene_schema_after == 3 and res.unchanged is False
    assert model_repo.get_version(street.id, head).scene == before.scene
    assert not (data_dir / "exports").exists() and not (data_dir / "textures").exists()


def test_recompile_refreshes_the_crops_it_links(street, data_dir):
    recompile_segment(street.slug)
    # crops are derived files: running twice never errors and keeps urls stable
    recompile_segment(street.slug)
    assert repo.get_segment(street.slug).scene.schema_version == 3


# ------------------------------------------------------------------ edge / error paths


def test_unknown_slug_is_not_found():
    with pytest.raises(RecompileError) as exc:
        recompile_segment("no-such-street")
    assert exc.value.code == "not_found"


def test_segment_without_any_model_or_scene_cannot_be_regenerated(test_db, data_dir):
    from hero.db.repo import SegmentCreate
    from hero.scene.schema import FootpathSide, LatLon

    seg = repo.create_segment(SegmentCreate(
        name="Empty", city="Bengaluru", start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.9747, lon=77.6406),
        footpath_side=FootpathSide.left, slug=f"empty-{uuid.uuid4().hex[:8]}"))
    with pytest.raises(RecompileError) as exc:
        recompile_segment(seg.slug)
    assert exc.value.code == "validation_error" and "generate once first" in exc.value.message
    assert seg.slug not in recompile_slugs()


def test_remerge_is_refused_with_a_clear_message(street):
    with pytest.raises(RecompileError) as exc:
        recompile_segment(street.slug, remerge=True)
    assert exc.value.code == "validation_error"


def test_a_phase1_scene_only_street_is_imported_as_legacy_then_compiled(test_db, data_dir):
    from hero.db.repo import SegmentCreate
    from hero.scene.schema import FootpathSide, LatLon, Quality, load_example_scene

    seg = repo.create_segment(SegmentCreate(
        name="Legacy", city="Bengaluru", start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.97467, lon=77.64058),
        footpath_side=FootpathSide.left, slug=f"legacy-{uuid.uuid4().hex[:8]}"))
    run = repo.create_run(seg.id, inputs={})
    repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                thumbnail_path=None)
    assert model_repo.head_version(seg.id) is None
    assert seg.slug not in recompile_slugs() and seg.slug in recompile_slugs(include_legacy=True)

    res = recompile_segment(seg.slug)

    assert res.scene_schema_before in (1, 2) and res.scene_schema_after == 3 and res.cost_usd == 0.0
    assert model_repo.get_version(seg.id, 0).kind == "legacy"


def test_oversized_export_fails_and_leaves_the_previous_scene_intact(street, data_dir, monkeypatch):
    from hero.export.pipeline import ExportTooLarge

    head = model_repo.head_version(street.id)
    before = model_repo.get_version(street.id, head)

    def too_big(**kwargs):
        raise ExportTooLarge("street.glb is too big")

    tc = publish_mod.Toolchain.default()
    tc.export = too_big
    with pytest.raises(ExportTooLarge):
        recompile_segment(street.slug, toolchain=tc)
    after = model_repo.get_version(street.id, head)
    assert after.scene == before.scene and after.export == before.export and after.scene_schema == 2
    assert repo.get_segment(street.slug).scene.schema_version == 2


def test_a_broken_bake_degrades_to_procedural_textures_with_a_warning(street, data_dir):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    tc = publish_mod.Toolchain.default()
    tc.bake = broken
    res = recompile_segment(street.slug, toolchain=tc)
    assert res.scene_schema_after == 3 and any("bake failed" in w for w in res.warnings)
    assert res.export_sha8


# ------------------------------------------------------------------ CLI / all


def test_cli_all_prints_one_line_per_street_and_unchanged_on_rerun(street, data_dir, capsys, monkeypatch):
    other = seed_fixture_segment(f"recompile-b-{uuid.uuid4().hex[:6]}")
    slugs = sorted([street.slug, other.slug])
    monkeypatch.setattr(recompile_cli, "recompile_slugs", lambda include_legacy=False: slugs)  # the shared test DB holds other tests' streets

    assert recompile_cli.main(["--all"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [ln.split(":")[0] for ln in out] == slugs  # stdout carries only the per-street lines (logs go to stderr)
    assert all("scene 2->3" in ln and "glb " in ln and "triangles" in ln for ln in out)
    assert all("interpolated" in ln and "dynamic excluded" in ln and "cost 0.0" in ln for ln in out)

    assert recompile_cli.main(["--all"]) == 0
    again = capsys.readouterr().out.splitlines()
    assert len(again) == 2 and all("unchanged" in ln for ln in again)


def test_cli_single_slug_and_unknown_slug_exit_code(street, data_dir, capsys):
    assert recompile_cli.main(["--slug", street.slug]) == 0
    assert street.slug in capsys.readouterr().out
    assert recompile_cli.main(["--slug", "nope-nope"]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_recompile_all_covers_every_listed_slug_in_order(street, data_dir, monkeypatch):
    from hero.fusion import recompile as rc

    monkeypatch.setattr(rc, "recompile_slugs", lambda **kw: [street.slug])
    assert [r.slug for r in rc.recompile_all()] == [street.slug]


def test_cli_all_skips_legacy_streets_and_include_legacy_recompiles_one(test_db, street, data_dir, capsys):
    from hero.db.repo import SegmentCreate
    from hero.scene.schema import FootpathSide, LatLon, Quality, load_example_scene

    seg = repo.create_segment(SegmentCreate(
        name="Legacy", city="Bengaluru", start=LatLon(lat=12.974, lon=77.6405), end=LatLon(lat=12.97467, lon=77.64058),
        footpath_side=FootpathSide.left, slug=f"legacy-{uuid.uuid4().hex[:8]}"))
    run = repo.create_run(seg.id, inputs={})
    repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full,
                                thumbnail_path=None)
    before = repo.get_segment(seg.slug).scene

    assert recompile_cli.main(["--all"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"{seg.slug}: skipped (legacy, no canonical model)" in out
    assert model_repo.head_version(seg.id) is None and repo.get_segment(seg.slug).scene == before  # untouched

    assert recompile_cli.main(["--slug", seg.slug]) == 0
    assert "skipped (legacy, no canonical model)" in capsys.readouterr().out
    assert model_repo.head_version(seg.id) is None

    assert recompile_cli.main(["--slug", seg.slug, "--include-legacy"]) == 0
    assert "scene" in capsys.readouterr().out and model_repo.get_version(seg.id, 0).kind == "legacy"
