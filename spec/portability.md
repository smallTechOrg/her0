# Portability — the street as engine-neutral game data *(Phase 2.1 r2)*

A generated street is stored and exported only as open, documented, engine-neutral data, so the game can later be rebuilt in Unity (or any engine) from the same files.
**No engine export UI exists**: there is no "Download for Unity / Unreal / Godot" button, no `export.zip` endpoint in the UI and no engine import guide. The formats below are the contract; an engine port is future work.
This file is the single home of the frame/units rules, the `street.glb` node contract, the manifest and `gameplay.json` field lists and the spawner pointer. Producer code: `src/hero/export/`. Reference consumer: `frontend/js/game/`.

## 1. The bundle (data formats)

| file | what | served at | produced by |
|---|---|---|---|
| canonical model JSON (`street-model/1`) | versioned truth per segment: reference line, cross-section, entities with evidence, fidelity (`spec/data.md`) | admin model API | `hero.model` |
| scene v3-lite JSON (schema 3) | what the UI around the game needs: segment info, walkability, About, overview tile, `assets` URLs + hashes (`spec/data.md`) | `GET /api/segments/{slug}/scene` | `hero.model.compile` |
| `street.glb` | glTF 2.0 binary: ground, kerbs, frontage, fixed furniture, 2 baked atlases. Metres, +Y up. No sky, no dynamic obstacles, no runner | `GET /api/segments/{slug}/model.glb` | `hero.export.glb.write_glb` |
| `street.manifest.json` | all non-geometry data: frame, reference line, lanes, cross-section, colliders, tokens, entities, spawn hints, attribution, embedded gameplay | `GET /api/segments/{slug}/manifest.json` | `hero.export.manifest.build_manifest` |
| `gameplay.json` | game rules as data (also embedded in the manifest) | `GET /api/gameplay.json` | `src/hero/export/gameplay.json` |
| `street.xodr` | OpenDRIVE 1.7, **best-effort byproduct, not exposed in the UI** (§8) | `GET /api/segments/{slug}/street.xodr` | `hero.export.xodr.build_xodr` |

Why engine-neutral: every file is plain JSON, glTF or XML; units are metres; the glb uses no extensions; the geometry is never mirrored; every gameplay number lives in data, not code; the spawner is a documented PRNG with committed test vectors. Each output is a pure, deterministic function of one stored model version + its observations (no clock, randomness or network).

## 2. Frame, units, handedness (normative)

- **Units: metres**; manifest angles are degrees (`*_deg`) unless the name ends `_rad`. glTF is right-handed, **+Y up**.
- **Street frame of `street.glb`:** origin = reference line at `s = 0`, ground `y = 0`. **At `s = 0` travel is toward −Z and +X is the RIGHT of travel.** A left footpath lies at negative X; geometry is never mirrored.
- Reference-line frame (OpenDRIVE): `s` metres along the line, `t` lateral metres **positive to the LEFT of travel**; `bearing_deg(s)` is the compass bearing.
- **Mapping `(s, t, y)` → world** (Python, JS and xodr identical): `φ(s) = wrap_pi(radians(bearing(s) − bearing(0)))`; forward `f = (sin φ, −cos φ)` on (X, Z); `P(0) = (0, 0)`, `P(s+ds) = P(s) + f(s)·ds` on the 2.5 m station grid (midpoint heading); `world(s, t, y) = (P.x(s) − t·cos φ(s), y, P.z(s) − t·sin φ(s))`.
- **Geographic anchor:** `frame.origin = {lat, lon}` at `s = 0`, `frame.heading_deg = bearing(0)`; the manifest also lists each station's `lat`/`lon`.
- The bundle only knows `t` (not the legacy game `lat`); `footpath_side: "left" | "right"` is lane 0's side.
- **Loop seam.** Copy *k+1* is placed with the end pose of copy *k* (`loop.end_pose = {x, z, yaw_rad}`); lane offsets, strip edges and frontage blend toward their station-0 values over the last `loop.seam_blend_m = 6` m.

## 3. `street.glb` contract

glTF 2.0, `extensionsRequired = []` (no Draco/meshopt/KTX2/texture transform). One scene `Street`, one root node `Street`, all transforms identity (world coordinates), one mesh with one primitive per node; empty nodes are omitted.
Node names: `Footpath`, `Footpath_Opposite`, `Road`, `Kerb`, `Ground_L`, `Ground_R`, `Frontage_L`, `Frontage_R` (contiguous in `s`), `Frontage_Caps`, `Furniture_Poles`, `Furniture_Trees`, `Furniture_Boxes`, `Furniture_Railings`, `Furniture_Decals`.
Materials (PBR metallic-roughness, opaque, single-sided): `mat_ground` (ground atlas), `mat_kerb`, `mat_frontage` (facade atlas), `mat_flat` (colour from `COLOR_0`). Attributes `POSITION` (mm-rounded), `NORMAL`, `TEXCOORD_0`, `COLOR_0`; CCW winding.
UVs are baked: ground `u = (14 − t)/28`, `v = (L − s)/L` over `t ∈ [−14, 14]`; facade atlas stacks `Frontage_L` (top half) over `Frontage_R`, `u = s/L`. Atlases are JPEG, longest side ≤ 1024.
Budgets (`tests/unit/test_mesh_budget.py`): ≤ 14 primitives, ≤ 12 000 triangles, ≤ 4 materials, ≤ 2 textures, ≤ 1.5 MB (×2 triangles/size at 200 m).

