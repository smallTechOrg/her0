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

**Phase 2 direction (playtest feedback, 2026-10-30).** The Phase-1 street was one straight line with
one fixed cross-section built from one satellite tile and three Street View frames — it did not
follow the real footpath, and every generation replaced the last. The street now becomes a
**cumulative, standardised model** (like a game asset / OpenDRIVE road): a reference line on the
real road, a cross-section that varies along it (the traced footpath), typed entities with stable
ids and per-fact evidence, fidelity levels per stretch, versions and diffs — fed by many more
images, merged (never overwritten) run after run, and described in plain language everywhere.

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
Phase-2 additions: full photo-upload UI (API and storage are real, the UI is a Phase-3 stub — **made real in Phase 2.1**);
"re-analyse everything" (re-running perception on already-fused images); rebasing a model onto a
different reference line; dual carriageways / medians modelled as two roads; photogrammetry or
mesh reconstruction; per-pixel textures (billboards and facade crops stay); real-time multi-user.

> **Assumed (legacy, Phase 1):** the original seed segment — Bengaluru, Indiranagar, 100 Feet
> Road, west footpath, start `12.97400, 77.64050` → end `12.97467, 77.64058` — used guessed
> coordinates; the annotated satellite tile showed its line cutting through houses. It is kept as
> the **legacy v1 import** (slug `100-feet-road-indiranagar-w`, model v0 `legacy`, "placement
> unverified") and is never deleted.
>
> **Assumed (Phase 2 canonical demo street):** re-resolve "100 Feet Road, Indiranagar" through the
> address flow (`POST /api/admin/segments/resolve` → the Indiranagar candidate near `12.9711, 77.6413`, road
> heading ≈ north) and create it with `from-address`, 50 m either way (100 m), footpath side `left`
> (the west side when travelling north), name "100 Feet Road, Indiranagar, canonical demo (west footpath)"
> → slug `100-feet-road-indiranagar-canonical-demo-w`. Its reference line is the OSM road centreline, so it lies on the
> road by construction; the placement is still verified on the annotated tile before the first generation (P0, below).

---

## Phases of Development

Gate rules: `../patterns/phases.md`. All commands run from the repo root
`/Users/sai/Workspace/her0`. Every gate runs against the **real** Anthropic + Google APIs
(keys from `.env`) and the **production DB engine (SQLite file)** via the test DB URL.
Documented run command (also the README's): `uv run hero-serve` → `http://0.0.0.0:8711/`
(open as `http://<mac-lan-ip>:8711/` from a phone).
**Phase-2 gate rule (cost control):** real services at the gate (Anthropic, Google Static Maps / Street View, Nominatim; Overpass is
flaky — degrade, never fail), but the *entire* gate may make at most **two** real full generations; everything deterministic
is proven with recorded fixtures (`ReplayLLM`, recorded Google transport). Never port 8711 (dev server) — the boot gate uses 8713, tests use free ports.

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
   `$0.00 / $50.00`. The seed segment is NOT pre-inserted: the "New segment" form is prefilled
   with "100 Feet Road, Indiranagar (west footpath)" — press **Create segment** (prefilled), it
   appears in the list, then press **Generate**. The run page opens; watch steps appear with timings (fetching imagery →
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
3. Labelled stubs (not bugs): gallery "Use my location — coming in Phase 3" (disabled; was "Phase 2" when
   Phase 1 shipped); admin run view "Correct detected lines — Phase 3" (disabled); admin "Seed city — Phase 3".

### Phase 2 — Cumulative street model + dense imagery

**Goal** — the street stops being a one-shot straight line and becomes cumulative knowledge: on the **real** road
(`100 Feet Road, Indiranagar` re-created through the address flow), with a **dense, cached, cost-guarded** photo set, a
**canonical versioned model** whose footpath is *traced* so the runner follows it, **fidelity per stretch**, **evidence on
every claim**, **v1 → v2 diffs**, and descriptions everywhere.
Capabilities (all real): `street-model`, `imagery-acquisition`, `model-fusion`, `model-inspection`, `street-description`,
plus the extended `street-run` (curved corridor, About panel) and `walkability-report` (v2 scene). `street-generation` is superseded
(legacy behaviour retained for the imported v1 segment). Labelled stubs: photo-upload UI ("Upload a photo — Coming in Phase 3"), and the
Phase-3 stubs (GPS, seed city, correct lines — captions renamed to "Coming in Phase 3").

**Smallest user-testable win (the journey)** — admin creates (or opens) the 100 m *100 Feet Road* demo street from the address,
checks the line is on the road → **Generate** with dense forward/back imagery → model v1 with a **fidelity/coverage map**, entities with
**evidence**, and a **run report** → play it and the runner **follows the traced footpath** → **Add more imagery** (left/right headings) →
Generate again → model v2 with a **diff**, higher fidelity on the stretches that got new images, and a report that explains the change →
Generate a third time with nothing new → a free **no-op**.

**Phase-2 success criteria**
- [ ] The demo street's reference line comes from OSM road geometry (`source ∈ {osm_nominatim, osm_overpass}`), snaps ≤ 25 m from the geocoded centre, has length 100 ± 3 m and a mean bearing within 25° of north; the annotated placement image (`docs/evidence/placement.png`) shows it on the road (orchestrator confirms by viewing it).
- [ ] Run 1 (forward+back @ 10 m): ≥ 12 distinct Street View observations (pano dedupe and coverage gaps may remove some of the 22 planned) with capture dates, plus the overview and ≥ 2 close-up crops, stored; `ceil(L/5)` fidelity bins, ≥ 80 % of them L1 and **none** L2 (no direct side views yet); ≥ 5 entities, each with a non-empty description and ≥ 1 evidence item; the profile has a modelled-side footpath strip over ≥ 50 % of the length with width in [0.8, 5] m; Anthropic cost ≤ the run cap ($1.00) from `llm_calls`.
- [ ] Run 2 (+ left/right): version 2 with `parent_version = 1`; every v1 entity id is still in v2 (state may change, never missing); Google requests for everything run 1 already stored are cache hits (`new ≤ planned − run-1 images`, `cached ≥ run-1 new images`); overall fidelity score rises by ≥ 0.5 and ≥ 80 % of bins are L2; `diff.fidelity.improved` non-empty; ≥ 1 entity has ≥ 2 supporting observations; Anthropic cost ≤ $1.00.
- [ ] Run 3 (nothing new): `version_created=false`, $0.00 Anthropic, 0 new Google requests.
- [ ] Total real Anthropic spend of the phase gate (app's own `llm_calls`) is reported and ≤ $2.50; exactly two full generations occur in the gate.
- [ ] The Scene v2 plays: on the curved fixture the runner stays on the footpath strip (e2e), existing v1 behaviour intact, ≥ 30 fps manual check on a mid-range phone.

**Independent slices** (disjoint owned paths; the orchestrator fans out each *wave* in one message; arrows are TRUE dependencies).

| Slice | Owns (disjoint paths) | Depends on |
|---|---|---|
| `S1a-contract` | `src/hero/model/{__init__,types,batch,catalogue,refline,ids,scene_v2}.py`; `src/hero/db/{models.py,repo.py,model_repo.py}`; `src/hero/db/migrations/versions/0002_street_model.py`; `src/hero/config.py`; `.env.example`; `pyproject.toml` (pytest `live` marker only); `src/hero/llm/defaults.py`; `src/hero/api/__init__.py` (router auto-discovery); `src/hero/tools/load_fixture_street.py` (CLI + `seed_fixture_segment(slug)`); `tests/conftest.py`; `tests/fixtures/{__init__,street_fixtures}.py`; `tests/unit/{test_model_types,test_refline,test_scene_v2_schema,test_model_repo,test_config_phase2,test_migration_0002,test_repo}.py` | scaffold + Phase 1 |
| `S1b-geometry` | `src/hero/model/{roadclip,projection,priors}.py`; `tests/unit/{test_roadclip,test_projection,test_priors}.py` | `S1a` types/refline (code against `spec/architecture.md` → Contracts) |
| `S1c-merge` | `src/hero/model/{calibrate,merge,diff}.py`; `tests/unit/{test_calibrate,test_merge,test_merge_properties,test_diff}.py` | `S1a` types/batch |
| `S1d-compile` | `src/hero/model/{coverage,fidelity,compile,describe,report,legacy}.py`; `tests/unit/{test_coverage,test_fidelity,test_compile,test_describe,test_report,test_legacy_import}.py`; `tests/fixtures/make_model_fixtures.py`; `tests/e2e/fixtures/model_fixture/**` and `tests/e2e/fixtures/scene_curved_v2.json` (generated, committed) | `S1a`; fixture generation uses `S1b` (projection) and `S1c` (merge) |
| `S2-acquisition` | `src/hero/imagery/{google,roads,plan,acquire,rectify,noimagery,annotate}.py`; `src/hero/api/{admin_imagery,media}.py`; `tests/unit/{test_plan,test_rectify,test_noimagery,test_roads}.py`; `tests/integration/{test_imagery,test_acquire,test_admin_imagery_api}.py`; `tests/fixtures/google/**` | `S1a` (refline, model_repo, config) |
| `S3a-llm` | `src/hero/llm/{client,anthropic_client,pricing,budget,errors}.py`, `src/hero/llm/prices.json`; `src/hero/tools/{check_keys,check_structured}.py`; `tests/unit/{test_llm_structured,test_pricing}.py`; `tests/integration/test_llm_client.py` | `S1a` (config) |
| `S3b-perception` | `src/hero/fusion/{__init__,schemas,views,convert,render,critique}.py`; `src/hero/prompts/{perceive_overhead,perceive_streetlevel,critique_model}.md`; `tests/unit/{test_fusion_schemas,test_views,test_convert,test_render}.py`; `tests/fixtures/extractions/**`; `tests/fixtures/replay_llm.py` | `S1a`; calls `S1b` projection + `S1d` coverage + `S2` rectify (signatures in Contracts) |
| `S3c-orchestrator` | `src/hero/fusion/{state,steps,runner,publish,assets}.py`; `src/hero/pipeline/registry.py`; `src/hero/api/admin.py`; `src/hero/tools/{import_legacy,create_demo_street}.py`; `tests/unit/test_fusion_publish.py`; `tests/integration/{test_fusion_run,test_admin_api,test_pipeline_generate}.py` | **`S1a`–`S1d`, `S2`, `S3a`, `S3b` landed** (wires them) |
| `S4-game` | `frontend/index.html`, `frontend/play/index.html`, `frontend/js/gallery.js`, `frontend/js/game/*.js` (adds `path.js`), `frontend/css/game.css`; `tests/e2e/{test_play_fixture,test_play_curved,test_play_live}.py` | spec only (contract `spec/data.md` → Scene v2, `spec/ui.md`); e2e needs `S1d` fixtures |
| `S5-admin-ui` | `frontend/admin/{index,run,segment}.html`, `frontend/js/admin/*.js` (adds `segment_view,coverage,entities,versions,report,diff,imagery_form`), `frontend/css/admin.css`; `tests/e2e/{test_admin_live,test_admin_model}.py` | spec only (`spec/api.md`, `spec/ui.md`); e2e needs `S1a` fixtures + `S1d` fixture data + `S6` API |
| `S6-api` | `src/hero/api/{public,admin_model}.py`; `tests/integration/{test_public_api,test_admin_model_api}.py` | `S1a`; fixtures from `S1d` |
| `S7-live-gate` | `tests/live/{conftest,test_phase2_generation,test_phase2_ui}.py`; `docs/evidence/**`; verifies `README.md` | everything landed |

**Waves.** W1: `S1a` alone (the schema/interface contract; small, fast). W2, all concurrently: `S1b S1c S1d S2 S3a S3b S4 S5 S6` (they code against the signatures in
`spec/architecture.md` → Contracts; each slice's own unit tests run as soon as `S1a` is merged; cross-slice integration tests wait for their listed dependency). W3: `S3c`. W4: `S7`,
P0, the gate, then the human test-handoff. Shared files nobody else edits: `src/hero/app.py` (scaffold), everything owned above exactly once.
Shared fixture-directory contract (written by `S1d`'s generator, read by `S1a`'s `hero.tools.load_fixture_street.seed_fixture_segment(slug)`, which the `seeded_fixture_segment` conftest fixture and the `load_fixture_street` CLI both call): `tests/e2e/fixtures/model_fixture/{manifest.json, observations.json, images/*.jpg, model_v0..v2.json, scene_v0..v2.json, fidelity_v0..v2.json, diff_v1..v2.json, summary_v0..v2.txt, report_run1.json, report_run2.json, extractions.json}` — a curved reference line (a 25°+ arc over 100 m), a footpath that narrows, shifts and breaks, two versions, one no-op run report; slug `fixture-curved-street`.

**Pre-gate steps (P0, orchestrator, not slices)**
1. `uv run python -m hero.tools.check_keys` → `result: PASS`; `uv run python -m hero.tools.check_structured` → prints a table (`claude-sonnet-4-5` / `claude-sonnet-5-5` × `forced_tool | auto_tool | json_text`: ok/fail + cost). **If `claude-sonnet-5-5` passes `auto_tool` or `json_text` on the real `StreetExtraction` schema, set `DEFAULT_VISION_MODEL` and `DEFAULT_REVIEW_MODEL` to `claude-sonnet-5-5` in `src/hero/llm/defaults.py`**, update README's model section, and note the result (date, mode, cost) in `NOTES.md`; otherwise leave `claude-sonnet-4-5` and note the EOL (2026-11-30) in `NOTES.md`.
2. Create the demo street in the **test** DB happens inside the live fixture; for the dev DB the orchestrator runs `uv run python -m hero.tools.create_demo_street` (resolve → pick the candidate within 400 m of `12.9711, 77.6413` → `from-address` → `placement-preview`), **opens `docs/evidence/placement.png` with the Read tool and confirms the line lies on the road/footpath** (records the observation in `NOTES.md`), and only then proceeds to the gate.

**Gate** (exact, in order; every command from the repo root; the first failure stops the gate; tee the whole output).

```
cd /Users/sai/Workspace/her0
mkdir -p docs/evidence
# G0 environment
uv sync
uv run python -m hero.tools.vendor_three
uv run python -m hero.tools.check_keys
# G1 migrations: fresh DB AND upgrade of an existing dev DB (Phase-1 data keeps working)
rm -f data/gate_fresh.db data/gate_upgrade.db
HERO_DATABASE_URL=sqlite:///data/gate_fresh.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///data/gate_fresh.db uv run alembic current   # must print 0002 (head)
sqlite3 data/hero.db ".backup data/gate_upgrade.db"      # safe copy while the dev server may be running
HERO_DATABASE_URL=sqlite:///data/gate_upgrade.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///data/gate_upgrade.db uv run alembic current
HERO_DATABASE_URL=sqlite:///data/gate_upgrade.db uv run python -m hero.tools.import_legacy       # prints "imported N legacy segment(s)"
sqlite3 data/gate_upgrade.db "select (select count(*) from segments where scene_json is not null), (select count(*) from model_versions where kind='legacy'), (select count(*) from game_runs);"   # first two numbers equal; game_runs unchanged vs the source DB
# G2 deterministic tiers (no Anthropic; recorded fixtures; Google/Nominatim only where a test says live; Overpass failures degrade, never fail)
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run playwright install chromium
uv run pytest tests/e2e -q
# G3 live gate: AT MOST TWO real full generations (+ one free no-op run); evidence persisted
uv run pytest tests/live -q -s 2>&1 | tee docs/evidence/phase2-gate.log
# G4 boot gate on a spare port (never 8711, which the dev server owns; tests use their own free ports)
HERO_PORT=8713 uv run hero-serve        # start with the background mechanism (run_in_background), not '&'
curl -s http://127.0.0.1:8713/api/health                         # {"ok":true,"data":{"status":"ok",…}}
curl -s http://127.0.0.1:8713/ | grep -q 'data-hero-page="gallery"'
curl -s http://127.0.0.1:8713/admin/ | grep -q 'data-hero-page="admin"'
curl -s 'http://127.0.0.1:8713/admin/segment.html?slug=x' | grep -q 'data-hero-page="admin-segment"'
curl -sI http://127.0.0.1:8713/js/game/path.js | grep -q '200'
# …then stop ONLY the process started for G4 (note its pid from the background tool) and delete data/gate_fresh.db data/gate_upgrade.db
```

Rules the gate enforces: `tests/live` performs **exactly two** real full generations — run 1 `{headings: [forward, back], sv_spacing_m: 10}` and run 2 `{headings: [forward, back, left, right], sv_spacing_m: 10}` — plus a third **no-op** generate (asserts zero Anthropic calls); a module-level counter in `tests/live/conftest.py` fails the session if a third LLM-calling run is attempted. The test writes `docs/evidence/phase2-gate.json` (per run: report JSON, total Anthropic USD from the app's own `llm_calls`, Google new/cached, fidelity before→after, entity counts, calibration table) and `docs/evidence/placement.png`; the orchestrator appends the tail of `phase2-gate.log` and the total USD to `NOTES.md`. Merge, entity resolution, compile, fidelity, diff, curved-corridor geometry, cost guards are covered **only** by deterministic tests with recorded fixtures (property tests: merge idempotence, monotone fidelity under added evidence, stable ids across versions, projection round-trips on a curved reference line, compile determinism). Tests use per-process DBs (`hero_test_<pid>.db`) so slices run concurrently. Existing tests are updated to the new contracts, never deleted (stub captions "Phase 3", scene v1|v2 discrimination, the session fixture `generated_segment` moves to `tests/live/conftest.py` and now yields the v2 street after the two live generations, while integration/e2e tiers use the no-spend `seeded_fixture_segment` fixture, `test_pipeline_generate.py` rewritten onto `ReplayLLM`).

Plus (observability gate): during `tests/live` the captured stdout contains ≥ 1 JSON line with `event="pipeline.step"`, ≥ 1 with `event="llm.call"` and `cost_usd`, `event="model.merge"` and `event="model.published"` (asserted by the test from the server log file it tails).

**How the user tests it** (phone on the same Wi-Fi for the game part; costs are real: ≈ $0.3–0.6 Claude + ≈ $0.2 Google per generation)

1. `http://<mac-ip>:8711/admin/` → password. The demo street *100 Feet Road, Indiranagar, canonical demo (west footpath)* is in the list (the orchestrator created it; creating another one through the address form works the same way). Open it → **segment page**. Press **Check placement**: the overview image shows a cyan line along the real road with 10 m ticks — it must sit on 100 Feet Road, not through the buildings (compare with the old *legacy* segment, labelled "legacy · placement unverified", kept for reference).
2. On the segment page, **Add more imagery** panel: tick *forward* and *back* only, spacing 10 m → **Fetch + Generate**. The run page shows: images used (≈ 22 Street View + overview + 3 close-up crops, all "new"), steps with timings, the run report (images by heading, new objects with descriptions, fidelity before → after, cost, warnings), "Model v0 → v1". Expected: `succeeded` in about 3–5 minutes, Anthropic cost under $1.00.
3. Back on the segment page: **coverage map** all amber (L1) with the hint "needs a second viewpoint, left and right views"; click a stretch → covering photos; **entities** table (poles, parked vehicles, trees, drains… each with a sentence and confidence); click one → the supporting photos with the detection box; **version history** shows v1.
4. Phone: gallery card shows `L1 · 24 photos` (and the legacy card "legacy"); tap → the Ready overlay says "Fidelity L1 · …"; tap **About this street** (plain-language summary, what is least certain, how to improve it); run: the corridor follows the road and **the footpath lane runs along the traced footpath** (it narrows/shifts/breaks where the real one does). Optional: the gallery also lists *fixture-curved-street* ("test fixture", loaded by `uv run python -m hero.tools.load_fixture_street`) — a deliberately curved road so you can see the corridor bend.
5. Segment page → **Add more imagery** → tick *left* and *right* too → **Fetch + Generate**. The run page: ≈ 22 new images, 26 cached, "Model v1 → v2", a diff (new/changed objects, confidence changes, "L1 → L2 over 100 m"), cost box, and a plain-language summary. The coverage map turns green; the About panel now says L2; confidence of previously-seen objects rose.
6. **Generate** once more with nothing new → run page says "No change — model stays v2", $0.00, 0 new Google requests.
7. Labelled stubs (not bugs): "Upload a photo" (segment page), "Use my location", "Seed city", "Correct detected lines" — all "Coming in Phase 3".

### Phase 2.1 r2 — Realism + add-to-model, with a shared Python mesh builder, glTF/OpenDRIVE export and gameplay-as-data *(AUTHORITATIVE; approved change of plan, 2026-10-30)*

> **Reading rule.** This section (r2) is authoritative. The r1 text that follows it under "Phase 2.1 — r1" is kept only for the parts listed in **"r1 still in force"**; every other r1 block is marked `VOID (r1)` at its heading. Where r1 and r2 disagree, r2 wins.

**What changed vs r1 (user-approved).**
1. Geometry is built **once, in Python** (`hero.export.mesh_builder.build_street_mesh`), deterministic, and shipped as `street.glb`; the browser **loads that glb** with a vendored `GLTFLoader` instead of building terrain/frontage/furniture in JS. JS keeps runtime concerns only.
2. The same bundle is exported for engines: `street.glb` (glTF 2.0), `street.manifest.json`, best-effort `street.xodr` (OpenDRIVE 1.7), `gameplay.json` — contract in **`spec/portability.md`** (single home of node names, `extras`, manifest and gameplay fields, PRNG and test vectors).
3. Gameplay constants and the dynamic-obstacle spawner parameters are **data** (`src/hero/export/gameplay.json`), with a **Python reference spawner** (`hero.export.gameplay_ref`) and shared test vectors asserted in both Python and JS.
4. Per-run **model choice** (Sonnet 5.5 default, Opus 5.5 verified before enabling).
5. Everything else from r1 (add-to-model panel, real photo upload, dynamic split, gap analysis + interpolation, recompile migration, visual before/after, one-generation cap) stands.

**Decisions (each is `> Assumed:` unless the user stated it).**
- **glTF library = `pygltflib`** (exact pin in `pyproject.toml`, resolved once with `uv add pygltflib` and frozen in `uv.lock`). Why not `trimesh`: it drags in numpy (the project is deliberately numpy-free: Pillow + plain floats), its exporter's JSON/buffer layout is not a stability contract, and we need full control of node/primitive/extras layout. `pygltflib` is pure Python, dataclass-based and writes exactly the structure we build. **Determinism is not trusted to the library:** `hero.export.glb.write_glb` builds the binary chunk itself with `struct.pack` (little-endian, 4-byte aligned, fixed buffer order), serialises the JSON chunk with `json.dumps(sort_keys=False, separators=(",", ":"), ensure_ascii=True)` from an ordered dict, pads with spaces/zeros and writes the 12-byte header itself; `pygltflib` is used to *model and re-load* the document (round-trip + structural validation), not to emit bytes. Tests: byte equality across two builds and across subprocesses.
- **Validation:** `npx --yes gltf-validator` (Khronos validator, Node 22 is on the Mac) when reachable — `tests/integration/test_glb_khronos_validator.py` (marker `validator`, asserts 0 errors; skipped *with a visible reason* when `npx` or the network is unavailable); always-on: `pygltflib` reload + `hero.export.glb_validate` (accessor bounds/min/max, alignment, index range, UV range, material/texture refs, node-name contract) + the browser's own `GLTFLoader` parse in e2e.
- **Scene v3 survives only as the lightweight "web scene"** (`spec/data.md` → *Scene JSON v3 (r2)*). The manifest supersedes it for everything geometric or runtime; they do not overlap (`spec/portability.md` §4 last paragraph). The r1 "v3 with `ground_tex/frontage/furniture/corridor.kerb`" shape becomes the **internal `StreetPlan`** (pure, never served, never stored) — the compile's geometry half, consumed by the mesh builder and the manifest builder.
- **No `?schema=2` downlevel.** Stored v1/v2 scenes are served as stored and play on the legacy JS geometry path (kept, minimal, untouched except exports). A v3 street whose glb fails to load shows a retry/error screen (no silent fallback to a different look). `GET …/scene?schema=` is ignored.
- **Browser true-frame:** the glb is in the **true, unmirrored** street frame (`spec/portability.md` §2); the glb-path `Track` adapter converts the legacy runner's `lat` to `t`; legacy scenes keep the old mirrored frame.
- **Furniture/camera clearance without per-entity meshes:** furniture is merged per category, so "fade anything within 1.5 m of the camera path" is a shader clip (`material.onBeforeCompile`: dithered discard inside a 1.5 → 0.8 m cylinder around the camera, uniform updated per frame) plus near plane 0.3 m.
- **Model option:** one dropdown per run applies to vision **and** review/critique; the default is whatever `HERO_VISION_MODEL` resolves to (`claude-sonnet-5-5`) and is never changed silently.

**r1 still in force:** Goal text and migration promise (below); success criteria *Root cause with data*, *Dynamic split*, *Gaps closed in data*, *Add-to-model*, *Migration*, *Spend* (as amended by r2 criteria); the *Runtime classification*, *Gap analysis*, *Uploaded photo placement*, *Derived-cache columns* (amended), *Texture references* (amended) sections of `spec/data.md`; `improve-plan` / `fill-gaps` / `regenerate` / `gaps` / upload endpoints of `spec/api.md`; the admin panel/upload UI of `spec/ui.md`; the S2↔S3 integration seam (amended below).

**Phase-2.1 r2 success criteria** (every one a runnable assertion)
- [ ] **Root cause with data** — unchanged from r1 (`audit_scene` on the recorded v2 scene shows > 0 hole bins per side; on the recompiled Bellandur **glb/manifest** it shows 0 blank 5 m bins per side — `audit_scene` reads `StreetPlan`/manifest too).
- [ ] **Dynamic split** — unchanged from r1, asserted on `manifest.colliders`/`manifest.entities`/glb `extras` (0 parked vehicles etc.; model JSON byte-identical).
- [ ] **Gaps closed in data and in geometry** — `analyze_gaps` blank counts 0; additionally `tests/unit/test_mesh_coverage.py` samples the built mesh: for every 5 m bin and each side, `Frontage_L/R` triangles cover the bin (contiguous `s` coverage with no gap > 1 mm), and per station the ground nodes tile `t ∈ [−W_L, +W_R]` from wall to wall with no gap (> 1 mm) — on Bellandur and the curved fixture.
- [ ] **Scene contract** — `GET …/scene` returns v3-lite validating against `SceneV3` with `assets.{glb_url, manifest_url, xodr_url, mesh_sha256}`; v1 and v2 stored scenes still load and play (legacy e2e tests unchanged).
- [ ] **Mesh builder** — `build_street_mesh(model, scene, observations=…, data_dir=…)` returns a `StreetMesh` whose glb meets the budgets in `spec/portability.md` §3 (≤ 14 primitives, ≤ 12 000 triangles, ≤ 4 materials, ≤ 2 textures, ≤ 1.5 MB at 100 m); **same input ⇒ identical sha256** (in-process twice; subprocess with two `PYTHONHASHSEED`s); node names exactly the contract set.
- [ ] **glb valid & faithful** — `pygltflib` re-load OK; `glb_validate` OK; Khronos validator 0 errors when available; metres (street length from `Road` bounds ≈ `length_s` ± 0.5 m on both fixtures), Y-up, `extras` present with entity ids/types/confidence/fidelity/(s,t) equal to `manifest.entities`.
- [ ] **Manifest** validates against `ManifestV1` and its committed JSON Schema; `colliders`/`tokens` equal what the scene-level walkability counts; `gameplay_sha256` matches the file.
- [ ] **OpenDRIVE** — `street.xodr` passes the structural checks and the parse round-trip of `spec/portability.md` §8 (best effort, labelled so in README).
- [ ] **Serving** — `GET …/model.glb|manifest.json|street.xodr` 200 with correct content types, `ETag`, 304 on `If-None-Match`, `?v=<sha8>` URLs `immutable`; admin `export.zip` contains the four files + `IMPORT.txt` and is byte-deterministic.
- [ ] **Browser loads the glb** — on both scenes at 0/20/40/70 m: `__hero.source === "glb"`, `__hero.perf`: `draw_calls ≤ 60`, `triangles ≤ 60 000`, `textures ≤ 8`, `texture_mb ≤ 24`; the page never imports `scene_builder.js` terrain functions for a v3 street (asserted via `__hero.legacy_geometry === false`); a corrupted glb shows the error/retry screen.
- [ ] **Visual** — as r1, with AFTER PNGs captured from the glb-loaded game (`judge.py` also asserts `__hero.source === "glb"` for each shot).
- [ ] **Gameplay as data** — `gameplay.json` validates (`GameplayV1`), is embedded verbatim in the manifest and served at `/api/gameplay.json`; none of the constant names `LANE_TWEEN_MS|JUMP_MS|SLIDE_MS|SPEED_START|SPEED_MAX|HIT_DAMAGE|INVULN_S|START_GRACE_S|TOKEN_VALUE|HEALTH_MAX` is *defined* in `frontend/js/game/*.js` (a grep test; they are read from `gameplay.json`); compile reads lane rules from it.
- [ ] **Portable spawner** — `tests/unit/test_gameplay_ref.py` (Python reference == committed `spawner_vectors.json`, + fairness property tests 1 000 seeds × 10 loops × lengths 40–200 m) **and** `tests/e2e/test_spawner_vectors.py` (JS `spawner.js` == the same file and the bulk file, exact equality) pass; same `(slug, seed)` ⇒ same obstacles; different seeds ⇒ different layouts; vehicle-heavy `spawn_hints` ⇒ vehicle majority.
- [ ] **Portability docs** — `spec/portability.md` exists; README has *Using the street in Unity / Unreal / Godot* (`tests/unit/test_readme_sections.py` asserts the three engine subsections, the words `glTFast`, `Interchange`, `Godot`, the not-ported list, and `manifest`).
- [ ] **Model option** — see slice `SM-model`: `GET /api/admin/models` lists `claude-sonnet-5-5` (default, selected) and `claude-opus-5-5`; the verification evidence file exists; the chosen model is on `run.inputs.models`, `RunReport.model`, every `llm_calls.model` of that run, and the budget guard used its price (test with a fake client at two prices); if verification failed the option is `enabled:false` with `disabled_reason` and the API answers 422 for it.
- [ ] **Add-to-model, Migration, Spend** — as r1 (Spend: 0 generations; exactly **one tiny** real structured-output call for the Opus verification, cost from `llm_calls`/the evidence file, ≤ $0.05 expected).

**Slices r2 (disjoint owned paths; arrows = TRUE dependencies).** Edits to a file owned by another slice are forbidden; shared files are owned by exactly one slice.

| Slice | Owns (disjoint paths) | Depends on |
|---|---|---|
| `P0` (orchestrator) | `tests/fixtures/recorded/bellandur_scene_v2.json` (read-only GET of the dev server) | running dev server |
| `S1a-contract` | `src/hero/model/{scene_v3,gap_types,texture_types,street_plan_types}.py`; `src/hero/model/scene_v2.py` (**only** `load_scene` dispatch); `src/hero/model/types.py` (**only** adds `RunReport.model`); `src/hero/export/{__init__,mesh_types,manifest_types,gameplay_types}.py`; `src/hero/export/gameplay.json`; `src/hero/export/schemas/*.json` (generated); `pyproject.toml` + `uv.lock` (pins `pygltflib`, `Pillow`; markers `live`, `validator`); `tests/fixtures/make_contract_export.py`; `tests/fixtures/export/contract_curved/{plan.json,street.glb,street.manifest.json,street.xodr}` (a **stand-in** bundle with the real node names/extras/manifest shape built from simple boxes — what S2/S6/S7/S8/S10 code against); `tests/fixtures/scene_v3/contract_curved.json`; `tests/unit/{test_scene_v3_schema,test_manifest_schema,test_gameplay_schema,test_contract_fixture}.py` | `spec/` only |
| `S1d-rootcause` | as r1 (`scene_audit.py`, `tools/{audit_scene,export_recorded}.py`, `tests/fixtures/recorded/**` except the P0 file, `docs/evidence/phase2_1/rootcause_v2.json`, tests) — `audit_scene` additionally accepts a `StreetPlan` JSON | `P0` |
| `S5a-before` | as r1 (`tests/e2e/visual/{__init__,capture}.py`, `docs/evidence/phase2_1/before/*.png`, 8 files) — BEFORE uses the legacy path | `P0`; must finish before S2/S3 touch `frontend/` |
| `S1b-compile` | `src/hero/model/{compile,street_plan,dynamic,gaps,frontage,ground,furniture,describe,catalogue,coverage}.py`; `tests/unit/{test_compile,test_street_plan,test_dynamic_split,test_gaps,test_frontage,test_ground_plan,test_furniture,test_describe,test_lanes_from_gameplay,test_compile_bellandur_no_holes}.py`; `tests/fixtures/make_model_fixtures.py`; `tests/fixtures/scene_v3/{curved,bellandur}.json` and `tests/fixtures/plan/{curved,bellandur}.plan.json` (generated, committed). `compile_scene(model, …) -> CompileResult(scene: SceneV3, plan: StreetPlan, gaps: GapReport)`; lane/kerb/tokens/walkability rules read `gameplay.json` | `S1a`; `S1d` (audit used in a test) |
| `S1e-bake` | `src/hero/imagery/{bake,tints}.py`; `tests/unit/{test_bake,test_tints,test_bake_frame}.py`; `tests/fixtures/bake/**`. Output frame = true `(t, s)` strip (`spec/portability.md` §3); gaps are painted **into the image** (interpolated/procedural), `bin_source` reported | `S1a` |
| `S6-mesh` | `src/hero/export/{frame,mesh_ground,mesh_frontage,mesh_furniture,atlas,mesh_builder}.py`; `tests/unit/{test_mesh_frame,test_mesh_ground,test_mesh_frontage,test_mesh_furniture,test_mesh_builder,test_mesh_budget,test_mesh_coverage}.py`. `build_street_mesh(model, scene, *, observations, data_dir, plan=None) -> StreetMesh` (calls `plan_street` when `plan` is None) | `S1a` (types + plan/stand-in fixtures); `S1b` (`plan_street`) and `S1e` (atlases) for the integration tests |
| `S7-glb` | `src/hero/export/{glb,glb_validate}.py`; `src/hero/tools/validate_glb.py`; `tests/unit/{test_glb_write,test_glb_roundtrip,test_glb_validate}.py`; `tests/integration/test_glb_khronos_validator.py` | `S1a` (`StreetMesh`, stand-in fixture) |
| `S8-manifest-xodr` | `src/hero/export/{manifest,xodr}.py`; `tests/unit/{test_manifest,test_xodr,test_manifest_consistency}.py` | `S1a` (`StreetPlan`, `ManifestV1`, `gameplay.json`) |
| `S9-gameplay-ref` | `src/hero/export/{gameplay,gameplay_ref}.py`; `src/hero/tools/make_spawner_vectors.py`; `tests/fixtures/portability/{spawner_vectors,spawner_vectors_bulk}.json`; `tests/unit/{test_gameplay_ref,test_spawner_properties,test_gameplay_data}.py` | `S1a` (`gameplay.json`, `GameplayV1`) |
| `S10-export-api` | `src/hero/export/{store,bundle,pipeline}.py` (`build_and_store_exports(...) -> ExportRecord`: mesh → glb → manifest → xodr → content-addressed files `data/exports/<slug>/street_<sha8>.{glb,manifest.json,xodr}`; `build_bundle` → deterministic zip); `src/hero/api/{public_export,admin_export}.py`; `tests/unit/{test_bundle,test_export_determinism}.py`; `tests/integration/{test_export_api,test_export_pipeline}.py`; `tests/fixtures/export/baseline_hashes.json` | `S1a`; `S6`, `S7`, `S8` (integration tests; fakes until they land) |
| `S1c-migrate` | `src/hero/fusion/{recompile,publish,assets}.py` (publish/recompile call `build_and_store_exports` after compile); `src/hero/tools/recompile.py`; `src/hero/config.py` + `.env.example` (settings of `spec/architecture.md`); `src/hero/db/{models,repo,model_repo}.py`; `src/hero/db/migrations/versions/0003_scene_schema.py` (columns: `scene_schema, compiled_by, gaps_json, export_json`); `src/hero/api/{public,media,admin_model}.py` (scene = v3-lite, `POST …/runs` hazards from the stored manifest, `/media/textures`, `GET …/gaps`); `tests/unit/test_migration_0003.py`; `tests/integration/{test_recompile,test_public_scene_v3}.py` | `S1a`; calls `S1b.compile_scene`, `S1e.bake_textures`, `S10.build_and_store_exports` (fakes until landed) |
| `S2-renderer` | `frontend/js/game/{main,glb_loader,track_manifest,play_data,atmosphere,camera_rig,budget,about,scene_builder,path}.js`; `frontend/play/index.html`; `frontend/css/game.css`; `src/hero/tools/vendor_three.py`; `frontend/vendor/**` (adds `three/examples/jsm/loaders/GLTFLoader.js`, `three/examples/jsm/utils/BufferGeometryUtils.js`, `VENDOR.json` with sha256s; import map in `play/index.html`); `tests/e2e/{test_play_fixture,test_play_curved,test_render_budget,test_visual_smoke,test_glb_loading}.py` | `S1a` (stand-in glb/manifest fixture); `S3` only through the integration seam |
| `S3-gameplay` | `frontend/js/game/{gameplay,spawner,dynamic_obstacles,runner,hud,results,controls}.js`; `tests/e2e/{test_spawner_unit,test_spawner_vectors,test_dynamic_obstacles,test_play_gameplay}.py` | `S1a` (`gameplay.json`); `S9` vectors for `test_spawner_vectors` only |
| `S4a-improve-api` | `src/hero/api/{admin_improve,admin_imagery}.py`; `src/hero/imagery/plan.py` (targets); `src/hero/model/improve.py` (`estimate_cost(new_sv, new_sat, model)` uses `price_for(model)`); tests as r1 | `S1a`; `S1c.recompile_segment` signature; `SM.price_for` signature |
| `S4b-admin-ui` | `frontend/admin/{index,segment}.html`; `frontend/js/admin/{segments,improve,upload_form,placement_map,imagery_form,segment_view,export_button}.js`; `frontend/css/admin.css`; `tests/e2e/{test_admin_improve,test_admin_export,test_admin_model_select,admin_api_mock}.py`. Imports `model_select.js` (owned by `SM`) | `spec/api.md` (mock API) |
| `SM-model` | `src/hero/llm/{models.json,models.py,pricing.py,prices.json,budget.py}`; `src/hero/tools/check_structured.py` (adds `--models --modes --max-calls --out`); `src/hero/api/{admin,admin_models}.py`; `src/hero/pipeline/registry.py`; `src/hero/fusion/{runner,steps,state}.py` (model plumbing only); `src/hero/model/report.py`; `frontend/js/admin/model_select.js`; `docs/evidence/phase2_1/model_check_claude-opus-5-5.json`; `tests/unit/{test_models,test_pricing_opus}.py`; `tests/integration/test_admin_models_api.py` | `S1a` (`RunReport.model`) |
| `S5b-verify` | `tests/e2e/visual/judge.py`; `docs/evidence/phase2_1/{after/*.png,budget.json,cost.json,glb_validation.json,pr_body.md}`; `NOTES.md` (append) | everything landed |
| `S12-docs` | `README.md` (sections *Add to an existing model* and *Using the street in Unity / Unreal / Godot*, text from `spec/ui.md` and `spec/portability.md` §9); `tests/unit/test_readme_sections.py` | `S4a`/`S4b`/`S10` endpoint names (text only) |

**Producer ↔ consumer contract.** Producer = `hero.export` (`S6` geometry, `S7` bytes, `S8` manifest/xodr, `S10` storage/serving). Consumer = `frontend/js/game/glb_loader.js` (`S2`) + `spawner.js`/`runner.js` (`S3`). The full contract (node names, materials, UV rules, `extras` schema, manifest field list + example, `gameplay.json` field list + example, spawner algorithm) is `spec/portability.md` §3–§7; the stand-in bundle in `tests/fixtures/export/contract_curved/` is its executable form and is what the JS slices test against before the real builder lands. Consumer rules: look nodes up **by name**, tolerate missing optional nodes, never read geometry from `extras`, never hard-code a number from `gameplay.json`.

**S2 ↔ S3 integration point (amends r1).** Unchanged ownership: `frontend/js/game/main.js` is S2's; S3 never edits it, S2 never edits S3's files. Frozen names: `loadGameplay()` (S3, `gameplay.js`; returns the object from `manifest.gameplay`, or `GET /api/gameplay.json` for legacy scenes); `new Runner(playData, { gameplay, seed = null, runNumber = null })` where `playData = { length_m, track, colliders, tokens, spawnInput, lanes }` is produced by S2's `play_data.js` (`fromManifest(manifest)` for v3, `fromLegacyScene(scene)` for v1/v2 — the only place that knows both worlds); `runner.startRun()`, `runner.activeObstacles()`, `runner.reset()`; `createDynamicLayer(THREE, { scene, track, runner })`; `window.__hero = { source: "glb"|"legacy", legacy_geometry, perf, dynamic, gameplay }`. `Track` API (S2, `track_manifest.js` / legacy `path.js`): `world(s, lat)`, `laneLat(s, lane)`, `footpathAt(s)`, `applyCopy(p, k)`, `length` (unchanged names; the glb Track maps `lat → t` internally so `runner.js` is frame-agnostic).

**Waves r2.** W0: `P0`. W1 (concurrent): `S1a`, `S1d`, `S5a`. W2 (after `S1a` merged **and** the 8 BEFORE PNGs exist; all concurrent, 13 slices): `S1b S1e S1c S6 S7 S8 S9 S10 S2 S3 S4a S4b SM`. W3: `S5b`, `S12-docs`, then the gate and the human test-handoff. `SM`'s first action is the single real Opus verification call (cheap, independent of the rest); its result decides `enabled`.

**Commit + push rule (every verified slice).** When a slice's own gate command passes, the orchestrator immediately `git add <that slice's owned paths>` (explicit paths, never `-A`, never `.env`), commits `phase2.1(<slice>): <summary>` and `git push` (branch `feature/hero-20261029-1915-v0.1`), then records it below. A slice that fails its gate is not committed. The final PR body (`docs/evidence/phase2_1/pr_body.md`) lists these commits.

| slice | commit | pushed | gate command run |
|---|---|---|---|
| *(filled in by the orchestrator as each slice lands)* | | | |

**Per-slice gate commands (run by the orchestrator before committing that slice; all from the repo root, `uv run`).**

| Slice | Command |
|---|---|
| `S1a` | `uv sync && uv run pytest tests/unit/test_scene_v3_schema.py tests/unit/test_manifest_schema.py tests/unit/test_gameplay_schema.py tests/unit/test_contract_fixture.py -q` |
| `S1d` | `uv run pytest tests/unit/test_scene_audit.py tests/unit/test_recorded_bellandur_baseline.py -q` |
| `S5a` | `ls docs/evidence/phase2_1/before \| wc -l` = 8 |
| `S1b` | `uv run pytest tests/unit -q -k "compile or street_plan or dynamic or gaps or frontage or ground_plan or furniture or describe or lanes_from_gameplay"` |
| `S1e` | `uv run pytest tests/unit/test_bake.py tests/unit/test_tints.py tests/unit/test_bake_frame.py -q` |
| `S6` | `uv run pytest tests/unit -q -k "mesh_"` |
| `S7` | `uv run pytest tests/unit -q -k "glb_" && uv run pytest tests/integration/test_glb_khronos_validator.py -q` |
| `S8` | `uv run pytest tests/unit -q -k "manifest or xodr"` |
| `S9` | `uv run pytest tests/unit/test_gameplay_ref.py tests/unit/test_spawner_properties.py tests/unit/test_gameplay_data.py -q` |
| `S10` | `uv run pytest tests/unit/test_bundle.py tests/unit/test_export_determinism.py tests/integration/test_export_api.py tests/integration/test_export_pipeline.py -q` |
| `S1c` | `uv run pytest tests/unit/test_migration_0003.py tests/integration/test_recompile.py tests/integration/test_public_scene_v3.py -q` |
| `S2` | `uv run pytest tests/e2e/test_play_fixture.py tests/e2e/test_play_curved.py tests/e2e/test_render_budget.py tests/e2e/test_visual_smoke.py tests/e2e/test_glb_loading.py -q` |
| `S3` | `uv run pytest tests/e2e/test_spawner_unit.py tests/e2e/test_spawner_vectors.py tests/e2e/test_dynamic_obstacles.py tests/e2e/test_play_gameplay.py -q` |
| `S4a` | `uv run pytest tests/unit/test_improve.py tests/unit/test_plan_targets.py tests/integration/test_admin_improve_api.py tests/integration/test_upload_placement.py -q` |
| `S4b` | `uv run pytest tests/e2e/test_admin_improve.py tests/e2e/test_admin_export.py tests/e2e/test_admin_model_select.py -q` |
| `SM` | `uv run python -m hero.tools.check_structured --models claude-opus-5-5 --modes auto_tool --max-calls 1 --out docs/evidence/phase2_1/model_check_claude-opus-5-5.json && uv run pytest tests/unit/test_models.py tests/unit/test_pricing_opus.py tests/integration/test_admin_models_api.py -q` |
| `S12` | `uv run pytest tests/unit/test_readme_sections.py -q` |

**Gate r2** (exact, in order; repo root; first failure stops it; `GATE` is a scratch dir, never `data/`). Replaces the r1 gate; r1's G1/G4 commands are reused unchanged where noted.

```
cd /Users/sai/Workspace/her0
mkdir -p docs/evidence/phase2_1 && GATE=$(mktemp -d /private/tmp/claude-501/her0_gate21.XXXXXX) && echo $GATE
uv sync
# G1 migrations — as r1 (fresh DB → 0003 (head); upgrade of a COPY of data/hero.db twice, second a no-op)
HERO_DATABASE_URL=sqlite:///$GATE/fresh.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///$GATE/fresh.db uv run alembic current
sqlite3 data/hero.db ".backup $GATE/hero.db" && cp -R data/imagery data/crops data/uploads $GATE/ 2>/dev/null; mkdir -p $GATE/textures $GATE/exports
HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run alembic upgrade head
# G2 migrate + export existing models: zero spend, idempotent
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.recompile --all        # per slug: scene 2->3, glb sha8, triangles, interpolated bins, dynamic excluded, cost 0.0
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.recompile --all | grep -c unchanged   # == number of segments (glb bytes identical on rerun)
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.audit_scene --slug bellandur-main-road-n-2 --assert-no-holes
uv run python -m hero.tools.audit_scene tests/fixtures/plan/bellandur.plan.json --assert-no-holes
for f in $GATE/exports/*/street_*.glb; do uv run python -m hero.tools.validate_glb "$f" || exit 1; done | tee docs/evidence/phase2_1/glb_validation.json
# G3 deterministic tiers (live excluded by default; no Anthropic, no Google)
uv run pytest tests/unit -q
uv run pytest tests/integration -q          # includes the Khronos validator test (skips visibly offline)
uv run playwright install chromium
uv run pytest tests/e2e -q                  # glb loading, render budget on both scenes, spawner vectors (JS), fairness, admin improve/export/model-select, no upload stub
test -f docs/evidence/phase2_1/model_check_claude-opus-5-5.json
# G4 visual evidence (software GL flags inside capture.py; viewport 390x844) — AFTER comes from the glb-loaded game
uv run python tests/e2e/visual/capture.py --label after --scene all --out docs/evidence/phase2_1/after
uv run python tests/e2e/visual/judge.py docs/evidence/phase2_1/after      # also asserts __hero.source == "glb" per shot
# (orchestrator then READS the 8 AFTER PNGs against the 8 BEFORE PNGs and iterates until the visual criterion holds)
# G5 boot gate on a spare port against the COPY (never 8711)
HERO_PORT=8733 HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run hero-serve      # run_in_background; note the pid
B=http://127.0.0.1:8733/api/segments/bellandur-main-road-n-2
curl -s http://127.0.0.1:8733/api/health
curl -s $B/scene | grep -q '"schema_version":3'
curl -s $B/model.glb -o $GATE/m.glb && head -c4 $GATE/m.glb | grep -q glTF
ETAG=$(curl -sI $B/model.glb | awk 'tolower($1)=="etag:"{print $2}' | tr -d '\r'); test "$(curl -s -o /dev/null -w '%{http_code}' -H "If-None-Match: $ETAG" $B/model.glb)" = 304
curl -s $B/manifest.json | grep -q '"schema":"hero-street-manifest/1"'
curl -s $B/street.xodr | grep -q 'revMinor="7"'
curl -s http://127.0.0.1:8733/api/gameplay.json | grep -q '"schema":"hero-gameplay/1"'
curl -s -H "X-Hero-Admin-Password: $HERO_ADMIN_PASSWORD" http://127.0.0.1:8733/api/admin/models | grep -q 'claude-opus-5-5'     # password from the shell env; never printed
curl -sI http://127.0.0.1:8733/vendor/three/examples/jsm/loaders/GLTFLoader.js | grep -q '200'
curl -sI http://127.0.0.1:8733/js/game/spawner.js | grep -q '200'
curl -s 'http://127.0.0.1:8733/admin/segment.html?slug=x' | grep -q 'data-hero-page="admin-segment"'
# stop ONLY that pid; rm -rf $GATE
```

**How the user tests it (r2 additions to the r1 list below).** Phone: the street now loads as one 3D model (a second or two on LAN): footpath/road/kerb, continuous frontage both sides, sky and haze, slim poles and trees — all from the exported file; obstacles still spawn dynamically. Mac: segment page → **How to improve** panel as r1, plus a **model dropdown** next to every Generate / Fetch + Generate / Fill gaps button (default *Sonnet 5.5*; *Opus 5.5* greyed with a reason if the one-call verification failed; the run report and the run page show which model was used), and **Download for Unity / Unreal / Godot** (a zip of `street.glb`, `street.manifest.json`, `street.xodr`, `gameplay.json`, `IMPORT.txt`). Open the README section *Using the street in Unity / Unreal / Godot* to import it; nothing is stubbed except the Phase-3 items.

#### Phase 2.1 — r1 (text below is VOID where marked; kept for the parts listed under "r1 still in force")

**Feedback** — "How do I add to an already generated model? It's still not realistic enough and has gaps. We don't have to place
the other objects like cars (anything not on the footpath) — they can be placed dynamically for gameplay. But we still need to make
this more realistic." Root-verified on the real street `bellandur-main-road-n-2` (model v1, 112 entities, 35 of them parked vehicles):
flat grey footpath slab and flat road (no kerb, no texture although zoom-20 crops exist), photo cut-out billboards that fill the screen
and occlude the camera, floating "?" boxes and giant text labels, black sky, sparse detached side buildings, vehicles baked into the course.

**Goal** — one user-testable increment, no new generation spend: (1) the admin can **add to an existing model** from one page
(a "How to improve this model" panel: weakest stretches + what would raise them, **Fill gaps** with a priced preview, **Add more imagery**,
**real photo upload** placed on the coverage map, **Regenerate with what we have** at $0); (2) the street **looks like a street**
(textured footpath + road with kerb, continuous frontage on both sides, real small furniture meshes, sky + fog, readable camera,
no gaps); (3) everything that is not fixed footpath furniture (cars, two-wheelers, stalls, carriageway debris, pedestrians) is **no
longer baked** into the course: it stays in the model as `dynamic_context` evidence and the game **spawns generic obstacles
dynamically, deterministically and fairly** (health, tokens, lane logic unchanged).
Capabilities (all real): `model-improvement` (new), `dynamic-obstacles` (new), `scene-realism` (new); extended: `street-run`, `street-model`
(compile, gap analysis, recompile), `imagery-acquisition` (upload fields, targeted plans), `model-inspection` (admin panel),
`street-description` (About lists interpolated parts). No new Claude call anywhere in this phase; no new stubs (the Phase-2 "Upload a
photo" stub is retired — `data-testid="upload-stub"` must not exist after this phase).

**Migration promise** — compile is a pure function of a stored model version, so existing models (Bellandur v1, the canonical demo
street, `fixture-curved-street`, legacy imports) become Scene v3 with `uv run python -m hero.tools.recompile --all`: zero Google, zero
Claude. Alembic `0003_scene_schema` only adds derived/cache columns (`spec/data.md`); Phase 3's corrections migration is renumbered `0004`.

**[r1 — VOID: replaced by "Phase-2.1 r2 success criteria" above; kept as the wording source for the criteria r2 says are unchanged]** Phase-2.1 success criteria (every one is a runnable assertion)
- [ ] **Root cause with data (before any renderer change).** `uv run python -m hero.tools.audit_scene tests/fixtures/recorded/bellandur_scene_v2.json` prints, for the recorded real v2 scene: footpath intervals + states, ground strips with no texture, per-side building/wall lateral coverage per 5 m bin, hole bins. The committed baseline (`docs/evidence/phase2_1/rootcause_v2.json`) shows > 0 hole bins per side; the same tool on the **recompiled v3** Bellandur scene (`tests/fixtures/scene_v3/bellandur.json`) reports **0 blank 5 m bins per side** for ground and frontage, and `about.interpolated` lists exactly the bins that had no evidence.
- [ ] **Dynamic split.** Recompiled Bellandur v3: `objects[]` and `furniture[]` contain **0** entities of type `parked_vehicle`, `vendor_stall`, carriageway `debris`/`pothole`, `speed_breaker`, `other`; `dynamic_context.excluded_count` equals the number of model entities the test independently classifies as dynamic (see the note below this list); the canonical model JSON is byte-identical before/after recompile (knowledge never deleted).
- [ ] **Gaps closed in data.** `analyze_gaps` returns, for every 5 m bin, the evidence status of ground, both frontages, footpath state and furniture; `compile_scene` renders **0 blank bins**; every interpolated bin appears in `scene.about.interpolated` and in the admin gap report.
- [ ] **Scene contract.** `schema_version: 3`, validates against `hero.model.scene_v3.SceneV3`; `GET /api/segments/{slug}/scene?schema=2` still returns a valid v2 scene (downlevel); v1 and v2 stored scenes keep loading.
- [ ] **Render budget (e2e, phone viewport 390×844, software GL) on the recorded Bellandur v3 scene and the curved fixture, at 0/20/40/70 m of a run:** `__hero.perf.draw_calls ≤ 60`, `triangles ≤ 60 000`, `textures ≤ 8`, `texture_mb ≤ 24`.
- [ ] **Visual (read by the orchestrator, `docs/evidence/phase2_1/{before,after}/*.png`):** textured footpath with kerb, road with texture, continuous frontage both sides, sky + horizon, no billboard larger than its cap, no "?" box, no in-world text label, no baked vehicle; AFTER judged against the BEFORE list above.
- [ ] **Spawner fairness (unit + e2e):** for 1 000 seeds × 10 loops × street lengths 40–200 m, every generated row satisfies the rules below; same `(slug, run_number, loop)` ⇒ identical obstacles; different run numbers ⇒ different layouts; a street whose `dynamic_context` is vehicle-heavy spawns a majority of vehicles.
- [ ] **Add-to-model.** Admin segment page shows the How-to-improve panel with per-stretch fidelity + hints; **Fill gaps** previews ≥ 1 planned image with a USD estimate before spending; a **real file input** uploads a photo placed at a clicked `s` and the observation row has `source=upload`, `view`, `station_s`, `capture_date`; **Regenerate with what we have** returns `cost_usd: 0.0`, makes 0 Google requests and 0 Anthropic calls (asserted by guarded fakes), and the scene's `schema_version` becomes 3; an upload on an L2 stretch makes that bin L3 after Generate (fidelity unit test with a recorded extraction).
- [ ] **Migration.** Fresh DB `alembic upgrade head` → `0003 (head)`; upgrade of a copy of `data/hero.db` succeeds and a second `upgrade head` is a no-op; `recompile --all` on the copy is idempotent (second run reports `unchanged`).
- [ ] **Spend.** Phase gate makes **0** real generations (hard cap for the increment: 1, only if migrated data cannot supply a needed case; it would be `tests/live/test_phase2_1_optional.py`, skipped unless `HERO_ALLOW_ONE_LIVE=1`); total Anthropic spend of the gate = $0.00 from `llm_calls`.

> The excluded count is not hard-coded (it is ≥ 35 on Bellandur: the 35 parked vehicles plus any stalls/carriageway debris); the test computes the expected set from the recorded model using the table in `spec/data.md` → *Runtime classification*, re-implemented as a hand-written type list in the test (not an import of the classifier).

**[r1 — VOID as a contract: the values moved to `src/hero/export/gameplay.json` → `spawner`/`obstacle_types` and the algorithm to `spec/portability.md` §7; the table below is kept only as the rationale — 14 m min gap = 12 m/s × (0.15 s + 0.35 s) × 2 lanes, guaranteed passable lane, ramp per loop]** Spawner fairness rules

| rule | value |
|---|---|
| Lanes | `0` footpath, `1` road edge, `2` road (unchanged). Lane 0 is never used at stations where the traced footpath is `missing` (no walkable surface) |
| Seeding | `mulberry32(fnv1a32(slug + "\|" + (seed ?? runNumber) + "\|" + loop))`; `runNumber` = per-street counter in `localStorage["hero.run.<slug>"]`, +1 on every run start (first run = 1); `?seed=<int>` in the play URL overrides it (tests, sharing) |
| Free start | no spawns before `x = 30 m` in loop 0, `x = 12 m` in later loops; none within 8 m of the loop end (the seam) or within 6 m of a `crossing`/`ramp` |
| Row spacing (min gap in lanes terms) | rows are placed at `gap(L) = max(14, 26 − 2.5·L) m` (+ deterministic jitter in [0, 2.5] m, never below 14 m); **14 m ≥ 12 m/s × (0.15 s tween + 0.35 s reaction) × 2 lanes**, i.e. a full 2-lane shift is always possible at top speed |
| Max blocked lanes per row | `dodge`-type obstacles per row: loops 0–1 → **1**; loops ≥ 2 → **2**; never 3. At most one `jump`/`slide` obstacle per row, in a lane different from the dodge ones; total occupied lanes per row ≤ 2 |
| Guaranteed gap | at every x there is ≥ 1 lane with **no dodge obstacle** (dynamic or static, within ±6 m of x); loops 0–2 additionally keep ≥ 1 fully clear lane. If static hazards already block 2 lanes in the zone, the slot is skipped (shifted once by +6 m, else dropped) |
| Action spacing | no two obstacles requiring `jump`/`slide` (dynamic or static) within 9 m of each other in the same lane |
| Reachability | for consecutive rows *i−1*, *i* with passable lane sets A, B: `min over a∈A, b∈B of |a−b| ≤ 1 + floor(gap_m / (speed·0.5 s))`, with `speed` = the loop's speed (a property test at 12 m/s; with the 14 m minimum gap this is always satisfiable on 3 lanes, so the check only bites when static hazards sit between rows) |
| Ramp | per slot spawn probability `p(L) = min(0.85, 0.45 + 0.08·L) × (0.6 + 0.4·spawn_hints.density)` (loop index L from 0; density defaults to 0.5); speed ramp unchanged (6 + 0.1·L m/s, max 12) |
| Type mix | per-lane base weights (lane 0: two_wheeler 3, cone 3, barricade 2, stall 2, low_branch 1.5; lane 1: parked_car 3, two_wheeler 2, cone 2, barricade 1.5, pothole 2; lane 2: parked_car 3, two_wheeler 2, cone 1.5, barricade 1.5, pothole 2.5) × `(1 + 3·w[type])` with `w = scene.dynamic_context.spawn_hints.type_weights` (sum 1; missing ⇒ 0) |
| Actions | parked_car, two_wheeler, cone, barricade, stall → `dodge`; pothole → `jump`; low_branch → `slide` |
| Window | obstacles exist for `[x − 10, x + 80] m` of the run (fog hides spawn) |

**Render budget (single source, `spec/architecture.md` → *Render budget r2*; the numbers below are unchanged, the way they are met is not)**: ≤ **60 draw calls**, ≤ **60 000 triangles**, ≤ **8 textures**, ≤ **24 MB** texture memory per frame; terrain/frontage/furniture merged or instanced per material; dynamic layer ≤ 8 draw calls / ≤ 6 000 triangles.

**[r1 — VOID: replaced by "Slices r2" above. Do not use this table, its owned-path lists, or its waves.]** Independent slices (r1)

| Slice | Owns (disjoint paths) | Depends on |
|---|---|---|
| `P0` (orchestrator, 1 command) | `tests/fixtures/recorded/bellandur_scene_v2.json` = `curl -s http://127.0.0.1:8711/api/segments/bellandur-main-road-n-2/scene` → `.data` (read-only GET; never touch `data/hero.db`) | running dev server |
| `S1a-contract` | `src/hero/model/{scene_v3,gap_types,texture_types}.py`; `src/hero/model/scene_v2.py` (**only** `load_scene` dispatch: 3 → `SceneV3`); `tests/fixtures/make_scene_v3_contract.py`; `tests/fixtures/scene_v3/contract_curved.json` (generated from `tests/e2e/fixtures/s4_scene_curved_v2.json` by upgrading it with plausible `ground_tex/frontage/furniture/dynamic_context/about.interpolated` — the fixture S2 and S3 build against); `tests/unit/{test_scene_v3_schema,test_scene_downlevel}.py` | `spec/data.md` → Scene JSON v3 (this spec) |
| `S1d-rootcause` | `src/hero/model/scene_audit.py`; `src/hero/tools/{audit_scene,export_recorded}.py`; `tests/fixtures/recorded/**` except `bellandur_scene_v2.json` (model JSON, observations JSON, ≤ 4 MB of the images the model references, exported from a **copy** of the db); `docs/evidence/phase2_1/rootcause_v2.json`; `tests/unit/{test_scene_audit,test_recorded_bellandur_baseline}.py` | `P0` |
| `S5a-before` | `tests/e2e/visual/{__init__,capture}.py`; `docs/evidence/phase2_1/before/*.png` (8 files: `{bellandur,curved}_{0,20,40,70}m.png`) | `P0`; **must finish before S2/S3 touch `frontend/`** (orchestrator checks `ls docs/evidence/phase2_1/before \| wc -l` = 8) |
| `S1b-compile` | `src/hero/model/{compile,dynamic,gaps,frontage,ground,furniture,describe,catalogue,coverage}.py`; `tests/unit/{test_compile,test_dynamic_split,test_gaps,test_frontage,test_ground_plan,test_furniture,test_describe,test_compile_bellandur_no_holes}.py`; `tests/fixtures/make_model_fixtures.py` (extend: emit v3); `tests/fixtures/scene_v3/{curved,bellandur}.json` (generated, committed) | `S1a` (types); `S1d` (audit tool used in `test_compile_bellandur_no_holes`) |
| `S1e-bake` | `src/hero/imagery/{bake,tints}.py`; `tests/unit/{test_bake,test_tints}.py`; `tests/fixtures/bake/**` | `S1a` (`TextureRefs`); reuses existing `imagery/rectify.py` read-only |
| `S1c-migrate` | `src/hero/fusion/{recompile,publish,assets}.py`; `src/hero/tools/recompile.py`; `src/hero/config.py` + `.env.example` (3 new settings, `spec/architecture.md`); `src/hero/db/{models,repo,model_repo}.py`; `src/hero/db/migrations/versions/0003_scene_schema.py`; `src/hero/api/{public,media,admin_model}.py` (scene `?schema=2`, `/media/textures`, `GET …/gaps`); `tests/unit/test_migration_0003.py`; `tests/integration/{test_recompile,test_public_scene_v3}.py` | `S1a`; calls `S1b.compile_scene`, `S1e.bake_textures` (signatures in `spec/architecture.md` → Contracts 2.1; fakes until they land) |
| `S2-renderer` | `frontend/js/game/{scene_builder,main,about,path}.js`; **new** `frontend/js/game/{terrain,frontage,furniture,atmosphere,tex,budget,camera_rig}.js`; `frontend/play/index.html`; `frontend/css/game.css`; `tests/e2e/{test_play_fixture,test_play_curved,test_render_budget,test_visual_smoke}.py` | `S1a` fixture; `S3` only through the integration contract below (stubbed until it lands) |
| `S3-gameplay` | `frontend/js/game/{spawner,dynamic_obstacles,runner,hud,results,controls}.js`; `tests/e2e/{test_spawner_unit,test_dynamic_obstacles,test_play_gameplay}.py` | `S1a` fixture (reads `dynamic_context`); `S2` only through the integration contract |
| `S4a-improve-api` | `src/hero/api/{admin_improve,admin_imagery}.py` (new + upload/targets extension); `src/hero/imagery/plan.py` (adds `targets`); `src/hero/model/improve.py`; `tests/unit/{test_improve,test_plan_targets}.py`; `tests/integration/{test_admin_improve_api,test_upload_placement}.py` | `S1a` (`GapReport`); `S1c.recompile_segment` signature |
| `S4b-admin-ui` | `frontend/admin/segment.html`; `frontend/js/admin/{improve,upload_form,placement_map,imagery_form,segment_view}.js`; `frontend/css/admin.css`; `README.md` (new section *Add to an existing model*); `tests/e2e/{test_admin_improve,admin_api_mock}.py` | `spec/api.md` only (mock API); live check needs `S4a` |
| `S5b-verify` | `tests/e2e/visual/judge.py` (assertions on PNG stats: non-black sky, textured ground variance), `docs/evidence/phase2_1/after/*.png`, `docs/evidence/phase2_1/{budget.json,cost.json}`, `NOTES.md` (append lessons), PR body text file `docs/evidence/phase2_1/pr_body.md` | everything landed |

**[r1 — amended by "S2 ↔ S3 integration point (amends r1)" above: `Runner` now takes `playData` + `gameplay`; the layer/event/`__hero.dynamic` shapes below still hold]** Integration point (the only place S2 and S3 meet) — `frontend/js/game/main.js`, owned by S2. S2 and S3 own disjoint new modules; S3 never edits `main.js`/`scene_builder.js`, S2 never edits `runner.js`/`hud.js`/`results.js`/`spawner.js`/`dynamic_obstacles.js`. The contract S2 codes against and S3 provides (names are fixed; bodies are S3's):

```js
// S3 — frontend/js/game/runner.js (extended; old call sites keep working)
new Runner(scene, { seed = null, runNumber = null } = {})  // runNumber null ⇒ read+increment localStorage inside runner.startRun()
runner.startRun()                 // main.js calls it once on tap-to-start and on Run again; fixes seed/runNumber, generates loop 0
runner.activeObstacles()          // Obstacle[] for [x-10, x+80] m of the run, absolute: x_abs = loop*length + x_m
runner.reset()                    // unchanged semantic (health 100, tokens cleared); dynamic obstacles dropped
// S3 — frontend/js/game/dynamic_obstacles.js
export function createDynamicLayer(THREE, { scene, track, runner }) -> { group, update(), reset(), dispose() }
// group: ONE THREE.Group, ≤ 8 InstancedMesh (one per obstacle model), added once to the three.Scene by main.js; update() is called each frame after runner.update()
// Obstacle (also what `events.hit.object` carries): { id: "dyn-<loop>-<row>-<lane>", type: SPAWN_TYPES[i], dynamic: true, lane, x_m, length_m, action,
//                                                      size_m: {w,h,d}, label, loop }
// S2 — main.js wires: layer = createDynamicLayer(THREE, {scene, track: runner.track, runner}); three.add(layer.group);
//      per frame: layer.update(); Run again: runner.reset(); runner.startRun(); layer.reset();
```
`Runner` stays backwards compatible (the existing `new Runner(scene)` call and every existing `snapshot()` key/event keep their meaning), because `S2` owns the pre-existing `test_play_fixture.py`/`test_play_curved.py` and `S3` must not break them; new gameplay assertions live only in `S3`'s own test files.
Events (`token`, `loop`, `hit`, `crash`) and `snapshot()` keys are unchanged; `hit.object.dynamic === true` for spawned obstacles. `window.__hero.dynamic` (read-only, set by `main.js` from `runner`) = `{run_number, seed, obstacles: Obstacle[], rules}`.

**[r1 — VOID: see "Waves r2"]** Waves. W0: `P0`. W1 (concurrent): `S1a`, `S1d`, `S5a` (S5a only needs `P0` + the unmodified frontend). W2 (after `S1a` AND the 8 BEFORE PNGs exist; all concurrent): `S1b S1e S1c S2 S3 S4a S4b` (each codes against the contracts in this spec and tests against `contract_curved.json`/mocks; cross-slice integration tests wait for the listed dependency). W3: `S5b` (needs S1b recompiled fixtures + S2 + S3 + S4), then the gate, then the human test-handoff. `src/hero/app.py` stays untouched by every slice (router auto-discovery picks up `admin_improve`).

**[r1 — VOID: replaced by "Gate r2" above (G1, G4 and the copy-of-db discipline are carried over there)]** Gate (r1):

```
cd /Users/sai/Workspace/her0
mkdir -p docs/evidence/phase2_1 && GATE=$(mktemp -d /private/tmp/claude-501/her0_gate21.XXXXXX) && echo $GATE
uv sync
# G1 migrations — fresh DB and an upgrade of a COPY of the dev DB (idempotent)
HERO_DATABASE_URL=sqlite:///$GATE/fresh.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///$GATE/fresh.db uv run alembic current     # 0003 (head)
sqlite3 data/hero.db ".backup $GATE/hero.db" && cp -R data/imagery data/crops data/uploads $GATE/ 2>/dev/null; mkdir -p $GATE/textures
HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run alembic upgrade head && HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run alembic upgrade head    # second run: no-op
# G2 migration of existing models: zero spend, idempotent, then the hole audit on the real street
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.recompile --all                  # prints per slug: scene 2->3, interpolated bins, dynamic excluded, cost 0.0
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.recompile --all | grep -c unchanged   # == number of segments
HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run python -m hero.tools.audit_scene --slug bellandur-main-road-n-2 --assert-no-holes
uv run python -m hero.tools.audit_scene tests/fixtures/scene_v3/bellandur.json --assert-no-holes
# G3 deterministic tiers (live excluded by default; no Anthropic, no Google)
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run playwright install chromium
uv run pytest tests/e2e -q            # render budget on both scenes, spawner fairness, admin improve panel/upload/fill-gaps preview, no upload stub
# G4 visual evidence (software GL flags are set inside capture.py: --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist, viewport 390x844)
uv run python tests/e2e/visual/capture.py --label after --scene all --out docs/evidence/phase2_1/after
uv run python tests/e2e/visual/judge.py docs/evidence/phase2_1/after
# (orchestrator then READS the 8 AFTER PNGs against the BEFORE ones and iterates until the visual criterion holds)
# G5 boot gate on a spare port against the COPY (never 8711)
HERO_PORT=8733 HERO_DATA_DIR=$GATE HERO_DATABASE_URL=sqlite:///$GATE/hero.db uv run hero-serve     # run_in_background; note the pid
curl -s http://127.0.0.1:8733/api/health
curl -s 'http://127.0.0.1:8733/api/segments/bellandur-main-road-n-2/scene' | grep -q '"schema_version":3'
curl -s 'http://127.0.0.1:8733/api/segments/bellandur-main-road-n-2/scene?schema=2' | grep -q '"schema_version":2'
curl -s 'http://127.0.0.1:8733/admin/segment.html?slug=x' | grep -q 'data-hero-page="admin-segment"'
curl -sI http://127.0.0.1:8733/js/game/spawner.js | grep -q '200'
# stop ONLY that pid; rm -rf $GATE
```

**How the user tests it (r1; still valid, plus the r2 additions above)** (phone on the same Wi-Fi for the game; the Mac for admin; the dev server on 8711 is restarted once by the orchestrator after `uv run python -m hero.tools.recompile --all` so the existing streets are already v3)
1. Phone: gallery → *Bellandur Main Road* → run. Expected: grey-brown textured footpath with a lighter kerb and a darker textured road bending with the street; continuous building/wall frontage on both sides with no black gaps; a pale sky and haze; poles and trees as slim/small shapes beside the path, never filling the screen; no grey "?" boxes, no big words on screen; no parked vehicles standing in the course from the data — but cars, two-wheelers, cones, barricades, potholes, stalls and low branches appear in the lanes, with a safe lane always open. Tap *Run again*: the obstacle layout differs (run number +1); open the play URL with `?seed=7` twice: identical layouts. Health, tokens and lane switching behave as before.
2. Phone: **About this street** → new list "Filled in from neighbouring stretches" (e.g. "Right-hand frontage 40–60 m: no photo, interpolated") and "Seen on this street, spawned dynamically as obstacles: 35 parked vehicles…".
3. Mac: `/admin/` → open the street → **How to improve this model** panel: per-stretch fidelity bar with the weakest stretches ("40–60 m · L1 · no right-facing photo → add a Street View frame facing right, or upload one"), numbered how-to steps. Press **Fill gaps** → a plan preview (n images, new vs cached, estimated Google $ and Claude $, which stretches) — do not run it unless you want to spend. Press **Regenerate with what we have** → "Done — $0.00, 0 Google requests, 0 Claude calls; scene updated to v3" (the one-click re-compile; never creates spend).
4. Mac: same panel → **Upload photos**: choose one or more image files (real file picker), click the coverage map where each was taken (the field `s` fills), choose side + facing + optional date → **Upload**. Rows appear in the imagery grid as "uploaded · not yet fused". Press **Generate** (costs ≈ $0.05–0.15 Claude) → new model version; the stretch's coverage cell turns blue (L3) if it was already L2.
5. Nothing in this phase is a labelled stub except Phase-3 items ("Use my location", "Seed city", "Correct detected lines" — all "Coming in Phase 3").

### Phase 3 — Seeded city, GPS pick, admin correction *(was Phase 2 before the canonical street model was inserted)*

**Goal** — the one-street prototype becomes a city: 5–20 seeded segments, the player can pick
"the street I'm standing on", and the admin can fix detections without re-spending.
Capabilities: `seed-city`, `gps-pick`, `admin-correction` (all real), plus per-street top-5
run history on the results screen (part of `walkability-report`, rule "history"). Corrections now edit the **canonical model**
(`human` evidence → a model version of kind `edit`), seeding uses the Phase-2 cumulative pipeline, and the legacy Phase-1 pipeline
(`hero.pipeline.{runner,steps,assemble,review}`, Phase-1 prompts) is deleted in this phase's clean-up slice.

**Independent slices**

| Slice | Owns | Depends on |
|---|---|---|
| `P3-seed` | `src/hero/fusion/seed.py`, `src/hero/seeds/bengaluru.json`, admin endpoints `POST /api/admin/seed*` in `src/hero/api/admin_seed.py` (new), `tests/integration/test_seed.py` | Phase 2 |
| `P3-nearest-history` | `GET /api/segments/nearest` + `GET /api/segments/{slug}/runs` in `src/hero/api/public_nearest.py` (new), `src/hero/scene/geo.py` (extend: haversine), `tests/integration/test_nearest.py`, `tests/unit/test_haversine.py` | Phase 2 |
| `P3-correction-api` | `src/hero/db/migrations/versions/0004_corrections.py` (renumbered from 0003 by Phase 2.1), `src/hero/db/models.py` (extend: `segment_corrections`), `src/hero/model/edits.py`, `PUT/GET /api/admin/segments/{slug}/corrections` in `src/hero/api/admin_corrections.py` (new), `tests/integration/test_corrections.py` | Phase 2 |
| `P3-cleanup` | deletes `src/hero/pipeline/{runner,steps,assemble,review,state}.py`, the Phase-1 prompts, and `tests/unit/{test_assemble,test_preview}.py` + legacy-only integration tests (keeps `pipeline/registry.py`, `imagery/preview.py` if still used) | Phase 2 accepted by the human |
| `P3-ui` | `frontend/js/gallery.js` (GPS button real), `frontend/js/game/results.js` (history), `frontend/admin/correct.html`, `frontend/js/admin/correct.js`, `frontend/js/admin/seed.js`, `tests/e2e/test_gps_live.py`, `tests/e2e/test_correction_live.py` | spec only; live tests need the three API slices |

**Gate**

```
cd /Users/sai/Workspace/her0
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration tests/e2e -q
```

**How the user tests it** — Admin: "Seed city" lists the 5+ Bengaluru segments from the seed
file with per-segment status; "Generate all pending" runs them serially with the running
total visible; stops at budget. Admin opens a segment, "Correct detected lines": drag footpath
coverage ranges / move / delete objects on the satellite tile, Save → new model version, Google
requests 0, Claude cost $0. Player: gallery shows all ready streets; "Use my location" (allow
GPS) picks the nearest within 2 km or says "No street near you yet — pick from the gallery".
Results screen shows the street's top-5 runs.
