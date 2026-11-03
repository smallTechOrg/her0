"""Re-compile stored streets from their cached model + observations (zero Google, zero Anthropic spend).

    uv run python -m hero.tools.recompile --all
    uv run python -m hero.tools.recompile --slug bellandur-main-road-n-2 [--dry-run]

Per street one line: ``scene 2->3, glb <sha8>, triangles N, interpolated M m, dynamic excluded K, cost 0.0``
(``unchanged`` instead of ``scene a->b`` when a rerun reproduces the stored scene and export byte for byte).
Phase-1 legacy streets (no canonical model) print ``skipped (legacy, no canonical model)`` and are only recompiled
with ``--slug X --include-legacy``.
Reads ``HERO_DATABASE_URL`` / ``HERO_DATA_DIR``. Exit code 1 when any street failed.
"""

from __future__ import annotations

import argparse
import sys

import structlog

from hero.db import repo
from hero.fusion.recompile import RecompileError, is_legacy, recompile_segment, recompile_slugs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="every street that has a canonical model (legacy streets are skipped)")
    group.add_argument("--slug", help="one street")
    ap.add_argument("--include-legacy", action="store_true", help="with --slug: also recompile a Phase-1 legacy street")
    ap.add_argument("--dry-run", action="store_true", help="compile and compare, write nothing")
    a = ap.parse_args(argv)
    # stdout carries only the per-street summary lines; the JSON log lines go to the process's real stderr
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.__stderr__))
    slugs = recompile_slugs(include_legacy=True) if a.all else [a.slug]
    failed = 0
    for slug in slugs:
        seg = repo.get_segment(slug)
        if seg is not None and not a.include_legacy and is_legacy(seg):
            print(f"{slug}: skipped (legacy, no canonical model)")
            continue
        try:
            result = recompile_segment(slug, dry_run=a.dry_run)
        except RecompileError as exc:
            print(f"{slug}: ERROR {exc.message}", file=sys.stderr)
            failed += 1
            continue
        print(result.summary_line())
        for w in result.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
