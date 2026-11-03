"""Validate ``street.glb`` files: ``uv run python -m hero.tools.validate_glb FILE... [--no-contract] [--no-budgets] [--khronos]``.

Prints one compact JSON object per file (JSON lines, so the gate can ``tee`` several files) and exits 1 when any file has
errors (or, with ``--khronos``, when the Khronos validator reports errors; an unavailable validator is reported, not fatal).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hero.export.glb_validate import khronos_validate, validate_glb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hero.tools.validate_glb", description=__doc__)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--no-contract", action="store_true", help="generic glTF checks only (skip the street contract)")
    ap.add_argument("--no-budgets", action="store_true", help="skip the primitive/triangle/byte budgets")
    ap.add_argument("--khronos", action="store_true", help="also run the Khronos validator through npx (needs network)")
    args = ap.parse_args(argv)
    rc = 0
    for f in args.files:
        if not f.is_file():
            print(json.dumps({"file": str(f), "ok": False, "errors": [f"not a file: {f}"], "warnings": [], "stats": {}}))
            rc = 1
            continue
        rep = validate_glb(f.read_bytes(), contract=not args.no_contract, budgets=not args.no_budgets)
        out = {"file": str(f), **rep.to_dict()}
        if args.khronos:
            k = khronos_validate(f)
            out["khronos"] = k.to_dict()
            if k.ran and k.errors:
                out["ok"] = False
        print(json.dumps(out, separators=(",", ":")))
        if not out["ok"]:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
