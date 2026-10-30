# API

Base: `http://<host>:8711`. All JSON responses use the envelope in `spec/architecture.md`
(`## Conventions`). Admin endpoints require header `X-Hero-Admin-Password: <HERO_ADMIN_PASSWORD>`
(constant-time compare); missing/wrong → 401 `unauthorized`. No auth on public endpoints.
Every response echoes `X-Request-Id`.

## Static

| Path | Serves |
|---|---|
| `GET /` | `frontend/index.html` (gallery) |
| `GET /play/?segment=<slug>` | game |
| `GET /admin/` , `GET /admin/run.html?id=<run_id>` , `GET /admin/segment.html?slug=<slug>` | admin, pipeline/run view, segment (model) page |
| `GET /js/*`, `/css/*`, `/vendor/*` | assets (`/vendor/three.module.js`; r2 adds `/vendor/three/examples/jsm/loaders/GLTFLoader.js` and `/vendor/three/examples/jsm/utils/BufferGeometryUtils.js`, pinned, sha256s in `/vendor/VENDOR.json`) |
| `GET /media/{imagery|crops|previews|uploads|textures}/...` | files under the data dir; 404 outside; `Cache-Control: public, max-age=86400`. Files are content-addressed or version-suffixed (`?v=<n>`), so long caching is safe |

## Public

### `GET /api/health`
`{ok, data: {status: "ok", version, db: "ok"}}`.

### `GET /api/segments`
Ready segments, newest first.
`data: [{slug, name, city, quality, length_m, thumbnail_url, best_score, best_distance_m, run_count, walkability: {footpath_coverage_pct, hazard_count, crossing_present, score}, model: {version, fidelity_level: "L0".."L3", fidelity_label, photos, updated_at, legacy} | null}]`.
`model` is additive (Phase 2); `null` for a segment published by the Phase-1 pipeline that was never imported. Empty list is a valid 200 (gallery shows its empty state).

### `GET /api/segments/{slug}`
Metadata as above plus `start`, `end`, `status`. 404 if unknown.

### `GET /api/segments/{slug}/scene[?schema=2]`
`data: Scene` — **v1, v2 or v3, discriminated by `schema_version`** (`spec/data.md`). v2 is a strict superset of
v1 (`## Scene JSON v2`), v3 a strict superset of v2 (`## Scene JSON v3`); clients that only know older keys keep working.
The response is the scene compiled for the segment's current model version; it changes only when a new version is published
or the scene is recompiled. 404 if unknown or not `ready`
(`{code: "not_found", message: "This street has not been generated yet."}`).
Phase 2.1 **r2**: a v3 scene is the lightweight web scene (`spec/data.md` → *Scene JSON v3 (r2)*) with `assets.{glb_url, manifest_url, xodr_url}`; it is returned as stored. A stored v1/v2 scene (not yet recompiled) is returned as stored and plays on the legacy path.
The r1 `?schema=2` downlevel is **dropped** (any `schema` query parameter is ignored). Geometry/lanes/colliders/tokens are no longer in the scene: fetch the manifest (`## Phase 2.1 r2 — export endpoints`).

### `POST /api/segments/{slug}/runs`
Body `{score, distance_m, tokens_collected, loops, crashed_into_type, crashed_into_id, duration_s}`
(validation in `spec/data.md` → 422). Response
`data: {run_id, score, best_score, is_new_best, walkability, hazards: [{id, type, label, confidence, tile_px, latlon, action}]}`.
Phase 2.1: `crashed_into_type` may be a spawned-obstacle type (`parked_car|two_wheeler|cone|barricade|pothole|stall|low_branch`) and `crashed_into_id` a `dyn-<loop>-<row>-<lane>` id; both are stored verbatim (free text, ≤ 64 chars) and are not looked up in `hazards` (spawned obstacles are not part of the scene). No other validation change. r2: for a v3 street `hazards` is built from the stored manifest's `colliders` (`action != none`, `tile_px`/`latlon` from the collider); v1/v2 streets keep reading `scene.objects`. `gameplay.json` limits (`health`, token value) are used for the score validation instead of constants.

