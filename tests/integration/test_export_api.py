"""Public + admin export endpoints (S10): content types, ETag/304, immutable ?v=, CORS, 404s, admin zip."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from hero import export as export_pkg
from hero.config import get_settings, reset_settings
from hero.db import repo
from hero.export import store
from hero.export.pipeline import build_and_store_exports
from hero.scene.schema import FootpathSide, LatLon, SegmentStatus
from tests.fixtures.export import fake_builders

START = LatLon(lat=12.97400, lon=77.64050)
END = LatLon(lat=12.97467, lon=77.64058)


@pytest.fixture()
def env(test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    from hero.app import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, tmp_path
    reset_settings()


def _segment(slug: str, ready: bool = True):
    seg = repo.create_segment(repo.SegmentCreate(name=f"Export {slug}", city="Bengaluru", start=START, end=END,
                                                 footpath_side=FootpathSide.left, slug=slug))
    if ready:
        repo.update_segment(seg.id, repo.SegmentUpdate(status=SegmentStatus.ready))
    return seg


def _export(monkeypatch, data_dir, slug, *, salt=b"", version=2):
    fake_builders.install(monkeypatch, salt=salt)
    return build_and_store_exports(slug=slug, version=version, model=object(), scene=object(), plan=object(),
                                   observations={}, data_dir=data_dir)


@pytest.fixture()
def exported(env, monkeypatch, request):
    client, data_dir = env
    slug = "api-" + hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    _segment(slug)
    rec = _export(monkeypatch, data_dir, slug)
    return client, data_dir, slug, rec


def _admin():
    return {"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()}


# ------------------------------------------------------------------ public files


def test_glb_headers_and_body(exported):
    client, _, slug, rec = exported
    r = client.get(f"/api/segments/{slug}/model.glb")
    assert r.status_code == 200
    assert r.content == fake_builders.fixture_bytes("street.glb")
    assert r.content[:4] == b"glTF"
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.headers["etag"] == '"' + rec.mesh_sha256[:32] + '"'
    assert r.headers["cache-control"] == "public, max-age=60, must-revalidate"
    assert r.headers["content-length"] == str(len(r.content))
    assert r.headers["content-disposition"] == f'inline; filename="{slug}-v2.glb"'
    assert r.headers["access-control-allow-origin"] == "*"
    assert "etag" in r.headers["access-control-expose-headers"].lower()


def test_manifest_is_compact_unenveloped_json(exported):
    client, _, slug, _ = exported
    r = client.get(f"/api/segments/{slug}/manifest.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["schema"] == "hero-street-manifest/1"  # not wrapped in {"ok": ...}
    assert b", " not in r.content[:200] and b'": ' not in r.content[:200]
    assert r.headers["etag"]


def test_xodr_content_type(exported):
    client, _, slug, _ = exported
    r = client.get(f"/api/segments/{slug}/street.xodr")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert b'revMinor="7"' in r.content


def test_gameplay_json_needs_no_segment(env):
    client, _ = env
    r = client.get("/api/gameplay.json")
    assert r.status_code == 200 and r.content == export_pkg.gameplay_bytes()
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "public, max-age=300"
    assert r.json()["schema"] == "hero-gameplay/1"
    assert client.get("/api/gameplay.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


@pytest.mark.parametrize("name", ["model.glb", "manifest.json", "street.xodr"])
def test_if_none_match_gives_304_without_body(exported, name):
    client, _, slug, _ = exported
    url = f"/api/segments/{slug}/{name}"
    etag = client.get(url).headers["etag"]
    for header in (etag, "W/" + etag, f'"nope", {etag}', "*"):
        r = client.get(url, headers={"If-None-Match": header})
        assert r.status_code == 304, header
        assert r.content == b"" and r.headers["etag"] == etag
        assert "cache-control" in r.headers
    assert client.get(url, headers={"If-None-Match": '"stale"'}).status_code == 200


def test_versioned_url_is_immutable_and_survives_new_versions(exported, monkeypatch):
    client, data_dir, slug, old = exported
    new = _export(monkeypatch, data_dir, slug, salt=b"v3", version=3)
    assert new.sha8 != old.sha8
    head = client.get(f"/api/segments/{slug}/model.glb")
    assert head.content.endswith(b"v3") and "immutable" not in head.headers["cache-control"]
    pinned = client.get(f"/api/segments/{slug}/model.glb", params={"v": old.sha8})
    assert pinned.status_code == 200 and not pinned.content.endswith(b"v3")
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get(f"/api/segments/{slug}/model.glb", params={"v": new.sha8}).content == head.content


def test_db_export_json_wins_over_the_pointer(exported, monkeypatch):
    client, data_dir, slug, old = exported
    _export(monkeypatch, data_dir, slug, salt=b"v3", version=3)  # pointer now -> v3
    monkeypatch.setattr(store, "head_sha8_from_db", lambda s: old.sha8)
    assert not client.get(f"/api/segments/{slug}/model.glb").content.endswith(b"v3")


def test_head_request_has_headers_no_body(exported):
    client, _, slug, rec = exported
    r = client.head(f"/api/segments/{slug}/model.glb")
    assert r.status_code == 200 and r.content == b""
    assert r.headers["etag"] == '"' + rec.mesh_sha256[:32] + '"'


def test_cors_preflight(exported):
    client, _, slug, _ = exported
    r = client.options(f"/api/segments/{slug}/model.glb", headers={
        "Origin": "https://example.org", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "if-none-match"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "if-none-match" in r.headers["access-control-allow-headers"].lower()
    assert "GET" in r.headers["access-control-allow-methods"]


@pytest.mark.parametrize("v", ["deadbeef", "xyz", "../../x", "ABCDEF12"])
def test_unknown_or_malformed_version_is_404(exported, v):
    client, _, slug, _ = exported
    r = client.get(f"/api/segments/{slug}/model.glb", params={"v": v})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_404_unknown_draft_and_unexported_segments(env, monkeypatch):
    client, _ = env
    assert client.get("/api/segments/no-such-street/model.glb").status_code == 404
    _segment("api-draft-street", ready=False)
    assert client.get("/api/segments/api-draft-street/manifest.json").status_code == 404
    _segment("api-no-export-street")
    r = client.get("/api/segments/api-no-export-street/street.xodr")
    assert r.status_code == 404 and r.json()["error"]["message"] == "no export yet: run Regenerate or recompile"


def test_other_segment_routes_are_not_shadowed(exported):
    client, _, slug, _ = exported
    assert client.get(f"/api/segments/{slug}/scene").status_code != 405
    assert client.get(f"/api/segments/{slug}/whatever.glb").status_code == 404


def test_public_get_never_builds_a_missing_export(env, monkeypatch):
    client, data_dir = env
    _segment("api-nowrite-street")

    def boom(*a, **k):
        raise AssertionError("a public GET must not recompile (write-on-GET)")

    monkeypatch.setattr("hero.fusion.recompile.recompile_segment", boom)
    for name in ("model.glb", "manifest.json", "street.xodr"):
        r = client.get(f"/api/segments/api-nowrite-street/{name}")
        assert r.status_code == 404 and r.json()["error"]["message"] == "no export yet: run Regenerate or recompile"
    assert not (data_dir / "exports").exists(), "nothing was written"
    assert client.get("/api/segments/api-nowrite-street/model.glb", params={"v": "abc12345"}).status_code == 404


def test_sha8_and_slug_patterns_are_full_matches():
    assert store.SHA8_RE.fullmatch("abcdef01") and not store.SHA8_RE.fullmatch("abcdef01\n")
    assert not store.valid_slug("good-slug\n")


# ------------------------------------------------------------------ admin zip


def test_admin_zip_requires_the_password(exported):
    client, _, slug, _ = exported
    url = f"/api/admin/segments/{slug}/export.zip"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Hero-Admin-Password": "wrong"}).status_code == 401


def test_admin_zip_contents_headers_and_determinism(exported):
    client, _, slug, rec = exported
    url = f"/api/admin/segments/{slug}/export.zip"
    r = client.get(url, headers=_admin())
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == f'attachment; filename="{slug}-v2-hero-street.zip"'
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["street.glb", "street.manifest.json", "street.xodr", "gameplay.json", "IMPORT.txt"]
        assert zf.read("street.glb") == client.get(f"/api/segments/{slug}/model.glb").content
        assert zf.read("street.manifest.json") == client.get(f"/api/segments/{slug}/manifest.json").content
        assert json.loads(zf.read("gameplay.json"))["schema"] == "hero-gameplay/1"
        assert rec.sha8 in zf.read("IMPORT.txt").decode()
    again = client.get(url, headers=_admin())
    assert again.content == r.content
    assert client.get(url, headers={**_admin(), "If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.get(url, headers=_admin(), params={"v": "deadbeef"}).status_code == 404


def test_admin_zip_404_for_unknown_segment(env):
    client, _ = env
    r = client.get("/api/admin/segments/nope-nope/export.zip", headers=_admin())
    assert r.status_code == 404
