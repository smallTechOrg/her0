"""Content-addressed disk cache: ``<sha256(kind + canonical params)>.<suffix>`` + ``.params.json`` sidecar.

The canonical params never include an API key, so the key is stable across key rotation and
the sidecar is safe to serve or inspect. Writes are atomic (temp file + rename) so a failed
fetch never leaves a partial file that would later count as a hit.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    key: str
    hit: bool


class ImageryCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(kind: str, params: dict[str, Any]) -> str:
        canonical = json.dumps({"kind": kind, "params": params}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def path_for(self, kind: str, params: dict[str, Any], suffix: str) -> Path:
        return self.root / f"{self.key(kind, params)}.{suffix.lstrip('.')}"

    def get_or_fetch(self, kind: str, params: dict[str, Any], fetch: Callable[[], bytes], *, suffix: str) -> CacheEntry:
        key = self.key(kind, params)
        path = self.root / f"{key}.{suffix.lstrip('.')}"
        if path.exists() and path.stat().st_size > 0:
            return CacheEntry(path=path, key=key, hit=True)
        data = fetch()
        if not data:
            raise ValueError(f"{kind}: fetch returned no bytes; nothing cached")
        self._atomic_write(path, data)
        self._atomic_write(self.sidecar_for(path),
                           json.dumps({"kind": kind, "params": params}, sort_keys=True, indent=1).encode("utf-8"))
        return CacheEntry(path=path, key=key, hit=False)

    @staticmethod
    def sidecar_for(path: Path) -> Path:
        """``<key>.params.json`` next to the cached body (never collides with a ``.json`` body)."""
        key = path.name.split(".", 1)[0]
        return path.with_name(f"{key}.params.json")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + f".tmp{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
