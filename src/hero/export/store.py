"""Content-addressed export storage (spec/data.md -> *Export files*, spec/api.md -> *export endpoints*).

Layout: ``<data_dir>/exports/<slug>/street_<sha8>.{glb,manifest.json,xodr}`` plus a tiny pointer ``head.json`` used
as a fallback when the DB has no ``export_json`` for the head version yet. ``sha8`` is the first 8 hex of
``bundle_sha256 = sha256(mesh_sha256 + manifest_sha256 + xodr_sha256)`` (the three lowercase hex digests
concatenated), so a ``gameplay.json`` change that leaves the glb untouched still yields new file names.

Files are written atomically (tmp + ``os.replace``) and never deleted, so old ``?v=`` URLs keep resolving.
This module does IO only; it never touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SHA8_RE = re.compile(r"[0-9a-f]{8}")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
EXT = {"glb": ".glb", "manifest": ".manifest.json", "xodr": ".xodr"}
HEAD_POINTER = "head.json"


@dataclass
class ExportRecord:
    sha8: str
    mesh_sha256: str
    manifest_sha256: str
    xodr_sha256: str
    gameplay_sha256: str
    bundle_sha256: str
    glb_bytes: int
    triangles: int
    primitives: int
    files: dict[str, str]  # kind -> path relative to data_dir, e.g. "exports/<slug>/street_ab12cd34.glb"
    built_with: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The ``model_versions.export_json`` payload (spec/data.md)."""
        d = asdict(self)
        d["stats"] = {"primitives": self.primitives, "triangles": self.triangles}
        return d

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ExportRecord":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: data[k] for k in keys if k in data})


@dataclass(frozen=True)
class ExportFiles:
    slug: str
    sha8: str
    glb: Path
    manifest: Path
    xodr: Path

    def path(self, kind: str) -> Path:
        return {"glb": self.glb, "manifest": self.manifest, "xodr": self.xodr}[kind]

    def read(self, kind: str) -> bytes:
        return self.path(kind).read_bytes()

    @property
    def model_version(self) -> int:
        try:
            return int(json.loads(self.manifest.read_bytes())["build"]["model_version"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def etag_for(data: bytes) -> str:
    """Strong ETag: first 32 hex of the file's sha256, quoted."""
    return '"' + sha256_hex(data)[:32] + '"'


def bundle_sha256(mesh_sha256: str, manifest_sha256: str, xodr_sha256: str) -> str:
    return sha256_hex((mesh_sha256 + manifest_sha256 + xodr_sha256).encode("ascii"))


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug or ""))


def exports_dir(data_dir: Path, slug: str) -> Path:
    if not valid_slug(slug):
        raise ValueError(f"invalid slug {slug!r}")
    return Path(data_dir) / "exports" / slug


def file_name(sha8: str, kind: str) -> str:
    return f"street_{sha8}{EXT[kind]}"


def _atomic_write(path: Path, data: bytes) -> None:
    if path.exists() and path.stat().st_size == len(data) and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_export(data_dir: Path, slug: str, sha8: str, *, glb: bytes, manifest: bytes, xodr: bytes) -> dict[str, str]:
    """Write the three files; returns ``files`` (paths relative to ``data_dir``)."""
    if not SHA8_RE.fullmatch(sha8):
        raise ValueError(f"invalid sha8 {sha8!r}")
    base = exports_dir(data_dir, slug)
    rels: dict[str, str] = {}
    for kind, payload in (("glb", glb), ("manifest", manifest), ("xodr", xodr)):
        _atomic_write(base / file_name(sha8, kind), payload)
        rels[kind] = f"exports/{slug}/{file_name(sha8, kind)}"
    return rels


def write_head_pointer(data_dir: Path, slug: str, sha8: str, model_version: int) -> None:
    payload = json.dumps({"sha8": sha8, "model_version": model_version}, separators=(",", ":")).encode()
    _atomic_write(exports_dir(data_dir, slug) / HEAD_POINTER, payload)


def read_head_pointer(data_dir: Path, slug: str) -> str | None:
    try:
        sha8 = json.loads((exports_dir(data_dir, slug) / HEAD_POINTER).read_bytes()).get("sha8")
    except (OSError, ValueError, AttributeError):
        return None
    return sha8 if isinstance(sha8, str) and SHA8_RE.fullmatch(sha8) else None


def open_export(data_dir: Path, slug: str, sha8: str) -> ExportFiles | None:
    """The files of bundle ``sha8`` when all three exist, else None."""
    if not valid_slug(slug) or not SHA8_RE.fullmatch(sha8 or ""):
        return None
    base = exports_dir(data_dir, slug)
    files = ExportFiles(slug, sha8, base / file_name(sha8, "glb"), base / file_name(sha8, "manifest"),
                        base / file_name(sha8, "xodr"))
    return files if all(files.path(k).is_file() for k in EXT) else None


# ------------------------------------------------------------------ head resolution

def head_sha8_from_db(slug: str) -> str | None:
    """``export_json.sha8`` of the segment's head model version (None when unavailable)."""
    from hero.db import model_repo, repo

    try:
        seg = repo.get_segment(slug)
        head = model_repo.head_version(seg.id) if seg is not None else None
        rec = model_repo.get_version(seg.id, head) if head is not None else None
    except Exception as exc:  # noqa: BLE001 - a DB hiccup falls back to the head.json pointer
        from hero.logging import get_logger

        get_logger("hero.export").warning("export.head_lookup_failed", slug=slug, error=repr(exc))
        return None
    exp = getattr(rec, "export_json", None)
    sha8 = exp.get("sha8") if isinstance(exp, dict) else None
    return sha8 if isinstance(sha8, str) and SHA8_RE.fullmatch(sha8) else None


def load_export(slug: str, sha8: str | None, *, data_dir: Path) -> ExportFiles | None:
    """Files for ``sha8``, or the head bundle when ``sha8`` is None. None when it does not exist.

    Read-only (never builds): head lookup order is the DB ``export_json`` of the head version, then the ``head.json``
    pointer. Building happens only in the publish / recompile / admin Regenerate paths.
    """
    if not valid_slug(slug):
        return None
    if sha8 is not None:
        return open_export(data_dir, slug, sha8)
    for candidate in (head_sha8_from_db(slug), read_head_pointer(data_dir, slug)):
        if candidate:
            files = open_export(data_dir, slug, candidate)
            if files:
                return files
    return None
