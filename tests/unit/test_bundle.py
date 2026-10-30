"""export.zip bundle + content-addressed store (S10)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from hero import export as export_pkg
from hero.export import store
from hero.export.bundle import ZIP_NAMES, build_bundle
from hero.export.pipeline import ExportTooLarge, build_and_store_exports
from tests.fixtures.export import fake_builders


def _build(tmp_path, monkeypatch, *, salt=b"", version=2, gameplay=None):
    fake_builders.install(monkeypatch, salt=salt)
    return build_and_store_exports(
        slug=fake_builders.SLUG, version=version, model=object(), scene=object(), plan=object(), observations={},
        data_dir=tmp_path, gameplay=gameplay)


def test_store_is_content_addressed(tmp_path, monkeypatch):
    rec = _build(tmp_path, monkeypatch)
    glb = fake_builders.fixture_bytes("street.glb")
    assert rec.mesh_sha256 == hashlib.sha256(glb).hexdigest()
    assert rec.glb_bytes == len(glb)
    assert rec.bundle_sha256 == hashlib.sha256((rec.mesh_sha256 + rec.manifest_sha256 + rec.xodr_sha256).encode()).hexdigest()
    assert rec.sha8 == rec.bundle_sha256[:8]
    base = tmp_path / "exports" / fake_builders.SLUG
    assert (base / f"street_{rec.sha8}.glb").read_bytes() == glb
    assert (base / f"street_{rec.sha8}.manifest.json").is_file()
    assert (base / f"street_{rec.sha8}.xodr").read_bytes() == fake_builders.fixture_bytes("street.xodr")
    assert rec.files["glb"] == f"exports/{fake_builders.SLUG}/street_{rec.sha8}.glb"
    assert (rec.triangles, rec.primitives) == (8420, 12)
    assert set(rec.built_with) == {"pygltflib", "pillow", "python"}
    assert rec.gameplay_sha256 == hashlib.sha256(export_pkg.gameplay_bytes()).hexdigest()
    # the manifest on disk carries the real glb hash that the pipeline computed
    assert json.loads((base / f"street_{rec.sha8}.manifest.json").read_bytes())["build"]["mesh_sha256"] == rec.mesh_sha256


def test_new_content_gets_new_name_and_old_files_stay(tmp_path, monkeypatch):
    a = _build(tmp_path, monkeypatch, salt=b"", version=1)
    b = _build(tmp_path, monkeypatch, salt=b"x", version=2)
    assert a.sha8 != b.sha8
    assert store.open_export(tmp_path, fake_builders.SLUG, a.sha8) and store.open_export(tmp_path, fake_builders.SLUG, b.sha8)
    assert store.read_head_pointer(tmp_path, fake_builders.SLUG) == b.sha8


def test_rebuild_is_idempotent(tmp_path, monkeypatch):
    a = _build(tmp_path, monkeypatch)
    before = sorted(p.name for p in (tmp_path / "exports" / fake_builders.SLUG).iterdir())
    b = _build(tmp_path, monkeypatch)
    assert a == b
    assert sorted(p.name for p in (tmp_path / "exports" / fake_builders.SLUG).iterdir()) == before


def test_too_large_glb_fails_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("hero.export.pipeline._max_glb_bytes", lambda: 50_000)  # fixture glb is ~105 kB
    with pytest.raises(ExportTooLarge):
        _build(tmp_path, monkeypatch)
    assert not (tmp_path / "exports").exists()


def test_bundle_contents_and_order(tmp_path, monkeypatch):
    rec = _build(tmp_path, monkeypatch)
    data = build_bundle(rec, tmp_path, model_version=2)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == list(ZIP_NAMES)
        assert zf.testzip() is None
        assert zf.read("street.glb") == fake_builders.fixture_bytes("street.glb")
        assert zf.read("street.xodr") == fake_builders.fixture_bytes("street.xodr")
        assert zf.read("street.manifest.json") == (tmp_path / rec.files["manifest"]).read_bytes()
        assert zf.read("gameplay.json") == export_pkg.gameplay_bytes()
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        text = zf.read("IMPORT.txt").decode()
    for word in ("glTFast", "Interchange", "Godot", "Unity", "Unreal", fake_builders.SLUG, rec.sha8, rec.mesh_sha256,
                 "hero-street-manifest/1", "Satellite imagery"):
        assert word in text


def test_bundle_is_byte_deterministic(tmp_path, monkeypatch):
    rec = _build(tmp_path, monkeypatch)
    one = build_bundle(rec, tmp_path, model_version=2)
    two = build_bundle(rec, tmp_path, model_version=2)
    assert one == two
    assert hashlib.sha256(one).hexdigest() == hashlib.sha256(two).hexdigest()
    assert build_bundle(rec, tmp_path, model_version=3) != one  # IMPORT.txt names the version


def test_bundle_with_custom_gameplay_reproduces_it_from_the_manifest(tmp_path, monkeypatch):
    rec = _build(tmp_path, monkeypatch, gameplay=export_pkg.load_gameplay())
    assert rec.gameplay_sha256 == "feed" * 16  # the manifest's own gameplay hash wins
    with zipfile.ZipFile(io.BytesIO(build_bundle(rec, tmp_path, model_version=2))) as zf:
        assert json.loads(zf.read("gameplay.json")) == json.loads(fake_builders.fixture_bytes("street.manifest.json"))["gameplay"]


def test_bundle_missing_file_is_an_error(tmp_path, monkeypatch):
    rec = _build(tmp_path, monkeypatch)
    (tmp_path / rec.files["xodr"]).unlink()
    with pytest.raises(FileNotFoundError):
        build_bundle(rec, tmp_path, model_version=2)


@pytest.mark.parametrize("bad", ["../x", "A", "a/b", "", "-x"])
def test_store_rejects_bad_slugs(tmp_path, bad):
    assert store.load_export(bad, None, data_dir=tmp_path) is None
    with pytest.raises(ValueError):
        store.exports_dir(tmp_path, bad)


@pytest.mark.parametrize("bad", ["../../etc", "abc", "ABCDEF12", "abcdef123", ""])
def test_store_rejects_bad_sha8(tmp_path, bad):
    assert store.open_export(tmp_path, "ok-slug", bad) is None
