# #hero

A mobile-browser endless runner whose course is a real 50-100 m stretch of street,
reconstructed from satellite imagery, Street View and OpenStreetMap, with a walkability
score card after each run. Spec lives in `spec/`.

## Setup

All commands run from the repo root: `cd /Users/sai/Workspace/her0`. Every command is
prefixed with `uv run` (project-local `.venv`, Python 3.12.5 pinned in `.python-version`).

```bash
cd /Users/sai/Workspace/her0
cp .env.example .env            # then fill in the keys and HERO_ADMIN_PASSWORD
uv sync                         # verify: uv run python -c "import hero; print(hero.__version__)"
uv run python -m hero.tools.vendor_three   # fetches frontend/vendor/three.module.js (r170); verify: prints "downloaded" or "present"
uv run python -m hero.tools.check_keys     # verify: prints "result: PASS" (Street View non-OK is a warning)
uv run alembic upgrade head && uv run alembic current   # verify: prints the head revision
```

Environment variables: see `.env.example` (all `HERO_*`, documented inline). Required:
`HERO_ANTHROPIC_API_KEY`, `HERO_ANTHROPIC_WORKSPACE_ID`, `HERO_GOOGLE_MAPS_API_KEY`,
`HERO_ADMIN_PASSWORD`. Startup fails with a message naming any empty required var.

## Run

```bash
cd /Users/sai/Workspace/her0
uv run hero-serve               # http://0.0.0.0:8711/  -> open http://<mac-lan-ip>:8711/ from a phone
curl -s http://127.0.0.1:8711/api/health   # verify: {"ok":true,"data":{"status":"ok",...}}
```

Admin area: `http://<mac-lan-ip>:8711/admin/` (password = `HERO_ADMIN_PASSWORD`).

## Test

Tests use the same engine as production (SQLite) on a dedicated file from
`HERO_TEST_DATABASE_URL` (default `data/hero_test.db`), migrated and dropped by
`tests/conftest.py`. Integration and e2e tests call the real Anthropic and Google APIs.

```bash
cd /Users/sai/Workspace/her0
uv run pytest tests/unit -q
uv run playwright install chromium          # once, for tests/e2e
uv run pytest tests/unit tests/integration tests/e2e -q
```

## Layout

See `spec/architecture.md` (`## Layout`). Backend `src/hero/`, zero-build frontend
`frontend/` served at `/`, migrations `src/hero/db/migrations/`, cache + DB in `data/`.
