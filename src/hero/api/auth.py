"""Admin password dependency: header X-Hero-Admin-Password, constant-time compare."""

from __future__ import annotations

import secrets

from fastapi import Header

from hero.api.envelope import ApiError
from hero.config import get_settings

ADMIN_HEADER = "X-Hero-Admin-Password"


def require_admin(x_hero_admin_password: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_password.get_secret_value()
    supplied = x_hero_admin_password or ""
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise ApiError("unauthorized", "Admin password missing or wrong (X-Hero-Admin-Password).")