### 3.1 `extras`

`asset.extras`: `schema "hero-street-glb/1"`, `generator`, `segment_slug`, `model_version`, `model_hash`, `footpath_side`, `units`, `up_axis`, `travel`, `right`, `length_s`, `frame {origin, heading_deg}`, `manifest`, `gameplay_sha256` (no timestamps/hosts).
Ground nodes: `kind`, `side`, `runs[{s_from, s_to, state, material, height_m, confidence}]`, `bins[{index, s_from, s_to, ground_source, fidelity_level, interpolated}]`. Frontage nodes: `kind`, `side`, `segments[{s_from, s_to, type, source, building_id, entity_id, height_m, interpolated, confidence, fidelity_level, index_start, index_count}]`. Furniture nodes: `kind`, `category`, `entities[{id, type, variant, s, t, yaw_deg, confidence, fidelity_level, index_start, index_count}]`. `Frontage_Caps`: `{kind}`.
Entity ids and (s, t) equal `manifest.entities[]`. Consumers should read the manifest, not extras.

## 4. `street.manifest.json` (`hero-street-manifest/1`; `hero.export.manifest_types.ManifestV1`; schema `src/hero/export/schemas/manifest.schema.json`)

Numbers rounded to 3 dp, lat/lon 6 dp. Lane indices: `0` footpath, `1` road edge, `2` road. Top-level keys, in order:
`schema`; `build` (`generator, segment_slug, model_version, model_hash, mesh_sha256, glb_bytes, xodr_sha256, pygltflib, pillow, python, compiled_by`); `segment` (`slug, name, city, length_s, footpath_side, fidelity{overall_level, score, bins}`); `frame` (`units, up_axis, handedness, travel_at_s0, origin, heading_deg, world_from_st "hero-st-world/1"`);
`reference_line` (`source, osm_way_id, name, length_s, step_m 2.5, points[{s, lat, lon, x, z, bearing_deg}]`); `lanes` (`defs[{index, name, kind, width_m}]`, `stations[{s, lane_t[3], footpath{t_lo, t_hi, state, material, height_m, confidence}|null, kerb{t, height_m, width_m}}]`); `cross_section` (`stations[{s, strips[{kind, side, t_lo, t_hi, material, condition, height_m, confidence}]}]`);
`frontage` (`left`/`right`: `[{s_from, s_to, type, t_from, t_to, height_m, colour_hex, source, interpolated}]`); `entities` (`id, type, variant, s, t, yaw_deg, size_m, length_m, colour_hex, canopy_hex, confidence, fidelity_level, node, description`); `colliders` (`id, entity_id, type, label, s, length_m, lanes, action dodge|jump|slide|none, size_m, confidence, latlon, tile_px` web-only);
`tokens` (`id, s, lane, reason intact_footpath|crossing|ramp, latlon, tile_px`); `spawn_hints` (`density`, `type_weights` with exactly the 7 obstacle types summing to 1.0); `interpolated` (`what, side, s_from, s_to, method, note`); `loop` (`seam_blend_m, end_pose, chain`); `mesh_stats`; `attribution` (`lines`, `notice`: Google imagery and OSM ODbL); `gameplay` (verbatim §5); `gameplay_sha256`.
Scene v3-lite carries no geometry, lanes, colliders, tokens or gameplay: those exist only in the manifest.

## 5. `gameplay.json` (`hero-gameplay/1`; source `src/hero/export/gameplay.json`; `hero.export.gameplay_types.GameplayV1`; schema `src/hero/export/schemas/gameplay.schema.json`)

