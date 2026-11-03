"""Per-run model choice: the configured options (``models.json`` x ``prices.json`` x verification evidence).

* The default is ``settings.vision_model`` (never hard-coded here) and is always offered.
* An option without a price row is not listed (the pipeline never runs with an unpriced model).
* An option with an ``evidence`` file is ``enabled`` only when that file says ``ok: true``; otherwise it stays listed
  with a ``disabled_reason`` (the recorded error, or "verification evidence missing").
* ``resolve_model`` turns a request's optional ``model`` into the model for BOTH the vision and review passes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hero.config import PROJECT_ROOT, Settings, get_settings
from hero.llm.pricing import PriceTable, load_price_table

MODELS_FILE = Path(__file__).with_name("models.json")


class ModelChoiceError(ValueError):
    """The requested model is unknown, unpriced or disabled (HTTP 422 ``validation_error``)."""

    def __init__(self, message: str, *, model: str | None = None, reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.model = model
        self.reason = reason


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    enabled: bool
    disabled_reason: str | None
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    price_source_url: str | None
    price_verified_on: str | None
    verified_on: str | None
    evidence: str | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedModel:
    model: str
    source: str  # "request" | "default"


def _read_evidence(rel: str) -> tuple[bool, str | None, str | None]:
    """(ok, reason when not ok, verified_on)."""
    path = Path(rel)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "not verified yet: the verification evidence file is missing", None
    except (OSError, ValueError):
        return False, "verification evidence is unreadable", None
    if not isinstance(data, dict):
        return False, "verification evidence is malformed", None
    verified = data.get("checked_at")
    if data.get("ok") is True:
        return True, None, verified
    err = str(data.get("error") or "verification call failed")
    return False, f"verification failed: {err}"[:300], verified


def _configured(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw["models"]
        assert isinstance(entries, list)
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        raise ValueError(f"Cannot read the model list at {path}: {exc}") from exc
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def list_options(settings: Settings | None = None, *, prices: PriceTable | None = None,
                 models_path: Path | None = None) -> list[ModelOption]:
    settings = settings or get_settings()
    prices = prices or load_price_table(settings)
    entries = _configured(models_path or MODELS_FILE)
    if not any(e["id"] == settings.vision_model for e in entries):
        entries = [{"id": settings.vision_model, "label": f"{settings.vision_model} (default)", "evidence": None}, *entries]
    options: list[ModelOption] = []
    for entry in entries:
        model_id = str(entry["id"])
        if model_id not in prices.models:
            continue  # unpriced: not listed
        price = prices.require(model_id)
        evidence = entry.get("evidence")
        enabled, reason, verified = True, None, None
        if evidence:
            enabled, reason, verified = _read_evidence(str(evidence))
        options.append(ModelOption(
            id=model_id, label=str(entry.get("label") or model_id), enabled=enabled, disabled_reason=reason,
            input_usd_per_mtok=price.input_usd_per_mtok, output_usd_per_mtok=price.output_usd_per_mtok,
            price_source_url=price.source_url, price_verified_on=price.verified_on, verified_on=verified,
            evidence=str(evidence) if evidence else None))
    return options


def resolve_model(requested: str | None, settings: Settings | None = None, *,
                  options: list[ModelOption] | None = None) -> ResolvedModel:
    """``requested`` (or None = the default) -> the model for this run. Raises ``ModelChoiceError`` (422)."""
    settings = settings or get_settings()
    if requested is None or requested == "":
        return ResolvedModel(settings.vision_model, "default")
    if not isinstance(requested, str):
        raise ModelChoiceError("model must be a string model id.", model=str(requested))
    options = options if options is not None else list_options(settings)
    by_id = {o.id: o for o in options}
    option = by_id.get(requested)
    if option is None:
        raise ModelChoiceError(f"Unknown model {requested!r} (available: {', '.join(sorted(by_id)) or 'none'}).",
                               model=requested)
    if not option.enabled:
        raise ModelChoiceError(f"Model {requested!r} is disabled: {option.disabled_reason}", model=requested,
                               reason=option.disabled_reason)
    return ResolvedModel(option.id, "request")
