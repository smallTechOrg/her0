"""hero.db.model_repo + hero.tools.load_fixture_street against the real SQLite test DB (per-process file)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hero.db import model_repo, repo
from hero.db.model_repo import (
    ExtractionCreate,
    ModelVersionCreate,
    ObservationCreate,
    VersionConflictError,
)
from hero.db.session import get_session
from hero.model.scene_v2 import SceneV2
from hero.model.types import ModelKind, ObservationKind, ObservationSource, RunReport, ViewKind
from hero.scene.schema import FootpathSide, LatLon, Quality, RunStatus, SegmentStatus
from hero.tools import load_fixture_street as lfs
from tests.fixtures.street_fixtures import (
    make_entity,
    make_fidelity_map,
    make_model,
    minimal_scene_v2_dict,
    straight_reference_line,
    write_mini_fixture_dir,
)

START = LatLon(lat=12.9706, lon=77.6413)
END = LatLon(lat=12.9715, lon=77.6413)


@pytest.fixture()
def segment(test_db):
    name = f"Model Repo Street {uuid.uuid4().hex[:8]} (west footpath)"
    return repo.create_segment(repo.SegmentCreate(name=name, city="Bengaluru", start=START, end=END,
                                                  footpath_side=FootpathSide.left))


def _obs(segment_id: str, key: str = "k1", **over) -> ObservationCreate:
    data = dict(segment_id=segment_id, obs_key=key, kind=ObservationKind.streetview,
                source=ObservationSource.google_streetview, sha256="a" * 64, file_path=f"imagery/{key}.jpg",
                media_type="image/jpeg", width_px=640, height_px=640, bytes=1234, lat=12.9706, lon=77.6413,
                heading_deg=0.0, pitch_deg=0.0, fov_deg=90.0, pano_id="pano-1", capture_date="2024-02",
                station_s=10.0, offset_t=-1.5, view=ViewKind.forward, georef={"size_px": [640, 640]}, cost_usd=0.007)
    data.update(over)
    return ObservationCreate(**data)


def _version(segment, version: int, *, kind: ModelKind | None = None, scene: dict | None = None,
             entities=None, fused=None) -> ModelVersionCreate:
    kind = kind or (ModelKind.prior if version == 0 else ModelKind.fused)
    model = make_model(straight_reference_line(100.0), slug=segment.slug, version=version, kind=kind,
                       entities=entities or [], fused_observation_ids=fused or [])
    return ModelVersionCreate(version=version, kind=kind.value, parent_version=model.parent_version, run_id=None,
                              model=model, scene=scene, fidelity=make_fidelity_map(model.length_s, level=min(version, 2)),
                              diff=None, summary=f"v{version} summary")


# ---------------------------------------------------------------- observations


def test_upsert_observation_is_idempotent_on_segment_and_key(segment):
    first, created = model_repo.upsert_observation(_obs(segment.id))
    again, created_again = model_repo.upsert_observation(_obs(segment.id, cost_usd=0.0))
    assert created is True and created_again is False
    assert again.id == first.id and again.cost_usd == pytest.approx(0.007)  # the stored row wins
    assert first.kind is ObservationKind.streetview and first.view is ViewKind.forward
    assert first.georef == {"size_px": [640, 640]} and first.created_at.tzinfo is not None
    assert len(model_repo.get_observations(segment.id)) == 1


def test_same_key_in_two_segments_is_two_observations(segment, test_db):
    other = repo.create_segment(repo.SegmentCreate(name=f"Other {uuid.uuid4().hex[:8]}", city="Bengaluru", start=START,
                                                   end=END, footpath_side=FootpathSide.right))
    a, _ = model_repo.upsert_observation(_obs(segment.id))
    b, created = model_repo.upsert_observation(_obs(other.id))
    assert created is True and a.id != b.id


def test_upsert_observation_for_unknown_segment_raises(test_db):
    with pytest.raises(repo.NotFoundError):
        model_repo.upsert_observation(_obs(str(uuid.uuid4())))


def test_observation_create_rejects_bad_enums_and_oversized_keys(segment):
    with pytest.raises(ValueError):
        _obs(segment.id, kind="panorama")
    with pytest.raises(ValueError):
        _obs(segment.id, key="x" * 129)
    with pytest.raises(ValueError):
        _obs(segment.id, sha256="short")
    with pytest.raises(ValueError):
        _obs(segment.id, capture_date="2024-02-01")


def test_get_observations_filters_by_ids_kinds_and_pages(segment):
    rows = [model_repo.upsert_observation(_obs(segment.id, key=f"k{i}"))[0] for i in range(4)]
    sat, _ = model_repo.upsert_observation(_obs(
        segment.id, key="sat", kind=ObservationKind.satellite_overview, source=ObservationSource.google_static_maps,
        heading_deg=None, pitch_deg=None, fov_deg=None, pano_id=None, capture_date=None, view=None, zoom=19, scale=2,
        meters_per_px=0.15, station_s=None, offset_t=None))
    assert len(model_repo.get_observations(segment.id)) == 5
    assert {o.id for o in model_repo.get_observations(segment.id, ids=[rows[1].id, rows[2].id])} == {rows[1].id, rows[2].id}
    assert [o.id for o in model_repo.get_observations(segment.id, kinds=["satellite_overview"])] == [sat.id]
    assert len(model_repo.get_observations(segment.id, kinds=["streetview"], limit=2)) == 2
    assert len(model_repo.get_observations(segment.id, kinds=["streetview"], limit=2, offset=3)) == 1
    assert model_repo.get_observations(segment.id, ids=[]) == []
    assert model_repo.get_observation(sat.id).kind is ObservationKind.satellite_overview
    assert model_repo.get_observation("nope") is None
    assert model_repo.observation_counts(segment.id) == {"streetview": 4, "satellite_overview": 1}
    newest = model_repo.get_observations(segment.id, newest_first=True)
    assert [o.id for o in newest] == [o.id for o in reversed(model_repo.get_observations(segment.id))]


def test_observation_counts_for_a_segment_without_evidence_is_empty(segment):
    assert model_repo.observation_counts(segment.id) == {}


# ---------------------------------------------------------------- versions


def test_head_version_none_then_dense_versions_from_zero(segment):
    assert model_repo.head_version(segment.id) is None and model_repo.list_versions(segment.id) == []
    with pytest.raises(VersionConflictError):
        model_repo.save_version_only(segment.id, _version(segment, 1))  # must start at 0
    rec = model_repo.save_version_only(segment.id, _version(segment, 0))
    assert rec.version == 0 and rec.kind == "prior" and len(rec.model_hash) == 64
    assert model_repo.head_version(segment.id) == 0
    with pytest.raises(VersionConflictError):
        model_repo.save_version_only(segment.id, _version(segment, 0))
    with pytest.raises(VersionConflictError):
        model_repo.save_version_only(segment.id, _version(segment, 2))


def test_save_version_only_does_not_touch_the_segment(segment):
    model_repo.save_version_only(segment.id, _version(segment, 0))
    after = repo.require_segment(segment.slug)
    assert after.scene is None and after.status is SegmentStatus.draft and after.quality is None


def test_publish_version_writes_version_and_segment_pointer_in_one_step(segment):
    model_repo.save_version_only(segment.id, _version(segment, 0))
    scene = minimal_scene_v2_dict(100.0, slug=segment.slug)
    entity = make_entity(entity_id="ent-00000000b1")
    rec = model_repo.publish_version(segment.id, _version(segment, 1, scene=scene, entities=[entity],
                                                          fused=["obs-1", "obs-2"]),
                                     thumbnail_path="previews/r/thumb.png", quality="full")
    assert rec.version == 1 and rec.entity_count == 1 and rec.observation_count == 2 and rec.fidelity_level == 1
    seg = repo.require_segment(segment.slug)
    assert seg.status is SegmentStatus.ready and seg.quality is Quality.full
    assert isinstance(seg.scene, SceneV2) and seg.thumbnail_path == "previews/r/thumb.png"
    got = model_repo.get_version(segment.id, 1)
    assert got.model.entities[0].id == "ent-00000000b1" and got.scene == scene and got.summary == "v1 summary"
    assert [v.version for v in model_repo.list_versions(segment.id)] == [0, 1]
    assert model_repo.get_version(segment.id, 7) is None


def test_publish_version_requires_head_plus_one_a_scene_and_a_known_quality(segment):
    scene = minimal_scene_v2_dict(100.0, slug=segment.slug)
    with pytest.raises(VersionConflictError):
        model_repo.publish_version(segment.id, _version(segment, 1, scene=scene), thumbnail_path=None, quality="full")
    model_repo.save_version_only(segment.id, _version(segment, 0))
    with pytest.raises(VersionConflictError):
        model_repo.publish_version(segment.id, _version(segment, 2, scene=scene), thumbnail_path=None, quality="full")
    with pytest.raises(repo.InvalidInputError):
        model_repo.publish_version(segment.id, _version(segment, 1, scene=None), thumbnail_path=None, quality="full")
    with pytest.raises(repo.InvalidInputError):
        model_repo.publish_version(segment.id, _version(segment, 1, scene=scene), thumbnail_path=None, quality="great")
    assert model_repo.head_version(segment.id) == 0 and repo.require_segment(segment.slug).scene is None


def test_version_conflict_error_is_a_value_error_and_a_repo_error():
    assert issubclass(VersionConflictError, ValueError) and issubclass(VersionConflictError, repo.InvalidInputError)


def test_failed_publish_leaves_segment_and_versions_unchanged(segment, monkeypatch):
    model_repo.save_version_only(segment.id, _version(segment, 0))
    scene = minimal_scene_v2_dict(100.0, slug=segment.slug)

    def boom(*args, **kwargs):
        raise RuntimeError("forced failure after the version insert")

    monkeypatch.setattr(model_repo, "_apply_segment_pointer", boom)
    with pytest.raises(RuntimeError, match="forced"):
        model_repo.publish_version(segment.id, _version(segment, 1, scene=scene), thumbnail_path="x.png", quality="full")
    monkeypatch.undo()
    assert model_repo.head_version(segment.id) == 0
    assert model_repo.get_version(segment.id, 1) is None
    seg = repo.require_segment(segment.slug)
    assert seg.scene is None and seg.status is SegmentStatus.draft and seg.thumbnail_path is None


def test_failed_publish_keeps_a_previously_published_scene(segment, monkeypatch):
    model_repo.save_version_only(segment.id, _version(segment, 0))
    scene = minimal_scene_v2_dict(100.0, slug=segment.slug)
    model_repo.publish_version(segment.id, _version(segment, 1, scene=scene), thumbnail_path=None, quality="full")
    monkeypatch.setattr(model_repo, "_apply_segment_pointer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        model_repo.publish_version(segment.id, _version(segment, 2, scene={**scene, "walkability": scene["walkability"]}),
                                   thumbnail_path=None, quality="full")
    monkeypatch.undo()
    assert model_repo.head_version(segment.id) == 1
    assert repo.require_segment(segment.slug).scene.about.model_version == 1


def test_version_create_must_agree_with_its_model(segment):
    model = make_model(straight_reference_line(100.0), slug=segment.slug, version=0, kind=ModelKind.prior)
    with pytest.raises(ValueError, match="version"):
        ModelVersionCreate(version=1, kind="prior", model=model, fidelity=make_fidelity_map(100.0))
    with pytest.raises(ValueError, match="kind"):
        ModelVersionCreate(version=0, kind="fused", model=model, fidelity=make_fidelity_map(100.0))


def test_removed_entities_do_not_count_in_entity_count(segment):
    from hero.model.types import EntityState

    ents = [make_entity(), make_entity(entity_id="ent-00000000c2", state=EntityState.removed, confidence=0.1)]
    rec = model_repo.save_version_only(segment.id, _version(segment, 0, entities=ents))
    assert rec.entity_count == 1


def test_published_model_survives_an_engine_restart(segment):
    from hero.db.session import reset_engine

    model_repo.save_version_only(segment.id, _version(segment, 0))
    scene = minimal_scene_v2_dict(100.0, slug=segment.slug)
    published = model_repo.publish_version(segment.id, _version(segment, 1, scene=scene), thumbnail_path=None, quality="full")
    reset_engine()
    again = model_repo.get_version(segment.id, 1)
    assert again.model_hash == published.model_hash and again.model == published.model
    assert isinstance(repo.require_segment(segment.slug).scene, SceneV2)


# ---------------------------------------------------------------- extractions / reports


def test_extractions_round_trip_in_batch_order_and_filter_by_run(segment):
    r1 = repo.create_run(segment.id, {})
    r2 = repo.create_run(segment.id, {})
    for run, idx, kind in ((r1, 1, "streetlevel"), (r1, 0, "overhead"), (r2, 0, "critique")):
        model_repo.save_extraction(ExtractionCreate(
            segment_id=segment.id, run_id=run.id, batch_index=idx, kind=kind, observation_ids=["obs-1", "obs-2"],
            model="claude-sonnet-4-5", prompt_name=f"perceive_{kind}", extraction={"frames": [{"image_index": 1}]}))
    assert len(model_repo.list_extractions(segment.id)) == 3
    only1 = model_repo.list_extractions(segment.id, r1.id)
    assert len(only1) == 2 and only1[0].observation_ids == ["obs-1", "obs-2"]
    assert only1[0].extraction == {"frames": [{"image_index": 1}]} and only1[0].run_id == r1.id


def test_extraction_needs_an_existing_run_and_a_known_kind(segment):
    with pytest.raises(repo.NotFoundError):
        model_repo.save_extraction(ExtractionCreate(segment_id=segment.id, run_id=str(uuid.uuid4()), batch_index=0,
                                                    kind="overhead", observation_ids=[], model="m", prompt_name="p",
                                                    extraction={}))
    with pytest.raises(ValueError):
        ExtractionCreate(segment_id=segment.id, run_id="r", batch_index=0, kind="telepathy", observation_ids=[],
                         model="m", prompt_name="p", extraction={})


def test_run_report_save_get_and_replace(segment):
    run = repo.create_run(segment.id, {})
    assert model_repo.get_run_report(run.id) is None
    report = RunReport(run_id=run.id, segment_slug=segment.slug, mode="generate", base_version=0, result_version=1,
                       version_created=True, summary="first")
    model_repo.save_run_report(run.id, segment.id, report)
    assert model_repo.get_run_report(run.id) == report
    model_repo.save_run_report(run.id, segment.id, report.model_copy(update={"summary": "second", "version_created": False}))
    got = model_repo.get_run_report(run.id)
    assert got.summary == "second" and got.version_created is False
    with get_session() as s:
        assert s.execute(text("select count(*) from run_reports where run_id=:r"), {"r": run.id}).scalar() == 1
    with pytest.raises(repo.NotFoundError):
        model_repo.save_run_report(str(uuid.uuid4()), segment.id, report)


# ---------------------------------------------------------------- fixture loader (contract of the S1d directory)


@pytest.fixture()
def mini_fixture(tmp_path):
    return write_mini_fixture_dir(tmp_path / "model_fixture", slug="fixture-curved-street")


def _slug() -> str:
    return f"fx-{uuid.uuid4().hex[:8]}"


def test_loader_raises_a_clear_error_until_the_fixture_exists(tmp_path):
    assert lfs.fixture_available(tmp_path) is False
    with pytest.raises(lfs.FixtureMissingError, match="model_v0.json"):
        lfs.load_fixture(_slug(), fixture_dir=tmp_path / "nope")
    assert isinstance(lfs.FixtureMissingError("x"), FileNotFoundError)


def test_loader_seeds_segment_observations_versions_runs_and_extractions(test_db, mini_fixture, tmp_path):
    slug = _slug()
    rep = lfs.load_fixture(slug, fixture_dir=mini_fixture, data_dir=tmp_path / "data")
    seg = rep.segment
    assert seg.slug == slug and seg.status is SegmentStatus.ready and seg.quality is Quality.full
    assert rep.head_version == 2 and rep.versions_loaded == [0, 1, 2] and rep.observations == 4 and rep.runs == 2
    assert rep.warnings == []
    assert isinstance(seg.scene, SceneV2) and seg.scene.segment.slug == slug
    assert (tmp_path / "data" / "imagery" / "obs-fx-2.jpg").is_file()
    # model evidence resolves: observation ids preserved
    v2 = model_repo.get_version(seg.id, 2)
    assert v2.model.segment_slug == slug and v2.run_id and v2.model.run_id == v2.run_id
    ids = {o.id for o in model_repo.get_observations(seg.id)}
    assert set(v2.model.fused_observation_ids) <= ids
    assert {e.provenance[0].observation_id for e in v2.model.entities} <= ids
    assert v2.summary == "Summary of version 2." and v2.diff.to_version == 2 and v2.fidelity.overall.level == 2
    v0 = model_repo.get_version(seg.id, 0)
    assert v0.kind == "prior" and v0.parent_version is None
    # runs + reports + extractions
    runs = repo.list_runs_for_segment(seg.id)
    assert len(runs) == 2 and all(r.status is RunStatus.succeeded for r in runs)
    reports = [model_repo.get_run_report(r.id) for r in runs]
    assert sorted(r.result_version for r in reports) == [1, 2]
    assert all(r.segment_slug == slug for r in reports)
    assert {r.run_id for r in reports} == {r.id for r in runs}
    assert len(model_repo.list_extractions(seg.id)) == 2


def test_loader_is_idempotent_and_can_continue_to_a_later_version(test_db, mini_fixture, tmp_path):
    slug = _slug()
    data = tmp_path / "data"
    first = lfs.load_fixture(slug, up_to_version=1, fixture_dir=mini_fixture, data_dir=data)
    assert first.head_version == 1 and first.versions_loaded == [0, 1]
    assert repo.require_segment(slug).scene.about.model_version == 1
    second = lfs.load_fixture(slug, fixture_dir=mini_fixture, data_dir=data)
    assert second.versions_loaded == [2] and second.head_version == 2 and second.observations == 0
    third = lfs.load_fixture(slug, fixture_dir=mini_fixture, data_dir=data)
    assert third.versions_loaded == [] and third.head_version == 2 and third.runs == 0
    assert len(repo.list_runs_for_segment(repo.require_segment(slug).id)) == 2
    assert len(model_repo.list_versions(repo.require_segment(slug).id)) == 3


def test_loader_reports_missing_images_as_warnings_not_errors(test_db, mini_fixture, tmp_path):
    for img in (mini_fixture / "images").glob("obs-fx-3*"):
        img.unlink()
    rep = lfs.load_fixture(_slug(), fixture_dir=mini_fixture, data_dir=tmp_path / "data")
    assert any("obs-fx-3" in w for w in rep.warnings)


def test_loader_requires_a_scene_to_publish(test_db, mini_fixture, tmp_path):
    (mini_fixture / "scene_v1.json").unlink()
    with pytest.raises(lfs.FixtureMissingError, match="scene_v1.json"):
        lfs.load_fixture(_slug(), fixture_dir=mini_fixture, data_dir=tmp_path / "data")


def test_cli_returns_nonzero_with_a_message_when_fixture_is_absent(tmp_path, capsys):
    assert lfs.main(["--fixture-dir", str(tmp_path / "missing")]) == 2
    assert "model_v0.json" in capsys.readouterr().err


def test_cli_loads_the_mini_fixture(test_db, mini_fixture, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path / "data"))
    from hero.config import reset_settings

    reset_settings()
    slug = _slug()
    assert lfs.main(["--slug", slug, "--fixture-dir", str(mini_fixture)]) == 0
    out = capsys.readouterr().out
    assert slug in out and "head version 2" in out
    reset_settings()


def test_seed_fixture_segment_with_the_real_s1d_fixture(test_db):
    if not lfs.fixture_available():
        pytest.skip("S1d model fixture (tests/e2e/fixtures/model_fixture/) not generated yet")
    seg = lfs.seed_fixture_segment(_slug())
    assert seg.status is SegmentStatus.ready
    assert isinstance(seg.scene, SceneV2) and model_repo.head_version(seg.id) >= 1
