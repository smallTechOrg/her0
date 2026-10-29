"""Download the pinned Three.js ES module to frontend/vendor/three.module.js (skip if present).

Run: uv run python -m hero.tools.vendor_three
"""

from __future__ import annotations

import sys

import httpx

from hero.config import PROJECT_ROOT

THREE_VERSION = "0.170.0"
THREE_URL = f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js"
TARGET = PROJECT_ROOT / "frontend" / "vendor" / "three.module.js"


def main() -> int:
    if TARGET.exists() and TARGET.stat().st_size > 0:
        print(f"vendor_three: present {TARGET} ({TARGET.stat().st_size} bytes) - skipped")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(THREE_URL, follow_redirects=True, timeout=60.0)
    if resp.status_code != 200 or b"REVISION" not in resp.content:
        print(f"vendor_three: FAILED status={resp.status_code} bytes={len(resp.content)}")
        return 1
    TARGET.write_bytes(resp.content)
    print(f"vendor_three: downloaded three@{THREE_VERSION} -> {TARGET} ({len(resp.content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
