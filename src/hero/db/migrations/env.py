"""Alembic environment: URL comes from hero.config (HERO_DATABASE_URL), never alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from hero.config import get_settings
from hero.db.base import Base
from hero.db.session import make_engine

# S1-data adds: ``import hero.db.models  # noqa: F401`` so autogenerate sees the tables.
try:  # keep the scaffold runnable before models.py exists
    import hero.db.models  # noqa: F401
except ImportError:  # pragma: no cover
    pass

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    settings = get_settings()
    return settings.resolve_database_url()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
