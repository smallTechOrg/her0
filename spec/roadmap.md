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
Phase-2 additions: full photo-upload UI (API and storage are real, the UI is a Phase-3 stub);
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
| `P3-correction-api` | `src/hero/db/migrations/versions/0003_corrections.py`, `src/hero/db/models.py` (extend: `segment_corrections`), `src/hero/model/edits.py`, `PUT/GET /api/admin/segments/{slug}/corrections` in `src/hero/api/admin_corrections.py` (new), `tests/integration/test_corrections.py` | Phase 2 |
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
