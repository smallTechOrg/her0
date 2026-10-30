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
| `GET /js/*`, `/css/*`, `/vendor/*` | assets |
| `GET /media/{imagery|crops|previews|uploads}/...` | files under the data dir; 404 outside; `Cache-Control: public, max-age=86400`. Files are content-addressed or version-suffixed (`?v=<n>`), so long caching is safe |

## Public

### `GET /api/health`
`{ok, data: {status: "ok", version, db: "ok"}}`.

### `GET /api/segments`
Ready segments, newest first.
`data: [{slug, name, city, quality, length_m, thumbnail_url, best_score, best_distance_m, run_count, walkability: {footpath_coverage_pct, hazard_count, crossing_present, score}, model: {version, fidelity_level: "L0".."L3", fidelity_label, photos, updated_at, legacy} | null}]`.
`model` is additive (Phase 2); `null` for a segment published by the Phase-1 pipeline that was never imported. Empty list is a valid 200 (gallery shows its empty state).

### `GET /api/segments/{slug}`
Metadata as above plus `start`, `end`, `status`. 404 if unknown.

### `GET /api/segments/{slug}/scene`
`data: Scene` — **v1 or v2, discriminated by `schema_version`** (`spec/data.md`). v2 is a strict superset of
v1 (`## Scene JSON v2`); clients that only know v1 keys keep working. The response is the scene compiled for the
segment's current model version; it changes only when a new version is published. 404 if unknown or not `ready`
(`{code: "not_found", message: "This street has not been generated yet."}`).

### `POST /api/segments/{slug}/runs`
Body `{score, distance_m, tokens_collected, loops, crashed_into_type, crashed_into_id, duration_s}`
(validation in `spec/data.md` → 422). Response
`data: {run_id, score, best_score, is_new_best, walkability, hazards: [{id, type, label, confidence, tile_px, latlon, action}]}`.

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
The admin UI for this is a labelled stub in Phase 2; the endpoint and storage are real and tested.

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

### Phase 3
`POST /api/admin/seed` (load `src/hero/seeds/<city>.json` → create missing segments → `data: {created, existing}`),
`POST /api/admin/seed/generate-pending` (serial generation of `draft|failed` segments, stops at budget; returns the first run id; progress via `GET /api/admin/runs`),
`GET /api/admin/segments/{slug}/corrections` (latest ops), `PUT /api/admin/segments/{slug}/corrections` body `{patch_ops}` → applies the edits to the head canonical model as `human` evidence without any LLM/Google call, publishes a model version of kind `edit`, stores a `segment_corrections` row → `data: {scene, walkability, version}`.

## Error examples

```json
{"ok": false, "error": {"code": "budget_exhausted", "message": "Generation budget of $50.00 is used up ($50.12 spent). Raise HERO_CLAUDE_BUDGET_USD in .env to continue.", "detail": {"spent_usd": 50.12, "budget_usd": 50.0}}}
{"ok": false, "error": {"code": "upstream_error", "message": "Google Static Maps returned 403: check HERO_GOOGLE_MAPS_API_KEY and that Static Maps API is enabled.", "detail": {"kind": "satellite", "status": 403}}}
```
