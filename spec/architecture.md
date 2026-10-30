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
- **Export** (`hero.export`, Phase 2.1 r2) — the shared, deterministic mesh builder and exporters: `StreetMesh` → `street.glb` (glTF 2.0), `street.manifest.json`, best-effort `street.xodr`, plus `gameplay.json` and the Python reference spawner. The browser and Unity/Unreal/Godot consume the same files (`spec/portability.md`).
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
| `HERO_VISION_MODEL` | `claude-sonnet-5-5` (from `llm/defaults.py`; was `claude-sonnet-4-5` before the P0 spike) | passes A and B; also the **default of the per-run model dropdown** (Phase 2.1 r2, never changed silently) |
| `HERO_REVIEW_MODEL` | `claude-sonnet-5-5` | review loop (a per-run choice overrides both) |
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

> **Phase 2.1 r2 — `claude-opus-5-5` price row.** No number is written in this spec on purpose: slice `SM` looks the real per-token price up (the `claude-api` skill or the Anthropic pricing docs), adds the row to `src/hero/llm/prices.json` with `source_url` and `verified_on` fields (extra keys are ignored by the old reader and required by the new validator for any model added after Phase 2), and an unpriced model cannot be selected. `prices.json` row shape: `{"input_usd_per_mtok", "output_usd_per_mtok", "source_url"?, "verified_on"?}`.
>
> **Assumed (Phase 2):** price table ships with `claude-sonnet-5-5` and `claude-sonnet-4-5` both at 3.00 / 15.00 (5-5 rate assumed equal; verify) and
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

## Phase 2.1 r2 — architecture *(AUTHORITATIVE; shared mesh builder, glTF/OpenDRIVE export, browser loads the glb, gameplay as data, model choice)*

> Where this section and the r1 section after it ("Phase 2.1 r1") disagree, r2 wins. r1 items that are **VOID**: the JS `terrain/frontage/furniture/tex` modules, the r1 *Render budget* "how it is met" column and per-copy draw-call arithmetic, the r1 *Layout — Phase 2.1 additions* JS lines, `compile_scene -> SceneV3` (now `CompileResult`), and `to_v2`. r1 items still in force: bake (amended), recompile, improve, audit, new settings (plus the two below), observability events, `texture.baked`/`scene.compiled`.

```
 stored model vN ─┐                                        ┌─▶ scene v3-lite (scene_json)          ─▶ GET /scene  (UI, About, results)
 observations ────┼─▶ bake_textures ─▶ TextureRefs ─┐      │
 (files on disk)  │                                 ├─▶ compile_scene ─▶ CompileResult ─┼─▶ gaps_json
                  └─▶ analyze_gaps ─────────────────┘                  (scene, plan,  │
                                                                         gaps)         └─▶ StreetPlan ─┬─▶ build_street_mesh ─▶ StreetMesh ─▶ write_glb ─▶ street.glb ─┐
 src/hero/export/gameplay.json ──▶ compile (lane/token/walkability rules)                                └─▶ build_xodr ────────────────────▶ street.xodr ─────────────┼─▶ data/exports/<slug>/street_<sha8>.*
                               └─▶ manifest (embedded) ◀── build_manifest(plan, mesh, glb sha, xodr sha) ────────────────────▶ street.manifest.json ─────────┘      + model_versions.export_json
 browser: GET scene ∥ GET manifest ∥ GET model.glb ─▶ GLTFLoader ─▶ {glb scene (3 chained copies), Track from manifest, Runner(playData, gameplay), spawner, dynamic layer, sky/fog/light}
 admin:   GET export.zip (glb + manifest + xodr + gameplay.json + IMPORT.txt)        engines: portability.md
```