### `GET /api/segments/nearest?lat=&lon=` *(Phase 3)*
Nearest `ready` segment by haversine to the segment midpoint within 2000 m:
`data: {segment, distance_m}`; none → 404 `{code: "not_found", message: "No street near you yet."}`.

### `GET /api/segments/{slug}/runs?limit=5` *(Phase 3)*
Top runs by score: `data: [{score, distance_m, tokens_collected, created_at}]`.

## Admin

### `GET /api/admin/status`
`data: {budget_usd, spent_usd, remaining_usd, run_budget_usd, runs: {total, succeeded, failed, failed_budget}, google: {requests, cache_hits, est_cost_usd}, models: {vision, review}, streetview_available_last: bool|null, seed_segment: {name, city, start, end, footpath_side}, active_run_id: uuid|null}`.
`seed_segment` prefills the "New segment" form (the seed is not pre-inserted in the DB — see `spec/roadmap.md`).
Also serves as login check (401 on bad password).

### `GET /api/admin/segments`
All segments (any status) with `walkability`, `latest_run_id`, and `latest_run: {id, status, cost_usd, finished_at} | null`.
Phase 2 adds `model: {head_version, fidelity_level: "L0".."L3", legacy: bool, photos: int, placement_verified: bool} | null` (`null` = no version yet; does **not** trigger the lazy legacy import).

### `POST /api/admin/segments/resolve`
Body `{address, city}` (`address` may also be a pasted `"lat, lon"`). Geocodes with Google Geocoding (same key) plus
Nominatim (OpenStreetMap) as a second opinion, de-duplicated within 60 m, max 3 each, then attaches the road direction of the
nearest OSM road (Nominatim reverse geometry first, Overpass as fallback; `null` when neither answers).
`data: {candidates: [{id, label, lat, lon, provider, heading_deg|null, heading_source|null, road_name|null, travel_compass?, left_side?, right_side?}], half_length_m: {min: 50, max: 100}}`.
404 `not_found` when nothing matches. The admin never types coordinates.

### `POST /api/admin/segments/from-address`
Body `{name, city, lat, lon, heading_deg, half_length_m (50–100, default 50), footpath_side (default left)}` → 201 `data: segment`.
The segment is centred on `lat, lon`, running `half_length_m` either way along `heading_deg` (normalised to 0–180°), so its
length is `2 × half_length_m` (100–200 m). Slug from name + side (suffix `-2` on collision).

### `POST /api/admin/segments`
Body `{name, city, start: {lat, lon}, end: {lat, lon}, footpath_side}` → 201 `data: segment` (explicit endpoints; kept for scripts and tests).
Length outside 30–200 m → 422. Slug generated from name + side (suffix `-2` on collision).

### `POST /api/admin/segments/{slug}/generate`
Starts a **fusion** run (ingest the imagery plan → perceive anything not yet fused → merge → publish a new model version).
Optional JSON body (an empty body = all defaults): `{imagery?: {sv_spacing_m?, headings?: ["forward"|"back"|"left"|"right"], sat_crop_zoom?, sat_crop_spacing_m?, max_images?, s_range?: {s_from, s_to}}, run_budget_usd?: number}`;
fields override the env defaults for this run only and are echoed in `run.inputs.imagery_plan`. Out-of-range values → 422 (`sv_spacing_m` 4–40, `max_images` 1–200, `sat_crop_zoom` 18–21, ≥ 1 heading, `run_budget_usd` 0.05–5).
202 `data: {run_id, status: "queued", mode: "generate"}`.
409 `run_in_progress` if any run is queued/running; 402 `budget_exhausted` if `remaining_usd <= 0`.
Stale-run self-heal: single-process: a queued/running row with no live worker thread is retired immediately (marked `failed` before the 409 check).
A run with nothing new to fuse succeeds as a **no-op** (`version_created: false`, $0.00 Anthropic, 0 new Google requests when the plan is fully cached).

