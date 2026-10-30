"""Print the root-cause audit (footpath states, untextured strips, per-side frontage coverage, hole bins).

    uv run python -m hero.tools.audit_scene tests/fixtures/recorded/bellandur_scene_v2.json
    uv run python -m hero.tools.audit_scene tests/fixtures/plan/bellandur.plan.json --assert-no-holes
    HERO_DATABASE_URL=sqlite:///<copy>/hero.db uv run python -m hero.tools.audit_scene --slug bellandur-main-road-n-2

Input: a Scene v2 / Scene v3 / StreetPlan JSON file, or ``--slug`` (the stored head scene, read-only from the database
named by ``HERO_DATABASE_URL``, default ``data/hero.db``).  ``--json`` prints the full report; ``--out FILE`` writes it.
``--assert-no-holes`` exits 1 when any frontage or ground hole bin remains.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from hero.model.scene_audit import audit_scene, load_audit_input


def _db_path() -> Path:
    url = os.environ.get("HERO_DATABASE_URL", "sqlite:///data/hero.db")
    if not url.startswith("sqlite:///"):
        raise SystemExit(f"--slug needs a sqlite HERO_DATABASE_URL (got {url.split(':', 1)[0]})")
    p = Path(url[len("sqlite:///"):])
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / p
    return p


def scene_for_slug(slug: str) -> dict:
    con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        row = con.execute("select scene_json from segments where slug=?", (slug,)).fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        raise SystemExit(f"no stored scene for slug {slug!r}")
    return json.loads(row[0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", type=Path)
    ap.add_argument("--slug")
    ap.add_argument("--json", action="store_true", help="print the full JSON report")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--assert-no-holes", action="store_true")
    a = ap.parse_args(argv)
    if bool(a.path) == bool(a.slug):
        ap.error("give exactly one of PATH or --slug")
    scene = load_audit_input(a.path) if a.path else scene_for_slug(a.slug)
    try:
        rep = audit_scene(scene)
    except ValueError as e:
        print(f"audit_scene: {e}", file=sys.stderr)
        return 2
    if a.path:
        rep.source = {"file": a.path.name, "sha256": hashlib.sha256(a.path.read_bytes()).hexdigest()}
    doc = json.dumps(rep.to_dict(), indent=1, sort_keys=False)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(doc + "\n", encoding="utf-8")
    print(doc if a.json else rep.summary())
    if a.assert_no_holes:
        bad = {s: sorted(set(rep.hole_bins[s]) | set(rep.ground_hole_bins[s])) for s in rep.hole_bins}
        if any(bad.values()):
            print(f"HOLES: {bad}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
