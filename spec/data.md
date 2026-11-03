# Data

Storage: one relational database (engine in `spec/architecture.md`), migrated. Large JSON
documents (scenes, step lists, graphs, canonical-model snapshots) are stored as JSON text
columns — read whole, never queried by field. Imagery lives on disk under the data dir and is
referenced by relative path.

Layering (Phase 2): **observations** (raw, georeferenced evidence) → **canonical street model**
(versioned, standardised, cumulative; `## Canonical street model`) → **compiled game scene**
(`## Scene JSON v2`, a pure function of one model version). Runs (`pipeline_runs`) are the
*events* that add observations and produce model versions; they never own the knowledge.
Phase-1 data (`segments`, `pipeline_runs`, `llm_calls`, `google_requests`, `game_runs`, Scene v1)
is unchanged and keeps working.

## Entities

### segments

| field | type | notes |
|---|---|---|
| id | uuid pk | |
| slug | text unique | url id, e.g. `100-feet-road-indiranagar-w`. Derived: slugify(name minus any parenthetical) + `-` + compass letter of the footpath side (`n|e|s|w`, from heading + side); `-2`, `-3`… suffix on collision; admin create may pass an explicit `slug` |
| name | text | "100 Feet Road, Indiranagar (west footpath)" |
| city | text | "Bengaluru" |
| start_lat, start_lon, end_lat, end_lon | float | 100–200 m apart when made from an address (validated 30–200 m) |
| footpath_side | enum `left|right` | side of the modelled footpath relative to travel direction start→end |
| length_m, heading_deg | float | derived at creation |
| status | enum `draft|generating|ready|failed` | `ready` iff `scene_json` present |
| quality | enum `full|satellite_only|generic` nullable | from the last successful run |
| scene_json | json text nullable | the current compiled Scene: **v1** (legacy, Phase-1 segments) or **v2** (`## Scene JSON v2`, segments with a canonical model v≥1). Discriminated by `schema_version`; `hero.model.scene_v2.load_scene()` parses either. Written only by `publish_version` (Phase 2) or the Phase-1 pipeline |
| thumbnail_path | text nullable | `previews/<run_id>/thumb.png` |
| latest_run_id | uuid nullable | logical FK to pipeline_runs (not DB-enforced in Phase 1) |
| best_score, best_distance_m | int / float nullable | denormalised from game_runs |
| created_at, updated_at | datetime | |

Lifecycle: `draft` → (generate) `generating` → `ready` | `failed` (keeps previous scene if
any; `ready` stays `ready` on a failed regeneration — the scene is only replaced on success).

### pipeline_runs

| field | type | notes |
|---|---|---|
| id | uuid pk | |
| segment_id | fk | |
| status | enum `queued|running|succeeded|failed|failed_budget` | |
| inputs_json | json | endpoints, side, models, zoom, samples, max_iterations, budgets at start. **Phase 2 adds** `mode` (`generate` \| `acquire`), `imagery_plan` (the resolved `ImageryPlan`, `## Imagery plan`), `base_version` (int \| null), `structured_mode` |
| steps_json | json | `[{name, status, started_at, latency_ms, detail}]`, appended live |
| imagery_json | json | Phase 1: `{satellite: TileRef, streetview: [PanoRef], osm_path, streetview_available}`. Phase 2: `{observation_ids: [...], new_observation_ids: [...], osm_path, osm_available, osm_error, reference_line_source, warnings}` (the images themselves are `observations` rows) |
| layout_json, obstacles_json | json nullable | Phase-1 runs only (raw pass A/B graphs). `null` for Phase-2 runs |
| iterations_json | json | `[{n, preview_path, accepted, critique, patch_ops, applied}]` (Phase 2: critique iterations, `patch_ops` = `CritiqueOp`s) |
| scene_json | json nullable | final scene of this run (v1 or v2); `null` for `acquire` runs and for no-op runs |
| input_tokens, output_tokens | int | sums over llm_calls |
| cost_usd | numeric(10,6) | sum over llm_calls |
| google_requests, google_cached_hits | int | requests made vs served from cache (sum = attempts) |
| error_json | json nullable | `{code, message, step}` |
| started_at, finished_at | datetime nullable | |

### llm_calls

| field | type |
|---|---|
| id uuid pk; run_id fk; step text; model text; input_tokens int; output_tokens int; cost_usd numeric; latency_ms int; ok bool; error text nullable; request_summary text (≤500 chars: prompt name, image count, image bytes); response_summary text (≤500 chars: top-level counts); created_at |

Global spend = `SUM(cost_usd)` over all rows (including failed calls that returned usage).

### google_requests

| field | type |
|---|---|
| id uuid pk; run_id fk nullable; kind enum `satellite|streetview|streetview_metadata`; cache_key text; cache_hit bool; est_cost_usd numeric (`satellite 0.002`, `streetview 0.007`, `metadata 0`); path text nullable; created_at |

### game_runs

| field | type |
|---|---|
| id uuid pk; segment_id fk; score int; distance_m float; tokens_collected int; loops int; crashed_into_type text nullable (object type) ; crashed_into_id text nullable; duration_s float; client text (≤120 chars UA summary); created_at |

Validation on insert: `0 ≤ score ≤ distance_m*1 + tokens_collected*50 + 1`; `distance_m ≤ 5000`;
`tokens_collected ≤ loops * tokens_in_scene + tokens_in_scene`. Out-of-range → 422.

### segment_corrections (Phase 3)

| field | type |
|---|---|
| id uuid pk; segment_id fk; patch_ops json (ops contract in `spec/capabilities/admin-correction.md`); result_version int (the `model_versions.version` of kind `edit` it created); created_at |

### observations *(Phase 2)*

First-class evidence. One row per distinct image ever acquired or uploaded for a segment. Never
updated or deleted after insert (except `fused_in_version`-style facts, which live in the model
snapshot, not here).

