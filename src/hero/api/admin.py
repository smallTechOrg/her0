"""Admin JSON API: /api/admin/* (password header via ``require_admin``).

# Phase 1 slices extend this file IN PLACE (S2-pipeline adds status/segments/generate/runs).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hero.api.auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# Phase 1 slices extend this file: GET /status, GET/POST /segments, POST /segments/{slug}/generate, GET /runs*
