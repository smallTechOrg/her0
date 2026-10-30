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
| `GET /admin/` , `GET /admin/run.html?id=<run_id>` | admin, pipeline view |
| `GET /js/*`, `/css/*`, `/vendor/*` | assets |
| `GET /media/{imagery|crops|previews}/...` | files under the data dir; 404 outside; `Cache-Control: public, max-age=86400` |

## Public

### `GET /api/health`
`{ok, data: {status: "ok", version, db: "ok"}}`.

### `GET /api/segments`
Ready segments, newest first.
`data: [{slug, name, city, quality, length_m, thumbnail_url, best_score, best_distance_m, run_count, walkability: {footpath_coverage_pct, hazard_count, crossing_present, score}}]`.
Empty list is a valid 200 (gallery shows its empty state).

### `GET /api/segments/{slug}`
Metadata as above plus `start`, `end`, `status`. 404 if unknown.

### `GET /api/segments/{slug}/scene`
`data: Scene` (`spec/data.md`). 404 if unknown or not `ready`
(`{code: "not_found", message: "This street has not been generated yet."}`).

### `POST /api/segments/{slug}/runs`
Body `{score, distance_m, tokens_collected, loops, crashed_into_type, crashed_into_id, duration_s}`
(validation in `spec/data.md` → 422). Response
`data: {run_id, score, best_score, is_new_best, walkability, hazards: [{id, type, label, confidence, tile_px, latlon, action}]}`.

### `GET /api/segments/nearest?lat=&lon=` *(Phase 2)*
Nearest `ready` segment by haversine to the segment midpoint within 2000 m:
`data: {segment, distance_m}`; none → 404 `{code: "not_found", message: "No street near you yet."}`.

### `GET /api/segments/{slug}/runs?limit=5` *(Phase 2)*
Top runs by score: `data: [{score, distance_m, tokens_collected, created_at}]`.

## Admin

### `GET /api/admin/status`
`data: {budget_usd, spent_usd, remaining_usd, run_budget_usd, runs: {total, succeeded, failed, failed_budget}, google: {requests, cache_hits, est_cost_usd}, models: {vision, review}, streetview_available_last: bool|null, seed_segment: {name, city, start, end, footpath_side}, active_run_id: uuid|null}`.
`seed_segment` prefills the "New segment" form (the seed is not pre-inserted in the DB — see `spec/roadmap.md`).
Also serves as login check (401 on bad password).

### `GET /api/admin/segments`
All segments (any status) with `walkability`, `latest_run_id`, and `latest_run: {id, status, cost_usd, finished_at} | null`.

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
Starts a pipeline run. 202 `data: {run_id, status: "queued"}`.
409 `run_in_progress` if any run is queued/running; 402 `budget_exhausted` if `remaining_usd <= 0`.
Stale-run self-heal: a `queued|running` run with no live worker that is older than 15 min is marked `failed` on the next generate call before the 409 check.

### `GET /api/admin/runs/{run_id}`
Full run for the pipeline view (polled every 2 s while `queued|running`):
`data: {id, segment: {slug, name}, status, started_at, finished_at, inputs, steps[], imagery: {satellite: {url, annotated_url, zoom, meters_per_px}, streetview: [{url, x_m, heading}], streetview_status, osm_available, streetview_available, warnings[]}, layout, obstacles, iterations: [{n, preview_url, accepted, critique, patch_ops, applied, detail}], scene, preview_url, thumbnail_url, cost: {input_tokens, output_tokens, cost_usd, calls: [{step, model, input_tokens, output_tokens, cost_usd, latency_ms, ok}]}, google: {requests, cache_hits}, error, budget: {spent_total_usd, budget_usd}}`.

### `GET /api/admin/runs?limit=20`
Recent runs summary list.

### Phase 2
`POST /api/admin/seed` (load `src/hero/seeds/<city>.json` → create missing segments → `data: {created, existing}`),
`POST /api/admin/seed/generate-pending` (serial generation of `draft|failed` segments, stops at budget; returns the first run id; progress via `GET /api/admin/runs`),
`GET /api/admin/segments/{slug}/corrections` (latest ops), `PUT /api/admin/segments/{slug}/corrections` body `{patch_ops}` → applies to the current scene without any LLM/Google call, recomputes walkability + `tile_px`, stores a `segment_corrections` row, replaces `scene_json` → `data: {scene, walkability}`.

## Error examples

```json
{"ok": false, "error": {"code": "budget_exhausted", "message": "Generation budget of $50.00 is used up ($50.12 spent). Raise HERO_CLAUDE_BUDGET_USD in .env to continue.", "detail": {"spent_usd": 50.12, "budget_usd": 50.0}}}
{"ok": false, "error": {"code": "upstream_error", "message": "Google Static Maps returned 403: check HERO_GOOGLE_MAPS_API_KEY and that Static Maps API is enabled.", "detail": {"kind": "satellite", "status": 403}}}
```
