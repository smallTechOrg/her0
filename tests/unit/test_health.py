"""Scaffold smoke: envelope, health, request id, static shell, media mounts, settings."""

from __future__ import annotations

import os

import pytest

from hero.config import ConfigError, Settings, reset_settings, strip_inline_comment


def test_health_returns_ok_envelope_with_db_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["db"] == "ok"
    assert body["data"]["version"]


def test_every_response_echoes_request_id(client):
    resp = client.get("/api/health", headers={"X-Request-Id": "abc-123"})
    assert resp.headers["X-Request-Id"] == "abc-123"
    assert client.get("/api/health").headers["X-Request-Id"]


def test_unknown_api_path_uses_failure_envelope(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"
    assert "message" in body["error"]


def test_admin_router_rejects_missing_password_on_unknown_route(client):
    # No admin routes yet; a 404 (not a 401) is acceptable for an unknown path, but a route
    # under /api/admin must never leak without the dependency - checked via explicit 401 path
    # in S2's tests. Here we only assert the envelope shape.
    resp = client.get("/api/admin/nope")
    assert resp.json()["ok"] is False


def test_gallery_shell_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'data-hero-page="gallery"' in page.text
    css = client.get("/css/base.css")
    assert css.status_code == 200 and len(css.content) > 0
    js = client.get("/js/api.js")
    assert js.status_code == 200 and b"export const api" in js.content


def test_media_mount_rejects_traversal_and_missing(client):
    assert client.get("/media/imagery/missing.png").status_code == 404
    assert client.get("/media/imagery/../hero.db").status_code in (404, 400)
    assert client.get("/media/other/x.png").status_code == 404


def test_strip_inline_comment():
    assert strip_inline_comment("claude-sonnet-4-5   # model") == "claude-sonnet-4-5"
    assert strip_inline_comment("  50 ") == "50"
    assert strip_inline_comment(7) == 7


def test_settings_fail_loudly_naming_missing_required_var(monkeypatch):
    monkeypatch.setenv("HERO_ADMIN_PASSWORD", "")
    with pytest.raises(ConfigError) as exc:
        Settings()
    assert "HERO_ADMIN_PASSWORD" in str(exc.value)
    reset_settings()


def test_settings_strip_comments_from_env_values(monkeypatch):
    monkeypatch.setenv("HERO_VISION_MODEL", "claude-haiku-4-5  # cheap")
    monkeypatch.setenv("HERO_PORT", "9000 # alt")
    s = Settings()
    assert s.vision_model == "claude-haiku-4-5"
    assert s.port == 9000
    reset_settings()


def test_live_server_fixture_serves_health_and_gallery_on_8712(live_server):
    import httpx

    health = httpx.get(live_server + "/api/health", timeout=5.0).json()
    assert health["ok"] is True and health["data"]["db"] == "ok"
    assert 'data-hero-page="gallery"' in httpx.get(live_server + "/", timeout=5.0).text
