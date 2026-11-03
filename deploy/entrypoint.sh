#!/usr/bin/env sh
# Apply the schema (idempotent), then serve on $PORT (Cloud Run sets it).
set -e
cd /app
mkdir -p "${HERO_DATA_DIR:-/data}"
uv run --no-sync alembic upgrade head
exec uv run --no-sync hero-serve
