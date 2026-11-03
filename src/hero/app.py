"""create_app(): logging, DB init, routers, static mounts, request-id middleware.

This file is owned by the scaffold. Feature slices extend the routers in ``hero.api``
(``register_routers`` seam) and never edit this module.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from hero.api import register_routers
from hero.api.envelope import register_exception_handlers
from hero.config import PROJECT_ROOT, get_settings
from hero.db.session import get_engine
from hero.logging import bind_request, configure_logging, get_logger, unbind_request

FRONTEND_DIR = PROJECT_ROOT / "frontend"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("hero.app")

    for sub in ("imagery", "osm", "crops", "previews"):
        (settings.data_dir_abs / sub).mkdir(parents=True, exist_ok=True)
    get_engine()  # fail early if the DB path is unusable

    app = FastAPI(title="#hero", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        bind_request(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-Id"] = request_id
        path = request.url.path
        if not path.startswith(("/api/", "/media/", "/vendor/")):
            # Dev/beta: always revalidate HTML/JS/CSS so a refresh picks up new game code.
            response.headers["Cache-Control"] = "no-cache"
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            latency_ms=latency_ms,
        )
        unbind_request()
        return response

    register_exception_handlers(app)
    register_routers(app)  # /api/* and /media/* (media mounts the data/ subfolders)
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    return app
