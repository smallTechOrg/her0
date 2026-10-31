"""Admin export download: ``GET /api/admin/segments/{slug}/export.zip`` (spec/api.md r2 export endpoints).

Deterministic zip of ``street.glb``, ``street.manifest.json``, ``street.xodr``, ``gameplay.json``, ``IMPORT.txt``.
Same admin auth as the other admin routers (``X-Hero-Admin-Password``).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response

from hero.api.auth import require_admin
from hero.api.envelope import ApiError
from hero.api.public_export import NO_EXPORT_MESSAGE, data_dir, etag_matches
from hero.db import repo
from hero.export import store
from hero.export.bundle import build_bundle
from hero.logging import get_logger

router = APIRouter(prefix="/api/admin", tags=["admin-export"], dependencies=[Depends(require_admin)])


@router.api_route("/segments/{slug}/export.zip", methods=["GET", "HEAD"], include_in_schema=False)
def export_zip(slug: str, request: Request, v: str | None = None) -> Response:
    if v is not None and not store.SHA8_RE.fullmatch(v):
        raise ApiError("not_found", "Unknown asset version.")
    seg = repo.get_segment(slug) if store.valid_slug(slug) else None
    if seg is None:
        raise ApiError("not_found", f"No street {slug!r}.")
    files = store.load_export(slug, v, data_dir=data_dir())
    if files is None:
        raise ApiError("not_found", "Unknown asset version." if v else NO_EXPORT_MESSAGE)
    manifest = json.loads(files.read("manifest"))
    rec = _record_from_files(files, manifest)
    version = files.model_version
    payload = build_bundle(rec, data_dir(), model_version=version)
    etag = store.etag_for(payload)
    headers = {
        "ETag": etag, "Cache-Control": "private, no-cache",
        "Content-Disposition": f'attachment; filename="{slug}-v{version}-hero-street.zip"',
    }
    get_logger("hero.export").info("export.served", slug=slug, file="export.zip", sha8=files.sha8,
                                   status=304 if etag_matches(request.headers.get("if-none-match"), etag) else 200)
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    resp = Response(content=b"" if request.method == "HEAD" else payload, media_type="application/zip", headers=headers)
    resp.headers["Content-Length"] = str(len(payload))
    return resp


def _record_from_files(files: store.ExportFiles, manifest: dict) -> store.ExportRecord:
    """Rebuild the ExportRecord of stored files (the bundle only needs paths + hashes)."""
    from hero.export.pipeline import built_with

    glb, man, xodr = (files.read(k) for k in ("glb", "manifest", "xodr"))
    mesh = store.sha256_hex(glb)
    stats = manifest.get("mesh_stats", {})
    return store.ExportRecord(
        sha8=files.sha8, mesh_sha256=mesh, manifest_sha256=store.sha256_hex(man), xodr_sha256=store.sha256_hex(xodr),
        gameplay_sha256=manifest.get("gameplay_sha256", ""),
        bundle_sha256=store.bundle_sha256(mesh, store.sha256_hex(man), store.sha256_hex(xodr)),
        glb_bytes=len(glb), triangles=int(stats.get("triangles", 0)), primitives=int(stats.get("primitives", 0)),
        files={k: f"exports/{files.slug}/{files.path(k).name}" for k in ("glb", "manifest", "xodr")},
        built_with=built_with())
