"""/media/* - read-only serving of data/{imagery,crops,previews,uploads,textures} (path traversal rejected
by StaticFiles; anything outside those folders is a 404). `uploads` holds content-addressed user photos
(data/uploads/<sha256>.jpg|png); `textures` the baked, content-addressed ground/frontage JPEGs
(data/textures/<slug>/<name>_v<N>_<sha8>.jpg), so the one-day cache is safe.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from hero.config import get_settings

MEDIA_KINDS = ("imagery", "crops", "previews", "uploads", "textures")
CACHE_CONTROL = "public, max-age=86400"


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = CACHE_CONTROL
        return response


def mount_media(app: FastAPI) -> None:
    root = get_settings().data_dir_abs
    for kind in MEDIA_KINDS:
        folder = root / kind
        folder.mkdir(parents=True, exist_ok=True)
        app.mount(f"/media/{kind}", CachedStaticFiles(directory=str(folder)), name=f"media-{kind}")
