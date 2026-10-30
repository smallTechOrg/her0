"""Default model slugs (single place the P0 spike flips; spec/architecture.md -> Stack -> LLM).

P0 spike (2026-10-30): ``claude-sonnet-5-5`` passes ``auto_tool`` and ``json_text`` on the real
``StreetExtraction`` schema (rejects forced tool choice), so it is the default. ``claude-sonnet-4-5`` reaches EOL 2026-11-30.
"""

DEFAULT_VISION_MODEL = "claude-sonnet-5-5"
DEFAULT_REVIEW_MODEL = "claude-sonnet-5-5"