### `POST /api/admin/segments/{slug}/imagery`
"Add more imagery": same body as `generate.imagery` (required, at least one field) → starts an **acquire-only** run (no Claude call;
cost = new Google images only) and returns 202 `data: {run_id, status: "queued", mode: "acquire"}`. New observations
are stored unfused; the next Generate fuses them. Same 409/402/422 rules (402 only when the Google estimate for the *new* images would exceed the run cap).

### `POST /api/admin/segments/{slug}/observations/upload` *(multipart/form-data)*
Fields: `file` (jpeg/png, ≤ `HERO_UPLOAD_MAX_MB`), `lat`, `lon`, `heading_deg`, optional `pitch_deg`, `fov_deg` (default 70), `note`. 201 `data: Observation` (kind `user_photo`, source `upload`, `view`/`station_s`/`offset_t` computed from the segment's reference line; 422 if the camera is more than 60 m from the reference line, or no reference line exists yet — run Generate/placement preview first).
Phase 2.1 extends the fields (below, `## Phase 2.1`) and the admin UI makes it real (the Phase-2 stub is retired).

### `GET /api/admin/segments/{slug}/observations?kind=&limit=200&offset=0`
`data: {total, items: [{id, kind, source, url, lat, lon, heading_deg, pitch_deg, fov_deg, zoom, view, station_s, offset_t, pano_id, capture_date, cost_usd, created_at, fused_in_version: int | null}]}` (newest first). `fused_in_version` = the first model version whose `fused_observation_ids` contains it.

### `POST /api/admin/segments/{slug}/placement-preview`
No Claude. Resolves the reference line (road polyline from Nominatim/Overpass, else straight chord), fetches the overview tile (one cached Static Maps call), renders the reference line, 10 m `s` ticks and the footpath side onto it, stores the v0 `prior` model if the segment has none.
`data: {url, reference_line: {source, osm_way_id, name, length_s, extrapolated_m, snap_m}, placement_verified, warnings[]}`.

### `GET /api/admin/segments/{slug}/model`
Head summary (imports the Phase-1 scene as v0 `legacy` lazily on first call if the segment has a scene and no versions):
`data: {slug, head_version, legacy, placement_verified, reference_line: {source, osm_way_id, name, length_s, points: [{lat, lon, s}] (≤ 60 simplified)}, fidelity: FidelityMap (head), counts: {entities, removed, buildings, observations, fused_observations}, versions: [{version, kind, parent_version, run_id, created_at, entity_count, observation_count, fidelity_level, summary}]}`.
404 if the segment is unknown; `head_version: null` + empty arrays for a draft segment with no model.

### `GET /api/admin/segments/{slug}/model/versions/{n}`
`data: {version, kind, parent_version, run_id, created_at, summary, model: CanonicalModel, fidelity: FidelityMap, diff: ModelDiff | null}`. 404 unknown version.

### `GET /api/admin/segments/{slug}/model/diff?from=1&to=2`
`data: ModelDiff` (recomputed deterministically from the two stored snapshots; `from < to` not required). 404 unknown version.

### `GET /api/admin/segments/{slug}/model/coverage?version=`
`data: FidelityMap` for `version` (default head) + `observations_by_bin: [{index, observation_ids: [...] (≤ 12)}]` for the "which photos cover this stretch" popover.

### `GET /api/admin/segments/{slug}/model/entities?version=&type=&state=&min_confidence=`
`data: [{id, type, subtype, state, s, t, confidence, description, evidence_count, first_seen_version, last_changed_version}]` ordered by `s`.

### `GET /api/admin/segments/{slug}/model/entities/{entity_id}?version=`
`data: {entity: Entity, evidence: [{role, weight, crop_px, note, added_in_version, observation: {id, kind, view, url, station_s, heading_deg, capture_date, width_px, height_px}}], contradictions: [Contradiction], history: [{version, confidence, state, position}]}`. 404 if the id is not in that version.

### `GET /api/admin/runs/{run_id}`
Full run for the pipeline view (polled every 2 s while `queued|running`):
`data: {id, segment: {slug, name}, status, started_at, finished_at, inputs, steps[], imagery: {satellite: {url, annotated_url, zoom, meters_per_px}, streetview: [{url, x_m, heading}], streetview_status, osm_available, streetview_available, warnings[]}, layout, obstacles, iterations: [{n, preview_url, accepted, critique, patch_ops, applied, detail}], scene, preview_url, thumbnail_url, cost: {input_tokens, output_tokens, cost_usd, calls: [{step, model, input_tokens, output_tokens, cost_usd, latency_ms, ok}]}, google: {requests, cache_hits}, error, budget: {spent_total_usd, budget_usd}}` — the Phase-1 keys, unchanged for Phase-1 runs.
**Phase-2 additions (additive):** `mode`, `base_version`, `result_version`, `version_created`, `report_available: bool`, `imagery.observations: [{id, kind, view, url, station_s, capture_date, new: bool}]` (the images this run used; `new` = first ingested by this run), `imagery.planned`, `imagery.plan_trimmed`. For Phase-2 runs `layout`/`obstacles` are `null`, `imagery.satellite`/`streetview` are filled from the overview observation / street-view observations so the Phase-1 view still renders.

### `GET /api/admin/runs/{run_id}/report`
`data: RunReport` (`spec/data.md` → Run report). 404 `not_found` for Phase-1 runs and for runs that ended before the report step (failed runs get a partial report with `degraded: true`).

### `GET /api/admin/runs?limit=20`
Recent runs summary list (adds `mode`, `version_created`).

### `GET /api/admin/status` — Phase-2 additions
`limits: {run_budget_usd, max_images_per_run, sv_spacing_m, headings, sat_crop_zoom, sat_crop_spacing_m, batch_images, structured_mode}`, `models` unchanged, `demo_street: {query: "100 Feet Road, Indiranagar", city: "Bengaluru", name: "100 Feet Road, Indiranagar, canonical demo (west footpath)", hint: {lat: 12.9711, lon: 77.6413}, half_length_m: 50, footpath_side: "left"}`. `seed_segment` (legacy coordinates) is retained for the Phase-1 tests/import.

## Phase 2.1 — add-to-model endpoints *(additive; all admin, all in `hero.api.admin_improve` unless noted)*

Cost constants (one place, `hero.model.improve`): Street View image `$0.007`, satellite crop `$0.002`; Claude **r2: token-based, priced with the run's model** — per new image `1 500` input + `100` output tokens, per run `8 000` input + `400` output tokens, × `price_for(model)` (at 3 / 15 USD per Mtok this equals the r1 `≈ $0.006` per image + `$0.03` per run) (> **Assumed:** from the Phase-2 gate: 48 images ≈ $0.27–0.42;
shown as an *estimate*, the real figure comes from `llm_calls`). An estimate that exceeds `HERO_RUN_BUDGET_USD` or the remaining budget sets `will_exceed_budget: true` and the start endpoints answer 402 `budget_exhausted`.

