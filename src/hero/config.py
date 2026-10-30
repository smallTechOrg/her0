"""Settings loaded from the environment / .env (prefix HERO_).

Inline ``# comment`` suffixes (a ``#`` preceded by whitespace) are stripped from
non-secret values so a copied .env.example works unchanged. Secrets are taken verbatim: a
password or key may legitimately contain ``#``. Required secrets fail loudly, naming the
variable, when empty.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

REQUIRED_SECRETS = (
    "anthropic_api_key",
    "anthropic_workspace_id",
    "google_maps_api_key",
    "admin_password",
)


_INLINE_COMMENT = re.compile(r"\s#.*$")


def strip_inline_comment(value: Any) -> Any:
    """Return ``value`` without a trailing ``<whitespace># comment`` (and surrounding whitespace).

    A ``#`` that is not preceded by whitespace is part of the value (``pa#ss`` stays intact).
    """
    if not isinstance(value, str):
        return value
    return _INLINE_COMMENT.sub("", value).strip()


class ConfigError(RuntimeError):
    """Raised at startup when a required setting is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HERO_",
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_workspace_id: SecretStr = SecretStr("")
    google_maps_api_key: SecretStr = SecretStr("")
    admin_password: SecretStr = SecretStr("")

    claude_budget_usd: float = 50.0
    run_budget_usd: float = 2.00

    host: str = "0.0.0.0"
    port: int = 8711

    database_url: str = "sqlite:///data/hero.db"
    test_database_url: str = "sqlite:///data/hero_test.db"
    data_dir: Path = Path("data")

    vision_model: str = "claude-sonnet-4-5"
    review_model: str = "claude-sonnet-4-5"
    max_review_iterations: int = 2
    llm_price_table: Path = Path("src/hero/llm/prices.json")

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    satellite_zoom: int = 19
    streetview_samples: int = 3

    log_level: str = "INFO"

    @field_validator("*", mode="before")
    @classmethod
    def _strip_comments(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name in REQUIRED_SECRETS:
            return value
        return strip_inline_comment(value)

    @model_validator(mode="after")
    def _require_secrets(self) -> "Settings":
        missing = [
            f"HERO_{name.upper()}"
            for name in REQUIRED_SECRETS
            if not getattr(self, name).get_secret_value().strip()
        ]
        if missing:
            raise ConfigError(
                "Missing required setting(s): "
                + ", ".join(missing)
                + f". Fill them in {ENV_FILE} (see .env.example)."
            )
        return self

    @property
    def data_dir_abs(self) -> Path:
        return self.data_dir if self.data_dir.is_absolute() else PROJECT_ROOT / self.data_dir

    def resolve_database_url(self, url: str | None = None) -> str:
        """Make a relative ``sqlite:///path`` absolute against the project root."""
        url = url or self.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith(prefix + "/"):
            return prefix + str(PROJECT_ROOT / url[len(prefix):])
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop the cached Settings (tests change env vars between runs)."""
    get_settings.cache_clear()