| field | type | notes |
|---|---|---|
| id | uuid pk | |
| segment_id | fk, indexed | |
| obs_key | text(128) | content-identity key; **unique with `segment_id`**. Google: `ImageryCache.key(kind, canonical_params)` (the cache file stem); upload: `upload:<sha256>`. Re-acquiring the same image returns the existing row (idempotent ingest) |
| kind | enum `satellite_overview|satellite_crop|streetview|user_photo` | |
| source | enum `google_static_maps|google_streetview|upload` | |
| sha256 | char(64) | hash of the stored file bytes |
| file_path | text | relative to the data dir, e.g. `imagery/<key>.jpg`, `uploads/<sha256>.jpg`; URL = `/media/<file_path>` |
| media_type | text | `image/png` \| `image/jpeg` |
| width_px, height_px, bytes | int | |
| lat, lon | float | camera position (streetview: the **panorama's** actual position from metadata; upload: user supplied) or tile centre (satellite) |
| heading_deg, pitch_deg, fov_deg | float nullable | camera pose (streetview/upload); null for satellite |
| zoom, scale, meters_per_px | int/int/float nullable | satellite only |
| pano_id | text nullable | Street View pano id |
| capture_date | text(7) nullable | `YYYY-MM` from Street View metadata `date` |
| station_s, offset_t | float nullable | camera foot point on the segment's reference line: station and signed lateral offset (`t`, left positive). Null if no reference line existed when ingested |
| view | enum `forward|back|left|right|oblique` nullable | streetview/upload: heading relative to the reference tangent at `station_s` (`|φ|≤35` forward, `|φ|≥145` back, `φ∈(35,145)` right, `φ∈(−145,−35)` left, else oblique; φ = wrap180(heading − tangent_bearing)) |
| georef_json | json | satellite: `{center_lat, center_lon, zoom, scale, size_px:[w,h], meters_per_px}`; streetview: `{size_px, request:{…canonical params…}}`; upload: `{size_px, note}` |
| cost_usd | numeric(10,6) | est. Google cost when freshly fetched (`0.007` streetview, `0.002` satellite, `0` cache hit/upload) |
| acquired_run_id | text nullable | the run that first ingested it |
| note | text nullable | uploads: free text |
| created_at | datetime | |

### model_versions *(Phase 2)*

Immutable snapshots of the canonical model. Unique `(segment_id, version)`; versions are dense
integers from 0.

| field | type | notes |
|---|---|---|
| id | uuid pk | |
| segment_id | fk, indexed | |
| version | int | `0` = base (kind `legacy` or `prior`); then `1, 2, …` |
| kind | enum `legacy|prior|fused|edit` | `legacy` = imported Phase-1 scene; `prior` = L0 map-only model (reference line + OSM template, no vision); `fused` = produced by a fusion run; `edit` = admin correction (Phase 3) |
| parent_version | int nullable | the version this one was merged onto (`null` for 0; for the first `fused` version of a legacy segment it is `0` but the lineage is fresh — nothing is merged from a legacy model) |
| run_id | text nullable | the run that produced it (`null` for legacy import) |
| model_json | json | the full `CanonicalModel` |
| scene_json | json nullable | the compiled Scene v2 exactly as served to players for this version (`null` only for `legacy`, whose scene is the Phase-1 one, stored here as v1) |
| fidelity_json | json | the `FidelityMap` (`## Fidelity`) |
| diff_json | json nullable | `ModelDiff` vs `parent_version` (`null` for version 0) |
| summary | text | one plain-language paragraph (`hero.model.describe.version_summary`) |
| model_hash | char(64) | `hero.model.diff.model_hash(model)` |
| entity_count, observation_count | int | present entities; fused observations |
| fidelity_level | int | overall level 0–3 (length-weighted floor) |
| created_at | datetime | |

### observation_extractions *(Phase 2)*

The raw, schema-validated perception output of each model call, kept so any model version can be
**replayed without the LLM** and so every claim can be audited.

| field | type |
|---|---|
| id uuid pk; segment_id fk indexed; run_id fk indexed; batch_index int; kind enum `overhead|streetlevel|critique`; observation_ids json (list, image order = `image_index`−1); model text; prompt_name text; extraction_json json; created_at |

### run_reports *(Phase 2)*

| field | type |
|---|---|
| run_id text pk (fk pipeline_runs); segment_id fk indexed; mode enum `generate|acquire`; base_version int nullable; result_version int nullable; version_created bool; report_json json (`RunReport`, `## Run report`); created_at |

#### Extraction provenance and cache *(slice SQ; no schema change)*
Every stored extraction (street pass, overhead, critique round) carries `extraction_json._prov = {model, prompt_name, prompt_version (8 hex of the prompt file), pass_index, cost_usd, image_shas, cache_key}`; the perception schemas ignore the extra key. `cache_key = sha256(kind, sorted image shas, model, prompt_version, pass_index[, hash of the model under review])`. A re-run replays an answer whose key matches (pass 0 of a street batch keeps the older "latest paid answer for these photos" rule) and makes no call for it. Records without `_prov` are pass 0. `max quality` merges the N passes of a batch **by agreement** (`hero.fusion.maxquality.agree`): detections within 2 m (s) / 1.5 m (t) of the same type are one object; confidence x (0.5 + 0.5 * seen_in / N); nothing is dropped; the result is one new `model_versions` row.

## Relationships

segment 1—n pipeline_runs 1—n llm_calls; pipeline_runs 1—n google_requests; segment 1—n game_runs;
segment 1—n segment_corrections; **segment 1—n observations; segment 1—n model_versions;
pipeline_runs 1—n observation_extractions; pipeline_runs 1—0..1 run_reports.** Evidence links
inside `model_json` refer to `observations.id` (logical FK, validated at publish).

On-disk additions: `data/uploads/<sha256>.<ext>`, `data/roads/` (cached road polylines, via
`ImageryCache`), `data/previews/<run_id>/strip_<n>.png` (rectified overhead windows sent to the
model), `data/previews/<run_id>/model_iter<N>.png`, `data/crops/<segment_slug>/<id>_<obs8>.png`
(immutable entity/facade crops: the file name carries the source observation prefix, so a new
best crop is a new file and old versions keep working).

---

## Scene JSON v1 (legacy contract — `schema_version: 1`; still produced for imported Phase-1 segments, still served and playable)

Coordinate frame: **x_m** runs along the segment, 0 at `start`, `length_m` at `end`.
**lat_m** is lateral offset from the road centreline in metres, **positive toward the
modelled footpath side**. Up is up. The game maps `x_m → -z` (runner travels toward -z) and
`lat_m → +x` in its 3D world; `tile_px` are pixel coordinates in the satellite tile image
(origin top-left, in the image's actual pixel size).

```jsonc
{
  "schema_version": 1,
  "segment": {
    "slug": "100-feet-road-indiranagar-w", "name": "100 Feet Road, Indiranagar (west footpath)",
    "city": "Bengaluru",
    "start": {"lat": 12.97400, "lon": 77.64050}, "end": {"lat": 12.97467, "lon": 77.64058},
    "length_m": 74.6, "heading_deg": 6.7, "footpath_side": "left"   // 74.6 is a fixture constant; a real seed segment with these endpoints computes 75.0 m by haversine
  },
  "ground": {
    "tile_url": "/media/imagery/3f9a…c1.png",           // satellite, 1280×1280 px
    "size_px": [1280, 1280], "zoom": 19, "scale": 2,
    "center": {"lat": 12.974335, "lon": 77.64054},
    "meters_per_px": 0.1455,
    "frame_to_px": {                                    // affine: px = origin + x_m*along + lat_m*across
      "origin_px": [610.2, 894.8], "along_px_per_m": [0.80, -6.83], "across_px_per_m": [-6.83, -0.80]
    }
  },
  "cross_section": {
    "road_width_m": 9.0,
    "lanes": [
      {"index": 0, "name": "footpath",  "kind": "footpath",  "lat_m": 5.4, "width_m": 1.8},
      {"index": 1, "name": "road_edge", "kind": "road_edge", "lat_m": 3.6, "width_m": 1.6},
      {"index": 2, "name": "road",      "kind": "road",      "lat_m": 1.8, "width_m": 2.0}
    ],
    "footpath_coverage": [                               // tiles [0, length_m] exactly, ordered
      {"from_m": 0.0,  "to_m": 22.0, "state": "intact",  "confidence": 0.9},
      {"from_m": 22.0, "to_m": 31.0, "state": "missing", "confidence": 0.7},
      {"from_m": 31.0, "to_m": 74.6, "state": "broken",  "confidence": 0.6}
    ]
  },
  "buildings": [
    { "id": "b1", "source": "osm",
      "footprint": [[0.0, 7.5], [28.0, 7.5], [28.0, 19.0], [0.0, 19.0]],   // [x_m, lat_m] polygon, ≥3 pts
      "height_m": 9.0, "color_hex": "#c9b79c",
      "facade_url": "/media/crops/<run_id>/b1_facade.png",  // nullable
      "confidence": 0.95 }
  ],
  "objects": [
    { "id": "o1", "type": "parked_vehicle", "label": "parked car on footpath",
      "x_m": 12.5, "length_m": 4.2, "lanes": [0, 1], "action": "dodge",
      "size_m": {"w": 1.8, "h": 1.5, "d": 4.2},
      "confidence": 0.85,
      "source": {"image_url": "/media/imagery/7b2d…e0.png", "crop_px": [210, 300, 180, 140]},  // nullable
      "billboard_url": "/media/crops/<run_id>/o1.png",     // nullable → game draws a labelled box
      "latlon": {"lat": 12.974116, "lon": 77.640472}, "tile_px": [589.5, 805.8] },
    { "id": "o2", "type": "footpath_gap", "label": "footpath missing",
      "x_m": 22.0, "length_m": 9.0, "lanes": [0], "action": "dodge", "size_m": {"w": 1.8, "h": 0.2, "d": 9.0},
      "confidence": 0.7, "source": null, "billboard_url": null,
      "latlon": {"lat": 12.974202, "lon": 77.640474}, "tile_px": [590.9, 740.2] },
    { "id": "o3", "type": "pothole", "label": "pothole", "x_m": 40.0, "length_m": 1.0, "lanes": [2],
      "action": "jump", "size_m": {"w": 1.0, "h": 0.1, "d": 1.0}, "confidence": 0.6,
      "source": {"image_url": "/media/imagery/9c…11.png", "crop_px": [300, 420, 90, 60]},
      "billboard_url": "/media/crops/<run_id>/o3.png",
      "latlon": {"lat": 12.974359, "lon": 77.640527}, "tile_px": [629.9, 620.2] },
    { "id": "o4", "type": "low_branch", "label": "low branch", "x_m": 55.0, "length_m": 1.0, "lanes": [0],
      "action": "slide", "size_m": {"w": 1.8, "h": 0.6, "d": 1.0}, "confidence": 0.5,
      "source": null, "billboard_url": null,
      "latlon": {"lat": 12.974497, "lon": 77.640510}, "tile_px": [617.3, 514.8] },
    { "id": "o5", "type": "crossing", "label": "zebra crossing", "x_m": 60.0, "length_m": 3.0, "lanes": [0, 1, 2],
      "action": "none", "size_m": {"w": 9.0, "h": 0.0, "d": 3.0}, "confidence": 0.8,
      "source": null, "billboard_url": null,
      "latlon": {"lat": 12.974540, "lon": 77.640532}, "tile_px": [633.6, 482.1] }
  ],
  "tokens": [
    { "id": "t1", "x_m": 8.0, "lane": 0, "reason": "intact_footpath",
      "latlon": {"lat": 12.974077, "lon": 77.640459}, "tile_px": [579.7, 835.9] },
    { "id": "t2", "x_m": 60.0, "lane": 1, "reason": "crossing",
      "latlon": {"lat": 12.974540, "lon": 77.640532}, "tile_px": [633.6, 482.1] }
  ],
  "walkability": {
    "footpath_coverage_pct": 29.5, "hazard_count": 4, "crossing_present": true, "score": 50
  },
  "generation": {
    "run_id": "…", "quality": "full", "confidence_overall": 0.72, "review_iterations": 2,
    "models": {"vision": "claude-sonnet-4-5", "review": "claude-sonnet-4-5"},
    "warnings": []
  }
}
```

### Enumerations and rules

- `objects[].type` ∈ `footpath_gap | parked_vehicle | pothole | open_drain | debris | pole | tree | signboard | low_branch | crossing | ramp`.
- `objects[].action` ∈ `jump | slide | dodge | none`. Default mapping (pipeline may override
  from the model's judgement only within these): `footpath_gap, parked_vehicle, pole, tree,
  signboard → dodge`; `pothole, open_drain, debris → jump`; `low_branch → slide`;
  `crossing, ramp → none` (informational, drawn flat, and they generate tokens).
- `lanes` is a non-empty subset of `{0,1,2}`; `0 ≤ x_m < length_m`; `x_m + length_m ≤ length_m + 5`.
- Hazards = objects with `action != none`. `low-confidence` = `confidence < 0.6` (UI label).
- `footpath_coverage[].state` ∈ `intact | broken | missing`; ranges are contiguous and cover
  `[0, length_m]`; `missing` ranges always have a matching `footpath_gap` object (assembly
  rule) and `broken` ranges may.
- Lane `lat_m` are derived from `road_width_m` (R) and `footpath_width_m` (F, default 1.8 when
  unknown): `footpath.lat_m = R/2 + F/2`, `road_edge.lat_m = R/2 - 0.9`, `road.lat_m = road_edge.lat_m - 1.8`
  (R = 9.0, F = 1.8 → 5.4 / 3.6 / 1.8 as in the example). Lane widths: footpath F, road_edge 1.6, road 2.0.
  R is clamped to [5, 14] before use. The three `lat_m` values must be strictly decreasing from lane 0 to 2 by ≥ 1.4 m.
- Position of an object across the segment for `tile_px`/`latlon`: the mean `lat_m` of its
  `lanes` (tokens: their lane). `tile_px = origin_px + x_m·along + lat_m·across` (affine in
  `ground.frame_to_px`, computed by `hero.scene.geo`); `latlon` is the same point via the local
  frame. The shipped example file must satisfy this within 1 px / 0.5 m (unit-tested).
- `tokens`: one per 10 m of `intact` coverage (placed in lane 0, mid-interval, never inside a
  hazard footprint), one per `crossing`/`ramp` object (lane 1 at the object's `x_m`); max 12.
- `walkability` (pure function `compute_walkability`):
  `footpath_coverage_pct = 100 * intact_length / length_m` (1 dp);
  `hazard_count = |hazards|`; `crossing_present = any(type == crossing)`;
  `score = clamp(round(0.6 * footpath_coverage_pct + (20 if crossing_present else 0) + 20 * max(0, 1 - hazard_count / 10)), 0, 100)`.
  > **Assumed:** formula weights; tune after play-testing with the campaigners (Phase 3). Scene v2 uses the same formula (`## Scene JSON v2`).
- `generation.quality`: `full` = satellite + street view + both passes valid; `satellite_only` =
  no Street View or pass B dropped (objects only from pass A: gaps, trees, crossings);
  `generic` = reserved for the map-only fallback; Phase 1 never produced it, **Phase 2 does**
  (v2 scenes compiled from an L0 `prior` model — see `segments.quality` mapping in `## Fidelity`).
- `generation.confidence_overall` = mean of object + coverage confidences (2 dp).

### Pass outputs (typed, stored raw on the run)

`LayoutGraph` (pass A): `{road_width_m, footpath_width_m, footpath_coverage[], buildings[{x_from_m, x_to_m, depth_m, side: "footpath"|"opposite", height_estimate_m, color_hex, confidence}], trees[{x_m, lat_m, confidence}], crossings[{x_m, confidence}], ramps[{x_m, confidence}], notes}`.
`ObstacleGraph` (pass B): `{objects[{type, label, pano_index, crop_px, x_m, lanes, length_m, size_m, action, confidence}], facades[{x_from_m, x_to_m, color_hex, pano_index, crop_px}], notes}`.
`ReviewResult`: `{accepted: bool, critique: str (≤1500 chars), patch_ops: [...] (≤10)}` — ops in `spec/agent.md`.

The canonical example above is shipped equivalent (placeholders like the `tile_url` hash /
`run_id` filled with real values) as `src/hero/scene/example_scene.json`
and copied to `tests/e2e/fixtures/scene_example.json` (with `tile_url` pointing at a 1280×1280
test PNG under `tests/e2e/fixtures/`); both must validate against `hero.scene.schema.Scene`.

---

# Canonical street model *(Phase 2 — `schema: "street-model/1"`)*

The standardised, versioned, cumulative description of one segment, independent of any run and
of the game scene. Borrowed ideas: OpenDRIVE (a **reference line** with a station/offset `(s, t)`
frame and a cross-section described as ordered strips that change along `s`), CityGML/glTF
**levels of detail** (here: per-interval **fidelity levels**), and a typed **asset catalogue**
(entity types with stable ids). Pydantic models live in `hero.model.types` (contract owner: slice
`S1a`); every JSON key is `snake_case`; unknown keys are rejected (`extra="forbid"`).

## Frames

- **Model frame `(s, t)`** — `s` = metres along the reference line from its first point
  (`0 ≤ s ≤ length_s`); `t` = signed lateral metres, **positive to the LEFT of the direction of
  travel** (OpenDRIVE). The reference line runs in the segment's travel direction (start → end).
  Valid for `|t| ≤ 30` and local radius of curvature ≥ 40 m; `ReferenceLine.to_st` picks the nearest
  polyline segment, `point_at(s, t)` offsets along the left normal. Plane geometry is the same
  equirectangular local frame as `hero.scene.geo` (origin = first reference point).
- **Game lateral `lat`** (used by scene v1 and v2, lanes, the runner): positive toward the
  **modelled footpath side**. `lat = t` when `footpath_side == "left"`, else `lat = −t`.
  `x_m` in scene objects equals `s`.
- Satellite/overview pixels and lat/lon relate to `(s, t)` only through `ReferenceLine` +
  `hero.scene.geo` (Web Mercator); there is no other mapping.

## CanonicalModel (illustrative numbers — the shipped fixture is generated by script, never hand-edited)

```jsonc
{
  "schema": "street-model/1",
  "segment_slug": "100-feet-road-indiranagar-canonical-demo-w",
  "version": 2, "parent_version": 1, "kind": "fused",          // legacy | prior | fused | edit
  "run_id": "5a1f…", "created_at": "2026-10-30T11:02:11Z",
  "legacy": false,                     // true only for kind=legacy (imported Phase-1 scene)
  "placement_verified": true,          // false for legacy; true when the reference line came from OSM (or Overpass) geometry
  "footpath_side": "left",             // the modelled side (travel direction); game lateral +lat points there
  "reference_line": {
    "source": "osm_nominatim",         // osm_nominatim | osm_overpass | straight_chord | legacy_straight
    "osm_way_id": 25173540, "name": "100 Feet Road",
    "points": [ {"lat": 12.970660, "lon": 77.641300, "s": 0.0},
                {"lat": 12.971110, "lon": 77.641312, "s": 50.1},
                {"lat": 12.971560, "lon": 77.641341, "s": 100.2} ],
    "length_s": 100.2,
    "extrapolated_m": {"start": 0.0, "end": 0.0}     // metres appended straight along the end tangents because the OSM way was shorter than requested
  },
  "profile": [                         // tiles [0, length_s] exactly, ordered, 2.5 m ≤ interval ≤ 30 m (except a final remainder ≥ 0.5 m)
    { "id": "pi-0000", "s_from": 0.0, "s_to": 22.0,
      "footpath_state": "intact",      // intact | broken | missing | blocked | encroached | unknown  (the MODELLED side only)
      "state_votes": {"intact": 2.4, "broken": 0.3},   // accumulated evidence weight per state
      "strips": [                      // ordered by t ascending, non-overlapping within an interval; gaps = unmodelled ground
        { "id": "sp-footpath-L", "kind": "footpath", "side": "left", "material": "paver", "condition": "fair",
          "height_m": 0.15,            // height above the adjacent carriageway surface
          "t_lo": [5.10, 5.20], "t_hi": [6.70, 6.60],      // [at s_from, at s_to]; edge positions vary LINEARLY inside the interval
          "weight": 2.4, "confidence": 0.91,
          "description": "Paver footpath ~1.5 m wide, kerb-high, slightly uneven.",
          "provenance": ["obs-…", "obs-…"] },
        { "id": "sp-carriageway-C", "kind": "carriageway", "side": "centre", "material": "asphalt", "condition": "fair",
          "height_m": 0.0, "t_lo": [-3.6, -3.6], "t_hi": [4.9, 5.0], "weight": 1.1, "confidence": 0.67,
          "description": "Two-way asphalt carriageway about 8.5 m wide.", "provenance": ["obs-…"] } ],
      "description": "0–22 m: intact paver footpath on the west side; carriageway about 8.5 m.",
      "evidence_summary": "Overhead close-up + 5 Street View frames." }
  ],
  "entities": [
    { "id": "ent-3f9a1c07d2", "type": "parked_vehicle", "subtype": "auto-rickshaw",
      "state": "present",              // present | disputed | removed  (removed entities are KEPT and listed; never deleted)
      "position": {"s": 12.5, "t": 5.9}, "position_sigma_m": 0.9,
      "extent": {"length_s": 2.6, "width_t": 1.4}, "height_m": 1.7, "orientation_deg": 4.0,   // orientation relative to the reference tangent (0 = parallel to travel)
      "attributes": {"colour": "yellow", "blocks_footpath": true, "footpath_clear_width_m": 0.4},
      "description": "Yellow auto-rickshaw parked half on the footpath ~12 m from the start; leaves about 0.4 m of walkway.",
      "confidence": 0.86, "logit": 1.82,
      "provenance": [ {"observation_id": "obs-…", "role": "supports", "detection_key": "obs-…:d3", "crop_px": [210, 300, 180, 140], "weight": 0.9, "added_in_version": 1, "note": null},
                      {"observation_id": "obs-…", "role": "supports", "detection_key": "obs-…:d1", "crop_px": [95, 310, 260, 190], "weight": 0.6, "added_in_version": 2, "note": null} ],
      "billboard": {"observation_id": "obs-…", "crop_px": [210, 300, 180, 140]},     // best crop (largest area × weight); null if none
      "first_seen_version": 1, "last_changed_version": 2 }
  ],
  "buildings": [
    { "id": "bld-osm-1827364", "source": "osm",       // osm | vision | mixed
      "osm_way_id": 1827364, "side": "left",
      "footprint": [[0.0, 9.0], [28.0, 9.0], [28.0, 21.0], [0.0, 21.0]],   // [s, t] polygon, ≥ 3 points
      "height_m": 9.6, "height_confidence": 0.4, "storeys": 3, "colour_hex": "#c9b79c",
      "facades": [ {"observation_id": "obs-…", "crop_px": [0, 120, 400, 300], "s_from": 2.0, "s_to": 14.0, "weight": 0.8} ],
      "description": "Three-storey cream shopfront block, ground-floor shops, set back 9 m from the centreline.",
      "confidence": 0.9, "provenance": ["obs-…"] }
  ],
  "contradictions": [
    { "id": "ctr-77aa…", "kind": "presence_conflict",     // type_conflict | presence_conflict | position_conflict | attribute_conflict | footpath_state_conflict
      "entity_ids": ["ent-3f9a1c07d2"], "interval": null,
      "description": "Seen in 3 frames from the left, but not visible in 2 later frames that should show it.",
      "observation_ids": ["obs-…", "obs-…"], "opened_in_version": 2, "resolved_in_version": null } ],
  "fused_observation_ids": ["obs-…", "…"],            // observations already merged; the ONLY source of "what is new" for the next run
  "fidelity": { "…": "FidelityMap, see ## Fidelity (derived; stored for convenience, recomputed on load in tests)" },
  "stats": {"entity_count": 14, "removed_count": 1, "building_count": 4, "observation_count": 44}
}
```

## Enumerations (single source: `hero.model.types`)

- `EntityType`: `tree, pole, parked_vehicle, signboard, drain, pothole, bollard, crossing, ramp, wall, gate, kerb_cut, bus_stop, shopfront, debris, low_branch, manhole, vendor_stall, bin, speed_breaker, railing, other`.
- `StripKind`: `footpath, kerb, verge, planting, parking, cycle_lane, carriageway, median, shoulder, forecourt, unknown`.
- `Material`: `concrete, paver, tile, asphalt, gravel, earth, grass, stone, mixed, unknown`. `Condition`: `good, fair, poor, broken, unknown`.
- `FootpathState`: `intact, broken, missing, blocked, encroached, unknown`. `missing` ⇒ **no** `footpath` strip on the modelled side in that interval; `blocked` (impassable) and `encroached` (narrowed/partly obstructed) keep the strip and are explained by entities/attributes; `unknown` ⇒ no trace evidence yet (only the OSM template).
- `EntityState`: `present, disputed, removed`. `EvidenceRole`: `supports, contradicts, prior, critique, human`.
- `ObservationKind`/`Source`/`view`: see `observations`.

## Entity catalogue (`hero.model.catalogue.CATALOGUE`: typed, versioned with the code)

| type | class | game action | default size w×h×d (m) | notes |
|---|---|---|---|---|
| tree | obstacle | dodge | 0.8×5.0×0.8 | `canopy_low` attribute true ⇒ also emits a `low_branch` hazard |
| pole | obstacle | dodge | 0.3×7.0×0.3 | utility/lamp; `subtype` |
| parked_vehicle | obstacle | dodge | 1.8×1.5×4.2 (two-wheeler 0.7×1.2×1.9) | `subtype` auto-rickshaw/car/two-wheeler/bus/truck |
| signboard | obstacle | dodge | 0.8×2.0×0.2 | ground-standing boards; overhead signs are `scenery` |
| drain | hazard | jump | 0.8×0.1×1.0 | open/uncovered drain or broken slab (`attributes.covered`) |
| pothole | hazard | jump | 1.0×0.1×1.0 | |
| debris | hazard | jump | 1.0×0.4×1.0 | rubble, garbage heaps |
| speed_breaker | hazard | jump | 3.0×0.12×0.6 | |
| low_branch | hazard | slide | 1.8×0.6×1.0 | overhead clearance < 2 m |
| bollard | obstacle | dodge | 0.2×0.8×0.2 | |
| bin | obstacle | dodge | 0.6×1.0×0.6 | |
| vendor_stall | obstacle | dodge | 1.5×2.0×1.5 | |
| railing | obstacle | dodge | 0.1×1.0×(extent) | |
| bus_stop | obstacle | dodge | 1.5×2.5×3.0 | shelter/pole |
| gate | obstacle | dodge | 3.0×2.0×0.2 | |
| wall | scenery | none (dodge when inside a lane band) | 0.3×2.0×(extent) | |
| manhole | marking | none (jump when `attributes.open`) | 0.7×0.05×0.7 | |
| crossing | marking | none | 9.0×0.0×3.0 | generates tokens |
| ramp | marking | none | 1.5×0.15×1.5 | generates tokens |
| kerb_cut | marking | none | 1.2×0.1×1.0 | |
| shopfront | scenery | none | — | attached to a building, never a hazard |
| other | obstacle | dodge if `attributes.blocks_footpath` else none | 1.0×1.0×1.0 | |

`legacy mapping`: Phase-1 `open_drain → drain`, `footpath_gap →` (not an entity: a `missing`
profile interval), all other Phase-1 type names are identical.

## Evidence, confidence and state rules (implemented in `hero.model.merge` / `hero.model.calibrate`)

- **Stable ids** — `entity_id = "ent-" + sha1(f"{segment_slug}|{type}|{first_observation_id}|{round(s,1)}|{round(t,1)}|{ordinal}")[:10]`, minted once when an entity is first created, where `ordinal` counts same-type entities already minted from the same observation. Buildings with an OSM way: `bld-osm-<way_id>`; vision-only: `bld-v-<sha1(slug|side|round(s_center))[:8]>`. Ids never change and are never reused; replaying the same batches onto the same base yields identical ids (merge is deterministic).
- **Calibration** — `calibrate_confidence(raw)` = `clip(0.05 + 0.85 · raw^1.3, 0.05, 0.9)` for model-reported confidences (a monotone shrink; the model is over-confident). > **Assumed:** parameters, until labelled data exists; they live in one constant table so recalibration is a one-file change.
- **Range weight** `w_range(r)`: `1.0` for `r ≤ 10 m`, linear to `0.6` at 20 m, to `0.3` at 30 m; detections farther than 30 m are not projected. **Position sigma** `σ = 0.4 + 0.1·r` m (+1.0 m when the range came from the model's estimate instead of the ground plane).
- **Matching** (one batch): a detection matches an existing entity of the same type (or a non-`other` entity vs `other`) when `hypot(Δs, Δt) ≤ gate = clip(2.5·√(σ₁²+σ₂²) + 0.25·max(extent), 1.2, 4.0)`; greedy by ascending distance, one-to-one per observation. A different-type entity inside the gate with confidence ≥ 0.5 on both sides opens a `type_conflict` contradiction (both kept).
- **Support** — for a new or matched detection with calibrated confidence `p` and view weight `w`: `logit += w · 0.75^k · logit(p)` where `k` = supporting observations already counted. Evidence is **unique per `(observation_id, detection_key)`** (re-merging the same batch is a no-op). New entities start at `logit = w·logit(p)`.
- **Absence** — if an entity lies inside an observation's visible region (`VisibleRegion`, weight `v ≥ 0.5`) of a *fused* observation that produced no matching detection: `logit −= 0.5 · v` and an `contradicts` evidence item is added (unique per observation). Absence is weaker than presence by design.
- **Confidence** = `clip(sigmoid(logit), 0.02, 0.98)`. `state`: `removed` when `confidence < 0.15` **and** ≥ 2 contradicting observations; `disputed` when it has both ≥ 1 support and ≥ 1 contradiction within the last two versions and `0.15 ≤ confidence < 0.5`; `present` otherwise. A `removed` entity returns to `present` when new support lifts `confidence ≥ 0.35` (same id). **No entity is ever deleted from the snapshot.**
- **Position/size** — precision-weighted running mean (`1/σ²` weights); `position_sigma_m` shrinks as `1/√Σ(1/σ²)`; `attributes` merge key-wise preferring the value with the larger accumulated weight; a key whose two sides each have weight ≥ 0.5 and disagree opens an `attribute_conflict`.
- **Description** — the description of the highest-weight supporting evidence wins; it is replaced only by a description whose evidence weight is ≥ 1.25× the current one.

## Profile rules (cross-section along `s`)

- Strip `confidence = min(0.95, 1 − exp(−weight))` (weight 0.15 prior → 0.14; 1.1 → 0.67; 2.4 → 0.91); an interval's `confidence` is that of its footpath strip.
- The profile is stored as intervals with linearly varying strip edges, but **fused on a 2.5 m grid**: resample the intervals to stations → fold in new `ProfileEvidence` (per strip kind+side: `t_edge = (W·t_old + w·t_obs)/(W + w)`, `W += w`) → fold state votes → re-simplify (merge neighbours when states match and all edges are collinear within 0.15 m; minimum interval 2.5 m; maximum 30 m). Deterministic; a grid station with no evidence keeps its old values.
- **Prior** (kind `prior`, also the starting point of every lineage): carriageway centred on the reference line, width `lanes × 3.0` (OSM `lanes`, default 2) or OSM `width`, clamped to [5, 14]; on each side a 0.15 m kerb, a 1.5 m footpath (`state: unknown`, `material: unknown`, `weight 0.15`, `confidence ≤ 0.14`) then a forecourt/verge. OSM `sidewalk=*`/mapped `footway=sidewalk` ways, when Overpass answers, replace the template footpath offsets (weight 0.4). The prior is replaced, not blended, as soon as traced evidence covers a station (prior weight 0.15 vs ≥ 0.5 for any trace).
- `footpath_state` = the argmax of `state_votes`; if the runner-up has ≥ 80 % of the leader's weight the leader is kept and a `footpath_state_conflict` contradiction is opened for that interval. `unknown` carries weight only from the prior.
- Both sides of the street are modelled. Only the modelled side (`footpath_side`) drives lanes, tokens and walkability; the opposite side is rendered as scenery.

## Fidelity (levels of detail per station interval)

`compute_fidelity(model, observations, bin_m=5.0) -> FidelityMap` — a pure function of the **fused**
observations (so it is monotone: adding evidence never lowers a level).

Constants (`hero.model.fidelity`): `BIN_M = 5.0`, `FWD_RANGE_M = 15.0`, `SIDE_RANGE_M = 6.0`, `MIN_STATION_GAP_M = 6.0`, `MIN_OVERLAP_M = 2.5`.

Coverage cells (`hero.model.coverage.observation_cells`): for a streetview/upload observation at
station `s_c` — `forward`: `[s_c, s_c+15]` both sides; `back`: `[s_c−15, s_c]` both sides;
`left`/`right`: `[s_c−6, s_c+6]` that side only (direct side view); `oblique`: `[s_c−6, s_c+6]` both sides.
A `user_photo` also yields `closeup` cells (`[s_c−6, s_c+6]`, side by facing). A `satellite_crop`
with `zoom ≥ 20` marks `overhead_hires` on every bin whose centre `(s, t=0)` and `(s, ±8)` fall inside the crop. A cell
covers a bin when their overlap is ≥ 2.5 m (or the whole bin if the bin is shorter).

| level | rule per bin |
|---|---|
| **L0** | no street-level observation covers the bin: satellite overview + OSM only (a hires overhead crop is reported separately as `overhead_hires`; it improves the profile trace but not the level) |
| **L1** | ≥ 1 street-level observation covers it (single viewpoint) |
| **L2** | street-level observations from ≥ 2 distinct stations (|Δs_c| ≥ 6 m) cover it, **and** a direct left-facing and a direct right-facing cell cover it |
| **L3** | L2 **and** ≥ 1 `user_photo` closeup covers it |

`FidelityMap` JSON:

```jsonc
{ "bin_m": 5.0, "length_s": 100.2,
  "bins": [ {"index": 0, "s_from": 0.0, "s_to": 5.0, "level": 1, "views": 4, "stations": 1,
             "sides": {"left": 1, "right": 1}, "overhead_hires": true,
             "hint": "Add a Street View frame facing left or right from another station within 15 m (needs a second viewpoint for L2)."} ],
  "overall": {"level": 1, "score": 1.4, "histogram_m": {"L0": 0.0, "L1": 60.2, "L2": 40.0, "L3": 0.0}} }
```
`overall.score` = length-weighted mean level (2 dp); `overall.level = floor(score)` (so `L2` overall means ≥ 2.0 on average).
`hint` is deterministic (the cheapest missing condition for the next level). `segments.quality` on publish:
`full` if overall level ≥ 1, `satellite_only` if level 0 but ≥ 1 overhead observation was fused, `generic` if nothing but the map prior exists.

## Model diff (`hero.model.diff.diff_models(a, b, fid_a, fid_b) -> ModelDiff`; stored in `model_versions.diff_json`)

```jsonc
{ "from_version": 1, "to_version": 2, "is_empty": false,
  "entities": { "added": [{"id": "ent-…", "type": "bollard", "s": 41.2, "t": 5.5, "confidence": 0.58, "description": "…"}],
                "removed": [{"id": "ent-…", "type": "tree", "s": 70.0, "t": 6.0, "reason": "not seen in 2 later views; confidence 0.62 → 0.12"}],
                "revived": [],
                "changed": [{"id": "ent-3f9a1c07d2", "fields": {"confidence": [0.74, 0.86], "position": [[12.4, 5.8], [12.5, 5.9]], "attributes": {"colour": ["white", "yellow"]}}, "reason": "2 more frames supported it"}],
                "unchanged": 11 },
  "profile": { "changed_intervals": [{"s_from": 22.0, "s_to": 31.0, "what": "footpath_state broken → missing; footpath edge moved 0.4 m"}] },
  "buildings": { "added": [], "changed": [], "removed": [] },
  "fidelity": { "improved": [{"s_from": 0.0, "s_to": 100.2, "from": "L1", "to": "L2"}], "unchanged_len_m": 0.0, "overall": [1.0, 2.0] },
  "contradictions": { "opened": [], "resolved": [] },
  "observations": { "fused_added": 22 },
  "summary_lines": ["22 new photos fused (22 Street View)", "3 new objects: 2 bollards, 1 pole", "Fidelity L1 → L2 over 100 m"] }
```
A `changed` entry is emitted for a confidence delta ≥ 0.05, a position move ≥ 0.4 m, or any change in type/subtype/state/attributes/description. `is_empty` is true iff nothing in this document differs (including `fused_added == 0`).

## Imagery plan

```jsonc
{ "sv_spacing_m": 10.0, "headings": ["forward", "back", "left", "right"],
  "sat_crop_zoom": 20, "sat_crop_spacing_m": 40.0, "include_overview": true,
  "max_images": 60, "s_range": null }       // s_range: {"s_from":…, "s_to":…} restricts SV stations (admin "add imagery here")
```
Defaults come from settings (`HERO_SV_SPACING_M`, `HERO_SV_HEADINGS`, `HERO_SAT_CROP_ZOOM`, `HERO_SAT_CROP_SPACING_M`, `HERO_MAX_IMAGES_PER_RUN`). `plan_images` (`hero.imagery.plan`): stations at `s = 0, spacing, 2·spacing …` plus `length_s` when the remainder ≥ 0.5·spacing; one planned SV image per `(station, heading)`; satellite crops at `n = ceil(length_s / sat_crop_spacing_m)` centres `(i+0.5)·length_s/n`; the overview is always planned. If `#SV > max_images` the station spacing is widened (`spacing' = ceil_to_1m(length_s / (floor(max_images/#headings) − 1))`) and the plan records `trimmed: true` + the effective spacing. A planned image whose `obs_key` already exists as an observation is **cached** (no Google call, no cost).

## Run report (`RunReport`; table `run_reports`, endpoint `GET /api/admin/runs/{id}/report`)

```jsonc
{ "run_id": "…", "segment_slug": "…", "mode": "generate", "base_version": 1, "result_version": 2, "version_created": true,
  "started_at": "…", "finished_at": "…", "duration_s": 171.4,
  "images": { "planned": 48, "used_total": 48, "new": 22, "cached": 26, "fused_this_run": 22,     // used_total = planned − skipped = new + cached
              "by_source": {"google_streetview": 44, "google_static_maps": 4, "upload": 0},
              "by_kind": {"satellite_overview": 1, "satellite_crop": 3, "streetview": 44, "user_photo": 0},
              "by_view": {"forward": 11, "back": 11, "left": 11, "right": 11},
              "new_observation_ids": ["…"], "skipped": [{"station_s": 90.0, "view": "left", "reason": "streetview_zero_results"}] },
  "detections": { "new": [{"id": "ent-…", "type": "bollard", "description": "…", "confidence": 0.58}],
                  "changed": [{"id": "ent-…", "what": "confidence 0.74 → 0.86"}],
                  "removed": [], "contradicted": [{"id": "ctr-…", "description": "…"}], "unchanged_count": 11 },
  "profile_changes": ["22–31 m: footpath now 'missing' (was 'broken'), 4 frames agree"],
  "fidelity": { "before": {"overall": 1.0, "histogram_m": {"L1": 100.2}}, "after": {"overall": 2.0, "histogram_m": {"L2": 100.2}},
                "improved_intervals": [{"s_from": 0.0, "s_to": 100.2, "from": "L1", "to": "L2"}] },
  "confidence_changes": [{"entity_id": "ent-…", "type": "parked_vehicle", "before": 0.74, "after": 0.86, "reason": "2 more supporting frames"}],
  "cost": { "anthropic_usd": 0.27, "anthropic_calls": 4, "input_tokens": 51200, "output_tokens": 7400,
            "google_new_requests": 22, "google_cached": 26, "google_est_usd": 0.154,
            "total_usd": 0.424, "global_budget_usd": 50.0, "budget_remaining_usd": 49.2, "share_of_global_budget": 0.0085 },
  "warnings": [], "degraded": false,
  "summary": "Run added 22 new Street View photos (26 of 48 images were already cached) …",   // deterministic template, no LLM
  "suggestions": ["Metres 60–75 are the least certain (L2, 3 low-confidence objects): a close-up photo there would raise them to L3."] }
```
`summary`/`suggestions` come from `hero.model.report.build_run_report` (pure; tested by substring). A no-op
run (nothing new to fuse) has `version_created: false`, `cost.anthropic_usd: 0.0`, and a summary that says so.

---

# Perception output contracts *(Phase 2; `hero.fusion.schemas`, owner `S3b`)*

Raw model outputs, validated by Pydantic, stored verbatim in `observation_extractions`. The model
perceives; code projects and merges. Pixel boxes are `[x, y, w, h]` in the sent image's own pixel
space (images are sent at the recorded size; the caption states it). `image_index` is 1-based in
caption order.

**OverheadExtraction** — rectified top-down windows (one call; ≤ 4 windows):

```jsonc
{ "windows": [ {
    "image_index": 1,
    "strips": [ { "kind": "footpath", "side": "left", "material": "paver", "condition": "fair",
                  "edge_a_px": [[212, 0], [214, 120], [210, 300]],         // two bounding edges, each ≥ 2 points, y non-decreasing
                  "edge_b_px": [[251, 0], [249, 120], [247, 300]],
                  "confidence": 0.8, "description": "…" } ],
    "footpath_runs": [ {"side": "left", "y_from_px": 0, "y_to_px": 140, "state": "intact", "confidence": 0.8, "note": "continuous pavers"},
                       {"side": "left", "y_from_px": 140, "y_to_px": 210, "state": "missing", "confidence": 0.7, "note": "dirt gap, construction"} ],
    "objects": [ {"type": "tree", "box_px": [180, 90, 40, 40], "attributes": {"canopy_low": false}, "description": "…", "confidence": 0.7} ],
    "notes": "≤ 300 chars" } ] }
```
Each rectified image's axes: **up = direction of travel**, image-left = left of travel; the caption gives `s_top, s_bottom, t_left, t_right, metres_per_px` so `y_px → s = s_top − y·mpp`, `x_px → t = t_left − x·mpp` (`hero.model.refline.RectGrid`). 10 m `s`-ticks and 2 m `t`-ticks are drawn on the image. `footpath_runs` must tile each reported side's visible `y` range without overlap.

**StreetExtraction** — a batch of ≤ `HERO_PERCEPTION_BATCH_IMAGES` Street View / uploaded frames:

```jsonc
{ "frames": [ {"image_index": 1, "visibility_ok": true, "footpath_visible": true, "footpath_side": "left",   // left|right|both|none
               "footpath_state": "broken", "notes": "…≤200 chars"} ],
  "detections": [ { "local_id": "d3", "image_index": 2, "type": "parked_vehicle", "subtype": "auto-rickshaw",
                    "box_px": [210, 300, 180, 140], "touches_ground": true, "est_distance_m": 9.0,
                    "size_m": {"w": 1.4, "h": 1.7, "d": 2.6}, "orientation": "parallel",       // parallel | perpendicular | unknown
                    "attributes": {"colour": "yellow", "blocks_footpath": true},
                    "description": "Yellow auto-rickshaw parked half on the footpath.", "confidence": 0.8 } ],
  "facades": [ {"image_index": 3, "box_px": [0, 120, 400, 300], "side": "left", "colour_hex": "#c9b79c",
                "storeys": 3, "use": "shops", "description": "…", "confidence": 0.7} ],
  "notes": "≤ 300 chars" }
```

**CritiqueResult** (reflection over a top-down render of the model + satellite crops):
`{accepted: bool, critique: str ≤1200, ops: [CritiqueOp] ≤8}`; `CritiqueOp` = `{op: "flag_entity", entity_id, reason}` \| `{op: "nudge_entity", entity_id, ds_m, dt_m, reason}` (|ds|,|dt| ≤ 3) \| `{op: "set_footpath_state", side, s_from, s_to, state, reason}` \| `{op: "nudge_edge", kind, side, s_from, s_to, dt_m, reason}` (|dt| ≤ 1). Applied by `hero.model.merge.apply_critique` as `critique`-role evidence of weight 0.3; it can lower/raise confidence by at most 0.15 and can never delete.

---

# Scene JSON v2 *(Phase 2 — `schema_version: 2`; compiled deterministically from one model version)*

Additive superset of v1 so every existing consumer (walkability card, hazard map, results list,
runner) keeps working: all v1 keys keep their v1 meaning and shape (`x_m` = `s`, `lat_m` =
game lateral). New keys: `path`, `corridor`, `about`, per-object `entity_id/description/evidence/scenery`,
`model`. Pydantic: `hero.model.scene_v2.SceneV2` (`extra="forbid"`); `load_scene(dict)` returns `Scene` or `SceneV2`.

**Game world frame** (`path`, `corridor`): the runner advances toward −z; the footpath side is always
+x (v1's mirroring convention). Let `lat_sign = +1 if footpath_side == "left" else −1` (`lat = lat_sign · t`)
and `turn_sign = −1 if footpath_side == "left" else +1`. With `θ(s) = turn_sign · radians(bearing(s) − bearing(0))`
(wrap to (−π, π]), forward `f(s) = (sin θ, −cos θ)`, footpath-side normal `n(s) = (cos θ, sin θ)` (x, z), the centreline
`P(0) = (0, 0)`, `P(s+ds) = P(s) + f(s)·ds` (integrated on the sample grid), and the world position of `(s, lat)` is
`P(s) + lat · n(s)`. For a straight road `θ ≡ 0`: `(s, lat) → (lat, −s)` exactly as in v1. Because `n′ = −θ′·f`, the curve of
constant `lat` has speed `|1 − lat·θ′|` per metre of `s` (θ′ = dθ/ds), so on a constant-curvature
reference line its world length over `[s0, s1]` is `(s1−s0)·|1 − lat·θ′|` — a unit-tested property
(`tests/unit/test_compile.py`, tolerance 1 cm, circle radius 80 m built in the test).

```jsonc
{
  "schema_version": 2,
  "segment":  { /* as v1 SegmentInfo; length_m = model.reference_line.length_s */ },
  "ground":   { /* as v1 Ground; tile_url may be null → flat colour; frame_to_px = best-fit affine of the chord (compat only) */ },
  "path": { "length_m": 100.2,
            "samples": [ {"s": 0.0, "x": 0.0, "z": 0.0, "theta": 0.0, "tile_px": [655.2, 877.4]}, … ] },     // every 2.5 m + the end; strictly increasing s
  "corridor": { "step_m": 2.5, "ground_half_m": 30.0,
    "stations": [ {                                   // index-aligned with path.samples
        "s": 0.0,
        "lane_lat": [5.9, 4.1, 2.3],                  // lane centres (game lateral). lane0 = footpath centre (interpolated when missing), lane1 = carriageway_edge − 0.9, lane2 = lane1 − 1.8; lane0−lane1 ≥ 1.4 enforced (footpath centre is pushed out otherwise)
        "footpath": {"lo": 5.1, "hi": 6.7, "state": "intact", "material": "paver", "height_m": 0.15, "confidence": 0.91} | null,   // null ⇔ missing
        "strips": [ {"kind": "carriageway", "lo": -4.9, "hi": 3.6, "material": "asphalt", "height_m": 0.0, "confidence": 0.67}, … ],   // modelled side then beyond; ordered by lo
        "ground_uv": [[0.512, 0.401], [0.512, 0.612]]  // tile uv at lat = −ground_half_m and +ground_half_m
    }, … ] },
  "cross_section": { /* v1 shape. road_width_m = mean carriageway width; lanes[i].lat_m = length-weighted mean of lane_lat[i]; width_m 1.5/1.6/2.0 ;
                       footpath_coverage[] = profile intervals, state ∈ intact|broken|missing|blocked|encroached|unknown (extends v1's three), tiles [0,length] */ },
  "buildings": [ { /* v1 Building (footprint in [s, lat], facade_url) + */ "id": "bld-osm-1827364", "side": "modelled|opposite", "description": "…" } ],
  "objects": [ { /* v1 SceneObject fields + */
      "id": "ent-3f9a1c07d2", "entity_id": "ent-3f9a1c07d2",
      "type": "parked_vehicle",                  // the catalogue type verbatim (superset of v1 ObjectType; v2 consumers use TYPE_COLORS[type] ?? ACTION_COLORS[action]); synthetic ids "gap-…" use type "footpath_gap"
      "label": "auto-rickshaw parked on footpath", "x_m": 12.5, "length_m": 2.6, "lanes": [0, 1], "action": "dodge",
      "lateral_m": 5.9, "yaw_deg": 4.0, "size_m": {"w": 1.4, "h": 1.7, "d": 2.6}, "confidence": 0.86,
      "scenery": false,                                                                 // true ⇒ outside every lane band: drawn, never a hazard (action forced "none")
      "description": "Yellow auto-rickshaw parked half on the footpath ~12 m from the start; leaves about 0.4 m of walkway.",
      "evidence": {"observation_count": 3, "sources": {"street_view": 3, "satellite": 0, "upload": 0}, "summary": "Seen in 3 Street View frames (2 from ≤ 10 m).", "observation_ids": ["obs-…"], "state": "present"},
      "billboard_url": "/media/crops/<slug>/ent-3f9a1c07d2_ab12cd34.png?v=2", "source": {"image_url": "/media/imagery/…jpg", "crop_px": [210, 300, 180, 140]},
      "latlon": {"lat": 12.97122, "lon": 77.64125}, "tile_px": [640.1, 840.3] } ],
  "tokens": [ /* v1 Token; lane 0 tokens sit on the footpath-strip centre */ ],
  "walkability": { /* v1 Walkability, same formula; intact = state "intact" only */ },
  "about": {
      "model_version": 2, "fidelity": {"level": "L2", "score": 2.0, "label": "Multi-view: every part seen from at least two places, both sides",
                                       "histogram_m": {"L0": 0, "L1": 0, "L2": 100.2, "L3": 0}},
      "photos": {"total": 48, "street_view": 44, "satellite": 4, "user": 0},
      "last_updated": "2026-10-30", "capture_dates": {"oldest": "2023-04", "newest": "2024-02"},
      "low_confidence_parts": [ {"s_from": 60.0, "s_to": 75.0, "reason": "2 objects below 60 % confidence; footpath state seen from one side only"} ],
      "summary": "This street is modelled at fidelity L2 (multi-view) from 48 photos … 100 m of 100 Feet Road, Indiranagar …",   // plain language; deterministic template `hero.model.describe.about_summary`
      "improve_hint": "A close-up photo around 60–75 m would raise it to L3.",
      "legacy": false, "placement_verified": true, "degraded": false },
  "generation": { /* v1 Generation: run_id, quality, confidence_overall, review_iterations, models, warnings */ },
  "model": { "version": 2, "model_hash": "…", "compiled_by": "hero.model.compile/1" }
}
```
Compile rules (`hero.model.compile.compile_scene(model, *, observations, assets, tile) -> SceneV2`; no clock, no randomness, no network):
- **Stations** every 2.5 m; strips are evaluated at each station by interpolating the profile edges; the modelled-side `footpath` strip gives `lane_lat[0]` = its centre (when `missing`: the nearest known centre, linearly interpolated across the gap, else the prior centre); `carriageway_edge` = the `lat` of the carriageway's edge nearest the footpath.
- **Loop seam** — the last 6 m of `lane_lat`/strips are linearly blended toward the station-0 values (`blend: true` is not emitted; the blend is baked) so a rigid copy of the segment chained at the end continues smoothly; entities in the blend zone are kept but clamped to lanes by the same rule. > **Assumed:** 6 m is enough for a ≤ 1.5 m lateral step.
- **Objects** — present and `disputed` entities with `confidence ≥ 0.2`; `removed` entities are omitted from `objects` but listed in `about.low_confidence_parts` text as "previously seen". Lane assignment: an entity occupies lane `i` when its lateral extent overlaps the lane band `lane_lat[i] ± width_i/2` by ≥ 0.15 m (`width = (footpath width, 1.6, 2.0)`); none ⇒ `scenery: true`. `action` from the catalogue (+ attribute overrides), `none` for scenery. Footpath `missing` intervals add synthetic `footpath_gap` objects (`id: "gap-<interval id>"`, `confidence` = the interval's), `blocked` adds a `dodge` object spanning the interval (`type: "debris"`, `label: "footpath blocked"`).
- **Tokens/walkability** — v1 rules, applied to the compiled objects and the modelled-side profile (`blocked/encroached/broken/unknown` do not count as intact).
- **About** — `hero.model.describe.about_summary(...)`; contains the strings `fidelity L{n}`, `{N} photos`, `model v{version}`, the street name, and (when present) the `low_confidence_parts` list; for `legacy` models: "legacy, placement unverified"; for an L0 prior: "map-only sketch" (`degraded: true`).
- **Determinism** — `compile_scene(m, …)` called twice, or on a model reloaded from JSON, yields byte-identical `model_dump_json()` (unit-tested). `created_at` is read from the model, never from the clock.

> Phase 2.1 supersedes the compile output with **Scene JSON v3** (below). v2 stays readable/servable; the compile rules above that
> v3 does not mention (stations, lanes, loop seam, lane assignment, tokens, walkability formula) are unchanged.

---

# Phase 2.1 additions *(realism + add-to-model)*

## Runtime classification (dynamic vs fixed) — `hero.model.dynamic`

The canonical model is **not changed** (`street-model/1`, no new field, nothing deleted). The classification is a pure function
`runtime_of(entity, footpath_band) -> "fixed" | "dynamic_context"` evaluated at compile time; a catalogue column `runtime` in
`hero.model.catalogue` holds the type default, the position rule refines it. `footpath_band` = the modelled-side footpath strip at the
entity's `s` (`[lo, hi]` in game lateral, widened by 0.3 m; when the footpath is `missing`/`unknown`, the prior band `[edge+0.15, edge+1.65]`).

| entity type | runtime | rule |
|---|---|---|
| `tree, pole, bollard, bin, railing, signboard, bus_stop, drain, manhole, kerb_cut, ramp, crossing, shopfront` | **fixed** | footpath-attached or fixed furniture/markings. `signboard` stays fixed everywhere (frontage boards) |
| `wall, gate` | **fixed** | become frontage segments (`kind: wall|gate`), and `objects[]` hazards when inside a lane band |
| `low_branch` | **fixed** | tree-attached (also emitted from `tree.canopy_low`) |
| `pothole` | **fixed** if its lateral centre lies in `footpath_band` (footpath surface condition), else `dynamic_context` | |
| `debris` | **fixed** if in `footpath_band` (obstructed footpath), else `dynamic_context` (carriageway debris) | |
| `parked_vehicle, vendor_stall, speed_breaker` | **dynamic_context** | movable / carriageway |
| `other` | **dynamic_context** unless `attributes.blocks_footpath` is true **and** the centre is in `footpath_band` (then fixed) | |
| pedestrians (not in the catalogue today; any future `person`/`animal` type) | **dynamic_context** | |

Footpath *state* intervals (`intact|broken|missing|blocked|encroached`), the synthetic `footpath_gap`/`blocked` objects, kerb, kerb cuts and
ramps are always fixed. A `parked_vehicle` that makes an interval `encroached`/`blocked` still counts in the **walkability** and in the
interval *state* (that is evidence about the footpath, kept in the model); it just is no longer drawn as a solid object. `walkability.hazard_count` counts compiled
`objects[]` hazards only (so it can drop vs v2 on streets with many vehicles; the results card says "static hazards"; > **Assumed:** acceptable, vehicles move in reality — revisit after play-testing).
`dynamic_context` entities appear in neither `objects[]` nor `furniture[]` nor `tokens` placement.

## Gap analysis — `hero.model.gaps.analyze_gaps(model, observations, fidelity, textures) -> GapReport`

Pure; types in `hero.model.gap_types` (slice `S1a`). Bin grid = fidelity bins (`bin_m = 5.0`, last bin shorter). For every bin and every
layer it records **evidence** (what the data really supports) and **rendered** (what the compiled scene shows). The compile guarantees
`rendered != "blank"` everywhere; whatever was not evidence-backed is **interpolated** and listed.

```jsonc
{ "gap_report": 1, "model_version": 1, "bin_m": 5.0, "length_s": 100.2,
  "bins": [ { "index": 0, "s_from": 0.0, "s_to": 5.0, "fidelity_level": 1,
      "ground":    {"evidence": "satellite_hires",            "rendered": "satellite_hires"},   // evidence: satellite_hires | satellite_overview | none ; rendered: satellite_hires | satellite_overview | interpolated | procedural
      "frontage":  {"modelled": {"evidence": "osm+photo", "rendered": "osm"},                     // evidence: osm | photo | osm+photo | none ; rendered: osm | vision | interpolated | prior
                    "opposite": {"evidence": "none",      "rendered": "interpolated"}},
      "footpath":  {"evidence": "traced",                   "rendered": "traced"},               // evidence: traced | state_only | prior ; rendered: traced | state_only | interpolated | prior
      "furniture": {"evidence": "street_level"},            // street_level | none  (informational: no objects are invented)
      "hint": "No right-facing photo covers 0–5 m: add a Street View frame facing right, or upload a photo taken from the right footpath." } ],
  "uncovered": {"ground": 0, "frontage_modelled": 2, "frontage_opposite": 5, "footpath": 1, "furniture": 3},      // bin counts with evidence == none/prior
  "blank_after_compile": {"ground": 0, "frontage_modelled": 0, "frontage_opposite": 0, "footpath": 0},        // MUST all be 0 (asserted)
  "interpolations": [ {"what": "frontage", "side": "opposite", "s_from": 40.0, "s_to": 60.0, "method": "neighbours",   // what: ground_texture|frontage|footpath_state|kerb|furniture_unknown ; method: neighbours|prior|osm|procedural
                       "from_s": [35.0, 60.0], "note": "No photo or footprint covers the opposite side here; frontage blended between 35 m and 60 m."} ] }
```
Evidence rules: `ground` — `satellite_hires` when `compute_fidelity` marks `overhead_hires` for the bin, `satellite_overview` when the overview tile covers `(s, ±8)`, else
`none`. `frontage` per side — `osm` when a footprint (or `wall`/`gate` entity) lies within 35 m of the reference line on that side across ≥ 2.5 m of the bin; `photo` when a street-level
observation with `view` toward that side (left/right direct, or forward/back oblique at weight ≥ 0.5) covers the bin (`coverage.observation_cells`), `osm+photo` both. `footpath` — `traced` when a
footpath strip with weight ≥ 0.5 exists, `state_only` when only state votes exist, `prior` otherwise. `furniture` — `street_level` when any street-level observation covers the bin.
Stored in `model_versions.gaps_json`; served by `GET /api/admin/segments/{slug}/gaps`; summarised in `scene.about`.

**Interpolation (compile-time, deterministic)** — `frontage`: nearest evidence-backed bins within 30 m on each side → linear blend of `lat`/`height_m`/colour (`neighbours`); one-sided → copy + seeded jitter ≤ 0.4 m / 1.2 m (`neighbours`);
none on that side → neutral prior (`prior`): modelled-side wall plane at `footpath.hi + 1.5`, opposite side at `|far edge of opposite strips| + 1.5` (fallback `carriageway.lo − 3.0`), height 6.0–11.0 m, colour from a 6-entry neutral palette, both chosen by
`sha1(slug|side|bin)`. `ground`: textures are interpolated by the **bake** step (feathered extension of the nearest covered bins; bins with no source anywhere stay `procedural`). `footpath_state`: `unknown` intervals take
the nearest known neighbour state at confidence × 0.5 only for *rendering colour* (the model state is untouched, walkability treats `unknown` as not intact). `kerb`: default 0.15 m wherever a footpath strip is rendered.

## Texture references — `hero.model.texture_types` (produced by `hero.imagery.bake.bake_textures`, consumed by compile)

```python
class GroundStripRef(BaseModel):   # one straightened, road-aligned image of the whole ±14 m band
    url: str                       # "/media/textures/<slug>/ground_v<N>_<sha8>.jpg"
    size_px: tuple[int, int]       # (width, height); longest side <= 1024
    s_from: float; s_to: float; lat_lo: float; lat_hi: float; m_per_px: float
    bin_source: list[Literal["satellite_hires", "satellite_overview", "interpolated", "procedural"]]   # one per 5 m bin
class FrontageStripRef(BaseModel): # straightened facade ribbon for one side: u = s/length, v = 0 ground .. 1 top (12 m)
    side: Literal["modelled", "opposite"]; url: str; size_px: tuple[int, int]   # (<=1024, <=256)
    s_from: float; s_to: float; bin_source: list[Literal["user_photo", "street_view", "interpolated", "none"]]
class TextureRefs(BaseModel):
    ground: GroundStripRef | None; frontage: dict[str, FrontageStripRef]   # keys "modelled" / "opposite"
    tints: dict[str, str]          # entity_id -> "#rrggbb" median colour of the entity's best crop (poles, trees, bins, bollards …)
```
Files are content-addressed (`sha8` in the name) under `data/textures/<slug>/`, so a recompile that changes nothing reuses them and old scenes keep working.

> **r2 amendment (bake frame).** `GroundStripRef` is indexed by the OpenDRIVE frame, not game lateral: replace `lat_lo/lat_hi` by `t_lo = −14.0`, `t_hi = +14.0`; **column 0 = `t_hi` (left of travel), row 0 = `s_to`** (the `RectGrid` convention; `u = (t_hi − t)/(t_hi − t_lo)`, `v = (s_to − s)/(s_to − s_from)`). The image is never mirrored. `FrontageStripRef` is unchanged but its `side` is still `modelled|opposite` (the mesh builder maps to left/right by `footpath_side` and stacks both ribbons into the facade atlas, `spec/portability.md` §3). The bake **paints every gap into the image** (feathered extension of covered bins; otherwise a procedural pavement/asphalt/facade pattern seeded by `procedural_seed`), so downstream code never sees a blank texel; `bin_source` still records which bins were real vs interpolated vs procedural. The glb embeds these bytes; `data/textures/` remains the cache/source.

## Derived-cache columns — Alembic `0003_scene_schema` (idempotent; nothing in the knowledge tables changes)

| table | column | notes |
|---|---|---|
| `model_versions` | `scene_schema int not null default 2` | `schema_version` of `scene_json` (backfilled from the JSON for existing rows; stale = `< 3`) |
| `model_versions` | `compiled_by varchar(64) null` | `scene.model.compiled_by` (`hero.model.compile/2` for v3) |
| `model_versions` | `gaps_json json null` | the `GapReport` of this version's compile (null until recompiled) |
| `segments` | `scene_schema int null` | denormalised from the served `scene_json` (null = no scene) |
| `model_versions` | `export_json json null` | **r2:** `ExportRecord` of this version's bundle: `{mesh_sha256, glb_bytes, manifest_sha256, xodr_sha256, gameplay_sha256, bundle_sha256, sha8, files: {glb, manifest, xodr}, built_with: {pygltflib, pillow, python}, stats: {primitives, triangles}}`; null until exported. Files live under `data/exports/<slug>/street_<sha8>.{glb,manifest.json,xodr}` (content-addressed, never deleted by recompile, so old `?v=` URLs keep resolving) |

The migration checks `PRAGMA table_info` per column before `ADD COLUMN` (re-runnable on a partially migrated copy). `recompile` overwrites `model_versions.scene_json/scene_schema/compiled_by/gaps_json` of
the head version (and `segments.scene_json/scene_schema`) **in place** — the scene is a derived artefact, the snapshot columns `model_json`, `fidelity_json`, `diff_json`, `model_hash` are never rewritten by compile-only recompiles. Persisting entity runtime flags was considered and
rejected: the classification is a pure function of the stored model, so it is recomputed (and unit-tested) rather than stored.

## Uploaded photo placement (extends `observations`; no new column)

`POST …/observations/upload` accepts either `lat`+`lon` **or** `s` (+ `side`); when `s` is given the server computes the camera position as `ref.point_at(s, t_cam)` with `t_cam = ±5.5 m` by `side`
(`left` → +5.5, `right` → −5.5, `centre` → 0; > **Assumed:** 5.5 m ≈ the middle of a typical footpath, ±1 m of error is inside `HERO_UPLOAD_MAX_DISTANCE_M`). `heading_deg` may be given directly or derived from `facing ∈ forward|back|left|right|toward_left|toward_right`
(forward/back = ± the tangent at `s`; left/right = tangent ∓ 90°; `toward_left`/`toward_right` are aliases for left/right kept for the UI wording "facing the left shops"). `captured_at` (`YYYY-MM` or `YYYY-MM-DD`, not in the future)
fills `observations.capture_date` (`YYYY-MM`). `georef_json.placement = {"s", "t", "side", "facing", "captured_at", "how": "s_side"|"latlon"}` records how it was placed. Provenance stays `kind=user_photo`, `source=upload`.
Fidelity: a `user_photo` produces direct left/right/forward cells **and** `closeup` cells (existing rule), so one photo facing a side on a bin that is already L2 on both sides lifts that bin to **L3**; a photo on an L1 bin lifts it to L1→L2 only if a second station + both sides exist (the hint says so).

## [r1 shape, now the internal `StreetPlan` — NOT served, NOT stored; the served Scene v3 is the lite shape in "Scene JSON v3 (r2)" at the end of this file] Scene JSON v3 *(r1 body)*

> **r2 reading rule.** Everything in this r1 body (`corridor` incl. `kerb`/`ground_src`, `ground_tex`, `frontage`, `furniture`, `objects`, `tokens`, `dynamic_context`, interpolation + MAX_SIZE_M + determinism rules) is the content of **`hero.model.street_plan_types.StreetPlan`**,
> produced by `hero.model.street_plan.plan_street` (pure) and consumed by `hero.export.mesh_builder` (geometry) and `hero.export.manifest` (lanes, colliders, tokens, entities). Differences from the r1 text: (a) `StreetPlan` has **no** `schema_version`, `segment`, `about`, `generation`, `walkability` keys (those are the lite scene's); (b) it keeps game-lateral `lat` internally and adds `footpath_side`, so the exporter converts `t = lat` (left footpath) or `t = −lat` (right footpath);
> (c) `dynamic_context.spawn_hints` is emitted to the manifest only; the lite scene keeps counts/`by_type`/summaries; (d) `ground_tex.strip` follows the r2 bake frame (below); (e) the downlevel `to_v2`/`?schema=2` is dropped; (f) lane centres, kerb default and token/walkability rules read `gameplay.json` instead of constants. `SceneV3` below in this block is superseded by r2's `SceneV3`.

**Compatibility.** v3 = every v2 key with its v2 meaning (so `path`, `corridor.stations[].lane_lat/footpath/strips`, `cross_section`, `tokens`, `walkability`, `generation`, `model`, `about` keep their shape) **plus** the additive keys below; `objects[]` shrinks to
*fixed* things (see Runtime classification). `load_scene` returns `Scene | SceneV2 | SceneV3` by `schema_version` (1|2|3). `hero.model.scene_v3.to_v2(scene) -> SceneV2` is the **downlevel** used by `GET …/scene?schema=2`: it drops `ground_tex, frontage, furniture, dynamic_context, about.interpolated/gap_summary/dynamic_summary,
corridor.stations[].kerb/ground_src`, keeps `buildings[]` (v3 keeps the v2 `buildings[]` list as the extruded footprints for old clients — the v3 renderer ignores it in favour of `frontage`), sets `schema_version: 2` and `model.compiled_by: "hero.model.compile/2+downlevel"`.
Frontend rule: game code branches on `scene.schema_version >= 2` for corridor logic and `=== 3` (or the presence of `frontage`) for the realism path; an old cached client that only knows `=== 2` gets a v2 via `?schema=2`; the shipped client always asks for v3.

All new coordinates are **game frame**: `s` metres along the path (= `x_m`), `lat` game lateral (positive toward the modelled footpath side), metres; sizes `w` (across, lateral), `h` (up), `d` (along `s`); angles degrees; colours `#rrggbb`; counts ints; no nulls where a default exists.

```jsonc
{
  "schema_version": 3,
  "segment": { }, "ground": { }, "path": { }, "cross_section": { }, "buildings": [ ], "tokens": [ ], "walkability": { }, "generation": { },   // as v2
  "model": { "version": 1, "model_hash": "…", "compiled_by": "hero.model.compile/2" },

  "corridor": { "step_m": 2.5, "ground_half_m": 30.0, "stations": [ {          // v2 fields (s, lane_lat, footpath, strips, ground_uv) PLUS:
      "s": 12.5, "lane_lat": [5.9, 4.1, 2.3],
      "footpath": {"lo": 5.1, "hi": 6.7, "state": "intact", "material": "paver", "height_m": 0.15, "confidence": 0.91} ,
      "strips": [ {"kind": "carriageway", "lo": -4.9, "hi": 3.6, "material": "asphalt", "height_m": 0.0, "confidence": 0.67} ],
      "ground_uv": [[0.512, 0.401], [0.512, 0.612]],
      "kerb": {"lat": 3.6, "height_m": 0.15, "width_m": 0.2},           // boundary between carriageway and footpath at this station; height 0.0 where the footpath is missing or a kerb_cut/ramp lowers it. Always present (never null)
      "ground_src": "satellite_hires"                                    // satellite_hires | satellite_overview | interpolated | procedural — what the ground strip image really holds at this station (== ground_tex.bin_source[floor(s/5)])
  } ] },

  "ground_tex": {
    "mode": "baked_strip",                                              // baked_strip | procedural (procedural: strip == null; the renderer paints footpath/road from strips[].material + footpath.state)
    "procedural_seed": 91827364,                                        // int32, sha1(slug) — drives procedural patterns deterministically
    "bin_m": 5.0,
    "bin_source": ["satellite_hires", "satellite_hires", "interpolated", "satellite_overview"],     // len == ceil(length/5)
    "strip": { "url": "/media/textures/bellandur-main-road-n-2/ground_v1_3fa91c20.jpg", "size_px": [280, 1002],
               "s_from": 0.0, "s_to": 100.2, "lat_lo": -14.0, "lat_hi": 14.0, "m_per_px": 0.1,
               "uv": "u = (lat - lat_lo)/(lat_hi - lat_lo) ; v = (s - s_from)/(s_to - s_from) ; image row 0 (top) = s_to, column 0 (left) = lat_lo" }   // null when mode == procedural
  },

  "frontage": { "step_m": 2.5, "sides": [
    { "side": "modelled",                                               // always both "modelled" and "opposite", in this order
      "texture": { "url": "/media/textures/…/frontage_modelled_v1_b71d0a55.jpg", "size_px": [1024, 128], "s_from": 0.0, "s_to": 100.2,
                   "bin_source": ["street_view", "street_view", "interpolated", "user_photo"],
                   "uv": "u = s/length ; v = 0 at ground .. 1 at 12 m" },                                          // null when no photo at all on this side
      "segments": [                                                      // CONTIGUOUS: segments[0].s_from == 0, each s_to == next s_from, last s_to == length (validated); the renderer must not leave holes
        { "s_from": 0.0, "s_to": 28.0, "lat_from": 8.4, "lat_to": 8.6, "height_m": 9.6, "kind": "building",        // building | wall | gate
          "colour_hex": "#c9b79c", "storeys": 3, "source": "osm",                                                     // osm | vision | entity | interpolated | prior
          "building_id": "bld-osm-1827364", "entity_id": null, "interpolated": false, "confidence": 0.9 },
        { "s_from": 28.0, "s_to": 41.0, "lat_from": 8.6, "lat_to": 8.1, "height_m": 2.0, "kind": "wall",
          "colour_hex": "#8c8479", "storeys": 0, "source": "entity", "building_id": null, "entity_id": "ent-77aa31c2d0", "interpolated": false, "confidence": 0.7 },
        { "s_from": 41.0, "s_to": 100.2, "lat_from": 8.1, "lat_to": 8.1, "height_m": 7.5, "kind": "building",
          "colour_hex": "#b8b0a2", "storeys": 2, "source": "interpolated", "building_id": null, "entity_id": null, "interpolated": true, "confidence": 0.2 } ] },
    { "side": "opposite", "texture": null, "segments": [ ] } ] },

  "furniture": [                                                         // FIXED furniture only (poles, trees, bins, bollards, railings, signboards, bus stops, drains, manholes, kerb cuts, crossings, ramps); sorted by (s, id)
    { "id": "ent-3f9a1c07d2", "type": "pole", "variant": "utility", "s": 12.5, "lat": 6.4, "yaw_deg": 0.0,
      "size_m": {"w": 0.3, "h": 7.0, "d": 0.3}, "length_m": 0.0,         // length_m > 0 only for railings/crossings/drains (extent along s)
      "colour_hex": "#9aa0a6", "canopy_hex": null,                        // tint from the entity's best crop (tints[entity_id]); canopy_hex only for trees; null => catalogue default colour
      "object_id": "ent-3f9a1c07d2", "confidence": 0.8 }                  // object_id: the objects[] entry that is the collision proxy of this thing (null for pure scenery); renderers draw furniture, the runner collides with objects
  ],

  "objects": [ ],                                                        // v2 SceneObjectV2 for FIXED hazards/markings + synthetic gap-/blocked- objects. billboard_url is emitted for the results-card thumbnail ONLY; renderers must not draw it

  "dynamic_context": {                                                   // what the street showed that is NOT baked in; the spawner's only input from the model
    "excluded_count": 41, "entity_ids": ["ent-…"],
    "by_type": { "parked_vehicle": {"count": 35, "per_100m": 34.9, "subtypes": {"car": 12, "two-wheeler": 15, "auto-rickshaw": 8}, "lane_share": [0.2, 0.5, 0.3], "mean_confidence": 0.71},
                 "vendor_stall": {"count": 3, "per_100m": 3.0, "subtypes": {}, "lane_share": [1.0, 0.0, 0.0], "mean_confidence": 0.55} },
    "spawn_hints": { "density": 0.8,                                     // 0..1 = min(1, excluded_per_100m / 40); the spawner multiplies p(L) by (0.6 + 0.4·density)
                     "type_weights": {"parked_car": 0.30, "two_wheeler": 0.35, "cone": 0.04, "barricade": 0.04, "pothole": 0.10, "stall": 0.12, "low_branch": 0.05},   // exactly these 7 keys, sum == 1.0 (±1e-6)
                     "lane_weights": [0.2, 0.5, 0.3] }                   // informational (the spawner uses per-lane base tables); sums to 1
  },

  "about": { /* v2 About + */
    "interpolated": [ {"what": "frontage", "side": "opposite", "s_from": 40.0, "s_to": 60.0, "method": "neighbours", "from_s": [35.0, 60.0], "note": "…"} ],   // = gap_report.interpolations (what != furniture_unknown) — shown in the About panel
    "gap_summary": "About 20 m of the right-hand frontage and 10 m of ground texture are interpolated from neighbouring stretches (no photo covers them).",       // "" when nothing is interpolated
    "dynamic_summary": "Seen here but spawned dynamically during play instead of fixed in place: 35 parked vehicles, 3 vendor stalls."                           // "" when excluded_count == 0
  }
}
```
Derived rules (compile): `dynamic_context.type_weights` = normalise( `parked_vehicle` → `parked_car` (subtypes car/bus/truck/auto-rickshaw/other) and `two_wheeler` (subtype two-wheeler) split by subtype counts, `vendor_stall` → `stall`, carriageway `pothole`+`speed_breaker` → `pothole`,
carriageway `debris` → `cone`/`barricade` 50/50 ) mixed with the default prior `{parked_car .20, two_wheeler .20, cone .15, barricade .10, pothole .15, stall .10, low_branch .10}` as `0.7·observed + 0.3·prior` when `excluded_count ≥ 3`, else the prior alone. `furniture[].size_m` = entity extent/height (catalogue default when missing),
then **capped per type** (`MAX_SIZE_M`: tree 5.5 w × 10 h, pole 0.5 × 9, bin 1.2 × 1.4, bollard 0.4 × 1.2, railing 0.3 × 1.5, signboard 1.6 × 2.6, bus_stop 2.0 × 3.0) so no data error can produce a screen-filling object. Frontage `lat_*` (game lateral, so the opposite side is negative) are clamped to `[footpath.hi + 0.8, 30]` on the modelled side and to `[−30, min(strip.lo over carriageway/opposite footpath strips) − 1.0]` on the opposite side, never inside the carriageway or the modelled footpath. Every number is `round(x, 3)`.
Contiguity and "no blank bins" are `SceneV3` model validators (frontage tiling, `len(bin_source) == ceil(length/5)`, `dynamic_context.spawn_hints.type_weights` keys and sum, `furniture` types ⊆ FIXED_TYPES). Determinism: byte-identical `model_dump_json()` on repeat and JSON round-trip (unit-tested), exactly as v2.

---

# Scene JSON v3 (r2) — the lightweight "web scene" *(`schema_version: 3`, `hero.model.scene_v3.SceneV3`, `extra="forbid"`; authoritative)*

The scene is what the UI **around** the game needs (gallery, About, results, walkability, links). It contains **no geometry, no lanes, no colliders, no tokens, no spawn hints** — those are in the manifest (`spec/portability.md` §4) and the glb. One fact, one place.
Compiled by `hero.model.compile.compile_scene` (`compiled_by: "hero.model.compile/2"`), stored in `model_versions.scene_json` / `segments.scene_json`, served by `GET /api/segments/{slug}/scene`. `load_scene` dispatches `1 | 2 | 3`; v1 and v2 keep their shapes and play on the legacy path.

```jsonc
{
  "schema_version": 3,
  "segment":  { /* as v1/v2 SegmentInfo; length_m = model.reference_line.length_s */ },
  "ground":   { /* as v2 Ground: tile_url (overview satellite), size_px, zoom, scale, center, meters_per_px, frame_to_px — used ONLY by the results hazard map */ },
  "cross_section": { /* v1 shape summary (road_width_m, lanes[3] mean lat_m/width_m, footpath_coverage[] with the extended states) — for the walkability card and tests; NOT used for geometry */ },
  "walkability": { /* v1/v2 Walkability; formula constants from gameplay.json → scoring.walkability; hazard_count counts static colliders only */ },
  "about": { /* v2 About + */
      "interpolated": [ {"what": "frontage", "side": "right", "s_from": 40.0, "s_to": 60.0, "method": "neighbours", "from_s": [35.0, 60.0], "note": "…"} ],
      "gap_summary": "…", "dynamic_summary": "…" },
  "dynamic_context": { "excluded_count": 41, "entity_ids": ["ent-…"],
      "by_type": { "parked_vehicle": {"count": 35, "per_100m": 34.9, "subtypes": {"car": 12}, "lane_share": [0.2, 0.5, 0.3], "mean_confidence": 0.71} } },   // counts only; spawn_hints live in the manifest
  "generation": { /* v1/v2 Generation */ },
  "model":  { "version": 1, "model_hash": "…", "compiled_by": "hero.model.compile/2" },
  "assets": { "mesh_sha256": "4be0…", "sha8": "4be0a1c2",
              "glb_url": "/api/segments/bellandur-main-road-n-2/model.glb?v=4be0a1c2",
              "manifest_url": "/api/segments/bellandur-main-road-n-2/manifest.json?v=4be0a1c2",
              "xodr_url": "/api/segments/bellandur-main-road-n-2/street.xodr?v=4be0a1c2",
              "gameplay_url": "/api/gameplay.json", "glb_bytes": 1048576, "triangles": 8420, "primitives": 12 }
}
```
Rules: `assets` is filled by the export step (`S10`); a scene compiled but not yet exported has `assets: null` and is **not served as v3** — publish/recompile always export in the same transaction-equivalent sequence (export files are written before the DB rows are updated; a crash in between leaves the previous scene intact).
`about.fidelity`, `about.photos`, `about.low_confidence_parts`, `about.summary`, `about.improve_hint`, `about.legacy|placement_verified|degraded` are the v2 keys, unchanged. Payload budget ≤ 40 kB. `POST /api/segments/{slug}/runs` builds its `hazards` response and the results card's object list from `manifest.colliders` (server reads the stored export; the client reads the manifest it already fetched).
Determinism: byte-identical `model_dump_json()` on repeat and JSON round-trip (unit-tested).

## Export bundle on disk and in the DB (r2)

`data/exports/<slug>/street_<sha8>.glb | .manifest.json | .xodr` (content-addressed: `sha8` = first 8 hex of `bundle_sha256 = sha256(mesh_sha256 + manifest_sha256 + xodr_sha256)`, so a `gameplay.json` change that leaves the glb untouched still yields new file names and a new `?v=`). `model_versions.export_json` (above) points at them. The served "latest" resolves through `segments` → head version → `export_json`. Contract of the files: `spec/portability.md`.
