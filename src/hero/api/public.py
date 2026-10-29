"""Public JSON API: /api/health now; /api/segments* added by Phase 1 slice S3-public-api.

# Phase 1 slices extend this file IN PLACE (add routes to ``router``; do not edit app.py).
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from hero import __version__
from hero.api.envelope import ok
from hero.db.session import get_session

router = APIRouter(prefix="/api", tags=["public"])


@router.get("/health")
def health():
    with get_session() as session:
        session.execute(text("SELECT 1"))
    return ok({"status": "ok", "version": __version__, "db": "ok"})


# Phase 1 slices extend this file: GET /segments, GET /segments/{slug}, /scene, POST /runs (S3-public-api)
