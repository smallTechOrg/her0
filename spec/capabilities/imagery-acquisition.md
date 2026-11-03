# Capability: Imagery Acquisition *(Phase 2)*

## What It Does
Collects far more imagery for a segment than Phase 1 — Street View at dense stations along the real road in several directions
plus close-up satellite crops along the footpath — and stores every image as a georeferenced, hashed, costed **observation**,
cached so repeats are free, with per-run guardrails and an admin way to fetch more (and to attach uploaded photos) later.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Reference line (clipped road polyline) | `ReferenceLine` | `street-model` / `hero.imagery.roads` | yes (straight chord fallback) |
| Imagery plan (spacing, headings, crop zoom/spacing, max images, optional `s` range) | `ImageryPlan` | env defaults + admin overrides | yes |
| Street View metadata (free) → pano id, position, date | JSON | Google Street View Static metadata | yes per station |
| Street View frames, satellite overview + close-up crops | images | Google Street View Static / Static Maps | per plan |
| Uploaded photo + camera pose | file + fields | admin API | no |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `observations` rows (kind, source, sha256, georeference, pose, station `s`/offset `t`/view, capture date, cost) | rows | DB |
| Image files (content-addressed) | files | `data/imagery/`, `data/uploads/` → `/media/…` |
| Rectified overhead windows (derived, for perception) | PNG | `data/previews/<run_id>/strip_<n>.png` |
| Acquisition counts for the run report: new / cached / skipped / cost | struct | `AcquireResult` → `RunReport.images` |
| `google_requests` rows (every attempt, hit or miss) | rows | DB |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Google Street View metadata (free, cached) | one per station → pano id, location, date (shared by the station's headings) | non-OK status → that frame recorded in `skipped`; never fails the run |
| Google Street View image (≈ $0.007) | one per distinct `(pano_id, heading, pitch, fov)` not already stored | 3 retries; then `skipped` with the reason |
| Google Static Maps (≈ $0.002) | overview tile (zoom `HERO_SATELLITE_ZOOM`), close-up crops (zoom `HERO_SAT_CROP_ZOOM`, falls back to 19 when `looks_like_no_imagery`) | overview failure fails the run (`upstream_error`) unless one is already stored; crop failure → skipped |
| Nominatim reverse / Overpass | road polyline | `None` → straight chord, warning |

## Business Rules
- **Default plan** (`HERO_SV_*`, `HERO_SAT_CROP_*`): stations every 10 m along the reference line (plus the end), headings `forward, back, left, right` relative to the road tangent (so both sides and the footpath are seen), 90° field of view, pitch 0, 640×640; close-up crops every 40 m (`ceil(length/40)` centres); the overview tile once. A 100 m street ⇒ 11 stations × 4 = **44 frames** + 3 crops + 1 overview.
- **Dedupe before paying**: Street View is requested by pano id; two stations that snap to the same panorama at the same heading are one observation. Image identity is `obs_key` (cache key for Google, `upload:<sha256>` for uploads); ingest is idempotent.
- **Guardrails**: at most `HERO_MAX_IMAGES_PER_RUN` (60) NEW paid images per run; if the plan needs more, the spacing is widened to fit and the report says so; the planner never silently drops headings. Estimated Google spend is shown before and after.
- **Cached is free**: a plan whose images are all stored makes **zero** Google image requests (metadata calls are free and cached too); every attempt still logs a `google.request` line with `cache_hit`.
- **Every observation carries**: georeference (lat/lon, heading/pitch/fov or zoom/scale/meters-per-pixel), file hash, source, cost, capture date (Street View), station `s`, lateral offset `t`, view class.
- **Acquire-only runs** (`POST …/imagery`) store observations without calling Claude; they are fused by the next Generate.
- **Uploads** (`POST …/observations/upload`): jpeg/png ≤ `HERO_UPLOAD_MAX_MB`, camera pose supplied, rejected if > 60 m from the reference line; stored as `user_photo` (eligible for L3). The admin upload UI is a labelled Phase-3 stub.
- Close-up crops make the footpath trace sharper: perception reads them rectified (straightened along the road) at ≈ 0.1 m/px.

## Success Criteria
- [ ] `plan_images` on a 100.0 m straight reference line with defaults returns 44 SV + 3 crop + 1 overview items; on a 400 m line with `max_images=60` it returns ≤ 60 paid items, `trimmed=true`, `effective_spacing_m > 10`, all four headings still present.
- [ ] `acquire` (Google replaced by a recorded-response fake HTTP transport) stores one observation per distinct `(pano_id, heading)`; two stations resolving to the same pano and heading produce one; each row has `station_s`, `offset_t`, `view`, `sha256` matching the file, `capture_date`, `cost_usd == 0.007` for fresh Street View and `0` when cached.
- [ ] Running `acquire` twice with the same plan: second result `new == 0`, `cached == planned paid items`, `google_requests` rows for the second run all have `cache_hit=true`, and no new observation rows exist.
- [ ] A station whose metadata is `ZERO_RESULTS` appears in `skipped` with the status and does not raise; a 403 on every frame raises `UpstreamError` only for the overview, never for frames.
- [ ] `looks_like_no_imagery` is true for the fixture placeholder tile and false for the fixture satellite tile; a crop that fails it is re-fetched at zoom 19 and the observation's `zoom` is 19.
- [ ] `rectify_window` on the fixture tile with a straight reference line returns an image of `grid.size_px`; for a curved fixture reference line a pixel placed on the reference line (`t = 0`) in the source maps to the vertical centre line of the output within 1 px; `grid.st_to_px(grid.px_to_st(x, y)) == (x, y)`.
- [ ] Upload endpoint: valid photo → 201 with `view`, `station_s`; non-image → 422; > limit → 422; camera 100 m from the line → 422; served at `/media/uploads/<sha256>.jpg` with 200.
- [ ] Live (Google real, Overpass degrade-only): for the 100 m demo segment the plan with `headings=[forward]` and spacing 20 stores ≥ 4 Street View observations with `capture_date` set, a second identical acquire reports `new == 0`.
