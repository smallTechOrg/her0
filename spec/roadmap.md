# #hero — Roadmap

## What

**#hero** is a mobile-browser endless runner (between Temple Run and Subway Surfers) whose
course is a **real 50–100 m stretch of street**, reconstructed from satellite imagery,
street-level panoramas and open map geometry into a low-fidelity but recognisably-real 3D
corridor. The three lanes are the real cross-section (footpath / road edge / road); hazards
are the real civic problems detected in the imagery (broken footpath, parked vehicles,
potholes, poles). After a crash the player gets a walkability score card for that street
and a map of the detected hazards at their real coordinates. Companion to smallTech's "#"
product line; purpose: awareness of walkability and civic issues.

## Who

- **Players** — citizens and civic campaigners, at home, on a phone, 2-minute sessions. No
  accounts. They pick a street from a gallery (later: by GPS) and run it.
- **Admin / creator** — the one person who generates streets (password-protected area), watches
  the generation pipeline, controls spend, and (later) corrects what was detected.

## Primary journey (the thing Phase 1 must do end-to-end, real)

1. Admin logs into the admin area with the password, creates/selects the seed segment and
   presses **Generate**. The pipeline fetches real imagery and geometry, runs the vision and
   review passes, shows every step with timings, cost, tile count, scene JSON, top-down
   preview and review iterations, and persists the scene. A budget hard stop protects spend.
2. Player opens the site on a phone or desktop browser, picks the street from the gallery,
   runs it: swipe/arrow to change lane, jump, slide; civic tokens on good infrastructure;
   crash on a hazard.
3. Player sees score, distance, best-score status, the street's walkability score card and a
   satellite overlay of detected hazards. The run is logged; the best score per street is kept.

## Success criteria

- [ ] One real segment (default seed below) generates from real imagery with zero manual
      edits and the game is playable on it within one admin click.
- [ ] Every generation run records: inputs, every step with timing, each review iteration,
      final scene, tokens, USD cost, Google request count (and cache hits).
- [ ] Re-generating a segment makes **zero** new Google requests (all imagery cache hits).
- [ ] Cumulative generation spend never exceeds `HERO_CLAUDE_BUDGET_USD`; a run that would
      exceed it stops with a labelled `failed_budget` status before the call is made.
- [ ] Game renders at 30+ fps on a mid-range Android phone browser over LAN (manual check);
      the automated gate runs the game headless and asserts a crash + score card appear.
- [ ] Walkability card shows footpath coverage %, hazard count, crossing presence, and a
      0–100 score computed deterministically from the scene (`spec/capabilities/walkability-report.md`).
- [ ] Best score per street survives a server restart.

## Out of scope (whole roadmap, unless listed in a phase)

Mid-run hazard tagging; civic report export; chaining segments / A-to-B walks; dynamic
on-demand generation by players; avatars; Mapillary; playing in the field; accounts;
deployment; texture atlases (Phase 1 uses individual small crops — see `spec/architecture.md`).

> **Assumed:** default seed segment — Bengaluru, Indiranagar, 100 Feet Road, west footpath,
> start `12.97400, 77.64050` → end `12.97467, 77.64058` (≈75 m, heading ≈ north). Chosen because
> 100 Feet Road has dense Street View coverage and a mix of intact/obstructed footpath. The
> admin form is prefilled with it; any other endpoints can be entered.

---

## Phases of Development

