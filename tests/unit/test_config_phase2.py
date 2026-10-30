"""Phase-2 settings: defaults, validation, comment stripping (spec/architecture.md env table)."""

from __future__ import annotations

import pytest

from hero.config import Settings, reset_settings
from hero.llm import defaults


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


def _clear(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(f"HERO_{n}", raising=False)


def test_phase2_defaults_when_no_overrides(monkeypatch):
    _clear(monkeypatch, "SV_SPACING_M", "SV_HEADINGS", "SAT_CROP_ZOOM", "SAT_CROP_SPACING_M", "MAX_IMAGES_PER_RUN",
           "PERCEPTION_BATCH_IMAGES", "STRUCTURED_MODE", "UPLOAD_MAX_MB", "RUN_BUDGET_USD", "MAX_REVIEW_ITERATIONS",
           "VISION_MODEL", "REVIEW_MODEL")
    s = Settings(_env_file=None, anthropic_api_key="k", anthropic_workspace_id="w", google_maps_api_key="g",
                 admin_password="p")
    assert s.sv_spacing_m == 10.0
    assert s.sv_headings_list == ["forward", "back", "left", "right"]
    assert s.sat_crop_zoom == 20 and s.sat_crop_spacing_m == 40.0
    assert s.max_images_per_run == 60 and s.perception_batch_images == 8
    assert s.structured_mode == "auto"
    assert s.upload_max_mb == 10.0 and s.upload_max_bytes == 10 * 1024 * 1024
    assert s.run_budget_usd == 1.00 and s.max_review_iterations == 1
    assert s.vision_model == defaults.DEFAULT_VISION_MODEL and s.review_model == defaults.DEFAULT_REVIEW_MODEL


def test_model_defaults_live_in_llm_defaults():
    assert defaults.DEFAULT_VISION_MODEL and defaults.DEFAULT_REVIEW_MODEL
    assert defaults.DEFAULT_VISION_MODEL.startswith("claude-")


def test_env_overrides_with_inline_comments(monkeypatch):
    monkeypatch.setenv("HERO_SV_SPACING_M", "12.5   # denser")
    monkeypatch.setenv("HERO_SV_HEADINGS", "forward, back # only travel axis")
    monkeypatch.setenv("HERO_STRUCTURED_MODE", "JSON_TEXT")
    monkeypatch.setenv("HERO_MAX_IMAGES_PER_RUN", "30")
    s = Settings()
    assert s.sv_spacing_m == 12.5
    assert s.sv_headings_list == ["forward", "back"]
    assert s.structured_mode == "json_text"
    assert s.max_images_per_run == 30


def test_headings_are_deduplicated_in_order(monkeypatch):
    monkeypatch.setenv("HERO_SV_HEADINGS", "left,forward,left")
    assert Settings().sv_headings_list == ["left", "forward"]


@pytest.mark.parametrize("name,value", [
    ("SV_HEADINGS", "forward,sideways"),
    ("SV_HEADINGS", " , "),
    ("STRUCTURED_MODE", "magic"),
    ("SV_SPACING_M", "2"),
    ("SV_SPACING_M", "80"),
    ("SAT_CROP_ZOOM", "17"),
    ("SAT_CROP_ZOOM", "22"),
    ("MAX_IMAGES_PER_RUN", "0"),
    ("UPLOAD_MAX_MB", "0"),
])
def test_invalid_phase2_values_fail_loudly(monkeypatch, name, value):
    monkeypatch.setenv(f"HERO_{name}", value)
    with pytest.raises(ValueError) as exc:
        Settings()
    assert name.lower() in str(exc.value).lower() or "HERO_" in str(exc.value)


def test_env_example_lists_every_phase2_variable():
    from hero.config import PROJECT_ROOT

    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("HERO_SV_SPACING_M", "HERO_SV_HEADINGS", "HERO_SAT_CROP_ZOOM", "HERO_SAT_CROP_SPACING_M",
                "HERO_MAX_IMAGES_PER_RUN", "HERO_PERCEPTION_BATCH_IMAGES", "HERO_STRUCTURED_MODE",
                "HERO_UPLOAD_MAX_MB", "HERO_RUN_BUDGET_USD=1.00", "HERO_MAX_REVIEW_ITERATIONS=1"):
        assert var in text, var


# ---------------------------------------------------------------- router auto-discovery (hero.api.__init__)


def test_router_discovery_orders_by_router_order_then_name_and_skips_excluded(tmp_path, monkeypatch):
    import sys

    import hero.api as api

    mk = "from fastapi import APIRouter\nrouter = APIRouter()\n"
    (tmp_path / "zeta_first.py").write_text("ROUTER_ORDER = 10\n" + mk)
    (tmp_path / "beta.py").write_text(mk)
    (tmp_path / "alpha.py").write_text(mk)
    (tmp_path / "late.py").write_text("ROUTER_ORDER = 90\n" + mk)
    (tmp_path / "auth.py").write_text(mk)       # excluded by name
    (tmp_path / "envelope.py").write_text(mk)   # excluded by name
    (tmp_path / "_private.py").write_text(mk)   # leading underscore
    (tmp_path / "norouter.py").write_text("x = 1\n")
    monkeypatch.setattr(api, "__path__", [str(tmp_path)])
    try:
        found = [name for name, _ in api.discover_routers()]
    finally:
        for name in list(sys.modules):
            if name.startswith("hero.api.") and name.split(".")[-1] in {
                    "zeta_first", "beta", "alpha", "late", "norouter", "_private"}:
                del sys.modules[name]
    assert found == ["zeta_first", "alpha", "beta", "late"]


def test_real_app_registers_discovered_routers(client):
    import hero.api as api

    names = [n for n, _ in api.discover_routers()]
    assert "admin" in names and "public" in names and "media" not in names
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/admin/status").status_code == 401   # admin router mounted, auth enforced