### `GET /api/admin/segments/{slug}/improve-plan`
The data behind the "How to improve this model" panel. No spend, no network.
```jsonc
{ "head_version": 1, "scene_schema": 2, "fidelity": {"overall": {"level": 1, "score": 1.0, "histogram_m": {"L0": 0, "L1": 100.2, "L2": 0, "L3": 0}}},
  "intervals": [ { "s_from": 0.0, "s_to": 40.0, "level": 1, "gaps": ["frontage_opposite"], "weakest_reason": "single viewpoint; no right-facing photo",
                   "hint": "Add a Street View frame facing right from a second station within 15 m (L2).", "actions": ["fill_gaps", "upload_photo"] } ],   // maximal runs of bins with equal (level, gaps), ordered by s
  "weakest": [ /* up to 5 of `intervals`, ranked by (level asc, gap count desc, length desc) */ ],
  "gap_summary": {"uncovered": {"ground": 0, "frontage_modelled": 2, "frontage_opposite": 5, "footpath": 1, "furniture": 3}, "interpolated_m": 25.0},
  "actions": {
    "fill_gaps":  {"available": true, "default_body": {"target_level": 2, "max_images": 40, "max_usd": 1.0}},
    "add_imagery": {"available": true, "defaults": {"sv_spacing_m": 10, "headings": ["forward","back","left","right"], "sat_crop_zoom": 20}},
    "regenerate_cached": {"available": true, "scene_stale": true, "unfused_observations": 0, "stored_extractions": 6, "missing_extractions": 0, "cost_usd": 0.0},
    "upload": {"available": true, "max_mb": 10, "accepts": ["image/jpeg", "image/png"], "fields": ["file", "s", "side", "facing", "heading_deg", "captured_at", "lat", "lon", "pitch_deg", "fov_deg", "note"], "s_range": [0.0, 100.2]} },
  "steps": ["1. Look at the coverage bar: grey/amber stretches are the weakest.", "2. Press Fill gaps to see which photos would help and what they cost.", "3. Or upload your own photos: pick the files, click on the map where each was taken.", "4. Press Generate to fuse new photos into model v2 (a few cents of Claude).", "5. Press Regenerate with what we have any time to rebuild the scene from stored data for $0."] }   // deterministic template
```
404 unknown segment; a segment with no model → `head_version: null`, `intervals: []`, and `steps` starting with "Run Check placement, then Generate once to create the first model".