Gate rules: `../patterns/phases.md`. All commands run from the repo root
`/Users/sai/Workspace/her0`. Every gate runs against the **real** Anthropic + Google APIs
(keys from `.env`) and the **production DB engine (SQLite file)** via the test DB URL.
Documented run command (also the README's): `uv run hero-serve` → `http://0.0.0.0:8711/`
(open as `http://<mac-lan-ip>:8711/` from a phone).

### Scaffold — minimal runnable skeleton

**Goal** — the layout in `spec/architecture.md` exists, boots, migrates, logs, tests green,
keys validated. No feature logic.

Slice `S0-scaffold` (single generator) owns: `pyproject.toml`, `uv.lock`, `.python-version`,
`.env.example` (adds the new vars from `spec/architecture.md`), `README.md`, `alembic.ini`,
`src/hero/{__init__,config,logging,main,app}.py`, `src/hero/api/{__init__,envelope,public,admin,media}.py`
(empty routers + envelope + `/api/health`), `src/hero/db/{__init__,session,base}.py`,
`src/hero/db/migrations/` (env + empty initial revision), `src/hero/tools/{check_keys,vendor_three}.py`,
`frontend/index.html` (placeholder shell only), `frontend/css/base.css`, `frontend/vendor/` (fetched),
`tests/conftest.py`, `tests/unit/test_health.py`.

**Gate** (all must pass, in order):

```
cd /Users/sai/Workspace/her0
uv sync
uv run python -m hero.tools.vendor_three
uv run python -m hero.tools.check_keys
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit -q
uv run hero-serve        # background; then: curl -s http://127.0.0.1:8711/api/health
```

`check_keys` must: make one minimal Anthropic call with the `anthropic-workspace-id` header
on the configured model slug (prints model + ok), one Static Maps satellite fetch (prints
bytes), one Street View **metadata** call for the seed start point (prints status; a non-OK
status is a warning, not a failure). It prints key presence as booleans only.

### Phase 1 — First real street, playable end-to-end

**Goal** — the full primary journey above on ONE real segment, first-time-right.
Capabilities: `street-generation`, `street-run`, `walkability-report` (all real).
Labelled stubs: "Use my location" (gallery), "Correct detected lines" (admin run view),
"Seed more streets" (admin). The gallery is real but will show one card.

**Independent slices** (orchestrator fans out all five in one message; only the arrows are
true dependencies):

| Slice | Owns (disjoint paths) | Depends on |
|---|---|---|
| `S1-data` | `src/hero/db/models.py`, `src/hero/db/migrations/versions/0001_phase1.py`, `src/hero/db/repo.py`, `src/hero/scene/{__init__,schema,geo,walkability,example_scene.json}.py`, `tests/unit/test_scene_schema.py`, `tests/unit/test_geo.py`, `tests/unit/test_walkability.py`, `tests/unit/test_repo.py` | scaffold |
| `S2-pipeline` | `src/hero/llm/` (client, pricing, `prices.json`, budget), `src/hero/imagery/` (google, osm, cache, annotate, preview, crops), `src/hero/pipeline/` (state, steps, review, runner, registry), `src/hero/prompts/*.md`, `src/hero/api/admin.py` (extend in place), `tests/unit/test_pricing.py`, `tests/unit/test_cache.py`, `tests/unit/test_preview.py`, `tests/integration/test_llm_client.py`, `tests/integration/test_imagery.py`, `tests/integration/test_pipeline_generate.py`, `tests/integration/test_admin_api.py` | `S1-data` (imports `hero.scene.schema`, `hero.db.models`, `hero.db.repo`) |
| `S3-public-api` | `src/hero/api/public.py` (extend in place), `src/hero/api/media.py` (extend in place), `tests/integration/test_public_api.py` | `S1-data` |
| `S4-game-ui` | `frontend/index.html` (replace placeholder), `frontend/play/index.html`, `frontend/js/gallery.js`, `frontend/js/game/*.js`, `frontend/css/game.css`, `tests/e2e/fixtures/scene_example.json`, `tests/e2e/test_play_fixture.py`, `tests/e2e/test_play_live.py` | spec only (API contract in `spec/api.md`, scene schema in `spec/data.md`); its live gate test needs `S2`+`S3` landed |
| `S5-admin-ui` | `frontend/admin/index.html`, `frontend/admin/run.html`, `frontend/js/admin/*.js`, `frontend/css/admin.css`, `tests/e2e/test_admin_live.py` | spec only; its live gate test needs `S2` landed |

Shared, owned by nobody after scaffold (extend-only via the registration seams the scaffold
leaves): `src/hero/app.py` mounts routers from `hero.api` and static dirs — slices never edit it.

**Gate** (exact, in order; one generation per pytest session, shared via the session fixture
`generated_segment` in `tests/conftest.py` — see `## Conventions`):

```
cd /Users/sai/Workspace/her0
uv run alembic upgrade head && uv run alembic current
uv run playwright install chromium
uv run pytest tests/unit tests/integration tests/e2e -q
uv run hero-serve        # background; then the rendered-UI check:
curl -s http://127.0.0.1:8711/ | grep -q 'data-hero-page="gallery"'
curl -sI http://127.0.0.1:8711/js/gallery.js | grep -q '200'
curl -s http://127.0.0.1:8711/admin/ | grep -q 'data-hero-page="admin"'
```

Plus: one JSON log line with `event="pipeline.step"` and one with `event="llm.call"` and
`cost_usd` appears on stdout during the integration run (observability gate).

**How the user tests it**

1. Open `http://<mac-ip>:8711/admin/` on the Mac. Enter the admin password. See spend
   `$0.00 / $50.00`. The seed segment "100 Feet Road, Indiranagar (west footpath)" is listed.
   Press **Generate**. The run page opens; watch steps appear with timings (fetching imagery →
   detecting layout → detecting obstacles → assembling → review 1 → review 2 → finalize), the
   satellite tile and the Street View frames, the per-pass scene graph JSON, each review
   iteration's top-down preview + critique, the final scene JSON, tokens/USD/Google requests.
   Expected: status `succeeded` in under ~4 minutes, cost under $1.50, Google requests ≤ 6.
   Press **Generate** again: Google requests shows `0 new / N cached`.
2. On a phone on the same Wi-Fi, open `http://<mac-ip>:8711/`. The gallery shows the street
   card with its satellite thumbnail and "No runs yet". Tap it. Tap to start. Swipe
   left/right to change lane, up to jump, down to slide. Real objects (parked car, pole,
   broken footpath…) appear as image billboards; collect tokens on intact footpath/crossings.
   Crash → results: score, distance, "New best", walkability card (coverage %, hazard count,
   crossing yes/no, score /100), satellite overlay with hazard markers, detected-objects list
   with confidence (low-confidence items labelled). "Run again" works. Back in the gallery the
   best score shows. Restart the server: best score still there.
3. Labelled stubs (not bugs): gallery "Use my location — coming in Phase 2" (disabled);
   admin run view "Correct detected lines — Phase 2" (disabled); admin "Seed city — Phase 2".

### Phase 2 — Seeded city, GPS pick, admin correction

**Goal** — the one-street prototype becomes a city: 5–20 seeded segments, the player can pick
"the street I'm standing on", and the admin can fix detections without re-spending.
Capabilities: `seed-city`, `gps-pick`, `admin-correction` (all real), plus per-street top-5
run history on the results screen (part of `walkability-report`, rule "history").

**Independent slices**

| Slice | Owns | Depends on |
|---|---|---|
| `P2-seed` | `src/hero/pipeline/seed.py`, `src/hero/seeds/bengaluru.json`, admin endpoints `POST /api/admin/seed*` in `src/hero/api/admin.py` (extend), `tests/integration/test_seed.py` | Phase 1 |
| `P2-nearest-history` | `GET /api/segments/nearest` + `GET /api/segments/{slug}/runs` in `src/hero/api/public.py` (extend), `src/hero/scene/geo.py` (extend: haversine), `tests/integration/test_nearest.py`, `tests/unit/test_haversine.py` | Phase 1 |
| `P2-correction-api` | `src/hero/db/migrations/versions/0002_corrections.py`, `src/hero/db/models.py` (extend: `segment_corrections`), `src/hero/scene/apply_corrections.py`, `PUT/GET /api/admin/segments/{slug}/corrections` (extend `admin.py`), `tests/integration/test_corrections.py` | Phase 1 |
| `P2-ui` | `frontend/js/gallery.js` (GPS button real), `frontend/js/game/results.js` (history), `frontend/admin/correct.html`, `frontend/js/admin/correct.js`, `frontend/js/admin/seed.js`, `tests/e2e/test_gps_live.py`, `tests/e2e/test_correction_live.py` | spec only; live tests need the three API slices |

**Gate**

```
cd /Users/sai/Workspace/her0
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration tests/e2e -q
```

**How the user tests it** — Admin: "Seed city" lists the 5+ Bengaluru segments from the seed
file with per-segment status; "Generate all pending" runs them serially with the running
total visible; stops at budget. Admin opens a run, "Correct detected lines": drag footpath
coverage ranges / move / delete objects on the satellite tile, Save → scene updated, Google
requests 0, Claude cost $0. Player: gallery shows all ready streets; "Use my location" (allow
GPS) picks the nearest within 2 km or says "No street near you yet — pick from the gallery".
Results screen shows the street's top-5 runs.
