"""Import every Phase-1 segment (scene v1, no model versions) as legacy model v0.

    uv run python -m hero.tools.import_legacy            # prints "imported N legacy segment(s)"

Idempotent: a segment that already has a model version, or whose scene is already v2, is skipped. The Phase-1 scene
stays in ``segments.scene_json`` (it keeps playing); nothing else is touched - in particular ``game_runs`` is not.
Honours ``HERO_DATABASE_URL`` (the Phase-2 gate runs it against a copy of the dev DB).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from hero.db import repo
from hero.fusion.publish import import_legacy_segment


def import_all(*, now: datetime | None = None) -> list[str]:
    """Slugs of the segments imported by this call."""
    stamp = now or datetime.now(UTC)
    return [seg.slug for seg in repo.list_segments() if import_legacy_segment(seg, now=stamp)]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Import Phase-1 segments as legacy model v0.").parse_args(argv)
    imported = import_all()
    print(f"imported {len(imported)} legacy segment(s)" + (": " + ", ".join(imported) if imported else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