### `POST /api/admin/segments/{slug}/fill-gaps/preview`
Body (all optional): `{target_level?: 2|3 (default 2), max_images?: 1–200 (default 40), max_usd?: 0.05–5 (default 1.0), s_range?: {s_from, s_to}}`. Plans the cheapest imagery that would raise the weakest bins by one level and close ground/frontage evidence gaps, **without any request to Google or Claude**:
priority = bins by (level asc, gap count desc); per bin the missing condition from the fidelity `hint` → a *target*: a Street View frame at the best free station within 15 m facing the missing side (`view: left|right`), a second-station forward/back frame, or a zoom-20 satellite crop when `ground.evidence != satellite_hires`; targets already stored as observations are `cached` (free).
`data: {plan: ImageryPlan (incl. targets), images: [{kind, view, station_s, lat, lon, heading_deg, zoom, cached, reason}], counts: {planned, new, cached}, intervals_targeted: [{s_from, s_to, from_level, to_level}], est: {google_usd, claude_usd, total_usd}, trimmed: bool, will_exceed_budget: bool, note: "…"}`.
`images` is empty with `note: "Nothing to fill: every stretch is at the target level."` when there is nothing to do. 422 for out-of-range fields.

### `POST /api/admin/segments/{slug}/fill-gaps`
Same body + `fuse?: bool` (default `true`): starts the run for exactly the previewed plan (`plan_overrides.targets`): `fuse=true` → `generate` (acquire + perceive + merge + publish), `fuse=false` → `acquire` only. 202 `data: {run_id, status: "queued", mode, plan_summary: {new, cached, est_total_usd}}`; 409 `run_in_progress`; 402 `budget_exhausted`; 422.

### `ImageryPlan.targets` *(extends `## Imagery plan`, used by `…/imagery`, `…/generate.imagery`, `…/fill-gaps`)*
`targets?: [{kind: "streetview"|"satellite_crop", station_s: number, view?: "forward"|"back"|"left"|"right", zoom?: 18–21}]` (≤ 200). When present, `plan_images` plans **exactly** these (no uniform grid, overview still included once); mutually exclusive with `sv_spacing_m`/`headings`/`s_range` overrides (422 if both given).

