"""Default model slugs (single place the P0 spike flips; spec/architecture.md -> Stack -> LLM).

``claude-sonnet-4-5`` is validated (EOL 2026-11-30). The orchestrator's P0 step
(``uv run python -m hero.tools.check_structured``) may set both constants to ``claude-sonnet-5-5``.
"""

DEFAULT_VISION_MODEL = "claude-sonnet-4-5"
DEFAULT_REVIEW_MODEL = "claude-sonnet-4-5"