Rules as data; Python and JS read it and hard-code none of its numbers (a test greps the JS). Changing any value changes `gameplay_sha256` and invalidates the committed vectors until regenerated. Keys: `schema`, `version`;
`lanes` (`count, names, road_edge_inset_m, road_lane_gap_m, min_footpath_to_edge_m, default_footpath_width_m, widths_m`); `runner` (`speed_start_mps, speed_per_loop_mps, speed_max_mps, lane_tween_ms, jump_ms, slide_ms, jump_height_m, runner_half_m, health_max, hit_damage, invuln_s, start_grace_s`);
`tokens` (`value, radius_m, per_intact_m, max_per_street, crossing_lane`); `scoring` (`distance_unit_m, walkability{coverage_weight, crossing_bonus, hazard_weight, hazard_norm}`); `obstacle_types` (7 types in TYPE_ORDER `parked_car, two_wheeler, cone, barricade, pothole, stall, low_branch`, each `action, length_m, size_m, label`);
`spawner` (`seed_rule, prng "mulberry32", hash "fnv1a32"`, free-start/seam/keepout margins, row-gap and jitter, spawn probability and density terms, second-obstacle rule, gap-zone / clear-lane / action-gap, `variants`, `type_boost`, `lane_weights`, `default_spawn_hints`, `window_behind_m`, `window_ahead_m`). Seed = `?seed=` integer if given, else the per-street run number; PRNG key `slug|seed|loop`.

## 6. Runtime rules a port must reimplement (the parts that are NOT data)

Reference: `frontend/js/game/runner.js` (state machine, no rendering); every constant comes from `gameplay.runner`. Not data and not ported: camera, input gestures, HUD, sky/fog/light, avatar, obstacle visuals, audio.
Summary: three lanes with a one-deep input queue and lane tween; `speed = min(speed_max, speed_start + speed_per_loop·loops)`; swept token pickup; a collider hits when its `lanes` contain the runner's lane and it overlaps in `s` (also across the seam) unless jumping over / sliding under it; a hit costs `hit_damage` and grants `invuln_s`; health 0 ends the run; spawned obstacles are colliders with `id = "dyn-<loop>-<row>-<lane>"`, live only inside the window and are never persisted.

## 7. The deterministic spawner

Normative algorithm, resolved pseudocode and the open-point rulings: **`src/hero/export/SPAWNER_ALGORITHM.md`**. Reference `hero.export.gameplay_ref`; JS `frontend/js/game/spawner.js`. Both must emit identical `(loop, row, lane, type, x_mm, variant)` lists.
Numeric rules: uint32 arithmetic with `imul`; `fnv1a32` over the UTF-8 string `slug|seed|loop`; `mulberry32`; IEEE binary64 using only `+ − * /`, comparisons, `floor`, `min`, `max`; `x_mm = floor(x·1000 + 0.5)`; exactly 8 `rng()` draws per slot; weighted pick `w_i = base_i·(1 + type_boost·tw[type_i])` in TYPE_ORDER.
**[R8]** In `allowed(...)`, the gap zone is `[x − gap_zone_m, x + zlen + gap_zone_m]` with `zlen = max(len, lengths of obstacles already in the row)`.
Properties (tested on the reference): a lane without a dodge obstacle within ±6 m, a fully clear lane in loops ≤ 2, ≥ 14 m row spacing, nothing in free-start / seam / keepout zones.

### 7.1 Test vectors

`tests/fixtures/portability/spawner_vectors.json` and `spawner_vectors_bulk.json` (`hero-spawner-vectors/1`, keyed to `gameplay_sha256`; regenerate with `uv run python -m hero.tools.make_spawner_vectors`) are checked by `tests/unit/test_gameplay_ref.py` and `tests/e2e/test_spawner_vectors.py` (JS in Chromium). A port would run the same files.

## 8. OpenDRIVE (`street.xodr`) — best effort, not exposed in the UI

OpenDRIVE 1.7, one road, local metres from `frame.origin`, `hdg = π/2 − radians(bearing)`. `planView` is the 2.5 m polyline as `line` geometries; `lanes` has driving / curb / sidewalk lanes (sidewalk absent where the footpath is missing); `objects` list fixed furniture with `userData hero:entity_id`. No junctions, signals or elevation. `tests/unit/test_xodr.py` checks well-formedness and a `parse_xodr(build_xodr(...))` round-trip within 5 cm. Further polish is deferred.

## 9. No engine export UI

Engine-specific import steps (Unity, Unreal, Godot) are deliberately not documented or offered. A future port would consume §1's files, treat the manifest as the data source, and reimplement §6 and §7. Anything still in the code that offers an `export.zip` download is superseded by this decision.

## 10. Versioning, determinism, licence

- `hero-street-manifest/1`, `hero-street-glb/1`, `hero-gameplay/1`, `hero-spawner-vectors/1` are independent tags: additive changes keep the tag, meaning changes bump it.
- Same model version + observations + library versions ⇒ identical `street.glb`/manifest/xodr bytes (`tests/unit/test_export_determinism.py`; hash baseline `tests/fixtures/export/baseline_hashes.json` is regenerated in the same commit as any library upgrade).
- Textures derive from Google Static Maps / Street View and OSM data. `attribution` is mandatory wherever the street is shown; redistribution needs the Google Maps Platform terms checked by the owner (assumed: personal/evaluation use).