### `POST /api/admin/segments/{slug}/regenerate`
"Regenerate with what we have": **no Google, no Claude, ever.** Body `{mode?: "compile" | "remerge"}` (default `compile`). Synchronous (≤ 15 s).
- `compile` — re-bakes textures from stored observations (`hero.imagery.bake`), recompiles the head version with the current compiler, **rebuilds the export bundle (glb, manifest, xodr)**; overwrites only the derived scene/export columns (`spec/data.md` → 0003). Same as `uv run python -m hero.tools.recompile --slug <slug>`.
- `remerge` — additionally replays every stored `observation_extractions` row through projection → merge and publishes a **new** model version (kind `fused`) only if `model_hash` changes; observations without a stored extraction are listed in `missing_extractions` and stay unfused (they need **Generate**, which costs Claude).
`data: {mode, version, version_created: bool, scene_schema: 3, previous_scene_schema, export: {sha8, mesh_sha256, triangles, primitives}, unchanged: bool, cost_usd: 0.0, google_requests: 0, anthropic_calls: 0, interpolated_m: 25.0, dynamic_excluded: 41, missing_extractions: [obs ids], warnings[]}`. 404; 409 `run_in_progress` while a run is active; a segment with no model → 422 `validation_error` ("Nothing to regenerate: generate once first.").

### `GET /api/admin/segments/{slug}/gaps?version=`
`data: GapReport` (`spec/data.md` → Gap analysis) for `version` (default head); computed on the fly when the stored `gaps_json` is null. 404 unknown version.

### `POST /api/admin/segments/{slug}/observations/upload` — Phase 2.1 fields
`multipart/form-data`, **one file per request** (the UI sends a batch sequentially). Placement is either `lat`+`lon` (unchanged) **or** `s`+`side`:

| field | type | req | notes |
|---|---|---|---|
| `file` | jpeg/png | yes | ≤ `HERO_UPLOAD_MAX_MB` |
| `s` | number 0–`length_s` | one of (`s`,`side`) / (`lat`,`lon`) | metres along the reference line (the UI fills it from the coverage-map click) |
| `side` | `left`\|`right`\|`centre` | with `s` | which side of the road the photographer stood on, relative to travel; camera `t = ±5.5 m` |
| `facing` | `forward`\|`back`\|`left`\|`right`\|`toward_left`\|`toward_right` | no | converted to `heading_deg` from the tangent at `s`; one of `facing`/`heading_deg` is required |
| `heading_deg` | number | no | explicit compass heading (wins over `facing`) |
| `captured_at` | `YYYY-MM` or `YYYY-MM-DD` | no | not in the future; stored as `capture_date` `YYYY-MM`; missing → null ("capture date unknown") |
| `lat`,`lon`,`pitch_deg`,`fov_deg` (default 70),`note` | | no | as before |

