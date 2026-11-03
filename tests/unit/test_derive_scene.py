"""publish_bundle / derive_scene / model_repo.update_derived with a fake toolchain (no files needed)."""

from __future__ import annotations

import uuid

import pytest

from hero.config import reset_settings
from hero.db import model_repo, repo
from hero.fusion import publish as pub
from hero.model.types import ModelKind
from hero.tools.load_fixture_street import seed_fixture_segment


class FakeRec:
    sha8 = "0a1b2c3d"
    mesh_sha256 = "a" * 64
    glb_bytes = 1234
    triangles = 99
    primitives = 3

    def to_json(self):
        return {"sha8": self.sha8, "mesh_sha256": self.mesh_sha256, "glb_bytes": 1234,
                "stats": {"primitives": 3, "triangles": 99}, "files": {}}


@pytest.fixture()
def street(test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    seg = seed_fixture_segment(f"derive-{uuid.uuid4().hex[:8]}")
    yield seg
    reset_settings()


def _next_bundle(seg):
    head = model_repo.get_version(seg.id, model_repo.head_version(seg.id))
    obs = {o.id: o for o in model_repo.get_observations(seg.id, limit=1000)}
    cand = head.model.model_copy(update={"version": head.version + 1, "parent_version": head.version,
                                         "kind": ModelKind.fused, "run_id": None})
    bundle = pub.assemble_version(candidate=cand, base=head.model, observations=obs, assets={}, segment_name=seg.name,
                                  city=seg.city, models={"vision": "m"}, review_iterations=0, warnings=[])
    return head, bundle


def test_publish_bundle_exports_first_then_stores_the_derived_columns(street, tmp_path):
    calls = []

    def fake_export(**kw):
        calls.append(("export", kw["slug"], kw["version"], model_repo.head_version(street.id)))
        return FakeRec()

    tc = pub.Toolchain.default()
    tc.bake, tc.export = None, fake_export
    head, bundle = _next_bundle(street)
    rec = pub.publish_bundle(street.id, bundle, thumbnail_path=None, data_dir=tmp_path, toolchain=tc)

    assert calls == [("export", street.slug, head.version + 1, head.version)]  # files before the DB row exists
    assert rec.scene_schema == 3 and rec.compiled_by == "hero.model.compile/2" and rec.gaps["gap_report"] == 1
    assert rec.export["sha8"] == "0a1b2c3d"
    assert rec.scene["assets"]["glb_url"] == f"/api/segments/{street.slug}/model.glb?v=0a1b2c3d"
    seg = repo.get_segment(street.slug)
    assert seg.scene_schema == 3 and seg.scene.assets.sha8 == "0a1b2c3d"
    assert bundle.scene.assets.sha8 == "0a1b2c3d"  # the caller sees the exported scene
    assert rec.scene["generation"]["models"] == {"vision": "m"}


def test_a_failing_export_publishes_nothing(street, tmp_path):
    def boom(**kw):
        raise RuntimeError("disk full")

    tc = pub.Toolchain.default()
    tc.bake, tc.export = None, boom
    head, bundle = _next_bundle(street)
    with pytest.raises(RuntimeError):
        pub.publish_bundle(street.id, bundle, thumbnail_path=None, data_dir=tmp_path, toolchain=tc)
    assert model_repo.head_version(street.id) == head.version
    assert repo.get_segment(street.slug).scene.schema_version == 2


def test_missing_export_step_leaves_a_v3_scene_without_assets_and_a_warning(street, tmp_path):
    tc = pub.Toolchain.default()
    tc.bake, tc.export = None, None
    d = pub.derive_scene(model_repo.get_version(street.id, 0).model, observations={}, assets={},
                         fidelity=model_repo.get_version(street.id, 0).fidelity, segment_name="x", city="y",
                         data_dir=tmp_path, toolchain=tc)
    assert d.scene.assets is None and d.export is None
    assert any("not exported" in w for w in d.warnings) and any("procedural" in w for w in d.warnings)


def test_update_derived_touches_only_derived_columns(street):
    head = model_repo.head_version(street.id)
    before = model_repo.get_version(street.id, head)
    scene = {**before.scene, "generation": {**before.scene["generation"], "warnings": ["marker"]}}
    out = model_repo.update_derived(street.id, head, scene=scene, compiled_by="t/1", gaps={"g": 1},
                                    export={"sha8": "deadbeef"})
    assert out.scene == scene and out.compiled_by == "t/1" and out.gaps == {"g": 1}
    assert out.model == before.model and out.fidelity == before.fidelity and out.model_hash == before.model_hash
    assert model_repo.get_export(street.id)["sha8"] == "deadbeef"
    assert repo.get_segment(street.slug).scene_schema == 2  # segment pointer follows the head scene
    with pytest.raises(repo.InvalidInputError):
        model_repo.update_derived(street.id, 99, scene=scene, compiled_by=None, gaps=None, export=None)


def test_update_derived_of_an_old_version_does_not_move_the_segment_pointer(street):
    head = model_repo.head_version(street.id)
    seg_scene = repo.get_segment(street.slug).scene.model_dump(mode="json")
    old = model_repo.get_version(street.id, 0)
    model_repo.update_derived(street.id, 0, scene=old.scene, compiled_by="old/1", gaps=None, export=None)
    assert head > 0 and repo.get_segment(street.slug).scene.model_dump(mode="json") == seg_scene


def test_union_m_merges_overlaps():
    assert pub.union_m([(0, 10), (5, 12), (20, 25)]) == 17.0 and pub.union_m([]) == 0.0
