"""Router registration seam. app.py calls register_routers(app) once; slices extend the
router modules in place (public.py, admin.py, media.py) and never touch app.py."""

from __future__ import annotations

from fastapi import FastAPI

from hero.api import admin, media, public


def register_routers(app: FastAPI) -> None:
    app.include_router(public.router)
    app.include_router(admin.router)
    media.mount_media(app)
