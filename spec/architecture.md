# Architecture

## System overview

```
 phone / desktop browser (LAN)                      Mac (single process)
 ┌───────────────────────────┐   HTTP (single origin)  ┌──────────────────────────────────────────┐
 │ gallery  → play (Three.js)│ ───────────────────────▶│ FastAPI app  :8711  bind 0.0.0.0          │
 │ admin    → run view       │ ◀─────────────────────  │  /            static frontend             │
 └───────────────────────────┘                         │  /api/*       public + admin JSON API     │
                                                       │  /media/*     cached imagery, crops,      │
                                                       │               previews (read-only)        │
                                                       │  pipeline runner (one worker thread)      │
                                                       │   ├─ imagery: Google Static Maps,         │
                                                       │   │   Street View Static, Overpass (OSM)  │
                                                       │   ├─ llm: Anthropic (vision + review)     │
                                                       │   └─ scene assembly + top-down preview    │
                                                       │  SQLite (data/hero.db) via SQLAlchemy     │
                                                       │  disk cache data/imagery, data/osm        │
                                                       └──────────────────────────────────────────┘
```

Components:

- **Frontend (zero-build static)** — gallery, game (Three.js), admin + pipeline view. Served by
  the backend at `/`. Talks only to `/api/*` and `/media/*` on the same origin.
- **Public API** — list segments, fetch scene, post game runs. No auth.
- **Admin API** — password header; create segment, start generation, poll runs, status/budget.
- **Pipeline** (`hero.pipeline`) — the AI-native core; a fixed step graph with a bounded review
  loop (`spec/agent.md`). Runs in a single background thread; progress persisted to the DB
  after every step so the admin UI can poll.
- **Imagery** (`hero.imagery`) — Google + Overpass fetchers behind a content-addressed disk
  cache; every Google request counted and logged whether hit or miss.
- **LLM layer** (`hero.llm`) — one internal client; Anthropic adapter with the workspace header,
  retry/backoff, per-call token→USD pricing, budget guard.
- **Scene** (`hero.scene`) — the Scene JSON schema (typed), Web-Mercator/local-frame geometry,
  walkability scoring. Shared by pipeline, API and tests.
- **Persistence** — SQLite file + migrations. Entities in `spec/data.md`.

## Data flow

Generation: admin `POST /api/admin/segments/{slug}/generate` → run row `queued` → worker:
geometry → satellite tile → Street View verify/fetch → vision pass A (layout) → vision pass B
(obstacles) → assemble scene → review loop (≤2) → finalize → `segments.scene_json` replaced,
run `succeeded`. Each step appends to `pipeline_runs.steps_json`; each LLM call inserts
`llm_calls`; each Google request inserts `google_requests`.

Play: `GET /api/segments` → gallery → `GET /api/segments/{slug}/scene` → Three.js builds
the corridor from the scene (ground tile texture, extruded building boxes with facade crops,
billboard objects) → loop until crash → `POST /api/segments/{slug}/runs` → response carries
best-score status + walkability → results overlay draws hazards on the tile using
`objects[].tile_px`.

## Stack

Binding from intake: **Python FastAPI** backend, **Three.js** in the browser, Python 3.12.5
via asdf, `uv`, Node 22 available, no deployment, bind `0.0.0.0`, non-default port.

