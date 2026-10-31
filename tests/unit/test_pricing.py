"""hero.llm.pricing: tokens -> USD from the shipped price table; unknown model refuses."""

from __future__ import annotations

import json

import pytest

from hero.config import PROJECT_ROOT
from hero.llm.errors import UnknownModelError
from hero.llm.pricing import PriceTable, estimate_image_tokens, load_price_table

SHIPPED_TABLE = PROJECT_ROOT / "src" / "hero" / "llm" / "prices.json"


def test_shipped_table_lists_sonnet_and_haiku_with_spec_prices():
    table = PriceTable.load(SHIPPED_TABLE)

    sonnet = table.require("claude-sonnet-4-5")
    haiku = table.require("claude-haiku-4-5")

    assert (sonnet.input_usd_per_mtok, sonnet.output_usd_per_mtok) == (3.0, 15.0)
    assert (haiku.input_usd_per_mtok, haiku.output_usd_per_mtok) == (1.0, 5.0)


def test_cost_is_tokens_times_price_per_million_rounded_to_six_decimals():
    table = PriceTable.load(SHIPPED_TABLE)

    cost = table.cost_usd("claude-sonnet-4-5", input_tokens=10_000, output_tokens=2_000)

    assert cost == pytest.approx(0.03 + 0.03)
    assert cost == round(cost, 6)


def test_zero_tokens_cost_zero_and_negative_tokens_are_rejected():
    table = PriceTable.load(SHIPPED_TABLE)

    assert table.cost_usd("claude-sonnet-4-5", 0, 0) == 0.0
    with pytest.raises(ValueError):
        table.cost_usd("claude-sonnet-4-5", -1, 0)


def test_unknown_model_raises_naming_the_model_and_the_table():
    table = PriceTable.load(SHIPPED_TABLE)

    with pytest.raises(UnknownModelError) as exc:
        table.cost_usd("claude-made-up-9", 1, 1)

    assert "claude-made-up-9" in str(exc.value)
    assert "prices.json" in str(exc.value)


def test_malformed_table_file_fails_loudly(tmp_path):
    bad = tmp_path / "prices.json"
    bad.write_text(json.dumps({"claude-sonnet-4-5": {"input_usd_per_mtok": 3.0}}))

    with pytest.raises(ValueError):
        PriceTable.load(bad)


def test_load_price_table_resolves_relative_path_against_project_root(monkeypatch):
    from hero.config import Settings

    settings = Settings(llm_price_table="src/hero/llm/prices.json")

    table = load_price_table(settings)

    assert table.require("claude-sonnet-4-5").input_usd_per_mtok == 3.0


def test_image_token_estimate_follows_pixels_over_750():
    assert estimate_image_tokens(1024, 1024) == pytest.approx(1024 * 1024 / 750, rel=0.01)
    assert estimate_image_tokens(0, 0) == 0
