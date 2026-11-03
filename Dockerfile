# #hero on Cloud Run: one container serves the API, the admin and the game; files (imagery, panoramas, exports)
# live on a GCS bucket mounted at /data, the database on Cloud SQL (Postgres). See docs/deploy-cloud-run.md.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_SYSTEM_CERTS=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV HERO_DATA_DIR=/data HERO_HOST=0.0.0.0 PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