**Components added.** `hero.export` (pure except `store`): `frame` (the `(s,t)→world` mapping of `spec/portability.md` §2, shared by mesh, manifest and xodr — one implementation), `mesh_ground/frontage/furniture` + `atlas` + `mesh_builder` (geometry + atlas packing), `glb` (canonical glTF writer + validator), `manifest`, `xodr`, `gameplay`/`gameplay_ref` (rules as data + the Python reference spawner), `store`/`bundle`/`pipeline` (disk, ETag, zip, orchestration). `hero.model.street_plan` (the compile's geometry half). `hero.llm.models` (selectable models). Frontend: `glb_loader`, `track_manifest`, `play_data`, `gameplay`, `spawner` replace the r1 JS geometry modules.

**Stack additions (all `> Assumed:`).**

| Concern | Choice | Rationale |
|---|---|---|
| glTF | **`pygltflib`**, exact pin (`uv add pygltflib`; frozen in `uv.lock`), pure Python, no numpy. The writer is ours (`hero.export.glb`: `struct.pack` buffers, ordered-dict JSON, fixed padding) — the library models/re-reads the document | `trimesh` needs numpy and gives no byte-stability guarantee; determinism is a hard requirement (hash equality tests) so the bytes must not depend on a library's serialiser |
| Imaging | `Pillow` pinned **exactly** (it already is a dep, now frozen to one version because atlas JPEG bytes feed the hash); atlases: `Image.save(format="JPEG", quality=82, optimize=False, progressive=False, subsampling=2, exif=b"")`, resampling `Image.BICUBIC`/`LANCZOS` fixed per call | JPEG encoder output is deterministic for fixed library+params; version recorded in `manifest.build` |
| OpenDRIVE | stdlib `xml.etree.ElementTree` writer/parser, `%.6f` numbers, fixed attribute order | no dependency; best effort |
| Zip | stdlib `zipfile`, entries sorted, `ZipInfo` date `(1980,1,1,0,0,0)`, `ZIP_DEFLATED`, `compresslevel=6`, fixed `external_attr` | deterministic bytes |
| glb in browser | Three.js r170 `GLTFLoader` + `BufferGeometryUtils` vendored by `hero.tools.vendor_three` into `frontend/vendor/three/examples/jsm/{loaders,utils}/` (sha256-pinned in `frontend/vendor/VENDOR.json`; the tool verifies it); import map `{"three": "/vendor/three.module.js"}` in `play/index.html`; no Draco/meshopt/KTX2 loaders | same vendoring rule as Three itself (LAN/headless never depend on a CDN) |
| glb validation | Khronos `gltf-validator` through `npx --yes gltf-validator` (Node 22 on the Mac) when reachable; always-on `pygltflib` reload + `hero.export.glb_validate` + the browser parse | validator is a JS tool; not a Python dependency |
| JS unit tests | unchanged: in-browser via Playwright (`page.evaluate`); vectors passed in from Python | one toolchain |

**Data flow changes.** `publish` and `recompile` run: `bake_textures` → `compile_scene` (→ `CompileResult`) → `build_and_store_exports` (mesh → glb → xodr → manifest → write files → `ExportRecord`) → one DB transaction updating `model_versions.{scene_json, scene_schema, compiled_by, gaps_json, export_json}` and `segments.{scene_json, scene_schema}`. Files are written before the transaction (content-addressed ⇒ a crash leaves only orphan files). All network clients are monkeypatched to raise in these paths (tests).
Play: `GET /scene` (v1/v2/v3) → v3: parallel `GET manifest`, `GET model.glb` → `GLTFLoader.parse` → `play_data.fromManifest` → Track + Runner + spawner → render; v1/v2: legacy `scene_builder` + `fromLegacyScene` (gameplay from `GET /api/gameplay.json`).

**Render budget r2** (measured from `renderer.info` after a frame, phone viewport 390×844, DPR ≤ 2, three chained copies alive; the numbers are unchanged from r1, the arithmetic is not):

| metric | budget | how it is met |
|---|---|---|
| draw calls per frame | **≤ 60** | glb ≤ 14 primitives (= nodes) per copy, shared geometry/materials cloned for 3 copies ⇒ ≤ 42; dynamic layer ≤ 8; runner 2; AO blob 1; sky 0 (`scene.background`) ⇒ ≤ 53; frustum culling of the copy behind the camera ⇒ typical 38–46 |
| triangles per frame | **≤ 60 000** | glb ≤ 12 000 per copy × 3 = 36 000 (culling ⇒ ~24 000 typical); dynamic ≤ 6 000; runner/AO < 500 |
| textures alive | **≤ 8** | glb 2 (ground, facade; shared by all copies) + AO blob 1 + HUD/spare ≤ 2 |
| texture memory | **≤ 24 MB** | ground ≤ 1024² (5.6 MB mipmapped) + facade ≤ 1024×512 (2.8 MB) + small ⇒ ~9 MB |
| glb size / load | ≤ 1.5 MB (100 m); parse + first frame ≤ 2.5 s on LAN (recorded, not asserted) | JPEG atlases q82, 1 mm positions, indexed meshes |
| fps (manual) | ≥ 30 on a mid-range Android phone | not asserted in CI (software GL); recorded in evidence |
| manifest payload | ≤ 120 kB (100 m), ≤ 200 kB (200 m) | asserted on the fixtures |
| scene payload | ≤ 40 kB | asserted |

The JS renders with `MeshLambertMaterial` swapped in for the glb's `MeshStandardMaterial` (same `map`/`color`/`vertexColors`), `antialias: false`, no shadows/post-processing, and per-frame work O(near items): only the sky/fog/light, camera, runner, spawned instances and the camera-clearance uniform update each frame.

**Layout — Phase 2.1 r2 additions** (`(+)` new, `(~)` extended; slice tags = roadmap r2)

```
/Users/sai/Workspace/her0
├── docs/evidence/phase2_1/              (+) rootcause_v2.json before/*.png after/*.png budget.json cost.json glb_validation.json model_check_claude-opus-5-5.json pr_body.md   [S1d S5a S5b SM]
├── data/textures/<slug>/   (+, r1)      ground_v<N>_<sha8>.jpg frontage_*.jpg
├── data/exports/<slug>/                 (+) street_<sha8>.glb  street_<sha8>.manifest.json  street_<sha8>.xodr   (gitignored, content-addressed)   [S10]
├── src/hero/
│   ├── export/                          (+) THE SHARED MESH BUILDER AND EXPORTERS (pure except store/pipeline)
│   │   ├── __init__.py mesh_types.py manifest_types.py gameplay_types.py   dataclasses/Pydantic contracts, load_gameplay()       [S1a]
│   │   ├── gameplay.json  schemas/{manifest,gameplay}.schema.json         rules as data; generated JSON Schemas                  [S1a]
│   │   ├── frame.py  mesh_ground.py  mesh_frontage.py  mesh_furniture.py  atlas.py  mesh_builder.py                               [S6]
│   │   ├── glb.py  glb_validate.py                                                                                                  [S7]
│   │   ├── manifest.py  xodr.py                                                                                                     [S8]
│   │   ├── gameplay.py  gameplay_ref.py                                                                                             [S9]
│   │   └── store.py  bundle.py  pipeline.py                                                                                         [S10]
│   ├── model/ scene_v3.py street_plan_types.py gap_types.py texture_types.py (+) [S1a]   street_plan.py compile.py dynamic.py gaps.py frontage.py ground.py furniture.py describe.py catalogue.py coverage.py (+/~) [S1b]
│   │          scene_audit.py (+) [S1d]   improve.py (+) [S4a]   report.py (~) [SM]
│   ├── imagery/ bake.py tints.py (+) [S1e]   plan.py (~) [S4a]
│   ├── fusion/ recompile.py (+) publish.py assets.py (~) [S1c]   runner.py steps.py state.py (~, model plumbing only) [SM]
│   ├── llm/ models.json models.py (+) pricing.py prices.json budget.py (~) [SM]
│   ├── api/ public_export.py admin_export.py (+) [S10]   admin_models.py (+) admin.py (~) [SM]   admin_improve.py admin_imagery.py (+/~) [S4a]   public.py media.py admin_model.py (~) [S1c]
│   ├── db/ models.py repo.py model_repo.py (~) migrations/versions/0003_scene_schema.py (+) [S1c]
│   └── tools/ recompile.py (+) [S1c]  audit_scene.py export_recorded.py (+) [S1d]  validate_glb.py (+) [S7]  make_spawner_vectors.py (+) [S9]  vendor_three.py (~) [S2]  check_structured.py (~) [SM]
├── frontend/
│   ├── vendor/ three.module.js (existing)  three/examples/jsm/loaders/GLTFLoader.js  three/examples/jsm/utils/BufferGeometryUtils.js  VENDOR.json (+)   [S2]
│   ├── play/index.html (~ import map) css/game.css (~)                                                                                [S2]
│   ├── js/game/ main.js glb_loader.js track_manifest.js play_data.js atmosphere.js camera_rig.js budget.js (+/~)   about.js scene_builder.js path.js (~ legacy kept)   [S2]
│   ├── js/game/ gameplay.js spawner.js dynamic_obstacles.js (+)   runner.js hud.js results.js controls.js (~)                          [S3]
│   └── admin/{index,segment}.html (~)  js/admin/ improve.js upload_form.js placement_map.js export_button.js (+) imagery_form.js segment_view.js segments.js (~)   css/admin.css (~)   [S4b]    js/admin/model_select.js (+) [SM]
├── README.md  (~) sections "Add to an existing model", "Using the street in Unity / Unreal / Godot"                                      [S12]
├── spec/portability.md                      the contract (this phase's spec)
└── tests/
    ├── fixtures/export/contract_curved/ (+) stand-in bundle [S1a]   fixtures/export/baseline_hashes.json (+) [S10]   fixtures/portability/spawner_vectors*.json (+) [S9]
    ├── fixtures/plan/{curved,bellandur}.plan.json (+) [S1b]   fixtures/scene_v3/*.json (+)   fixtures/recorded/** (+)   fixtures/bake/** (+)
    └── unit|integration|e2e: per slice in the roadmap
```

**Contracts r2** (names/signatures fixed; bodies belong to the owning slice; all pure unless noted)

```python
# hero.model.street_plan / compile  [S1b]
@dataclass(frozen=True)
class CompileResult: scene: SceneV3; plan: StreetPlan; gaps: GapReport
def plan_street(model: CanonicalModel, *, observations: Mapping[str, Observation], fidelity: FidelityMap, gaps: GapReport,
                textures: TextureRefs | None, gameplay: GameplayV1) -> StreetPlan
def compile_scene(model, *, observations, assets, tile, fidelity, slug_name, city, textures=None, gameplay: GameplayV1 | None = None) -> CompileResult   # gameplay None ⇒ load_gameplay()

# hero.export.mesh_types / mesh_builder  [S1a types, S6 builder]
@dataclass(frozen=True)
class MeshNode:     name: str; material: str; positions: tuple[float, ...]; normals: tuple[float, ...]; uvs: tuple[float, ...] | None
                    colors: tuple[int, ...] | None; indices: tuple[int, ...]; extras: dict          # positions mm-rounded floats; colors RGBA uint8
@dataclass(frozen=True)
class MeshMaterial: name: str; base_color: tuple[float, float, float, float]; texture: str | None; metallic: float = 0.0; roughness: float = 1.0
@dataclass(frozen=True)
class MeshImage:    name: str; mime: Literal["image/jpeg"]; data: bytes; size_px: tuple[int, int]
@dataclass(frozen=True)
class StreetMesh:   nodes: tuple[MeshNode, ...]; materials: tuple[MeshMaterial, ...]; images: tuple[MeshImage, ...]; asset_extras: dict; stats: MeshStats   # nodes in the order of spec/portability.md §3
def build_street_mesh(model: CanonicalModel, scene: SceneV3, *, observations: Mapping[str, Observation], data_dir: Path,
                      plan: StreetPlan | None = None, textures: TextureRefs | None = None) -> StreetMesh
    # deterministic: no clock/random/dict-order/hash(); sorts everything; rounds to 1 mm; textures None ⇒ reads the baked files named in plan.ground_tex / frontage; missing file ⇒ procedural atlas from plan.ground_tex.procedural_seed

# hero.export.glb  [S7]
def write_glb(mesh: StreetMesh) -> bytes                     # canonical bytes; same mesh ⇒ same bytes
def read_glb(data: bytes) -> pygltflib.GLTF2                 # via pygltflib
def validate_glb(data: bytes) -> list[str]                   # [] = ok; checks listed in portability.md §3 + roadmap r2

# hero.export.manifest / xodr  [S8]
def build_xodr(model: CanonicalModel, plan: StreetPlan, *, name: str) -> bytes
def parse_xodr(data: bytes) -> XodrSummary                   # reference polyline, strips per laneSection, objects
def build_manifest(model, scene, plan, mesh: StreetMesh, *, glb_sha256: str, glb_bytes: int, xodr_sha256: str, gameplay: GameplayV1, built_with: dict) -> ManifestV1
def manifest_bytes(m: ManifestV1) -> bytes                   # compact canonical JSON

# hero.export.gameplay / gameplay_ref  [S1a loader, S9 reference]
def load_gameplay(path: Path | None = None) -> GameplayV1; def gameplay_sha256(path: Path | None = None) -> str
def fnv1a32(data: bytes) -> int; class Mulberry32: def __init__(self, seed: int); def next(self) -> float
@dataclass(frozen=True)
class SpawnInput: slug: str; seed: int; length_m: float; density: float; type_weights: Mapping[str, float]
                  footpath_missing: tuple[tuple[float, float], ...]; static: tuple[StaticBlocker, ...]; keepout: tuple[tuple[float, float], ...]
@dataclass(frozen=True)
class Spawned:    loop: int; row: int; lane: int; type: str; x_mm: int; variant: int          # id = f"dyn-{loop}-{row}-{lane}"
def spawn_input_from_manifest(manifest: ManifestV1 | dict, *, seed: int) -> SpawnInput
def generate_loop(gp: GameplayV1, inp: SpawnInput, loop: int) -> list[Spawned]              # spec/portability.md §7, normative

# hero.export.pipeline / store / bundle  [S10]  (IO: data_dir; no network)
@dataclass
class ExportRecord: sha8: str; mesh_sha256: str; manifest_sha256: str; xodr_sha256: str; gameplay_sha256: str; bundle_sha256: str; glb_bytes: int; triangles: int; primitives: int; files: dict[str, str]; built_with: dict
def build_and_store_exports(*, slug: str, version: int, model, scene, plan, observations, data_dir: Path, gameplay: GameplayV1 | None = None) -> ExportRecord
def load_export(slug: str, sha8: str | None) -> ExportFiles                                 # head when None; lazy build once if missing
def build_bundle(rec: ExportRecord, data_dir: Path, *, model_version: int) -> bytes        # deterministic zip incl. IMPORT.txt

# hero.llm  [SM]
@dataclass(frozen=True)
class ModelPrice: input_usd_per_mtok: float; output_usd_per_mtok: float; source_url: str | None; verified_on: str | None
def price_for(model: str) -> ModelPrice                      # raises UnknownModelPrice (never silently 0)
def available_models(settings) -> list[ModelOption]; def resolve_model(requested: str | None, settings) -> ResolvedModel   # raises ModelNotAllowed(reason)
```

```js
// S2 ↔ S3: see spec/roadmap.md → "S2 ↔ S3 integration point (amends r1)". S2 additionally exports (glb_loader.js):
//   loadStreet({ glbUrl, manifestUrl, renderer }) → { root: THREE.Group (one copy, shared geometry), manifest, stats: {primitives, triangles}, dispose() }
//   cloneCopy(street, k) → THREE.Group placed at the end pose of copy k−1 (portability.md §2 "Loop seam"); node lookup by NAME only
//   installClearance(street, camera) → per-frame uniform updater (dithered discard inside 1.5→0.8 m of the camera on mat_flat/mat_frontage/mat_ground-derived materials)
```

**New settings (r2; owner S1c for `config.py`/`.env.example`):** `HERO_EXPORT_MAX_GLB_MB` (`2.5`; a mesh over it fails the export with `export_too_large` — the previous scene stays), `HERO_GLTF_VALIDATOR` (`auto|off`, default `auto`; only the integration test and `validate_glb --khronos` read it). No setting for models (`models.json` + `HERO_VISION_MODEL`).
**Observability (r2):** `event="export.built"` (`slug`, `version`, `sha8`, `glb_bytes`, `triangles`, `primitives`, `textures`, `latency_ms`), `event="export.served"` (`slug`, `file`, `sha8`, `status` 200|304), `event="model.chosen"` (`run_id`, `model`, `source` request|default, `input_usd_per_mtok`, `output_usd_per_mtok`); `llm.call` already carries `model`.
**Conventions (r2):** everything under `hero.export` except `store/pipeline/bundle` is pure and deterministic — no clock, no randomness, no `set`/`dict` iteration order dependence, no Python `hash()`, floats rounded at serialisation (1 mm positions, 3 dp manifest, 6 dp lat/lon), integers for PRNG; a test runs the full build in two subprocesses with different `PYTHONHASHSEED` and compares sha256s.

## Phase 2.1 r1 — architecture additions *(realism + add-to-model; partly VOID, see the box above)*

**Data flow changes.** (1) *Compile* gains two inputs and one output: `compile_scene(model, …, textures: TextureRefs | None)` → `SceneV3` and, separately, `analyze_gaps(...)` → `GapReport`
(both pure). (2) *Bake* (`hero.imagery.bake.bake_textures`) is the only new step that touches pixels: it runs inside `publish` (fusion) and inside `recompile`, **after** the model is final and **before** compile, reads
stored observation files from the data dir, writes content-addressed JPEGs under `data/textures/<slug>/`, and returns `TextureRefs`; no network, no LLM, deterministic (same inputs ⇒ same bytes ⇒ same `sha8`).
(3) *Recompile* (`hero.fusion.recompile.recompile_segment`) = load head model + observations → bake → compile → gaps → write derived columns; the CLI (`hero.tools.recompile`) and `POST …/regenerate` call it; both are guarded so any attempt to
construct a Google/Anthropic client during recompile raises (tests monkeypatch the constructors to fail). (4) Game: `GET scene` → `buildWorld` (terrain, frontage, furniture, atmosphere — S2 modules) + `Runner` with the seeded spawner and `createDynamicLayer` (S3 modules); `main.js` is the single integration point.

```
 stored model vN ─┐
 observations ────┼─▶ bake_textures ─▶ TextureRefs ─┐
 (files on disk)  │                                 ├─▶ compile_scene ─▶ SceneV3 ─▶ model_versions.scene_json / segments.scene_json
                  └─▶ analyze_gaps ─────────────────┘                └─▶ gaps_json
 GET /api/segments/{slug}/scene ─▶ browser: terrain + frontage + furniture + sky/fog  (+ spawner → dynamic layer per run)
```

**Stack additions (all `> Assumed:`).** Pillow only for baking (no numpy; the strips are ≤ 1024 px per side, one pass of `Image.transform(MESH)` rectification + `Image.paste` with linear alpha masks — < 3 s for 100 m on the dev Mac); no new Python or JS dependencies; the spawner PRNG is a hand-written `mulberry32` (no library) so JS and Python-free tests agree;
JS unit tests run **in the browser through Playwright** (`page.evaluate(() => import('/js/game/spawner.js'))` against the live-server fixture), not Node, keeping one toolchain.

**[r1 table — VOID: use "Render budget r2" above; the numbers are the same, the "how it is met" column is not]** Render budget (r1) (the numbers every slice and e2e test uses; measured from `renderer.info` after a frame, phone viewport 390×844, DPR capped at 2, three chained copies of the segment alive).**

| metric | budget | how it is met |
|---|---|---|
| draw calls per frame | **≤ 60** | per copy (×3): ground strip 1, kerb 1, frontage 2 (textured wall + flat caps/returns), furniture instanced 6 (tree trunks, canopies, poles, boxes [bin/bollard/bus-stop/signboard], railings, decals), tokens 1 ≈ 11 → 33; plus dynamic layer ≤ 8; runner 2; AO blob 1; sky 0 (`scene.background`); frustum culling drops copies behind the camera ⇒ typical 40–48 |
| triangles per frame | **≤ 60 000** | ground+kerb ≈ 700/copy (stations every 2.5 m, ≤ 12 tris per station), frontage ≈ 1 000/copy, furniture ≤ 200 items × ≤ 60 tris, dynamic layer ≤ 6 000 total, runner/AO < 500 |
| textures alive | **≤ 8** | ground strip 1, frontage 2, furniture/decal atlas 1 (≤ 512²), procedural pattern atlas 1 (≤ 256²), AO blob 1, sky 1, spare 1 (token/HUD) |
| texture memory | **≤ 24 MB** | `Σ w·h·4·(4/3 if mipmapped)`: ground ≤ 1024×1024 (4 MB), frontage 2 × 1024×256 (2 MB), atlases (1.4 MB), sky/AO small; JS caps every loaded image to the sizes in the contract |
| fps (manual) | ≥ 30 on a mid-range Android phone over LAN | not asserted in CI (software GL); `fps_avg` is recorded in evidence only |
| scene payload | ≤ 250 kB JSON for a 100 m street (≤ 400 kB at 200 m) | asserted on the recorded fixtures |
| bake output | ≤ 350 kB per street (JPEG q82) | asserted in `test_bake` |

The renderer implements the budget by: merged `BufferGeometry` per material per copy (terrain, kerb, frontage), `InstancedMesh` for furniture and dynamic obstacles, shared geometry/material objects across copies (copies are `Group.clone(true)`-style transforms of the same meshes), `MeshLambertMaterial`/`MeshBasicMaterial` only, no shadows/post-processing, flat shading, `antialias: false`, and per-frame work ≤ O(near items) (furniture fade and obstacle matrices updated only inside a ±30 m window).

**[r1 layout — VOID for the `js/game` lines, `compile.py → SceneV3`, `scene_v3.py ... to_v2`, and every slice tag; the `model/`, `imagery/bake`, `fusion/recompile`, `db`, `tools` file lists remain as r2's tree re-tags them]** Layout — Phase 2.1 additions (r1) (`(+)` new, `(~)` extended in place; slice tags are the owners from `spec/roadmap.md`; the existing tree is otherwise unchanged)

```
/Users/sai/Workspace/her0
├── docs/evidence/phase2_1/              (+) rootcause_v2.json  before/*.png  after/*.png  budget.json  cost.json  pr_body.md   [S1d, S5a, S5b]
├── data/textures/<slug>/                (+) ground_v<N>_<sha8>.jpg  frontage_{modelled,opposite}_v<N>_<sha8>.jpg   (gitignored, content-addressed)
├── src/hero/
│   ├── model/
│   │   ├── scene_v3.py, gap_types.py, texture_types.py   (+) SceneV3 (+ to_v2 downlevel), GapReport, TextureRefs                       [S1a]
│   │   ├── scene_v2.py                      (~) load_scene dispatch only (3 → SceneV3)                                            [S1a]
│   │   ├── compile.py                       (~) → SceneV3 (compiled_by hero.model.compile/2)                                      [S1b]
│   │   ├── dynamic.py (+) runtime_of(), dynamic_context_summary()   gaps.py (+) analyze_gaps()   frontage.py (+) plan_frontage()
│   │   │   ground.py (+) plan_ground()   furniture.py (+) plan_furniture(), MAX_SIZE_M                                              [S1b]
│   │   ├── catalogue.py, coverage.py, describe.py (~) runtime column; frontage-side coverage helper; interpolation sentences      [S1b]
│   │   ├── scene_audit.py                   (+) audit_scene(scene) → AuditReport (pure; reads v2 or v3 JSON)                       [S1d]
│   │   └── improve.py                       (+) plan_improvements(), estimate_cost(), cost constants                               [S4a]
│   ├── imagery/
│   │   ├── bake.py, tints.py                (+) bake_textures(), entity tints                                                       [S1e]
│   │   └── plan.py                          (~) ImageryPlan.targets + plan_images(targets)                                          [S4a]
│   ├── fusion/ recompile.py (+)  publish.py, assets.py (~)                                                                          [S1c]
│   ├── api/ admin_improve.py (+)  admin_imagery.py (~)  public.py, media.py, admin_model.py (~)                                     [S4a / S1c]
│   ├── db/ models.py, repo.py, model_repo.py (~)  migrations/versions/0003_scene_schema.py (+)                                      [S1c]
│   └── tools/ recompile.py (+)  audit_scene.py (+)  export_recorded.py (+)                                                          [S1c / S1d]
├── frontend/
│   ├── play/index.html, css/game.css        (~)                                                                                      [S2]
│   ├── js/game/ terrain.js frontage.js furniture.js atmosphere.js tex.js budget.js camera_rig.js (+)  scene_builder.js main.js about.js path.js (~)   [S2]
│   ├── js/game/ spawner.js dynamic_obstacles.js (+)  runner.js hud.js results.js controls.js (~)                                      [S3]
│   ├── admin/segment.html (~)  js/admin/ improve.js upload_form.js placement_map.js (+)  imagery_form.js segment_view.js (~)  css/admin.css (~)   [S4b]
└── tests/
    ├── fixtures/recorded/**  (+) bellandur scene v2 (P0), model, observations, ≤ 4 MB images                                         [P0, S1d]
    ├── fixtures/scene_v3/{contract_curved,curved,bellandur}.json (+)  fixtures/bake/** (+)                                           [S1a, S1b, S1e]
    ├── e2e/visual/{capture,judge}.py (+)                                                                                              [S5a, S5b]
    └── unit|integration|e2e: new/updated files listed per slice in the roadmap
```

**Contracts 2.1 (r1; `compile_scene -> SceneV3`, `SceneV3`+`to_v2`, and the `recompile` slice tags are superseded by "Contracts r2"; bake/improve/audit/RecompileResult signatures below stand — `RecompileResult` gains `export_sha8: str | None`)** (names and signatures are fixed; bodies belong to the owning slice; all pure unless noted)

```python
# hero.model.texture_types / gap_types / scene_v3  [S1a]  (fields: spec/data.md)
class SceneV3(BaseModel): ...                         # extra="forbid"; validators: frontage tiling, bin_source lengths, spawn_hints keys+sum, furniture types
def to_v2(scene: SceneV3) -> SceneV2
def load_scene(data) -> Scene | SceneV2 | SceneV3     # hero.model.scene_v2.load_scene (dispatch 1|2|3)

# hero.model.dynamic  [S1b]
def runtime_of(e: Entity, footpath_band: tuple[float, float]) -> Literal["fixed", "dynamic_context"]
def dynamic_context_summary(entities: Sequence[Entity], bands: ..., length_m: float) -> DynamicContext

# hero.model.gaps  [S1b]
def analyze_gaps(model: CanonicalModel, observations: Mapping[str, Observation], fidelity: FidelityMap, textures: TextureRefs | None) -> GapReport

# hero.model.compile  [S1b]
def compile_scene(model, *, observations, assets, tile, fidelity, slug_name, city, textures: TextureRefs | None = None) -> SceneV3
#   `textures=None` ⇒ ground_tex.mode="procedural", frontage.texture=null, every ground/frontage bin "procedural"/"interpolated"/"prior" — valid, never blank (used by unit tests and when baking fails)

# hero.imagery.bake  [S1e]   (IO: reads data_dir/<file_path>, writes data_dir/textures/<slug>/; no network)
def bake_textures(*, slug: str, version: int, model: CanonicalModel, observations: Mapping[str, Observation], data_dir: Path,
                  mpp: float = 0.1, jpeg_quality: int = 82) -> TextureRefs      # never raises for missing/corrupt images: that bin becomes "procedural"/"none" and a warning is returned via .warnings (list[str])
# hero.imagery.tints  [S1e]
def entity_tints(entities: Sequence[Entity], observations: Mapping[str, Observation], data_dir: Path) -> dict[str, str]     # median colour of the central 50 % of the best crop, saturation clamped to [0.1, 0.8], value to [0.2, 0.9]

# hero.fusion.recompile  [S1c]   (IO: DB + data dir; guarded against network)
@dataclass
class RecompileResult: slug: str; version: int; scene_schema_before: int | None; scene_schema_after: int; unchanged: bool; version_created: bool
                       interpolated_m: float; dynamic_excluded: int; blank_bins: int; missing_extractions: list[str]; warnings: list[str]; cost_usd: float   # always 0.0
def recompile_segment(slug: str, *, remerge: bool = False, dry_run: bool = False, data_dir: Path | None = None) -> RecompileResult
def recompile_all(*, remerge: bool = False, dry_run: bool = False) -> list[RecompileResult]       # every segment that has a model version; legacy-only segments: lazily imported (as the admin model endpoint does), then compiled

# hero.model.improve  [S4a]
def plan_improvements(*, fidelity: FidelityMap, gaps: GapReport, observations: Sequence[Observation], ref: ReferenceLine,
                      target_level: int, max_images: int, max_usd: float, s_range: SRange | None) -> ImprovePlan
@dataclass
class ImprovePlan: plan: ImageryPlan; images: list[PlannedImageView]; intervals_targeted: list[dict]; est: CostEstimate; trimmed: bool; will_exceed_budget: bool; note: str
def estimate_cost(new_sv: int, new_sat: int) -> CostEstimate          # SV 0.007, sat 0.002, claude 0.006/image + 0.03

# hero.model.scene_audit  [S1d]
def audit_scene(scene: dict) -> AuditReport         # v2 or v3 JSON; AuditReport: footpath_intervals[], strips_without_texture[], frontage_coverage{side: [{bin, covered_m, hole: bool}]}, hole_bins{side: [bin]}, totals
```

```js
// S2 ↔ S3 integration contract: see spec/roadmap.md → Phase 2.1 → "Integration point". Frozen names: Runner(scene,{seed,runNumber}), runner.startRun(), runner.activeObstacles(),
// createDynamicLayer(THREE,{scene,track,runner}) → {group,update,reset,dispose}, window.__hero.dynamic. Shared read-only dependency for both: frontend/js/game/path.js Track API
// (world(s,lat), laneLat(s,lane), footpathAt(s), stripAt(s,lat), applyCopy(p,k), length) — S2 may ADD methods, never change these.
```

**New settings** (`.env.example` lists them; owner S1c for `config.py`/`.env.example` edits — the only edit of those files in this phase): `HERO_GROUND_STRIP_MPP` (`0.1`, bake resolution; widened automatically so the longest side ≤ 1024 px), `HERO_BAKE_JPEG_QUALITY` (`82`), `HERO_UPLOAD_MAX_DISTANCE_M` (`60`, was a constant). `HERO_ALLOW_ONE_LIVE` (`0`) is read by the optional live test only.

**Observability.** New log events (JSON lines, same envelope): `event="texture.baked"` (`slug`, `version`, `ground_bins_by_source`, `frontage_bins_by_source`, `bytes`, `latency_ms`), `event="scene.compiled"` (`slug`, `version`, `schema`, `interpolated_m`, `dynamic_excluded`, `blank_bins`, `latency_ms`),
`event="scene.recompiled"` (`slug`, `version`, `from_schema`, `to_schema`, `unchanged`, `cost_usd=0.0`), `event="improve.plan"` (`slug`, `new`, `cached`, `est_usd`), `event="observation.ingested"` gains `how` (`s_side|latlon`). The recompile CLI prints one summary line per segment.

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
