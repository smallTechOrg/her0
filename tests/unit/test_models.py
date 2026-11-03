"""hero.llm.models: option list (models.json x prices.json x evidence) and per-run model resolution. No network."""

from __future__ import annotations

import json

import pytest

from hero.config import PROJECT_ROOT, Settings
from hero.llm.models import ModelChoiceError, list_options, resolve_model
from hero.llm.pricing import PriceTable

PRICES = {
    "claude-sonnet-5-5": {"input_usd_per_mtok": 2.0, "output_usd_per_mtok": 10.0},
    "claude-opus-5-5": {"input_usd_per_mtok": 4.0, "output_usd_per_mtok": 20.0,
                        "source_url": "https://example.test/pricing", "verified_on": "2026-10-30"},
}


def _settings(**kw) -> Settings:
    return Settings(anthropic_api_key="sk-test", anthropic_workspace_id="w", google_maps_api_key="g",
                    admin_password="p", **kw)


def _setup(tmp_path, evidence: dict | None, *, prices=PRICES, extra_models=()):
    price_file = tmp_path / "prices.json"
    price_file.write_text(json.dumps(prices))
    ev_path = tmp_path / "evidence.json"
    if evidence is not None:
        ev_path.write_text(json.dumps(evidence))
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"models": [
        {"id": "claude-sonnet-5-5", "label": "Sonnet", "evidence": None},
        {"id": "claude-opus-5-5", "label": "Opus", "evidence": str(ev_path)}, *extra_models]}))
    return PriceTable.load(price_file), models


def test_shipped_files_list_default_selected_and_opus_matching_the_evidence_file():
    options = {o.id: o for o in list_options(_settings())}
    assert options["claude-sonnet-5-5"].enabled and options["claude-sonnet-5-5"].disabled_reason is None
    evidence = json.loads((PROJECT_ROOT / "docs/evidence/phase2_1/model_check_claude-opus-5-5.json").read_text())
    opus = options["claude-opus-5-5"]
    assert opus.enabled is evidence["ok"]
    assert opus.price_source_url and opus.price_verified_on
    assert (opus.input_usd_per_mtok, opus.output_usd_per_mtok) == (4.0, 20.0)
    assert opus.enabled or opus.disabled_reason


def test_opus_enabled_only_when_the_evidence_says_ok(tmp_path):
    prices, models = _setup(tmp_path, {"ok": True, "checked_at": "2026-10-30T16:00:00+00:00"})
    opus = {o.id: o for o in list_options(_settings(), prices=prices, models_path=models)}["claude-opus-5-5"]
    assert opus.enabled and opus.verified_on == "2026-10-30T16:00:00+00:00"


def test_failed_evidence_disables_with_the_recorded_error(tmp_path):
    prices, models = _setup(tmp_path, {"ok": False, "error": "NotFoundError: model not found"})
    opus = {o.id: o for o in list_options(_settings(), prices=prices, models_path=models)}["claude-opus-5-5"]
    assert not opus.enabled and "model not found" in opus.disabled_reason


def test_missing_evidence_disables_with_a_reason(tmp_path):
    prices, models = _setup(tmp_path, None)
    opus = {o.id: o for o in list_options(_settings(), prices=prices, models_path=models)}["claude-opus-5-5"]
    assert not opus.enabled and "not verified" in opus.disabled_reason


def test_unpriced_model_is_not_listed(tmp_path):
    prices, models = _setup(tmp_path, {"ok": True}, extra_models=[{"id": "claude-mystery-9", "label": "?"}])
    ids = [o.id for o in list_options(_settings(), prices=prices, models_path=models)]
    assert "claude-mystery-9" not in ids and ids == ["claude-sonnet-5-5", "claude-opus-5-5"]


def test_default_follows_settings_and_is_always_offered(tmp_path):
    prices = dict(PRICES, **{"claude-haiku-4-5": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0}})
    table, models = _setup(tmp_path, {"ok": True}, prices=prices)
    settings = _settings(vision_model="claude-haiku-4-5")
    assert "claude-haiku-4-5" in [o.id for o in list_options(settings, prices=table, models_path=models)]
    assert resolve_model(None, settings).model == "claude-haiku-4-5"


def test_resolve_absent_is_default_and_valid_choice_is_request(tmp_path):
    prices, models = _setup(tmp_path, {"ok": True})
    options = list_options(_settings(), prices=prices, models_path=models)
    default = resolve_model(None, _settings(), options=options)
    assert (default.model, default.source) == ("claude-sonnet-5-5", "default")
    chosen = resolve_model("claude-opus-5-5", _settings(), options=options)
    assert (chosen.model, chosen.source) == ("claude-opus-5-5", "request")


def test_resolve_unknown_model_raises(tmp_path):
    prices, models = _setup(tmp_path, {"ok": True})
    options = list_options(_settings(), prices=prices, models_path=models)
    with pytest.raises(ModelChoiceError, match="Unknown model 'gpt-9'"):
        resolve_model("gpt-9", _settings(), options=options)


def test_resolve_disabled_model_raises_with_the_reason(tmp_path):
    prices, models = _setup(tmp_path, {"ok": False, "error": "HTTP 404 no such model"})
    options = list_options(_settings(), prices=prices, models_path=models)
    with pytest.raises(ModelChoiceError) as exc:
        resolve_model("claude-opus-5-5", _settings(), options=options)
    assert "disabled" in exc.value.message and "no such model" in exc.value.message
    assert "no such model" in exc.value.reason