| Concern | Choice | Rationale |
|---|---|---|
| Runtime / deps | Python 3.12.5 (`.python-version`), `uv` project with `.venv`; every command `uv run …` | binding |
| Web framework | FastAPI 0.115+ / Starlette ≥ 0.40, `uvicorn[standard]`; entry `hero-serve` console script = `uvicorn hero.main:app --host 0.0.0.0 --port 8711` | binding; port 8711 avoids 8000 (`tech-stack.md`); env-overridable `HERO_PORT`/`HERO_HOST` |
| LLM | Anthropic Python SDK ≥ 0.40, model `claude-sonnet-4-5` (validated; EOL 2026-11-30; `claude-sonnet-5-5` answers plain calls but rejects forced `tool_choice` (HTTP 400), so it needs an auto-tool-choice code path before it can be the default) via `HERO_VISION_MODEL`, `HERO_REVIEW_MODEL`; `default_headers={"anthropic-workspace-id": …}` on every request; structured output via forced tool-use with the Pydantic-derived JSON schema. Note: `claude-sonnet-4-5` emits an SDK deprecation warning (EOL 2026-11-30) — re-validate the model slug and `prices.json` with `check_keys` before then; both models are configurable via `HERO_VISION_MODEL` / `HERO_REVIEW_MODEL` | validated key/slug; org-level key requires the header; tool-use gives schema-valid JSON without prose parsing |
| Imagery | `httpx` (sync) for Google Static Maps, Street View Static + metadata, Overpass; `Pillow` for annotation, cropping, top-down preview rendering | no browser needed in the pipeline; previews are 2D drawings over the tile |
| Database | **SQLite** file `data/hero.db`, SQLAlchemy 2.x (sync, `pysqlite`), Alembic migrations, WAL mode, `timeout=30`, `check_same_thread=False` | > **Assumed:** single process, one admin, tens of segments, no deployment — SQLite is the production engine and is guaranteed locally-available (Postgres is not verified on this Mac). Tests run on the same engine (`data/hero_test.db`). The ORM + migrations keep the path to Postgres open for the public beta. |
| Logging | `structlog` JSON lines to stdout | structured per-request and per-step fields required in Phase 1 |
| Config | `pydantic-settings`, prefix `HERO_`, `extra="ignore"`, secrets as `SecretStr` | fail loudly at startup; `.env` is a commons |
| Frontend | zero-build static HTML + ES modules; Three.js **r170** vendored to `frontend/vendor/three.module.js` by `uv run python -m hero.tools.vendor_three` (downloads `https://unpkg.com/three@0.170.0/build/three.module.js`); no bundler, no npm | > **Assumed:** vendored (not CDN) so the phone-on-LAN path and the headless gate never depend on third-party uptime; single origin keeps the gate to "page + assets return 200". |
| E2E | **Python Playwright** (`playwright` dev dep, chromium) in `tests/e2e`, server launched by a pytest fixture on port 8712 | > **Assumed:** one toolchain (`uv run`), no Node project to maintain; Node 22 stays unused. |
| Tests | pytest, `pytest-timeout`; tiers `tests/unit`, `tests/integration` (real DB + real APIs), `tests/e2e` | harness rules |

Environment variables (`.env.example` must list all; secrets never printed):

