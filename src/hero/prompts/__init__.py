"""Prompt files (markdown) loaded at runtime; ``{{placeholders}}`` rendered by simple substitution."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@lru_cache(maxsize=16)
def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt {name!r} not found at {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: object) -> str:
    text = _read(name)
    missing: list[str] = []

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return str(values[key])

    rendered = _PLACEHOLDER.sub(sub, text)
    if missing:
        raise KeyError(f"Prompt {name!r} is missing values for: {', '.join(sorted(set(missing)))}")
    return rendered
