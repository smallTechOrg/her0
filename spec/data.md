# Data

Storage: one relational database (engine in `spec/architecture.md`), migrated. Large JSON
documents (scenes, step lists, graphs) are stored as JSON text columns — read whole, never
queried by field. Imagery lives on disk under the data dir and is referenced by relative path.

## Entities

### segments

| field | type | notes |
|---|---|---|
| id | uuid pk | |
| slug | text unique | url id, e.g. `100-feet-road-indiranagar-w`. Derived: slugify(name minus any parenthetical) + `-` + compass letter of the footpath side (`n|e|s|w`, from heading + side); `-2`, `-3`… suffix on collision; admin create may pass an explicit `slug` |
| name | text | "100 Feet Road, Indiranagar (west footpath)" |
| city | text | "Bengaluru" |
| start_lat, start_lon, end_lat, end_lon | float | ~50–100 m apart (validated 30–150 m) |
| footpath_side | enum `left|right` | side of the modelled footpath relative to travel direction start→end |
| length_m, heading_deg | float | derived at creation |
| status | enum `draft|generating|ready|failed` | `ready` iff `scene_json` present |
| quality | enum `full|satellite_only|generic` nullable | from the last successful run |
| scene_json | json text nullable | the current Scene (below) |
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
| inputs_json | json | endpoints, side, models, zoom, samples, max_iterations, budgets at start |
| steps_json | json | `[{name, status, started_at, latency_ms, detail}]`, appended live |
| imagery_json | json | `{satellite: TileRef, streetview: [PanoRef], osm_path, streetview_available}` |
| layout_json, obstacles_json | json nullable | raw pass outputs (typed graphs) |
| iterations_json | json | `[{n, preview_path, accepted, critique, patch_ops, applied}]` |
| scene_json | json nullable | final scene of this run |
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

### segment_corrections (Phase 2)

| field | type |
|---|---|
| id uuid pk; segment_id fk; patch_ops json (same ops contract as `spec/agent.md`); applied_scene_json json; created_at |

## Relationships

segment 1—n pipeline_runs 1—n llm_calls; pipeline_runs 1—n google_requests; segment 1—n game_runs;
segment 1—n segment_corrections.

---

## Scene JSON (contract between pipeline, API and game — `schema_version: 1`)

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
  > **Assumed:** formula weights; tune in Phase 2 if the campaigners disagree.
- `generation.quality`: `full` = satellite + street view + both passes valid; `satellite_only` =
  no Street View or pass B dropped (objects only from pass A: gaps, trees, crossings);
  `generic` = pass A failed validation twice is NOT allowed (run fails) — `generic` is reserved
  for the Phase-2 "fallback corridor" and never produced in Phase 1.
- `generation.confidence_overall` = mean of object + coverage confidences (2 dp).

### Pass outputs (typed, stored raw on the run)

`LayoutGraph` (pass A): `{road_width_m, footpath_width_m, footpath_coverage[], buildings[{x_from_m, x_to_m, depth_m, side: "footpath"|"opposite", height_estimate_m, color_hex, confidence}], trees[{x_m, lat_m, confidence}], crossings[{x_m, confidence}], ramps[{x_m, confidence}], notes}`.
`ObstacleGraph` (pass B): `{objects[{type, label, pano_index, crop_px, x_m, lanes, length_m, size_m, action, confidence}], facades[{x_from_m, x_to_m, color_hex, pano_index, crop_px}], notes}`.
`ReviewResult`: `{accepted: bool, critique: str (≤1500 chars), patch_ops: [...] (≤10)}` — ops in `spec/agent.md`.

The canonical example above is shipped equivalent (placeholders like the `tile_url` hash /
`run_id` filled with real values) as `src/hero/scene/example_scene.json`
and copied to `tests/e2e/fixtures/scene_example.json` (with `tile_url` pointing at a 1280×1280
test PNG under `tests/e2e/fixtures/`); both must validate against `hero.scene.schema.Scene`.
