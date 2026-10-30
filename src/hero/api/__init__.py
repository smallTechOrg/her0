"""Router registration seam. ``app.py`` calls ``register_routers(app)`` once; slices add router modules
(or extend existing ones) and never touch ``app.py``.

Auto-discovery (Phase 2): every module in ``hero.api`` except ``auth``, ``envelope`` and ``media`` that exposes a
``router`` attribute is included, ordered by its optional ``ROUTER_ORDER`` (default 50) and then by module name.
Static routes that must win over path parameters (e.g. ``/api/segments/nearest`` before
``/api/segments/{slug}``) set a lower ``ROUTER_ORDER``. A module that fails to import is a hard error (a broken
slice must not be silently dropped). ``media`` is always mounted last via ``media.mount_media``.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter, FastAPI

from hero.api import media

EXCLUDED_MODULES = frozenset({"auth", "envelope", "media"})
DEFAULT_ROUTER_ORDER = 50


def discover_routers() -> list[tuple[str, APIRouter]]:
    """``[(module_name, router)]`` sorted by ``(ROUTER_ORDER, module_name)``."""
    found: list[tuple[int, str, APIRouter]] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name in EXCLUDED_MODULES or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append((int(getattr(module, "ROUTER_ORDER", DEFAULT_ROUTER_ORDER)), info.name, router))
    found.sort(key=lambda item: (item[0], item[1]))
    return [(name, router) for _, name, router in found]


def register_routers(app: FastAPI) -> None:
    for _name, router in discover_routers():
        app.include_router(router)
    media.mount_media(app)