| Var | Default | Purpose |
|---|---|---|
| `HERO_ANTHROPIC_API_KEY` | — (required) | Anthropic key |
| `HERO_ANTHROPIC_WORKSPACE_ID` | — (required) | sent as `anthropic-workspace-id` on every Anthropic request |
| `HERO_GOOGLE_MAPS_API_KEY` | — (required) | Static Maps + Street View Static |
| `HERO_CLAUDE_BUDGET_USD` | `50` | cumulative hard stop (all runs, all time, from `llm_calls`) |
| `HERO_RUN_BUDGET_USD` | `2.00` | > **Assumed:** per-run hard stop (safety against a runaway loop) |
| `HERO_ADMIN_PASSWORD` | — (required) | admin area password, header `X-Hero-Admin-Password` |
| `HERO_HOST` / `HERO_PORT` | `0.0.0.0` / `8711` | bind |
| `HERO_DATABASE_URL` | `sqlite:///data/hero.db` | production DB |
| `HERO_TEST_DATABASE_URL` | `sqlite:///data/hero_test.db` | test DB (conftest) |
| `HERO_DATA_DIR` | `data` | imagery/osm cache, previews, crops |
| `HERO_VISION_MODEL` | `claude-sonnet-4-5` | passes A and B |
| `HERO_REVIEW_MODEL` | `claude-sonnet-4-5` | review loop |
| `HERO_MAX_REVIEW_ITERATIONS` | `2` | bounded reflection |
| `HERO_LLM_PRICE_TABLE` | `src/hero/llm/prices.json` | path to the `{model: {input_usd_per_mtok, output_usd_per_mtok}}` table |
| `HERO_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | OSM |
| `HERO_SATELLITE_ZOOM` | `19` | Static Maps zoom (640×640 @ scale 2 ≈ 186 m at Bengaluru) |
| `HERO_STREETVIEW_SAMPLES` | `3` | panoramas per segment (0 m, mid, end) |
| `HERO_LOG_LEVEL` | `INFO` | |

> **Assumed:** price table ships with `claude-sonnet-5-5` and `claude-sonnet-4-5` both at 3.00 / 15.00 (5-5 rate assumed equal; verify) and
> `claude-haiku-4-5: 1.00 / 5.00` USD per million tokens; a model missing from the table makes
> the pipeline fail fast at startup of a run (never silently $0). Haiku is listed so the admin
> can switch the vision model via env after `check_keys` validates the slug; Sonnet stays the
> default because it is the only validated slug.

## Layout

```
/Users/sai/Workspace/her0
├── README.md                      # all commands from repo root, uv run prefix, verify after each setup step
├── pyproject.toml                 # [project] deps incl. sqlalchemy, alembic (deploy-time), scripts: hero-serve
├── uv.lock
├── .python-version                # 3.12.5
├── alembic.ini                    # script_location = src/hero/db/migrations
├── .env.example
├── spec/
├── data/                          # gitignored; created on boot
│   ├── hero.db  hero_test.db
│   ├── imagery/   <sha256>.png + .json      # Google tiles/panos, content-addressed by canonical request
│   ├── osm/       <sha256>.json
│   ├── crops/     <run_id>/<object_id>.png  # billboards + facade crops
│   └── previews/  <run_id>/iter<N>.png  thumb.png
├── src/hero/
│   ├── __init__.py
│   ├── main.py                    # `app = create_app()`; `serve()` console entry (uvicorn, host/port from settings)
│   ├── app.py                     # create_app(): logging, DB init, routers, static mounts, request-id middleware
│   ├── config.py                  # Settings (pydantic-settings) + get_settings()/reset_settings()
│   ├── logging.py                 # structlog JSON config, bind helpers
│   ├── api/
│   │   ├── __init__.py            # register_routers(app) — imports public, admin, media
│   │   ├── envelope.py            # ok()/fail() response shapes + exception handlers
│   │   ├── auth.py                # admin password dependency
│   │   ├── public.py              # /api/segments*
│   │   ├── admin.py               # /api/admin/*
│   │   └── media.py               # /media/* (safe path join under data/)
│   ├── db/
│   │   ├── base.py  session.py    # Declarative base; engine factory (WAL), get_session(), reset_engine()
│   │   ├── models.py              # entities from spec/data.md
│   │   ├── repo.py                # typed query functions (segments, runs, llm_calls, google_requests, game_runs)
│   │   └── migrations/  env.py  versions/0001_phase1.py
│   ├── scene/
│   │   ├── schema.py              # Scene Pydantic models (the contract in spec/data.md) + enums
│   │   ├── geo.py                 # haversine, bearing, local frame ↔ lat/lon, Web-Mercator tile px ↔ lat/lon
│   │   ├── walkability.py         # compute_walkability(scene) — pure
│   │   └── example_scene.json     # the canonical example from spec/data.md (used by unit tests)
│   ├── llm/
│   │   ├── client.py              # LLMClient protocol: vision_json(images, prompt, schema, step) → (obj, usage)
│   │   ├── anthropic_client.py    # adapter: workspace header, retries, tool-use structured output
│   │   ├── pricing.py             # usage → USD from the price table
│   │   ├── prices.json
│   │   └── budget.py              # BudgetGuard: check_before_call(run_id, est) / record(call)
│   ├── imagery/
│   │   ├── cache.py               # get_or_fetch(kind, canonical_params, fetch_fn) → path, cache_hit
│   │   ├── google.py              # satellite tile, streetview metadata, streetview image (counted)
│   │   ├── osm.py                 # Overpass bbox query → ways (highway/building/footway)
│   │   ├── annotate.py            # draw segment line, ticks, side arrow on the tile for pass A
│   │   ├── preview.py             # render_topdown(scene, tile) → PNG (review + thumbnail)
│   │   └── crops.py               # cut billboards/facades from Street View frames
│   ├── prompts/
│   │   ├── detect_layout.md       # pass A
│   │   ├── detect_obstacles.md    # pass B
│   │   └── review_scene.md        # reflection
│   ├── pipeline/
│   │   ├── state.py               # PipelineState dataclass (spec/agent.md)
│   │   ├── steps.py               # one function per step
│   │   ├── review.py              # bounded review loop
│   │   ├── assemble.py            # merge passes → Scene, lanes, tokens, tile_px, latlon
│   │   ├── runner.py              # run_pipeline(run_id): ordered steps, error handler, finalize, persistence
│   │   └── registry.py            # single-flight worker (one run at a time), start_run()
│   └── tools/
│       ├── check_keys.py          # scaffold gate key/model validation
│       └── vendor_three.py        # downloads pinned three.module.js
├── frontend/
│   ├── index.html                 # gallery  (data-hero-page="gallery")
│   ├── play/index.html            # game     (data-hero-page="play")
│   ├── admin/index.html           # admin    (data-hero-page="admin")
│   ├── admin/run.html             # pipeline view (data-hero-page="admin-run")
│   ├── css/base.css  game.css  admin.css
│   ├── js/api.js                  # fetch wrapper for the envelope (scaffold-owned, read-only for slices)
│   ├── js/gallery.js
│   ├── js/game/{main,scene_builder,runner,controls,hud,results,overlay}.js
│   ├── js/admin/{auth,segments,run_view}.js
│   └── vendor/three.module.js
└── tests/
    ├── conftest.py                # test DB via HERO_TEST_DATABASE_URL (alembic upgrade → drop), settings reset,
    │                              # session-scoped `generated_segment` (one real generation per session),
    │                              # `live_server` fixture (uvicorn on 8712 against the test DB)
    ├── unit/
    ├── integration/
    └── e2e/  fixtures/scene_example.json
