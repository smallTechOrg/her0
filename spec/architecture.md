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
                                                       │  fusion runner (one worker thread)        │
                                                       │   ├─ imagery: Google Static Maps,         │
                                                       │   │   Street View Static, Nominatim,      │
                                                       │   │   Overpass (OSM)  → observations      │
                                                       │   ├─ llm: Anthropic (batched perception   │
                                                       │   │   + critique)                         │
                                                       │   ├─ model: project → merge → fidelity →  │
                                                       │   │   diff → compile (pure code)          │
                                                       │   └─ legacy v1 pipeline (kept, unreachable│
                                                       │      from the API; Phase-3 removal)       │
                                                       │  SQLite (data/hero.db) via SQLAlchemy     │
                                                       │  disk cache data/imagery, osm, roads      │
                                                       └──────────────────────────────────────────┘
```

Phase 2 data layering: `observations` (evidence) → `model_versions` (canonical, versioned model)
→ compiled Scene v2 (what the game plays). A run is an *event* that appends observations and
produces the next model version; knowledge accumulates in the model, never in the run.

Components:

- **Frontend (zero-build static)** — gallery, game (Three.js), admin + pipeline view. Served by
  the backend at `/`. Talks only to `/api/*` and `/media/*` on the same origin.
- **Public API** — list segments, fetch scene, post game runs. No auth.
- **Admin API** — password header; create segment, start generation, poll runs, status/budget.
- **Fusion pipeline** (`hero.fusion`, Phase 2) — the AI-native core: acquire observations →
  batched perception → deterministic projection → evidence-grounded merge into the canonical
  model → bounded critique → publish a new model version + compiled scene + run report
  (`spec/agent.md`). Single background thread; progress persisted after every step so the admin
  UI can poll.
- **Street model** (`hero.model`, Phase 2, pure code, no network/DB) — canonical model types,
  reference line (OpenDRIVE-style `(s, t)` frame), projection of detections into `(s, t)`,
  merge/entity resolution, calibration, fidelity, diff, compile to Scene v2, describe/report text,
  legacy import. Everything the player sees is a deterministic function of one model version.
- **Imagery** (`hero.imagery`) — Google (Static Maps, Street View metadata+image), Nominatim and
  Overpass fetchers behind a content-addressed disk cache; every Google request counted and
  logged whether hit or miss; acquisition turns each image into an `observation` row.
- **LLM layer** (`hero.llm`) — one internal client; Anthropic adapter with the workspace header,
  retry/backoff, per-call token→USD pricing, budget guard, and three structured-output modes
  (`forced_tool`, `auto_tool`, `json_text`) so newer model slugs that reject forced `tool_choice`
  still work.
- **Scene** (`hero.scene`) — the Scene v1 JSON schema (legacy), Web-Mercator/local-frame geometry,
  walkability scoring. Shared by the legacy pipeline, API and tests. Scene v2 lives in
  `hero.model.scene_v2`.
- **Legacy pipeline** (`hero.pipeline`, Phase 1) — kept in the tree and unit-tested but no longer
  reachable from the API (`POST …/generate` dispatches to `hero.fusion`); `hero.pipeline.registry`
  is the single-flight worker for both. Removal is a Phase-3 clean-up.
- **Persistence** — SQLite file + migrations. Entities in `spec/data.md`.

## Data flow

Generation (Phase 2): admin `POST /api/admin/segments/{slug}/generate` (or `…/imagery` for
acquire-only) → run row `queued` → worker, steps in order: `load_base_model` → `resolve_reference_line`
(OSM road polyline clipped to the segment, v0 `prior` model if none) → `fetch_osm_context` →
`plan_imagery` → `acquire_imagery` (observations; Google only for images not already stored) →
`select_unfused` (**nothing new ⇒ no-op: no LLM call, no new version, $0**) → `prepare_views` →
`perceive_overhead` (rectified strips, 1 call) → `perceive_streetlevel` (batches of ≤ 8 frames) →
`project_detections` → `merge` → `consistency_check` → `critique_1` (≤ `HERO_MAX_REVIEW_ITERATIONS`)
→ `publish` (fidelity, diff, compile Scene v2, crops, atomically write `model_versions` +
`segments.scene_json`) → `report` (`run_reports`). Each step appends to `pipeline_runs.steps_json`;
each LLM call inserts `llm_calls` (+ `observation_extractions`); each Google request inserts
`google_requests`; each ingested image inserts/finds an `observations` row.

Play: `GET /api/segments` → gallery → `GET /api/segments/{slug}/scene` (v1 or v2) → Three.js builds
the corridor from the scene (v2: curved ground strip along `path`, variable-width strips from
`corridor.stations`, extruded building footprints, billboard entities; v1: the straight corridor
as before) → loop until health 0 → `POST /api/segments/{slug}/runs` → response carries
best-score status + walkability → results overlay draws hazards on the tile using
`objects[].tile_px` and the "About this street" panel reads `scene.about`.

Inspect (admin): segment page → `GET /api/admin/segments/{slug}/model[/versions|/diff|/coverage|/entities]`
and `…/observations`; run page → `GET /api/admin/runs/{id}` + `/report`.

## Stack

Binding from intake: **Python FastAPI** backend, **Three.js** in the browser, Python 3.12.5
via asdf, `uv`, Node 22 available, no deployment, bind `0.0.0.0`, non-default port.

| Concern | Choice | Rationale |
|---|---|---|
| Runtime / deps | Python 3.12.5 (`.python-version`), `uv` project with `.venv`; every command `uv run …` | binding |
| Web framework | FastAPI 0.115+ / Starlette ≥ 0.40, `uvicorn[standard]`; entry `hero-serve` console script = `uvicorn hero.main:app --host 0.0.0.0 --port 8711` | binding; port 8711 avoids 8000 (`tech-stack.md`); env-overridable `HERO_PORT`/`HERO_HOST` |
| LLM | Anthropic Python SDK ≥ 0.40, models via `HERO_VISION_MODEL`, `HERO_REVIEW_MODEL` (defaults live in `src/hero/llm/defaults.py`: `claude-sonnet-4-5` until the P0 spike below flips them; 4-5 is validated, EOL 2026-11-30 — documented in README); `default_headers={"anthropic-workspace-id": …}` on **every** request. **Structured output has three modes** (`HERO_STRUCTURED_MODE`, default `auto`): `forced_tool` (Phase-1 behaviour: `tool_choice={"type":"tool",…}`), `auto_tool` (`tool_choice={"type":"auto"}` + a system instruction "answer by calling `emit_result` exactly once"), `json_text` (no tools; "reply with one JSON object matching this schema"; fenced-block tolerant parse). `auto` = try `forced_tool` unless the model slug is known to reject it (a per-process set filled on an HTTP 400 mentioning `tool_choice`, and seeded from `check_structured`), then `auto_tool`; if no `tool_use` block comes back → one repair retry in `json_text`; schema failure → the existing single re-ask. **P0 spike (orchestrator, before the gate):** `uv run python -m hero.tools.check_structured` sends one image + the real `StreetExtraction` schema to each of `claude-sonnet-4-5` and `claude-sonnet-5-5` in `auto_tool` and `json_text`; if `claude-sonnet-5-5` returns schema-valid output in either, the orchestrator sets `DEFAULT_VISION_MODEL = DEFAULT_REVIEW_MODEL = "claude-sonnet-5-5"` in `src/hero/llm/defaults.py` and records the result in `NOTES.md`; otherwise 4-5 stays. Both slugs stay in `prices.json` (5-5 rate assumed equal to 4-5; verify with the first real invoice line) | validated key/slug; org-level key requires the header; the 5-5 forced-tool rejection recorded in `NOTES.md` is worked around instead of pinning an end-of-life model |
| Imagery | `httpx` (sync) for Google Static Maps, Street View Static + metadata, Nominatim reverse (road LineString), Overpass; `Pillow` (**no numpy**) for annotation, cropping, top-down rendering and **curvilinear rectification** (`Image.transform(…, Image.MESH, …)`, one quad per ≤ 2 m of `s`) of the satellite tile into a straightened road-aligned strip | no browser needed in the pipeline; a straightened strip makes the footpath a vertical band for the model and lets code read its edges as `(s, t)` exactly, on curved roads too |
| Street model | pure Python in `hero.model` on Pydantic v2 models; geometry in an equirectangular local plane (same as `hero.scene.geo`); no numpy/shapely | the modelled area is ≤ 200 m × 60 m; plain float maths is exact enough (mm) and keeps deps unchanged |
| Database | **SQLite** file `data/hero.db`, SQLAlchemy 2.x (sync, `pysqlite`), Alembic migrations, WAL mode, `timeout=30`, `check_same_thread=False` | > **Assumed:** single process, one admin, tens of segments, no deployment — SQLite is the production engine and is guaranteed locally-available (Postgres is not verified on this Mac). Tests run on the same engine (`data/hero_test.db`). The ORM + migrations keep the path to Postgres open for the public beta. |
| Logging | `structlog` JSON lines to stdout | structured per-request and per-step fields required in Phase 1 |
| Config | `pydantic-settings`, prefix `HERO_`, `extra="ignore"`, secrets as `SecretStr` | fail loudly at startup; `.env` is a commons |
| Frontend | zero-build static HTML + ES modules; Three.js **r170** vendored to `frontend/vendor/three.module.js` by `uv run python -m hero.tools.vendor_three` (downloads `https://unpkg.com/three@0.170.0/build/three.module.js`); no bundler, no npm | > **Assumed:** vendored (not CDN) so the phone-on-LAN path and the headless gate never depend on third-party uptime; single origin keeps the gate to "page + assets return 200". |
| E2E | **Python Playwright** (`playwright` dev dep, chromium) in `tests/e2e`, server launched by a pytest fixture on a free port chosen per session (Phase 1 used a fixed 8712) | > **Assumed:** one toolchain (`uv run`), no Node project to maintain; Node 22 stays unused. |
| Tests | pytest, `pytest-timeout`; tiers `tests/unit`, `tests/integration` (real DB + real APIs), `tests/e2e` | harness rules |

Environment variables (`.env.example` must list all; secrets never printed):

| Var | Default | Purpose |
|---|---|---|
| `HERO_ANTHROPIC_API_KEY` | — (required) | Anthropic key |
| `HERO_ANTHROPIC_WORKSPACE_ID` | — (required) | sent as `anthropic-workspace-id` on every Anthropic request |
| `HERO_GOOGLE_MAPS_API_KEY` | — (required) | Static Maps + Street View Static |
| `HERO_CLAUDE_BUDGET_USD` | `50` | cumulative hard stop (all runs, all time, from `llm_calls`) |
| `HERO_RUN_BUDGET_USD` | `1.00` | per-run Claude hard stop, checked before every call (Phase 2 lowers the Phase-1 default `2.00`; a typical dense run is $0.25–0.50) |
| `HERO_ADMIN_PASSWORD` | — (required) | admin area password, header `X-Hero-Admin-Password` |
| `HERO_HOST` / `HERO_PORT` | `0.0.0.0` / `8711` | bind |
| `HERO_DATABASE_URL` | `sqlite:///data/hero.db` | production DB |
| `HERO_TEST_DATABASE_URL` | `sqlite:///data/hero_test.db` | test DB **base name**; `tests/conftest.py` appends the process id (`data/hero_test_<pid>.db`) so concurrent slices/sessions never share a file (NOTES.md lesson), and removes it at session end |
| `HERO_DATA_DIR` | `data` | imagery/osm cache, previews, crops |
| `HERO_VISION_MODEL` | `claude-sonnet-4-5` | passes A and B |
| `HERO_REVIEW_MODEL` | `claude-sonnet-4-5` | review loop |
| `HERO_MAX_REVIEW_ITERATIONS` | `1` | bounded reflection (critique of the model render; Phase 1 default was 2) |
| `HERO_LLM_PRICE_TABLE` | `src/hero/llm/prices.json` | path to the `{model: {input_usd_per_mtok, output_usd_per_mtok}}` table |
| `HERO_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | OSM |
| `HERO_SATELLITE_ZOOM` | `19` | Static Maps zoom (640×640 @ scale 2 ≈ 186 m at Bengaluru) |
| `HERO_STREETVIEW_SAMPLES` | `3` | Phase-1 legacy pipeline only: panoramas per segment (0 m, mid, end) |
| `HERO_SV_SPACING_M` | `10` | Phase 2: Street View station spacing along the reference line (default imagery plan) |
| `HERO_SV_HEADINGS` | `forward,back,left,right` | Phase 2: headings captured at each station, relative to the road tangent (`forward\|back\|left\|right`, comma list) |
| `HERO_SAT_CROP_ZOOM` | `20` | Phase 2: zoom of the close-up satellite crops along the footpath (falls back to 19 when the tile looks like "no imagery") |
| `HERO_SAT_CROP_SPACING_M` | `40` | Phase 2: spacing of close-up crop centres |
| `HERO_MAX_IMAGES_PER_RUN` | `60` | Phase 2: cap on NEW paid Google image requests (Street View + crops) per run; the planner widens spacing to fit |
| `HERO_PERCEPTION_BATCH_IMAGES` | `8` | Phase 2: images per perception call (fewer, richer calls) |
| `HERO_STRUCTURED_MODE` | `auto` | Phase 2: `auto\|forced_tool\|auto_tool\|json_text` (see Stack → LLM) |
| `HERO_UPLOAD_MAX_MB` | `10` | Phase 2: max size of an uploaded observation photo |
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
    │                              # Phase 1: session-scoped `generated_segment` (one real generation per session) and
    │                              # `live_server` (uvicorn on 8712); Phase 2 supersedes both — see "Layout — Phase 2 additions"
    ├── unit/
    ├── integration/
    └── e2e/  fixtures/scene_example.json
```

## Layout — Phase 2 additions (paths marked `(+)` are new; `(~)` extended in place; the tree above is otherwise unchanged)

```
/Users/sai/Workspace/her0
├── docs/evidence/                       (+) persisted gate evidence: phase2-gate.log, phase2-gate.json, placement.png
├── data/                                 uploads/ (+)  roads/ (+)  crops/<segment_slug>/ (+)
├── src/hero/
│   ├── config.py                        (~) new settings (see env table); defaults imported from llm/defaults.py
│   ├── model/                           (+) the street model — PURE (no network, no DB, no clock)
│   │   ├── types.py                     CanonicalModel, Entity, Strip, ProfileInterval, Building, Observation, Evidence,
│   │   │                                Contradiction, FidelityMap, ModelDiff, RunReport, enums            [S1a]
│   │   ├── batch.py                     ProjectedBatch, ProjectedDetection, VisibleRegion, ProfileEvidence, StripSample [S1a]
│   │   ├── catalogue.py                 CATALOGUE, entry(), legacy mapping                                  [S1a]
│   │   ├── refline.py                   ReferenceLine, RectGrid                                              [S1a]
│   │   ├── ids.py                       entity_id(), building_id(), obs_key_*                                [S1a]
│   │   ├── scene_v2.py                  SceneV2 (+ sub-models), load_scene()                                 [S1a]
│   │   ├── roadclip.py                  clip_road(), straight_chord()                                        [S1b]
│   │   ├── projection.py                SV pixel → (s,t), rectified px ↔ (s,t), tile px ↔ (s,t), strip traces [S1b]
│   │   ├── priors.py                    build_prior_model(), osm_priors()                                    [S1b]
│   │   ├── calibrate.py                 calibrate_confidence(), logit/sigmoid, range_weight()                [S1c]
│   │   ├── merge.py                     merge_batches(), apply_critique()                                    [S1c]
│   │   ├── diff.py                      diff_models(), model_hash()                                          [S1c]
│   │   ├── coverage.py                  observation_cells(), visible_regions()                               [S1d]
│   │   ├── fidelity.py                  compute_fidelity()                                                   [S1d]
│   │   ├── compile.py                   compile_scene()                                                      [S1d]
│   │   ├── describe.py                  about_summary(), version_summary(), entity/strip/interval text       [S1d]
│   │   ├── report.py                    build_run_report()                                                   [S1d]
│   │   └── legacy.py                    import_legacy()                                                      [S1d]
│   ├── db/models.py, repo.py            (~) 4 new tables; repo parses Scene v1|v2                            [S1a]
│   ├── db/model_repo.py                 (+) observation / model-version / extraction / report persistence    [S1a]
│   ├── db/migrations/versions/0002_street_model.py (+)                                                       [S1a]
│   ├── imagery/
│   │   ├── google.py                    (~) metadata `date`, pano-id image fetch, satellite crop, cost consts [S2]
│   │   ├── roads.py                     (+) fetch_road_polyline()                                            [S2]
│   │   ├── plan.py                      (+) ImageryPlan, plan_images()                                       [S2]
│   │   ├── acquire.py                   (+) acquire() → observations                                         [S2]
│   │   ├── rectify.py                   (+) rectify_window(), draw_rect_overlay()                            [S2]
│   │   └── noimagery.py                 (+) looks_like_no_imagery()                                          [S2]
│   ├── llm/ client.py, anthropic_client.py (~) structured modes; defaults.py (+, created by S1a, flipped by P0); 
│   ├── fusion/                          (+) the Phase-2 pipeline
│   │   ├── schemas.py                   OverheadExtraction, StreetExtraction, CritiqueResult                 [S3b]
│   │   ├── views.py                     image payloads + captions + batching                                 [S3b]
│   │   ├── convert.py                   extraction → ProjectedBatch                                          [S3b]
│   │   ├── render.py, critique.py       model top-down render, critique step                                 [S3b]
│   │   ├── state.py, steps.py, runner.py, publish.py, assets.py                                              [S3c]
│   ├── prompts/                         (+) perceive_overhead.md, perceive_streetlevel.md, critique_model.md  [S3b]
│   ├── pipeline/registry.py             (~) dispatches generate/acquire to hero.fusion                       [S3c]
│   ├── api/
│   │   ├── __init__.py                  (~) router auto-discovery: every module in `hero.api` except `auth, envelope, media` that exposes `router`, ordered by its optional `ROUTER_ORDER` (default 50) then name  [S1a]
│   │   ├── admin.py                     (~) status, generate (+plan body), run detail + report link          [S3c]
│   │   ├── admin_imagery.py             (+) /imagery, /observations*, /placement-preview                     [S2]
│   │   ├── admin_model.py               (+) /model*, /runs/{id}/report                                       [S6]
│   │   ├── public.py                    (~) summary adds `model`, scene v1|v2                                [S6]
│   │   └── media.py                     (~) serves data/uploads                                              [S2]
│   └── tools/ check_structured.py (+) [S3a]; import_legacy.py, create_demo_street.py (+) [S3c]; load_fixture_street.py (+) [S1a]
├── frontend/
│   ├── play/index.html, js/game/*.js    (~) curved corridor, About panel                                     [S4]
│   ├── admin/segment.html               (+) segment page (data-hero-page="admin-segment")                    [S5]
│   └── js/admin/ segment_view.js coverage.js entities.js versions.js report.js (+)                           [S5]
└── tests/
    ├── conftest.py                      (~) per-process DB + free-port live server + generation fixtures      [S1a]
    ├── fixtures/street_fixtures.py      (+) builders for curved reference lines, observations, models        [S1a]
    ├── fixtures/extractions/*.json      (+) recorded/hand-made extraction outputs                           [S3b]
    ├── e2e/fixtures/scene_curved_v2.json (+) compiled from the curved fixture model                          [S1d]
    └── live/                            (+) the ≤ 2-generation Phase-2 gate                                  [S7]
```

Module interfaces that parallel slices code against (names and signatures are the contract;
bodies are the owning slice's job) are in `## Contracts between slices` below.

## Contracts between slices

All are plain functions/dataclasses; unless noted they raise `ValueError` on invalid input and
never touch the network, DB or clock (callers pass `now`).

**`hero.model.refline`** *(S1a)*

```python
@dataclass(frozen=True)
class StProjection:  s: float; t: float; dist_m: float; clamped: bool   # dist_m = |input point − foot point|

class ReferenceLine:
    @classmethod
    def from_latlons(cls, latlons: Sequence[tuple[float, float]]) -> "ReferenceLine"  # ≥ 2 distinct points
    @classmethod
    def from_model(cls, m: ReferenceLineModel) -> "ReferenceLine"
    def to_model(self, *, source: str, osm_way_id: int | None, name: str | None,
                 extrapolated_m: tuple[float, float] = (0.0, 0.0)) -> ReferenceLineModel
    length_s: float                                              # property
    def xy(self, s: float) -> tuple[float, float]               # (east_m, north_m) from the first point; clamps s to [0, length_s]
    def bearing_deg(self, s: float) -> float                    # [0, 360)
    def point_at(self, s: float, t: float) -> tuple[float, float]       # (lat, lon); t left-positive
    def to_st(self, lat: float, lon: float) -> StProjection             # nearest foot point; beyond the ends: extend along the end tangent (clamped=True)
    def sample_s(self, step_m: float) -> list[float]            # 0, step, 2·step … and length_s
    def turning_deg(self, s0: float, s1: float) -> float        # signed bearing change, clockwise +

@dataclass(frozen=True)
class RectGrid:                      # a straightened window: up = travel, image-left = left of travel
    s_top: float; s_bottom: float; t_left: float; t_right: float; m_per_px: float   # s_top > s_bottom, t_left > t_right
    @property
    def size_px(self) -> tuple[int, int]
    def px_to_st(self, x: float, y: float) -> tuple[float, float]   # s = s_top − y·mpp ; t = t_left − x·mpp
    def st_to_px(self, s: float, t: float) -> tuple[float, float]
```

**`hero.model.roadclip`** *(S1b)* — `clip_road(road_latlons, centre: tuple[float,float], length_m: float, travel_bearing_deg: float, *, max_snap_m: float = 25.0) -> tuple[ReferenceLine, ClipInfo]`: project `centre` onto the polyline (nearest point; error if farther than `max_snap_m`), orient the polyline so its tangent there is within 90° of `travel_bearing_deg`, walk `length_m/2` each way by arc length; if the polyline ends early, extend straight along the end tangent and report it in `ClipInfo(extrapolated_start_m, extrapolated_end_m, snap_m)`. `straight_chord(start, end) -> ReferenceLine`.

**`hero.model.projection`** *(S1b)*

```python
CAMERA_HEIGHT_M = 2.5            # Street View car roof camera
def project_street_detection(obs: Observation, ref: ReferenceLine, box_px: tuple[int,int,int,int], *,
                             touches_ground: bool, est_distance_m: float | None) -> ProjectedPoint | None
    # ProjectedPoint(s, t, range_m, sigma_m, weight, method: "ground_plane"|"estimated_distance")
    # ground plane: pinhole f = (W/2)/tan(fov/2); ray for the box bottom-centre pixel (u, v); pitch rotates the ray about the camera x axis;
    # horizontal range d = CAMERA_HEIGHT_M · hypot(f, u−cx)/(v−cy) at pitch 0; azimuth = heading + atan((u−cx)/f);
    # falls back to est_distance_m when the bottom edge is within 1.5° of the horizon or not touches_ground; None when range > 30 m
    # or neither is available. If both exist and disagree by > max(4 m, 40 %): use the ground-plane value, weight × 0.5, flag "range_disagreement".
def project_overhead_polyline(grid: RectGrid, pts_px: Sequence[tuple[float, float]]) -> list[tuple[float, float]]   # → [(s, t)]
def strip_samples_from_traces(grid: RectGrid, edge_a_px, edge_b_px, *, kind, material, condition, side, weight, step_m: float = 2.5) -> list[StripSample]
def footpath_runs_to_states(grid: RectGrid, runs: Sequence[FootpathRun]) -> list[StateSpan]      # y ranges → (s_from, s_to, state, weight)
def tile_px_to_st(ref: ReferenceLine, georef: TileGeoref, px: float, py: float) -> tuple[float, float]
def st_to_tile_px(ref: ReferenceLine, georef: TileGeoref, s: float, t: float) -> tuple[float, float]
```

**`hero.model.priors`** *(S1b)* — `build_prior_model(*, segment_slug: str, footpath_side: str, ref: ReferenceLine, ref_model: ReferenceLineModel, osm: OsmContext | None, now: datetime) -> CanonicalModel` (kind `prior`, version 0, `placement_verified = ref_model.source.startswith("osm")`); `OsmContext(highway_tags: dict, footway_polylines: list[list[(lat,lon)]], buildings: list[OsmBuilding])` is adapted from `hero.imagery.osm.OsmGeometry` by `hero.fusion` (not by S1b).

**`hero.model.batch`** *(S1a — merge's input contract)*

```python
@dataclass(frozen=True)
class VisibleRegion:        observation_id: str; s_from: float; s_to: float; side: Literal["left","right","both"]; weight: float
@dataclass(frozen=True)
class ProjectedDetection:   key: str; observation_id: str; type: EntityType; subtype: str | None; s: float; t: float; sigma_m: float
                            extent_s: float; extent_t: float; height_m: float | None; orientation_deg: float | None
                            attributes: dict[str, str | float | bool]; description: str; raw_confidence: float
                            crop_px: tuple[int,int,int,int] | None; weight: float; flags: tuple[str, ...] = ()
@dataclass(frozen=True)
class StripSample:          s: float; kind: StripKind; side: Literal["left","right","centre"]; t_lo: float; t_hi: float
                            material: Material; condition: Condition; weight: float; observation_id: str
@dataclass(frozen=True)
class StateSpan:            observation_id: str; side: Literal["left","right"]; s_from: float; s_to: float; state: FootpathState; weight: float
@dataclass(frozen=True)
class FacadeDetection:      observation_id: str; side: Literal["left","right"]; s_from: float; s_to: float; colour_hex: str | None
                            storeys: int | None; description: str; crop_px: tuple[int,int,int,int]; weight: float
@dataclass(frozen=True)
class ProjectedBatch:       batch_id: str; observation_ids: tuple[str, ...]; detections: tuple[ProjectedDetection, ...]
                            strips: tuple[StripSample, ...]; states: tuple[StateSpan, ...]; facades: tuple[FacadeDetection, ...]
                            visible: tuple[VisibleRegion, ...]
```

**`hero.model.merge`** *(S1c)*

```python
@dataclass
class ChangeRecord:   kind: str; id: str; before: dict | None; after: dict | None; reason: str      # kind: entity_added|entity_updated|entity_state|strip_changed|state_changed|building_added|building_updated|contradiction_opened|contradiction_resolved
@dataclass
class MergeResult:    model: CanonicalModel; changes: list[ChangeRecord]; ignored_batches: list[str]   # batches whose observations were all already fused
def merge_batches(base: CanonicalModel, batches: Sequence[ProjectedBatch], *, new_version: int, run_id: str | None, now: datetime) -> MergeResult
    # Deterministic (batches sorted by (min observation station, batch_id); detections by key). Idempotent: merge(merge(m, B), B) == merge(m, B) except version/run_id.
    # Sets fused_observation_ids ∪= batch.observation_ids, parent_version = base.version, kind="fused". Never deletes an entity.
def apply_critique(model: CanonicalModel, ops: Sequence[CritiqueOp], *, run_id: str | None, now: datetime) -> tuple[CanonicalModel, list[ChangeRecord], list[str]]   # (model, changes, skipped-op reasons)
```
`hero.model.calibrate`: `calibrate_confidence(raw: float) -> float`, `logit(p)`, `sigmoid(x)`, `range_weight(range_m: float) -> float`, `sigma_for_range(range_m: float, estimated: bool) -> float`.
`hero.model.diff`: `diff_models(a: CanonicalModel | None, b: CanonicalModel, fid_a: FidelityMap | None, fid_b: FidelityMap) -> ModelDiff`; `model_hash(m: CanonicalModel) -> str` (sha256 of canonical JSON excluding `version, parent_version, run_id, created_at, kind`).

**`hero.model.coverage` / `fidelity` / `compile` / `describe` / `report` / `legacy`** *(S1d)*

```python
@dataclass(frozen=True)
class CoverageCell: observation_id: str; kind: Literal["forward","back","left","right","oblique","closeup","overhead_hires"]; s_from: float; s_to: float; side: Literal["left","right","both"]; station_s: float
def observation_cells(obs: Observation, ref: ReferenceLine) -> list[CoverageCell]
def visible_regions(obs: Observation, ref: ReferenceLine) -> list[VisibleRegion]      # weight = range_weight(distance from the camera station)
def compute_fidelity(model: CanonicalModel, observations: Mapping[str, Observation], *, bin_m: float = 5.0) -> FidelityMap
def compile_scene(model: CanonicalModel, *, observations: Mapping[str, Observation], assets: Mapping[str, str],
                  tile: Observation | None, fidelity: FidelityMap, slug_name: str, city: str) -> SceneV2
    # assets: {entity_id|building_id: "/media/crops/…png?v=N"}; tile: the satellite_overview observation used as ground (None → ground.tile_url null)
def about_summary(...) -> str;  def version_summary(model, diff, fidelity) -> str
def build_run_report(*, run, mode, base, result, diff, fid_before, fid_after, new_observations, cached_count, costs, warnings, now) -> RunReport
def import_legacy(scene: Scene, *, segment_slug: str, footpath_side: str, now: datetime) -> CanonicalModel   # kind "legacy", version 0
```

**`hero.db.model_repo`** *(S1a)* — all return Pydantic records, one transaction per call. `ObservationCreate` = the `observations` columns minus `id/created_at`
(`georef_json` as `georef: dict`); `ModelVersionCreate(version: int, kind: str, parent_version: int | None, run_id: str | None, model: CanonicalModel, scene: dict | None, fidelity: FidelityMap, diff: ModelDiff | None, summary: str)` (the repo fills `model_hash`, counts, `fidelity_level`);
`ExtractionCreate(segment_id, run_id, batch_index, kind, observation_ids, model, prompt_name, extraction: dict)`.
`hero.db.repo.SegmentRecord.scene` is `Scene | SceneV2 | None` (via `hero.model.scene_v2.load_scene`); both expose `.walkability`, `.hazards()`, `.objects`.

```python
def upsert_observation(data: ObservationCreate) -> tuple[Observation, bool]            # (row, created); idempotent on (segment_id, obs_key)
def get_observations(segment_id: str, *, ids: Sequence[str] | None = None, kinds: Sequence[str] | None = None,
                     limit: int = 1000, offset: int = 0) -> list[Observation]
def observation_counts(segment_id: str) -> dict[str, int]                               # by kind
def head_version(segment_id: str) -> int | None
def get_version(segment_id: str, version: int) -> ModelVersionRecord | None             # model, scene (dict), fidelity, diff, summary, kind, run_id, created_at
def list_versions(segment_id: str) -> list[ModelVersionSummary]
def save_version_only(segment_id: str, rec: ModelVersionCreate) -> ModelVersionRecord   # v0 prior/legacy: stores a version WITHOUT touching segments
def publish_version(segment_id: str, rec: ModelVersionCreate, *, thumbnail_path: str | None, quality: str) -> ModelVersionRecord
    # one transaction: insert model_versions (version must be head+1), set segments.scene_json/status="ready"/quality/latest_run_id/thumbnail_path
def save_extraction(rec: ExtractionCreate) -> str;  def list_extractions(segment_id: str, run_id: str | None = None) -> list[ExtractionRecord]
def save_run_report(run_id: str, segment_id: str, report: RunReport) -> None;  def get_run_report(run_id: str) -> RunReport | None
```

**`hero.imagery`** *(S2)*

```python
# google.py (extended)
class StreetViewMeta:  status; pano_id; lat; lon; date: str | None; cache_hit      # date = "YYYY-MM"
GoogleImagery.streetview_image_pano(pano_id: str, heading_deg: float, *, fov: int = 90, pitch: int = 0) -> FetchedImage
GoogleImagery.satellite_crop(lat: float, lon: float, zoom: int) -> FetchedImage     # same call as satellite_tile, kind "satellite"
# roads.py
@dataclass
class RoadPolyline:  latlons: list[tuple[float, float]]; source: Literal["osm_nominatim","osm_overpass"]; osm_way_id: int | None; name: str | None; tags: dict[str, str]
def fetch_road_polyline(lat: float, lon: float, *, cache: ImageryCache, overpass_url: str, http: httpx.Client | None = None) -> RoadPolyline | None   # Nominatim reverse(polygon_geojson) first, Overpass fallback (short timeout), None = degrade to straight_chord
# plan.py
class ImageryPlan(BaseModel):  sv_spacing_m: float; headings: list[Literal["forward","back","left","right"]]; sat_crop_zoom: int; sat_crop_spacing_m: float; include_overview: bool; max_images: int; s_range: SRange | None
def default_plan(settings: Settings) -> ImageryPlan;  def merge_plan(base: ImageryPlan, overrides: dict) -> ImageryPlan   # validates ranges: spacing 4–40, ≥ 1 heading, zoom 18–21
@dataclass(frozen=True)
class PlannedImage:  kind: str; view: str | None; station_s: float; lat: float; lon: float; heading_deg: float | None; zoom: int | None
def plan_images(ref: ReferenceLine, plan: ImageryPlan) -> tuple[list[PlannedImage], PlanInfo]    # PlanInfo(trimmed: bool, effective_spacing_m, counts)
# acquire.py
@dataclass
class AcquireResult:  observations: list[Observation]; new: int; cached: int; skipped: list[dict]; cost_usd: float; warnings: list[str]
def acquire(*, segment_id: str, run_id: str, ref: ReferenceLine, plan: ImageryPlan, google: GoogleImagery, data_dir: Path) -> AcquireResult
    # per planned SV image: free metadata → (pano_id, real position, date) → dedupe on (pano_id, heading, pitch, fov) → image fetch by pano id →
    # upsert_observation (sha256, station_s/offset_t/view from `ref`). Non-OK metadata (ZERO_RESULTS, NOT_FOUND) and failed frames are recorded in `skipped`, never raised. Stops fetching at plan.max_images NEW images.
# rectify.py
def rectify_window(tile_path: Path, georef: TileGeoref, ref: ReferenceLine, grid: RectGrid, *, overlay: bool = True) -> Image.Image
# noimagery.py
def looks_like_no_imagery(img: Image.Image) -> bool        # low-variance light-grey tile (Google's "we don't have imagery at this zoom" placeholder)
```

**`hero.llm`** *(S3a)* — `LLMClient.vision_json(...)` keeps its Phase-1 signature; adds keyword `structured_mode: str | None = None` (default from settings) and returns the same `(obj, LLMUsage)`; `AnthropicClient.supported_modes(model) -> list[str]`; `BudgetGuard(settings, *, run_budget_usd: float | None = None, …)` lets a request override the per-run cap (0.05–5) without touching the global guard; `LlmCallCreate` gains `structured_mode: str | None` (logged on `llm.call`, not stored in a new column — it is part of `request_summary`).

**`hero.fusion.views`** *(S3b)* — `build_street_batches(observations: Sequence[Observation], ref: ReferenceLine, *, data_dir: Path, max_images: int) -> list[StreetBatch]` (batches = contiguous station windows, ≤ `max_images` images, deterministic order; each `StreetBatch(batch_id, observations, images: list[LLMImage], captions)` caption states observation id, view, station_s, heading, and "forward = direction of travel"); `build_overhead_windows(...)`; `hero.fusion.convert.street_to_batch(batch, extraction, ref) -> ProjectedBatch`, `overhead_to_batch(windows, extraction, ref) -> ProjectedBatch`.

**`hero.fusion.runner`** *(S3c)* — `run_fusion(run_id: str, *, llm=None, google=None) -> PipelineRunRecord` (never raises; mirrors Phase-1 `run_pipeline`); `hero.pipeline.registry.start_run(slug, *, mode="generate", plan_overrides=None)`.

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
  Phase 2 adds: `event="observation.ingested"` (`run_id`, `observation_id`, `kind`, `view`, `station_s`, `cache_hit`, `cost_usd`),
  `event="model.merge"` (`run_id`, `base_version`, `batches`, `entities_added`, `entities_updated`, `contradictions_opened`),
  `event="model.published"` (`run_id`, `segment_slug`, `version`, `entity_count`, `fidelity_overall`, `quality`),
  `event="fusion.noop"` (`run_id`, `reason`), `event="fusion.report"` (`run_id`, `images_new`, `images_cached`, `cost_usd`).
  The Phase-2 steps keep using `event="pipeline.step"` / `"llm.call"`.
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
  key is missing. **Phase 2:** `generated_segment` lives in `tests/live/conftest.py` (two generations
  per session, hard-capped); other tiers use `seeded_fixture_segment` (fixture street seeded from
  `tests/e2e/fixtures/model_fixture/` via `hero.tools.load_fixture_street.seed_fixture_segment`, no spend).
- **Determinism** — everything in `hero.model` is a pure function: no clock (callers pass `now`),
  no randomness, no dict-order dependence (sort before iterating), floats rounded only at
  serialisation (`round(x, 3)` for metres, 6 for lat/lon). `compile_scene` and `merge_batches`
  are tested for byte-identical output on repeat and on JSON round-trip.
- **Test tiers in Phase 2** — `tests/unit` (no network, no DB engine other than the per-process
  SQLite file), `tests/integration` (real DB; Google/Nominatim live where the test says so, Overpass
  failures degrade-and-assert, **no Anthropic** except the ≤ 3 tiny structured-output probes in
  `test_llm_client.py`; the LLM is a `ReplayLLM` that returns recorded `tests/fixtures/extractions/*.json`
  and records the calls it would have made), `tests/e2e` (Playwright, phone viewport, fixture
  scenes/DB — no generation), `tests/live` (the ≤ 2 real generations). Markers: `live` (needs
  network/real keys; registered in `pyproject.toml`).
- **Per-process test DB** — `tests/conftest.py` derives `data/hero_test_<pid>.db` from
  `HERO_TEST_DATABASE_URL`, uses a free port for the live server (`socket.bind(("127.0.0.1", 0))`,
  never 8711 or the dev server's port) and deletes both at session end, so slices run concurrently.
- **Migrations** — every schema change is an Alembic revision; `uv run alembic current` is the
  verify command; the app does not auto-create tables.
- **Prompts** — markdown files in `src/hero/prompts/`, loaded at runtime, with `{{placeholders}}`
  rendered by simple substitution; never string literals in code.
