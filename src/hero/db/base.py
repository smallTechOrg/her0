"""Declarative base shared by all models and by Alembic's target_metadata."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