201 `data: Observation + {placement: {s, t, side, facing, lat, lon, heading_deg, how}, affects: {s_from, s_to, can_reach_level: 2|3, message}}` (`affects` = the bins the photo's cells cover and the level it can lift them to, from `compute_fidelity` on the head model + this observation; informational). Idempotent on the file hash (re-upload → 200 with the existing row, placement unchanged). 422 for: neither placement given, `s` out of range, camera > 60 m from the line, future `captured_at`, unreadable image.

### Admin additions to existing responses
`GET /api/admin/segments/{slug}/model` gains `scene_schema` (int|null) and `gaps: {uncovered: {...}, interpolated_m}`; `GET /api/admin/segments` model badge gains `scene_schema`. `GET /api/admin/status.limits` gains `upload_max_mb`.

## Phase 2.1 r2 — export endpoints *(public unless marked admin; module `hero.api.public_export` / `admin_export`)*

The files are the bundle of `spec/portability.md`. All four public endpoints answer for any `ready` segment whose head version has an export (an export is created by publish/recompile; a missing one is built lazily once on first request, under a per-segment lock, still with zero network); unknown/not-ready segment → 404 `not_found`.
Query `?v=<sha8>` selects the content-addressed file of that bundle (404 `not_found` "unknown asset version" if it never existed); without `v` the head bundle is served.

| Endpoint | Body | Headers |
|---|---|---|
| `GET /api/segments/{slug}/model.glb` | `street.glb` bytes | `Content-Type: model/gltf-binary`, `ETag: "<sha256 of the file, first 32 hex>"`, `Content-Length`, `Cache-Control: public, max-age=31536000, immutable` when `?v=` is given, else `public, max-age=60, must-revalidate`; `If-None-Match` match → **304** with the same `ETag` and no body; `Content-Disposition: inline; filename="<slug>-v<model_version>.glb"` |
| `GET /api/segments/{slug}/manifest.json` | the manifest, compact JSON (`separators=(",",":")`) | `Content-Type: application/json`, same ETag/Cache-Control/304 rules. **Not** wrapped in the envelope (it is a file, like the glb) |
| `GET /api/segments/{slug}/street.xodr` | OpenDRIVE XML | `Content-Type: application/xml`, same rules; best effort (header comment says so) |
| `GET /api/gameplay.json` | `gameplay.json` bytes | `Content-Type: application/json`, `ETag` = sha256, `Cache-Control: public, max-age=300` |
| `GET /api/admin/segments/{slug}/export.zip` *(admin)* | deterministic zip: `street.glb`, `street.manifest.json`, `street.xodr`, `gameplay.json`, `IMPORT.txt` (the engine import notes of `spec/portability.md` §9 + attribution) | `Content-Type: application/zip`, `Content-Disposition: attachment; filename="<slug>-v<model_version>-hero-street.zip"`. The admin UI downloads it with `fetch` + the password header (a plain link cannot send it) |

`GET /api/admin/segments/{slug}/model` gains `export: {sha8, mesh_sha256, glb_bytes, triangles, primitives, built_with} | null`. Responses never log or echo keys.

### Model choice *(slice `SM-model`; module `hero.api.admin_models`)*
`GET /api/admin/models` → `data: {default: "claude-sonnet-5-5", options: [{id, label, enabled: bool, disabled_reason: string|null, input_usd_per_mtok, output_usd_per_mtok, price_source_url, price_verified_on, verified_on: string|null, evidence: string|null}]}`.
`default` is `settings.vision_model` (never hard-coded in the API). Options come from `src/hero/llm/models.json` joined with `prices.json`; an option without a price row is not listed (the pipeline never runs with an unpriced model). `claude-opus-5-5` is listed with `enabled: false` and a `disabled_reason` until the verification evidence file exists and says `ok: true`.
Per-run selection: `model?: string` (id from the list) is accepted by `POST …/segments/{slug}/generate`, `POST …/segments/{slug}/fill-gaps` (when `fuse` is true) and ignored by acquire-only endpoints (no LLM). It sets **both** the vision and the review model for that run. Unknown id → 422 `validation_error`; a listed but disabled id → 422 `validation_error` with the `disabled_reason` as message. Absent ⇒ the configured default. The model used is echoed in `run.inputs.models = {vision, review, source: "request"|"default"}`, in `RunReport.model`, on every `llm.call` log line and `llm_calls.model`; the pre-call budget check uses that model's price (`hero.llm.pricing.price_for`). `POST /api/admin/segments/{slug}/fill-gaps/preview` accepts body `model` and prices the Claude estimate with it (`est.claude_usd`, `est.model`).

### Phase 3
`POST /api/admin/seed` (load `src/hero/seeds/<city>.json` → create missing segments → `data: {created, existing}`),
`POST /api/admin/seed/generate-pending` (serial generation of `draft|failed` segments, stops at budget; returns the first run id; progress via `GET /api/admin/runs`),
`GET /api/admin/segments/{slug}/corrections` (latest ops), `PUT /api/admin/segments/{slug}/corrections` body `{patch_ops}` → applies the edits to the head canonical model as `human` evidence without any LLM/Google call, publishes a model version of kind `edit`, stores a `segment_corrections` row → `data: {scene, walkability, version}`.

## Error examples

```json
{"ok": false, "error": {"code": "budget_exhausted", "message": "Generation budget of $50.00 is used up ($50.12 spent). Raise HERO_CLAUDE_BUDGET_USD in .env to continue.", "detail": {"spent_usd": 50.12, "budget_usd": 50.0}}}
{"ok": false, "error": {"code": "upstream_error", "message": "Google Static Maps returned 403: check HERO_GOOGLE_MAPS_API_KEY and that Static Maps API is enabled.", "detail": {"kind": "satellite", "status": 403}}}
```
