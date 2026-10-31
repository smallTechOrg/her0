"""Archive (or restore) a street: hidden from the gallery, the admin list and ``recompile --all``.

    uv run python -m hero.tools.archive_segment --slug bellandur-main-road-n --slug 100-feet-road-indiranagar-w
    uv run python -m hero.tools.archive_segment --slug bellandur-main-road-n --restore

Nothing is deleted: runs, observations and llm_calls stay, so the global Claude spend counter is unchanged, and files
under data/ are left alone. ``--restore`` sets the street back to ``ready`` (or ``draft`` when it has no scene).
Reads ``HERO_DATABASE_URL``. Exit code 1 when a slug does not exist.
"""

from __future__ import annotations

import argparse

from hero.db import repo
from hero.scene.schema import SegmentStatus


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug", action="append", required=True, help="street slug (repeatable)")
    ap.add_argument("--restore", action="store_true", help="un-archive instead")
    a = ap.parse_args(argv)
    failed = 0
    for slug in a.slug:
        seg = repo.get_segment(slug)
        if seg is None:
            print(f"{slug}: not found")
            failed += 1
            continue
        if a.restore:
            status = SegmentStatus.ready if seg.scene is not None else SegmentStatus.draft
        else:
            status = SegmentStatus.archived
        repo.update_segment(seg.id, repo.SegmentUpdate(status=status))
        print(f"{slug}: {seg.status.value} -> {status.value}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