```

## Conventions

- **Naming** — modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`;
  JSON keys `snake_case` everywhere (API, scene, logs). IDs are UUID4 strings; segments also
  carry a URL `slug` (`[a-z0-9-]+`, unique). Timestamps ISO-8601 UTC.
- **Types at boundaries** — Pydantic models for every request/response and for the Scene;
  SQLAlchemy models never leave `hero.db`; `repo.py` returns Pydantic/dataclass values.
- **Response envelope** — success `{"ok": true, "data": …}`; failure
  `{"ok": false, "error": {"code": "<snake_case>", "message": "<human sentence>", "detail": …}}`.
  Error codes used: `not_found`, `validation_error`, `unauthorized`, `run_in_progress`,
  `budget_exhausted`, `upstream_error`, `internal_error`. HTTP status matches (404/422/401/409/402/502/500).
  Never a raw traceback in a response.
- **Logging fields** — every line: `ts`, `level`, `event`, `request_id` (middleware,
  `X-Request-Id` echoed). Per request: `event="http.request"`, `method`, `path`, `status`,
  `latency_ms`. Per pipeline step: `event="pipeline.step"`, `run_id`, `segment_slug`, `step`,
  `status` (`started|ok|skipped|failed`), `latency_ms`, `detail` (short). Per LLM call:
  `event="llm.call"`, `run_id`, `step`, `model`, `input_tokens`, `output_tokens`, `cost_usd`,
  `latency_ms`, `ok`, `error`. Per Google request: `event="google.request"`, `kind`, `cache_hit`.
  Never log keys, the admin password, or full image bytes.
- **Errors** — external calls (`httpx`, Anthropic) have timeouts (Google 20 s, Overpass 30 s,
  Anthropic 120 s) and 3 retries with exponential backoff on 429/5xx/timeouts; auth/model
  errors are not retried and surface as `upstream_error` with an actionable message
  ("Anthropic rejected the key: check HERO_ANTHROPIC_API_KEY / HERO_ANTHROPIC_WORKSPACE_ID in .env").
  Pipeline failures set `pipeline_runs.status=failed` + `error`; the server never crashes.
- **Settings** — `get_settings()` cached, `reset_settings()` for tests; enum-like env values
  stripped of inline `#` comments. Startup fails with a clear message if a required var is empty.
- **Static serving** — `frontend/` mounted at `/` with `html=True`; `data/` subfolders
  `imagery`, `crops`, `previews` served at `/media/` read-only (path traversal rejected).
- **Frontend JS** — ES modules, no globals except `window.__hero` (debug handle used by the
  e2e tests: `__hero.state`, `__hero.crashNow()` is NOT allowed; tests drive real input).
  Every page root element carries `data-hero-page`. Each view implements empty/loading/error/
  populated states (`../patterns/ui-ux.md`).
- **Tests** — pytest; names are sentences (`test_budget_guard_blocks_call_that_would_exceed_total`);
  unit tests inject clocks and stub only the LLM/HTTP boundary with thin fakes; integration and
  e2e use the real APIs and the SQLite test DB created/dropped by `conftest.py`. Real-LLM tests
  assert structure (keys, ranges, counts), never prose. The `generated_segment` session fixture
  runs the pipeline once via `run_pipeline` against the test DB and is reused by API, admin and
  e2e tests so a full gate costs one generation. Gate is BLOCKED (not skipped) if a required
  key is missing.
- **Migrations** — every schema change is an Alembic revision; `uv run alembic current` is the
  verify command; the app does not auto-create tables.
- **Prompts** — markdown files in `src/hero/prompts/`, loaded at runtime, with `{{placeholders}}`
  rendered by simple substitution; never string literals in code.
