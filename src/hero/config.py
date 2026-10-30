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

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hero.llm.defaults import DEFAULT_REVIEW_MODEL, DEFAULT_VISION_MODEL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

STRUCTURED_MODES = ("auto", "forced_tool", "auto_tool", "json_text")
HEADING_NAMES = ("forward", "back", "left", "right")

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

    host: str = "0.0.0.0"
    port: int = 8711

    database_url: str = "sqlite:///data/hero.db"
    test_database_url: str = "sqlite:///data/hero_test.db"
    data_dir: Path = Path("data")

    vision_model: str = DEFAULT_VISION_MODEL
    review_model: str = DEFAULT_REVIEW_MODEL
    max_review_iterations: int = 1  # Phase 2 default (Phase 1 was 2)
    llm_price_table: Path = Path("src/hero/llm/prices.json")

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    satellite_zoom: int = 19
    streetview_samples: int = 3  # Phase-1 legacy pipeline only

    # ---- Phase 2 (imagery plan, perception, uploads)
    sv_spacing_m: float = Field(default=10.0, ge=4.0, le=40.0)
    sv_headings: str = "forward,back,left,right"  # comma list; see ``sv_headings_list``
    sat_crop_zoom: int = Field(default=20, ge=18, le=21)
    sat_crop_spacing_m: float = Field(default=40.0, gt=0)
    max_images_per_run: int = Field(default=60, ge=1, le=200)
    perception_batch_images: int = Field(default=8, ge=1, le=20)
    structured_mode: str = "auto"
    upload_max_mb: float = Field(default=10.0, gt=0)

    # ---- Phase 2.1 (texture bake, uploads, export)
    ground_strip_mpp: float = Field(default=0.1, gt=0)  # bake resolution (widened so the longest side <= 1024 px)
    bake_jpeg_quality: int = Field(default=82, ge=40, le=95)
    upload_max_distance_m: float = Field(default=60.0, gt=0)  # max camera distance from the reference line
    export_max_glb_mb: float = Field(default=2.5, gt=0)  # a bigger mesh fails the export (`export_too_large`)
    gltf_validator: str = "auto"  # auto|off: only the integration test and `validate_glb --khronos` read it

    log_level: str = "INFO"

    @field_validator("*", mode="before")
    @classmethod
    def _strip_comments(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name in REQUIRED_SECRETS:
            return value
        return strip_inline_comment(value)

    @field_validator("structured_mode")
    @classmethod
    def _structured_mode_known(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in STRUCTURED_MODES:
            raise ValueError(f"HERO_STRUCTURED_MODE must be one of {', '.join(STRUCTURED_MODES)} (got {value!r})")
        return value

    @field_validator("gltf_validator")
    @classmethod
    def _gltf_validator_known(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("auto", "off"):
            raise ValueError(f"HERO_GLTF_VALIDATOR must be auto or off (got {value!r})")
        return value

    @field_validator("sv_headings")
    @classmethod
    def _headings_known(cls, value: str) -> str:
        names = [h.strip().lower() for h in value.split(",") if h.strip()]
        bad = [h for h in names if h not in HEADING_NAMES]
        if not names or bad:
            raise ValueError(
                f"HERO_SV_HEADINGS must be a comma list of {'|'.join(HEADING_NAMES)} (got {value!r})")
        return ",".join(dict.fromkeys(names))

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
    def sv_headings_list(self) -> list[str]:
        """``sv_headings`` as a de-duplicated list in the order given."""
        return self.sv_headings.split(",")

    @property
    def upload_max_bytes(self) -> int:
        return int(self.upload_max_mb * 1024 * 1024)

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
