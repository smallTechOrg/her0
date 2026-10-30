"""Token usage -> USD from the price table (``HERO_LLM_PRICE_TABLE``). Unknown model refuses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hero.config import PROJECT_ROOT, Settings
from hero.llm.errors import UnknownModelError

PIXELS_PER_IMAGE_TOKEN = 750.0  # Anthropic's documented approximation: tokens ~= (w * h) / 750
CHARS_PER_TEXT_TOKEN = 4.0
COST_DECIMALS = 6


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice], source: Path | None = None):
        self._prices = dict(prices)
        self.source = source

    @classmethod
    def load(cls, path: Path) -> "PriceTable":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read the LLM price table at {path}: {exc}") from exc
        if not isinstance(raw, dict) or not raw:
            raise ValueError(f"LLM price table {path} must be a non-empty JSON object keyed by model slug.")
        prices: dict[str, ModelPrice] = {}
        for model, entry in raw.items():
            try:
                prices[model] = ModelPrice(
                    input_usd_per_mtok=float(entry["input_usd_per_mtok"]),
                    output_usd_per_mtok=float(entry["output_usd_per_mtok"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"LLM price table {path}: model {model!r} needs numeric input_usd_per_mtok and "
                    f"output_usd_per_mtok ({exc})."
                ) from exc
        return cls(prices, source=Path(path))

    @property
    def models(self) -> list[str]:
        return sorted(self._prices)

    def require(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            where = self.source.name if self.source else "the price table"
            raise UnknownModelError(
                f"Model {model!r} is not in {where} (known: {', '.join(self.models)}). "
                f"Add it to {self.source or 'the price table'} or change HERO_VISION_MODEL / HERO_REVIEW_MODEL."
            ) from None

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        price = self.require(model)
        usd = input_tokens * price.input_usd_per_mtok / 1e6 + output_tokens * price.output_usd_per_mtok / 1e6
        return round(usd, COST_DECIMALS)


def load_price_table(settings: Settings) -> PriceTable:
    path = settings.llm_price_table
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return PriceTable.load(path)


def estimate_image_tokens(width_px: int, height_px: int) -> int:
    return int(round(width_px * height_px / PIXELS_PER_IMAGE_TOKEN))


def estimate_text_tokens(text: str) -> int:
    return int(round(len(text) / CHARS_PER_TEXT_TOKEN))
