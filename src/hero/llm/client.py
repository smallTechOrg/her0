"""LLMClient protocol: one batched vision call -> a validated Pydantic object + priced usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class LLMImage:
    data: bytes
    media_type: str  # image/jpeg | image/png
    caption: str
    width_px: int
    height_px: int


@dataclass(frozen=True)
class LLMUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    calls: int  # 1, or 2 when a schema re-ask was needed


class LLMClient(Protocol):
    def vision_json(self, *, run_id: str, step: str, images: Sequence[LLMImage], prompt: str,
                    schema: type[T], model: str, prompt_name: str, system: str | None = None,
                    max_tokens: int = 4096) -> tuple[T, LLMUsage]:
        """Send ``images`` + ``prompt`` in ONE request; the model must answer via a forced tool whose
        input schema is ``schema``; returns the validated object and the priced usage."""
        ...
