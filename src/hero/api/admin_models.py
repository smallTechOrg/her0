"""GET /api/admin/models: the configured per-run model options (spec/api.md -> Model choice).

``default`` is ``settings.vision_model``; ``claude-opus-5-5`` is listed with ``enabled: false`` and a
``disabled_reason`` unless its verification evidence says ``ok: true``. Unpriced models are not listed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hero.api.auth import require_admin
from hero.api.envelope import ok
from hero.config import get_settings
from hero.llm.models import list_options

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/models")
def list_models():
    settings = get_settings()
    return ok({"default": settings.vision_model, "options": [o.to_json() for o in list_options(settings)]})
