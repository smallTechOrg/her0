"""Vendor the pinned Three.js build + the two examples modules the glb loader needs into frontend/vendor/.

Files (all from the same pinned npm release, fetched from unpkg):
  three.module.js                                   -> frontend/vendor/three.module.js
  examples/jsm/loaders/GLTFLoader.js                -> frontend/vendor/three/examples/jsm/loaders/GLTFLoader.js
  examples/jsm/utils/BufferGeometryUtils.js         -> frontend/vendor/three/examples/jsm/utils/BufferGeometryUtils.js

``frontend/vendor/VENDOR.json`` records the version, url and sha256 of every file. Files already present are
skipped when their sha256 matches the recorded one (or, for a first run, recorded as is); a mismatch is an error
(the vendored copy was changed or corrupted) unless ``--force`` re-downloads. ``--verify`` only checks.

Run: uv run python -m hero.tools.vendor_three [--verify] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

import httpx

from hero.config import PROJECT_ROOT

THREE_VERSION = "0.170.0"
BASE = f"https://unpkg.com/three@{THREE_VERSION}"
VENDOR_DIR = PROJECT_ROOT / "frontend" / "vendor"
MANIFEST = VENDOR_DIR / "VENDOR.json"
# local path (relative to frontend/vendor) -> remote path (relative to the package root)
FILES = {
    "three.module.js": "build/three.module.js",
    "three/examples/jsm/loaders/GLTFLoader.js": "examples/jsm/loaders/GLTFLoader.js",
    "three/examples/jsm/utils/BufferGeometryUtils.js": "examples/jsm/utils/BufferGeometryUtils.js",
}
SENTINELS = {  # a cheap "is this really the right file" check on fresh downloads
    "three.module.js": b"REVISION",
    "three/examples/jsm/loaders/GLTFLoader.js": b"class GLTFLoader",
    "three/examples/jsm/utils/BufferGeometryUtils.js": b"mergeGeometries",
}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {}


def verify() -> list[str]:
    """Return a list of problems (empty = every vendored file matches VENDOR.json)."""
    problems: list[str] = []
    rec = _load_manifest().get("files", {})
    for local in FILES:
        p = VENDOR_DIR / local
        if not p.exists():
            problems.append(f"missing {local}")
        elif local not in rec:
            problems.append(f"{local} not recorded in VENDOR.json")
        elif sha256_of(p.read_bytes()) != rec[local]["sha256"]:
            problems.append(f"sha256 mismatch for {local}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="only verify the vendored files against VENDOR.json")
    ap.add_argument("--force", action="store_true", help="re-download everything")
    a = ap.parse_args(argv)
    if a.verify:
        problems = verify()
        for p in problems:
            print(f"vendor_three: {p}")
        print("vendor_three: OK" if not problems else "vendor_three: FAILED")
        return 1 if problems else 0

    old = _load_manifest().get("files", {})
    files: dict[str, dict] = {}
    for local, remote in FILES.items():
        target = VENDOR_DIR / local
        url = f"{BASE}/{remote}"
        if target.exists() and target.stat().st_size > 0 and not a.force:
            digest = sha256_of(target.read_bytes())
            if local in old and old[local]["sha256"] != digest:
                print(f"vendor_three: {local} differs from VENDOR.json (use --force to re-download)")
                return 1
            files[local] = {"url": url, "sha256": digest, "bytes": target.stat().st_size}
            print(f"vendor_three: present {local} ({target.stat().st_size} bytes) - skipped")
            continue
        resp = httpx.get(url, follow_redirects=True, timeout=60.0)
        if resp.status_code != 200 or SENTINELS[local] not in resp.content:
            print(f"vendor_three: FAILED {url} status={resp.status_code} bytes={len(resp.content)}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.content)
        files[local] = {"url": url, "sha256": sha256_of(resp.content), "bytes": len(resp.content)}
        print(f"vendor_three: downloaded {local} ({len(resp.content)} bytes)")
    MANIFEST.write_text(json.dumps({"three_version": THREE_VERSION, "files": files}, indent=2) + "\n")
    print(f"vendor_three: wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
