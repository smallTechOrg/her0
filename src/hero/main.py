"""ASGI entry: ``app = create_app()``; ``serve()`` is the ``hero-serve`` console script."""

from __future__ import annotations

import uvicorn

from hero.app import create_app
from hero.config import get_settings

app = create_app()


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "hero.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,  # the request-id middleware emits the http.request line
    )


if __name__ == "__main__":
    serve()
