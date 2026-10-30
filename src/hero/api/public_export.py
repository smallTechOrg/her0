"""Public export endpoints (spec/api.md -> *Phase 2.1 r2 - export endpoints*).

``GET /api/segments/{slug}/model.glb | manifest.json | street.xodr | panoramas.json`` and ``GET /api/gameplay.json``. These are files
(not enveloped): content type, strong ``ETag`` (first 32 hex of the file's sha256), ``If-None-Match`` -> 304,
``?v=<sha8>`` -> immutable one-year cache, otherwise ``max-age=60, must-revalidate``. CORS-safe: any origin may read
them (``Access-Control-Allow-Origin: *`` with ETag exposed; ``OPTIONS`` preflight answered). 404 envelope when the
segment is unknown/not ready or has no export yet. A GET never builds anything (no write-on-GET): a missing
export is built by the admin Regenerate endpoint or the ``recompile`` CLI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response

from hero import export as export_pkg
from hero.api.envelope import ApiError
from hero.config import get_settings
from hero.db import repo
from hero.export import store
from hero.logging import get_logger

router = APIRouter(prefix="/api", tags=["export"])

NO_EXPORT_MESSAGE = "no export yet: run Regenerate or recompile"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "public, max-age=60, must-revalidate"
GAMEPLAY_CACHE = "public, max-age=300"
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "ETag, Content-Length, Content-Disposition, Cache-Control",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Timing-Allow-Origin": "*",
}
FILES = {
    "model.glb": ("glb", "model/gltf-binary", ".glb"),
    "manifest.json": ("manifest", "application/json", ".manifest.json"),
    "street.xodr": ("xodr", "application/xml", ".xodr"),
}


def data_dir() -> Path:
    return get_settings().data_dir_abs


def require_export(slug: str, sha8: str | None) -> store.ExportFiles:
    """The ExportFiles for a ready segment (head, or ``sha8``); ApiError 404 otherwise."""
    if sha8 is not None and not store.SHA8_RE.fullmatch(sha8):
        raise ApiError("not_found", "Unknown asset version.")
    seg = repo.get_segment(slug) if store.valid_slug(slug) else None
    if seg is None or seg.status != "ready":
        raise ApiError("not_found", f"No street {slug!r}.")
    files = store.load_export(slug, sha8, data_dir=data_dir())
    if files is None:
        raise ApiError("not_found", "Unknown asset version." if sha8 else NO_EXPORT_MESSAGE)
    return files


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = etag.strip('"')
    for token in header.split(","):
        token = token.strip()
        if token.startswith("W/"):
            token = token[2:]
        if token.strip('"') == wanted:
            return True
    return False


def file_response(request: Request, payload: bytes, *, content_type: str, cache_control: str,
                  disposition: str | None = None, log_fields: dict | None = None) -> Response:
    etag = store.etag_for(payload)
    headers = {"ETag": etag, "Cache-Control": cache_control, **CORS_HEADERS}
    if disposition:
        headers["Content-Disposition"] = disposition
    not_modified = etag_matches(request.headers.get("if-none-match"), etag)
    if log_fields is not None:
        get_logger("hero.export").info("export.served", **log_fields, status=304 if not_modified else 200)
    if not_modified:
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(payload))
    body = b"" if request.method == "HEAD" else payload
    resp = Response(content=body, media_type=content_type, headers=headers)
    resp.headers["Content-Length"] = str(len(payload))
    return resp


def preflight(request: Request) -> Response:
    requested = request.headers.get("access-control-request-headers", "If-None-Match, Range")
    return Response(status_code=204, headers={
        **CORS_HEADERS, "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": requested, "Access-Control-Max-Age": "86400"})


def _make_file_route(name: str):
    kind, content_type, suffix = FILES[name]

    def handler(slug: str, request: Request, v: str | None = None) -> Response:
        files = require_export(slug, v)
        return file_response(
            request, files.read(kind), content_type=content_type, cache_control=IMMUTABLE if v else REVALIDATE,
            disposition=f'inline; filename="{slug}-v{files.model_version}{suffix}"',
            log_fields={"slug": slug, "file": name, "sha8": files.sha8})

    handler.__name__ = "get_" + name.replace(".", "_")
    router.add_api_route(f"/segments/{{slug}}/{name}", handler, methods=["GET", "HEAD"], include_in_schema=False)
    router.add_api_route(f"/segments/{{slug}}/{name}", preflight, methods=["OPTIONS"], include_in_schema=False)


@router.api_route("/gameplay.json", methods=["GET", "HEAD"], include_in_schema=False)
def get_gameplay(request: Request) -> Response:
    return file_response(request, export_pkg.gameplay_bytes(), content_type="application/json",
                         cache_control=GAMEPLAY_CACHE, log_fields={"slug": None, "file": "gameplay.json", "sha8": None})




NO_PANORAMAS_MESSAGE = "no panoramas yet: run Regenerate or recompile after fetching Street View imagery"


@router.api_route("/segments/{slug}/panoramas.json", methods=["GET", "HEAD"], include_in_schema=False)
def get_panoramas(slug: str, request: Request) -> Response:
    """The stitched Street View panoramas of a ready street (``hero-panoramas/1``; built by the export step)."""
    from hero.imagery.panorama import MANIFEST_NAME, panoramas_dir

    seg = repo.get_segment(slug) if store.valid_slug(slug) else None
    if seg is None or seg.status != "ready":
        raise ApiError("not_found", f"No street {slug!r}.")
    path = panoramas_dir(data_dir(), slug) / MANIFEST_NAME
    if not path.is_file():
        raise ApiError("not_found", NO_PANORAMAS_MESSAGE)
    return file_response(request, path.read_bytes(), content_type="application/json", cache_control=REVALIDATE,
                         log_fields={"slug": slug, "file": "panoramas.json", "sha8": None})


for _name in FILES:
    _make_file_route(_name)
router.add_api_route("/segments/{slug}/panoramas.json", preflight, methods=["OPTIONS"], include_in_schema=False)
router.add_api_route("/gameplay.json", preflight, methods=["OPTIONS"], include_in_schema=False)
