# Portability — the street as a game asset *(Phase 2.1 r2)*

A generated street is exported as a small, **engine-neutral bundle** so the same street can be played in the browser (Three.js) and rebuilt in Unity, Unreal or Godot.
This file is the **single home** of: the frame/units rules, the `street.glb` node + `extras` contract, the `street.manifest.json` field list, the `gameplay.json` field list,
the runtime rules a port must reimplement, the deterministic obstacle spawner (PRNG + numeric rules + test vectors) and the engine import notes.
Other spec files cross-reference it; they do not restate it. Producer code: `src/hero/export/` (`spec/architecture.md` → *Phase 2.1 r2*). Consumer reference: `frontend/js/game/`.

## 1. The bundle

| file | what | served at (public, cacheable) | produced by |
|---|---|---|---|
| `street.glb` | glTF 2.0 binary: ground strips, kerbs, continuous frontage, fixed furniture, 2 baked texture atlases. Metres, +Y up. **No** sky, **no** dynamic obstacles, **no** runner | `GET /api/segments/{slug}/model.glb` | `hero.export.mesh_builder.build_street_mesh` → `hero.export.glb.write_glb` |
| `street.manifest.json` | everything a runtime needs that is not geometry: frame, reference line, lanes, cross-section, colliders, tokens, entity list, spawn hints, attribution, **gameplay** (embedded) | `GET /api/segments/{slug}/manifest.json` | `hero.export.manifest.build_manifest` |
| `street.xodr` | OpenDRIVE 1.7 road description (**best effort**, see §8) | `GET /api/segments/{slug}/street.xodr` | `hero.export.xodr.build_xodr` |
| `gameplay.json` | the game rules as data (also embedded verbatim in the manifest) | `GET /api/gameplay.json` | source file `src/hero/export/gameplay.json` |
| `export.zip` | the four files + `IMPORT.txt` (admin "Download for Unity / Unreal / Godot") | `GET /api/admin/segments/{slug}/export.zip` | `hero.export.bundle.build_bundle` |

Everything is a **pure, deterministic function of one stored model version + its stored observations** (no clock, no randomness, no network). Same input ⇒ identical bytes within one pinned environment
(§10). Compile-time inputs: `spec/data.md` (Scene v3 lite, StreetPlan, gaps, bake).

## 2. Frame, units, handedness (normative)

- **Units: metres.** Angles in the manifest are degrees (`*_deg`) unless the name ends `_rad`. glTF is right-handed, **+Y up**.
- **World axes of `street.glb` (the "street frame"):** origin = reference line at `s = 0`, ground at `y = 0` (carriageway surface). **At `s = 0` travel is toward −Z and +X is the RIGHT-hand side of travel.** (Right × Up = +Z: right-handed, no mirroring.)
  A left-side footpath therefore lies at negative X; geometry is **never mirrored**.
- Reference line frame (OpenDRIVE): `s` metres along the line, `t` lateral metres, **positive to the LEFT of travel**. Heading `bearing_deg(s)` is the compass bearing of the line (clockwise from north).
- **Mapping `(s, t, y)` → world** (identical in Python, JS and the xodr writer):
  `φ(s) = wrap_pi(radians(bearing(s) − bearing(0)))`; forward `f = (sin φ, −cos φ)` on (X, Z); right normal `n = (cos φ, sin φ)`; centreline `P(0) = (0, 0)`, `P(s+ds) = P(s) + f(s)·ds` integrated on the 2.5 m station grid (midpoint heading);
  `world(s, t, y) = (P.x(s) − t·cos φ(s), y, P.z(s) − t·sin φ(s))`. For a street heading north this makes X ≈ east and −Z ≈ north.
- **Geographic anchor:** `frame.origin = {lat, lon}` (reference line point at `s = 0`), `frame.heading_deg = bearing(0)`. World → geographic: rotate the local (X, −Z) plane by `heading_deg` about the origin and apply the local equirectangular scale; the manifest also lists every station's `lat`/`lon`, so ports normally just read those.
- **Game lateral `lat`** (used by the legacy v1/v2 scenes) is *not* used by the bundle; the bundle only knows `t`. The footpath side is `footpath_side: "left" | "right"` (lane 0 side).
- **Loop seam.** The street is a loop for endless play: copy *k+1* is placed with the end pose of copy *k* (`loop.end_pose = {x, z, yaw_rad}` = P(L), and the rotation `φ(L)`), i.e. a rigid transform so that the start of the next copy meets the end of the previous one. Lane offsets, strip edges and frontage are blended toward their station-0 values over the last `loop.seam_blend_m = 6` m at build time, so the join is continuous for ≤ 1.5 m lateral steps. Heading is *not* blended (the chain turns by `φ(L)` per copy).

## 3. `street.glb` contract (producer: `hero.export`; consumer: `frontend/js/game/glb_loader.js`, Unity/Unreal/Godot)

glTF 2.0 only, **no extensions required** (`extensionsRequired = []`; no Draco/meshopt/KTX2/KHR_texture_transform — UVs are baked). One scene `Street` (index 0), one root node `Street`, children below; **all node transforms are identity** (geometry is in world coordinates).
Each node has exactly one mesh with exactly one primitive; **one primitive per node, nodes with no geometry are omitted** (e.g. `Furniture_Railings` when the street has none). One material may be shared by several nodes.

| node name | content | material |
|---|---|---|
| `Footpath` | the modelled-side footpath surface (top face) at `height_m` above the carriageway; only where a footpath strip exists | `mat_ground` |
| `Footpath_Opposite` | the other side's footpath (traced or prior template) | `mat_ground` |
| `Road` | carriageway surface kerb-to-kerb | `mat_ground` |
| `Kerb` | kerb faces + 0.2 m tops of both sides (zero height ⇒ degenerate triangles are not emitted; the surfaces meet) | `mat_kerb` |
| `Ground_L`, `Ground_R` | ground between the footpath/road edge and the frontage wall (forecourt, verge, earth where the footpath is `missing`); guarantees no hole | `mat_ground` |
| `Frontage_L`, `Frontage_R` | continuous facade ribbon, **contiguous in `s`** (no gaps), extruded to each segment's `height_m`; vertical steps between segments closed by a perpendicular face | `mat_frontage` |
| `Frontage_Caps` | roof caps, parapet lines, returns (flat colour) | `mat_flat` |
| `Furniture_Poles`, `Furniture_Trees`, `Furniture_Boxes` (bins, bollards, bus stops, signboards), `Furniture_Railings`, `Furniture_Decals` (drains, manholes, crossings, ramps, kerb cuts — flat quads 10 mm above the surface) | one merged mesh per category; trees = trunk + 2 canopy blobs, poles = tapered cylinder (+ cross-arm for `variant: utility`) | `mat_flat` |

Materials (glTF `pbrMetallicRoughness`, `doubleSided: false`, `alphaMode: OPAQUE`): `mat_ground` (baseColorTexture = ground atlas, `metallicFactor 0`, `roughnessFactor 1`), `mat_kerb` (baseColorFactor `#b9b9b9`), `mat_frontage` (baseColorTexture = facade atlas), `mat_flat` (baseColorFactor white, colour from `COLOR_0`). Vertex colours (`COLOR_0`, UNSIGNED_BYTE normalised, VEC4) on ground nodes carry the pavement/asphalt darkening and the state tint (they multiply the texture).
Vertex attributes: `POSITION` (FLOAT VEC3, millimetre-rounded), `NORMAL`, `TEXCOORD_0` (ground/frontage only), `COLOR_0`; indices `UNSIGNED_SHORT` when < 65 535 vertices else `UNSIGNED_INT`; no tangents/skins/animations/cameras/lights. Triangle winding CCW seen from the front face.

**UV rules (baked, no texture transform):** ground atlas = one straightened, road-aligned satellite image of the band `t ∈ [−14, +14]`, `s ∈ [0, L]` (row 0 = `s = L`, column 0 = `t = +14`, i.e. image up = travel, image left = left of travel; missing/uncovered bins are filled **in the image** with interpolated or procedural pavement by `hero.imagery.bake`, so there is never a blank texel):
`u = (14 − t) / 28`, `v = (L − s) / L` (glTF UV origin = top-left). Facade atlas = the left and right facade ribbons stacked vertically (top half = `Frontage_L`, bottom half = `Frontage_R`): `u = s / L`, `v = (v0 + (1 − h/12)·0.5)` with `h` metres above ground (`v0 = 0` left, `0.5` right; heights above 12 m clamp). Both atlases are JPEG (q 82, baseline, 4:2:0, no EXIF), longest side ≤ 1024, power-of-two not required, sampler `LINEAR` / `LINEAR_MIPMAP_LINEAR`, wrap `CLAMP_TO_EDGE`.

**Budgets (asserted by `tests/unit/test_mesh_budget.py` for the 100 m fixtures; ×2 for 200 m streets except primitives):** ≤ **14 primitives** (= draw calls per copy), ≤ **12 000 triangles**, ≤ **4 materials**, ≤ **2 textures**, `street.glb` ≤ **1.5 MB** (≤ 2.5 MB at 200 m).

### 3.1 `extras` (JSON, deterministic key order = as listed)

`asset.extras` (glTF `asset`):

```jsonc
{ "schema": "hero-street-glb/1", "generator": "hero.export/1",
  "segment_slug": "bellandur-main-road-n-2", "model_version": 1, "model_hash": "9c1e…", "footpath_side": "left",
  "units": "metres", "up_axis": "+Y", "travel": "-Z at s=0", "right": "+X at s=0",
  "length_s": 100.2, "frame": {"origin": {"lat": 12.9301, "lon": 77.6762}, "heading_deg": 3.1},
  "manifest": "street.manifest.json", "gameplay_sha256": "…" }
```
No timestamps, no host names (determinism). `fidelity_level` below is the 5 m bin's level 0–3 (`spec/data.md` → Fidelity); `confidence` is 0–1.

Ground nodes (`Footpath`, `Footpath_Opposite`, `Road`, `Ground_L/R`, `Kerb`) — `node.extras`:

```jsonc
{ "kind": "footpath", "side": "left",                       // kind: footpath|road|kerb|ground ; side: left|right|both
  "runs": [ {"s_from": 0.0, "s_to": 22.0, "state": "intact", "material": "paver", "height_m": 0.15, "confidence": 0.91} ],     // footpath/road: per state run; others: []
  "bins": [ {"index": 0, "s_from": 0.0, "s_to": 5.0, "ground_source": "satellite_hires", "fidelity_level": 2, "interpolated": false} ] }   // ground_source: satellite_hires|satellite_overview|interpolated|procedural
```

Frontage nodes — `node.extras`: `index_start`/`index_count` address the node's single index buffer (triangle indices) so a segment's triangles can be found:

```jsonc
{ "kind": "frontage", "side": "left",
  "segments": [ {"s_from": 0.0, "s_to": 28.0, "type": "building", "source": "osm", "building_id": "bld-osm-1827364", "entity_id": null,
                 "height_m": 9.6, "interpolated": false, "confidence": 0.9, "fidelity_level": 2, "index_start": 0, "index_count": 168} ] }   // type: building|wall|gate ; source: osm|vision|entity|interpolated|prior
```

Furniture nodes — `node.extras`:

```jsonc
{ "kind": "furniture", "category": "poles",                 // poles|trees|boxes|railings|decals
  "entities": [ {"id": "ent-3f9a1c07d2", "type": "pole", "variant": "utility", "s": 12.5, "t": -6.4, "yaw_deg": 0.0,
                 "confidence": 0.8, "fidelity_level": 2, "index_start": 0, "index_count": 96} ] }
```
`Frontage_Caps` extras: `{"kind": "frontage_caps"}`. Entity ids and (s, t) here are the **same** values as `manifest.entities[]` (a test asserts equality). Unity's glTFast does not surface `extras` by default — **ports must read the manifest, not extras**; extras exist for Blender/Godot inspection and for the Three.js debug overlay.

## 4. `street.manifest.json` (schema `hero-street-manifest/1`; Pydantic `hero.export.manifest_types.ManifestV1`; JSON Schema committed at `src/hero/export/schemas/manifest.schema.json`)

Field list (all numbers rounded to 3 dp, lat/lon 6 dp, keys in this order). `lane` indices: `0` footpath, `1` road edge, `2` road.

| key | type | meaning |
|---|---|---|
| `schema` | `"hero-street-manifest/1"` | version tag; additive changes keep `/1`, breaking ⇒ `/2` |
| `build` | object | `generator`, `segment_slug`, `model_version`, `model_hash`, `mesh_sha256` (of `street.glb`), `glb_bytes`, `xodr_sha256`, `pygltflib`, `pillow`, `python` (versions that produced the bytes), `compiled_by` |
| `segment` | object | `slug, name, city, length_s, footpath_side, fidelity: {overall_level, score, bins: [{index, s_from, s_to, level}]}` |
| `frame` | object | `units: "metres"`, `up_axis: "+Y"`, `handedness: "right"`, `travel_at_s0: "-Z"`, `origin: {lat, lon}`, `heading_deg`, `world_from_st: "<§2 formula id: hero-st-world/1>"` |
| `reference_line` | object | `source, osm_way_id, name, length_s, step_m (2.5), points: [{s, lat, lon, x, z, bearing_deg}]` — `points` on the 2.5 m grid + the end point; `x, z` are the world centreline `P(s)` |
| `lanes` | object | `defs: [{index, name, kind, width_m}]`; `stations: [{s, lane_t: [t0, t1, t2], footpath: {t_lo, t_hi, state, material, height_m, confidence} \| null, kerb: {t, height_m, width_m}}]` — index-aligned with `reference_line.points`. `lane_t[i]` is the lane-centre `t` (left-positive). `footpath == null` ⇒ lane 0 has no walkable surface at that station (`state: "missing"`) |
| `cross_section` | object | `stations: [{s, strips: [{kind, side, t_lo, t_hi, material, condition, height_m, confidence}]}]` index-aligned with `lanes.stations`; strips ordered by `t_lo` ascending; includes carriageway, footpaths, kerb-top, forecourt/verge |
| `frontage` | object | `left` / `right`: `[{s_from, s_to, type, t_from, t_to, height_m, colour_hex, source, interpolated}]` contiguous in `s`; the walls as geometry-free data (for ports that rebuild collision/occlusion) |
| `entities` | array | fixed furniture: `{id, type, variant, s, t, yaw_deg, size_m: {w, h, d}, length_m, colour_hex, canopy_hex, confidence, fidelity_level, node, description}` — `node` = the glb node that contains its mesh |
| `colliders` | array | what the runner can hit: `{id, entity_id \| null, type, label, s, length_m, lanes: [..], action: "dodge"\|"jump"\|"slide"\|"none", size_m, confidence, latlon: {lat, lon}, tile_px: [x, y] \| null}`. `tile_px` is a **web-only** hint for the results overlay (pixel in the overview tile); ports ignore it. Synthetic `gap-…` colliders (footpath missing) are included |
| `tokens` | array | `{id, s, lane, reason: "intact_footpath"\|"crossing"\|"ramp", latlon, tile_px}` |
| `spawn_hints` | object | `{density: 0..1, type_weights: {parked_car, two_wheeler, cone, barricade, pothole, stall, low_branch}}` — exactly these 7 keys, sum 1.0 (±1e-6). The *only* model-derived input of the spawner |
| `interpolated` | array | `[{what, side, s_from, s_to, method, note}]` — the parts that had no evidence (also shown in the About panel) |
| `loop` | object | `{seam_blend_m: 6.0, end_pose: {x, z, yaw_rad}, chain: "rigid_end_to_start"}` |
| `mesh_stats` | object | `{primitives, triangles, vertices, materials, textures, nodes: [{name, triangles, material}]}` (budget check without opening the glb) |
| `attribution` | object | `{lines: ["Satellite imagery © Google", "Street-level imagery © Google", "Map data © OpenStreetMap contributors (ODbL)"], notice: "Textures derive from Google imagery and are provided for evaluation; check the Google Maps Platform terms before redistributing."}` |
| `gameplay` | object | the full `gameplay.json` (§5), verbatim |
| `gameplay_sha256` | string | sha256 of `gameplay.json` bytes (also in `asset.extras`); the spawner test vectors are keyed to it |

Example (abridged, illustrative numbers):

```jsonc
{ "schema": "hero-street-manifest/1",
  "build": {"generator": "hero.export/1", "segment_slug": "bellandur-main-road-n-2", "model_version": 1, "model_hash": "9c1e…", "mesh_sha256": "4be0…", "glb_bytes": 1048576,
            "xodr_sha256": "77a1…", "pygltflib": "1.16.5", "pillow": "11.0.0", "python": "3.12.5", "compiled_by": "hero.model.compile/2"},
  "segment": {"slug": "bellandur-main-road-n-2", "name": "Bellandur Main Road", "city": "Bengaluru", "length_s": 100.2, "footpath_side": "left",
              "fidelity": {"overall_level": 1, "score": 1.4, "bins": [{"index": 0, "s_from": 0.0, "s_to": 5.0, "level": 1}]}},
  "frame": {"units": "metres", "up_axis": "+Y", "handedness": "right", "travel_at_s0": "-Z", "origin": {"lat": 12.9301, "lon": 77.6762}, "heading_deg": 3.1, "world_from_st": "hero-st-world/1"},
  "reference_line": {"source": "osm_nominatim", "osm_way_id": 25173540, "name": "Bellandur Main Road", "length_s": 100.2, "step_m": 2.5,
                     "points": [{"s": 0.0, "lat": 12.9301, "lon": 77.6762, "x": 0.0, "z": 0.0, "bearing_deg": 3.1}, {"s": 2.5, "lat": 12.930122, "lon": 77.676201, "x": 0.0, "z": -2.5, "bearing_deg": 3.4}]},
  "lanes": {"defs": [{"index": 0, "name": "footpath", "kind": "footpath", "width_m": 1.6}, {"index": 1, "name": "road_edge", "kind": "road_edge", "width_m": 1.6}, {"index": 2, "name": "road", "kind": "road", "width_m": 2.0}],
            "stations": [{"s": 0.0, "lane_t": [5.9, 4.1, 2.3], "footpath": {"t_lo": 5.1, "t_hi": 6.7, "state": "intact", "material": "paver", "height_m": 0.15, "confidence": 0.91},
                          "kerb": {"t": 3.6, "height_m": 0.15, "width_m": 0.2}}]},
  "cross_section": {"stations": [{"s": 0.0, "strips": [{"kind": "carriageway", "side": "centre", "t_lo": -4.9, "t_hi": 3.6, "material": "asphalt", "condition": "fair", "height_m": 0.0, "confidence": 0.67}]}]},
  "frontage": {"left": [{"s_from": 0.0, "s_to": 28.0, "type": "building", "t_from": 8.4, "t_to": 8.6, "height_m": 9.6, "colour_hex": "#c9b79c", "source": "osm", "interpolated": false}], "right": []},
  "entities": [{"id": "ent-3f9a1c07d2", "type": "pole", "variant": "utility", "s": 12.5, "t": 6.4, "yaw_deg": 0.0, "size_m": {"w": 0.3, "h": 7.0, "d": 0.3}, "length_m": 0.0,
                "colour_hex": "#9aa0a6", "canopy_hex": null, "confidence": 0.8, "fidelity_level": 1, "node": "Furniture_Poles", "description": "Utility pole beside the footpath."}],
  "colliders": [{"id": "ent-3f9a1c07d2", "entity_id": "ent-3f9a1c07d2", "type": "pole", "label": "pole", "s": 12.2, "length_m": 0.6, "lanes": [0], "action": "dodge",
                 "size_m": {"w": 0.3, "h": 7.0, "d": 0.3}, "confidence": 0.8, "latlon": {"lat": 12.93013, "lon": 77.67621}, "tile_px": [640.1, 840.3]}],
  "tokens": [{"id": "tok-1", "s": 8.0, "lane": 0, "reason": "intact_footpath", "latlon": {"lat": 12.93008, "lon": 77.67620}, "tile_px": [579.7, 835.9]}],
  "spawn_hints": {"density": 0.8, "type_weights": {"parked_car": 0.30, "two_wheeler": 0.35, "cone": 0.04, "barricade": 0.04, "pothole": 0.10, "stall": 0.12, "low_branch": 0.05}},
  "interpolated": [{"what": "frontage", "side": "right", "s_from": 40.0, "s_to": 60.0, "method": "neighbours", "note": "No photo covers this stretch; blended from 35 m and 60 m."}],
  "loop": {"seam_blend_m": 6.0, "end_pose": {"x": 4.1, "z": -100.1, "yaw_rad": 0.05}, "chain": "rigid_end_to_start"},
  "mesh_stats": {"primitives": 12, "triangles": 8420, "vertices": 6100, "materials": 4, "textures": 2, "nodes": [{"name": "Road", "triangles": 160, "material": "mat_ground"}]},
  "attribution": {"lines": ["Satellite imagery © Google", "Street-level imagery © Google", "Map data © OpenStreetMap contributors (ODbL)"], "notice": "…"},
  "gameplay": { "…": "see §5" }, "gameplay_sha256": "…" }
```

**Scene v3 lite vs manifest (no duplicate contract).** The web scene (`GET /api/segments/{slug}/scene`, schema 3, `spec/data.md`) carries only *what the UI around the game needs*: segment info, walkability, About, generation info, a count/summary of `dynamic_context`, the satellite overview used by the results map, and `assets` (URLs + hashes of the files above).
Geometry, lanes, colliders, tokens, spawn hints and gameplay exist **only** in the manifest. The results card and `POST …/runs` read colliders from the manifest (server side: the stored export).

## 5. `gameplay.json` (schema `hero-gameplay/1`; source `src/hero/export/gameplay.json`; Pydantic `hero.export.gameplay_types.GameplayV1`; JSON Schema at `src/hero/export/schemas/gameplay.schema.json`)

Rules as data. Python (`hero.model` compile for lanes/tokens/walkability, `hero.export.gameplay_ref` for the spawner) and JS (`frontend/js/game/{runner,spawner,gameplay}.js`) **read this file; neither hard-codes a number from it** (a test greps the JS for the old constants). Changing any value changes `gameplay_sha256`, which invalidates the committed vectors until regenerated.

```jsonc
{ "schema": "hero-gameplay/1", "version": 1,
  "lanes": { "count": 3, "names": ["footpath", "road_edge", "road"],
             "road_edge_inset_m": 0.9,            // lane 1 centre = carriageway edge nearest the footpath − this
             "road_lane_gap_m": 1.8,              // lane 2 centre = lane 1 centre − this
             "min_footpath_to_edge_m": 1.4,       // lane 0 − lane 1 ≥ this (footpath centre is pushed out otherwise)
             "default_footpath_width_m": 1.8, "widths_m": {"road_edge": 1.6, "road": 2.0} },
  "runner": { "speed_start_mps": 6.0, "speed_per_loop_mps": 0.1, "speed_max_mps": 12.0,
              "lane_tween_ms": 150, "jump_ms": 500, "slide_ms": 600, "jump_height_m": 1.3, "runner_half_m": 0.3,
              "health_max": 100, "hit_damage": 10, "invuln_s": 1.5, "start_grace_s": 3.0 },
  "tokens": { "value": 50, "radius_m": 0.6, "per_intact_m": 10.0, "max_per_street": 12, "crossing_lane": 1 },
  "scoring": { "distance_unit_m": 1.0, "walkability": { "coverage_weight": 0.6, "crossing_bonus": 20, "hazard_weight": 20, "hazard_norm": 10 } },
  "obstacle_types": {                                              // the 7 spawnable types, in this order (TYPE_ORDER)
    "parked_car":  {"action": "dodge", "length_m": 4.2, "size_m": {"w": 1.8, "h": 1.5, "d": 4.2}, "label": "parked car"},
    "two_wheeler": {"action": "dodge", "length_m": 1.9, "size_m": {"w": 0.7, "h": 1.2, "d": 1.9}, "label": "two-wheeler"},
    "cone":        {"action": "dodge", "length_m": 0.4, "size_m": {"w": 0.4, "h": 0.7, "d": 0.4}, "label": "traffic cone"},
    "barricade":   {"action": "dodge", "length_m": 0.6, "size_m": {"w": 1.6, "h": 1.0, "d": 0.6}, "label": "barricade"},
    "pothole":     {"action": "jump",  "length_m": 1.0, "size_m": {"w": 1.0, "h": 0.1, "d": 1.0}, "label": "pothole"},
    "stall":       {"action": "dodge", "length_m": 1.5, "size_m": {"w": 1.5, "h": 2.0, "d": 1.5}, "label": "vendor stall"},
    "low_branch":  {"action": "slide", "length_m": 1.0, "size_m": {"w": 1.8, "h": 0.6, "d": 1.0}, "label": "low branch"} },
  "spawner": {
    "seed_rule": "seed = ?seed integer if given else run_number; run_number = per-street counter in localStorage 'hero.run.<slug>', +1 at every run start, first run = 1; PRNG key = slug|seed|loop",
    "prng": "mulberry32", "hash": "fnv1a32",
    "free_start_loop0_m": 30.0, "free_start_later_m": 12.0, "seam_free_m": 8.0, "keepout_margin_m": 6.0, "footpath_missing_margin_m": 2.0,
    "row_gap_min_m": 14.0, "row_gap_base_m": 26.0, "row_gap_per_loop_m": 2.5, "jitter_max_m": 2.5,
    "spawn_p_base": 0.45, "spawn_p_per_loop": 0.08, "spawn_p_max": 0.85, "density_floor": 0.6, "density_gain": 0.4,
    "second_obstacle_from_loop": 2, "second_obstacle_prob": 0.5,
    "gap_zone_m": 6.0, "clear_lane_until_loop": 2, "action_gap_m": 9.0, "variants": 6,
    "type_boost": 3.0,
    "lane_weights": { "0": {"two_wheeler": 3.0, "cone": 3.0, "barricade": 2.0, "stall": 2.0, "low_branch": 1.5},
                      "1": {"parked_car": 3.0, "two_wheeler": 2.0, "cone": 2.0, "barricade": 1.5, "pothole": 2.0},
                      "2": {"parked_car": 3.0, "two_wheeler": 2.0, "cone": 1.5, "barricade": 1.5, "pothole": 2.5} },
    "default_spawn_hints": {"density": 0.5, "type_weights": {"parked_car": 0.20, "two_wheeler": 0.20, "cone": 0.15, "barricade": 0.10, "pothole": 0.15, "stall": 0.10, "low_branch": 0.10}},
    "window_behind_m": 10.0, "window_ahead_m": 80.0 } }
```
`default_spawn_hints` is used by streets without a manifest (legacy v1/v2 scenes). The spawner rules table of `spec/roadmap.md` r1 is superseded by `spawner` above + §7.

## 6. Runtime rules a port must reimplement (the parts that are NOT data)

Reference: `frontend/js/game/runner.js` (state machine, no rendering) — every constant comes from `gameplay.runner`.

- **State per run:** `x` (distance along the unrolled chain, m), `loops`, `lane ∈ {0,1,2}`, `laneFrom`, `tweenT ∈ [0,1]`, `action ∈ {none, jumping, sliding}`, `actionT`, `health`, `invulnUntil`, `elapsed`, `tokens`, `queued` (one-deep input). `speed = min(speed_max, speed_start + speed_per_loop·loops)`. `score = round(x) + tokens.value·tokens`.
- **Input:** `left`/`right` accepted only when `tweenT ≥ 1`; `jump`/`slide` only when `action == none`; otherwise one command is queued (a newer one replaces it). Lane clamps to 0..2. `visual t = lerp(lane_t[from], lane_t[to], tweenT)` evaluated at `xWrapped` on the station grid (linear interpolation of `lanes.stations`).
- **Update(dt):** advance timers; `x += speed·dt`; when `x ≥ (loops+1)·L` then `loops += 1`, tokens collected this loop are cleared, emit `loop`.
- **Tokens:** swept test over `[prevX, x]` ± `radius_m` for tokens in the runner's lane (jumping/sliding still collects). Token world s = `token.s + loop·L`.
- **Collisions (colliders from the manifest + spawned obstacles):** a collider hits when `lanes ∋ lane`, and `xw + half > s && xw − half < s + length_m` for `xw = x_wrapped` or `x_wrapped + L` (seam), unless `action == jump && jumping` or `action == slide && sliding`; ignored while `elapsed < invulnUntil` or if `id:loop` was already hit. A hit: `health −= hit_damage`, `invulnUntil = elapsed + invuln_s`; `health == 0` ⇒ crash (run over). `invulnUntil` starts at `start_grace_s`. `action == none` colliders (crossings, ramps) never hit.
- **Spawned obstacles** are colliders with `id = "dyn-<loop>-<row>-<lane>"`, `s = x_mm/1000` of the loop, same rules; they exist for `[x − window_behind_m, x + window_ahead_m]` and are never written anywhere.
- **Not data, not ported:** camera, input gestures, HUD, sky/fog/lighting, runner avatar, obstacle visuals, audio. A port draws the dynamic obstacles itself from `obstacle_types[...].size_m`.

## 7. The deterministic spawner (normative; reference `hero.export.gameplay_ref`, JS `frontend/js/game/spawner.js`)

Both implementations must produce **identical `(loop, row, lane, type, x_mm, variant)` lists** for identical inputs. Numeric rules (the whole contract for "byte-for-byte"):

1. **Integers are uint32.** After *every* integer operation reduce modulo 2³² (`& 0xFFFFFFFF`; JS: `>>> 0`). `imul(a, b) = (a·b) mod 2³²` computed exactly (JS `Math.imul(a, b) >>> 0`; Python `(a * b) & MASK`; C#/C++ `unchecked((uint)(a*b))`).
2. **`fnv1a32(bytes)`**: `h = 0x811C9DC5`; for each byte `b` of the UTF-8 string: `h = (h ^ b) & MASK; h = imul(h, 0x01000193)`; return `h`. Input string = `slug + "|" + str(seed) + "|" + str(loop)` (decimal integers, no padding, ASCII slugs).
3. **`mulberry32(a)`** (state `a`, uint32): each call:
   ```
   a = (a + 0x6D2B79F5) & MASK
   t = a
   t = imul(t ^ (t >>> 15), t | 1)
   t = (t ^ ((t + imul(t ^ (t >>> 7), t | 61)) & MASK)) & MASK
   r = ((t ^ (t >>> 14)) & MASK) / 4294967296.0          // IEEE-754 double, exact (numerator < 2^32, divisor 2^32)
   ```
4. **Floats are IEEE-754 binary64** and only `+ − * /`, comparisons, `floor`, `min`, `max` are used — no `pow/exp/sin/round`. Evaluate in the written order (no fused multiply-add, no reassociation, no `float32`; in C++ compile with `-ffp-contract=off`, in C# use `double`).
5. **`x_mm = floor(x·1000.0 + 0.5)`** (integer). Python must use `math.floor`, never `round()`. All comparisons (`<`, `≤`) inside the algorithm use the unrounded doubles.
6. **Exactly 8 `rng()` draws per slot, in order `d0 … d7`, whether or not they are used.** Draw counts therefore never depend on geometry, so a divergence cannot hide.
7. **Weighted pick:** items in `TYPE_ORDER` restricted to the lane's table (base weight > 0): `w_i = base_i · (1.0 + type_boost · tw[type_i])` (`tw` missing ⇒ 0.0); `total = w_0 + w_1 + …` summed left to right; `x = u · total`; for i in order: `x = x − w_i; if x < 0: return type_i`; fall through ⇒ last item.

**Inputs `SpawnInput`:** `slug, seed (int ≥ 0), length_m, density, type_weights` (7 keys), `footpath_missing: [[s0, s1]]` (from `lanes.stations[].footpath == null`: each station covers `[s − 1.25, s + 1.25]`, adjacent ranges merged), `static: [{s0, s1, lanes, action}]` (colliders with `action != none`, `s0 = s`, `s1 = s + length_m`), `keepout: [[a, b]]` (colliders of type `crossing`/`ramp`: `[s − keepout_margin_m, s + length_m + keepout_margin_m]`).

**`generate_loop(gp, inp, L)`** (`sp = gp.spawner`):

```
rng     = mulberry32(fnv1a32(slug|seed|L))
cursor  = sp.free_start_loop0_m if L == 0 else sp.free_start_later_m
limit   = length_m − sp.seam_free_m
gap     = max(sp.row_gap_min_m, sp.row_gap_base_m − sp.row_gap_per_loop_m · L)
p       = min(sp.spawn_p_max, sp.spawn_p_base + sp.spawn_p_per_loop · L) · (sp.density_floor + sp.density_gain · density)
row = −1; out = []
loop:
  d = [rng() for 8]                                   # d0..d7
  x = cursor + d0 · sp.jitter_max_m
  if x > limit: stop
  row += 1;  cursor = x + gap
  if not (d1 < p): continue
  if any(a ≤ x ≤ b for [a,b] in keepout): continue
  cands = [0,1,2] without 0 if a footpath_missing range [s0,s1] satisfies s0 < x + max_len + margin and s1 > x − margin
          # margin = footpath_missing_margin_m; max_len = max(length_m) over obstacle_types (type is not known yet, so the longest is used)
  if cands is empty: continue
  lane_a = cands[floor(d3 · len(cands))];  type_a = pick(lane_a, d5);  var_a = floor(d7 · sp.variants)
  two    = L ≥ sp.second_obstacle_from_loop and d2 < sp.second_obstacle_prob
  place A if allowed(type_a, lane_a, x, row_so_far=[])  else continue (drop the row)
  if two:
     cb = cands without lane_a;  if cb non-empty:
        lane_b = cb[floor(d4 · len(cb))];  type_b = pick(lane_b, d6)
        if action(type_a) != dodge and action(type_b) != dodge: type_b = first type in TYPE_ORDER of lane_b's table with action dodge
        var_b = (var_a + sp.variants // 2) mod sp.variants
        place B if allowed(type_b, lane_b, x, row_so_far=[A])   # else only B is dropped
  emit rows as (L, row, lane, type, x_mm, variant); id = "dyn-<L>-<row>-<lane>"
```
`allowed(type, lane, x, row)` with `len = obstacle_types[type].length_m`, `zone = [x − gap_zone_m, x + len + gap_zone_m]`:
1. no `static` entry whose lanes ∋ lane overlaps `[x, x + len]` (strict overlap);
2. `S_all` = lanes of `static` entries overlapping `zone` ∪ lanes of `row` ∪ {lane}; `S_dodge` = same but only static entries with action `dodge` and row entries with action `dodge` (∪ {lane} if the new type is `dodge`);
3. if `L ≤ clear_lane_until_loop`: require `|S_all| ≤ 2` (a fully clear lane remains); else if the new type is `dodge`: require `|S_dodge| ≤ 2`;
4. if the new type's action ≠ `dodge`: no `static` entry with action ≠ `dodge` in this lane and no already-emitted obstacle of this loop with action ≠ `dodge` in this lane whose `[x, x+len]` comes within `action_gap_m` of `[x, x + len]`.

Guarantees (property-tested on the Python reference over 1 000 seeds × 10 loops × lengths 40–200 m, and on the JS implementation by equality with the reference): ≥ 1 lane without a dodge obstacle within ±6 m of any x (static + dynamic); loops ≤ 2 keep a fully clear lane; ≤ 2 occupied lanes per row, 1 obstacle per row before loop 2; consecutive row spacing ≥ 14 m; nothing before `free_start` or inside `seam_free_m`/keepouts; reachability between consecutive rows (`min |a − b| ≤ 1 + floor(gap/(speed·0.5))` over passable lane sets, `speed = 12`) — always satisfiable with a 14 m gap on 3 lanes.

### 7.1 Test vectors

`tests/fixtures/portability/spawner_vectors.json` (generated by `uv run python -m hero.tools.make_spawner_vectors`, committed; schema `hero-spawner-vectors/1`):

```jsonc
{ "schema": "hero-spawner-vectors/1", "gameplay_sha256": "…",
  "cases": [ { "name": "plain-100m-seed1",
               "input": {"slug": "vector-street", "seed": 1, "length_m": 100.0, "density": 0.5,
                         "type_weights": {"parked_car": 0.2, "two_wheeler": 0.2, "cone": 0.15, "barricade": 0.1, "pothole": 0.15, "stall": 0.1, "low_branch": 0.1},
                         "footpath_missing": [[40.0, 52.5]], "static": [{"s0": 60.0, "s1": 60.6, "lanes": [0], "action": "dodge"}], "keepout": [[78.0, 90.0]]},
               "expected": [ [0, 1, 2, "parked_car", 41233, 4], [0, 3, 1, "cone", 83110, 0] ] } ] }       // [loop, row, lane, type, x_mm, variant] for loops 0, 1, 2 — ILLUSTRATIVE numbers; the committed file is authoritative
```
≥ 8 cases: seeds {1, 7, 424242}, lengths {60, 100, 200}, density {0, 0.5, 1}, vehicle-heavy and default weights, with/without footpath-missing, static blockers and keepouts, loops 0–2 each. A second file `spawner_vectors_bulk.json` (50 seeds × 3 loops × 3 streets, ≤ 200 kB) backs the JS equality sweep.
**Both** `tests/unit/test_gameplay_ref.py` (recomputes and compares to the committed file; also fails when `gameplay_sha256` differs) **and** `tests/e2e/test_spawner_vectors.py` (loads `/js/game/spawner.js` in Chromium via `page.evaluate`, passes the vectors and `/api/gameplay.json`, compares exact equality) must pass. A Unity/Unreal/Godot port runs the same vector file in its own test runner.

## 8. OpenDRIVE (`street.xodr`) — best effort

OpenDRIVE **1.7** (`revMajor 1, revMinor 7`), one `<road id="1" junction="-1">`. Coordinates: OpenDRIVE inertial frame (x east, y north, z up, right-handed) in **local metres** from `frame.origin`; `<header>` carries `<geoReference>` `+proj=tmerc +lat_0=<lat> +lon_0=<lon> +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs`, `vendor="hero"`, `name`. Heading conversion `hdg = π/2 − radians(bearing)`.
- `<planView>`: the reference line as `<geometry s x y hdg length><line/></geometry>` pieces (the 2.5 m polyline; consecutive pieces whose heading differs by < 0.02° merged). Arcs are not fitted (a dense-line approximation; best effort).
- `<lanes>`: `<laneSection>` at every `s` where the profile's lane structure changes (footpath appears/disappears, kerb height 0↔0.15). Centre lane `0`. Left lanes `1, 2, 3` (outward): `driving` (carriageway part left of the reference line), `curb` (kerb, `width = kerb.width_m`), `sidewalk` (the footpath strip; **absent where the footpath is missing**). Right lanes `-1, -2, -3` mirrored. Widths per `s`-interval: `<width sOffset a b c d>` with `a` = width at the start, `b` = linear change per metre, `c = d = 0` (profile edges vary linearly within an interval). The two carriageway halves are asymmetric because the reference line is the OSM centreline, not the carriageway centre.
- `<objects>`: each fixed furniture entity as `<object id name type s t zOffset="0" hdg height width length orientation="none">` (`type`: `pole`, `tree`, `bollard`, `barrier` for railings, `obstacle` otherwise) with `<userData code="hero:entity_id" value="…"/>`.
- Validation (`tests/unit/test_xodr.py`): well-formed XML; header attributes; geometry `s` strictly increasing and `Σ length == road length ±1 mm`; lane ids contiguous from the centre; widths ≥ 0; **round-trip** `hero.export.xodr.parse_xodr(build_xodr(...))` reproduces the reference-line polyline and every strip's `t`-edges within 5 cm and the same entity ids; consistency with the manifest. If `tests/fixtures/xodr/OpenDRIVE_1.7.xsd` exists (not committed — licence) the test also validates against it; otherwise that assertion is skipped with a visible reason. Declared **best effort**: no junctions, no signals, no elevation profile, flat z.

## 9. Engine import notes (copied verbatim into README → *Using the street in Unity / Unreal / Godot*)

Bundle: unzip `export.zip` (admin segment page → **Download for Unity / Unreal / Godot**). Keep `street.glb` and `street.manifest.json` together. Treat the manifest as the data source; glb `extras` are a convenience. What a port must rebuild itself: **all runtime gameplay code** (§6, §7), camera, input, HUD, sky/fog/light, avatar, obstacle visuals.

**Unity (2022 LTS or newer; any render pipeline).** (1) *Window → Package Manager → + → Add package by name* → `com.unity.cloud.gltfast`. (2) Drag `street.glb` into `Assets/`; glTFast's importer produces a prefab; drag it into a scene (or load at runtime with `GltfImport.Load(...)`/`GameObject` instantiate). (3) glTF is right-handed, Unity left-handed, so the importer mirrors one axis (glTFast's documented behaviour is to negate X; UnityGLTF negates Z — check your version's docs). **Do not assume which**: apply to every manifest position the *same* flip the importer applied to the vertices, and verify once with the end point — the world position `reference_line.points[last] = (x, z)` must coincide with the end of the imported `Road` mesh's bounds. (4) 1 glTF unit = 1 metre = 1 Unity unit. (5) Parse `street.manifest.json` (`JsonUtility`/Newtonsoft) for lanes, colliders, tokens, spawn hints, `gameplay`. (6) Materials import as the pipeline's PBR-metallic-roughness shader; `mat_flat` is vertex-coloured (enable vertex colours in the shader graph if the default shader ignores them).
**Unreal (5.3+).** (1) *Edit → Plugins*: enable **Interchange** (and the **glTF Importer** if offered by the engine version); restart. (2) *Content Browser → Import* `street.glb` (Interchange pipeline: import as static meshes, "Combine static meshes" off to keep one mesh per node, import materials/textures on). (3) Unreal is **Z-up, left-handed, centimetres**: the importer applies a ×100 scale and an axis conversion from glTF (right-handed, Y-up, metres) to UE; the exact axis mapping depends on the importer and version, so apply the same conversion to manifest positions and **verify once** with the street length (`length_s` ≈ the imported `Road` bounds along its long axis) and the end point `reference_line.points[last]`. If the scale is wrong, set *Import Uniform Scale* to 100. (4) Load `street.manifest.json` with a JSON parser (Blueprint *Json Blueprint Utilities* plugin or C++ `FJsonSerializer`).
**Godot (4.x).** (1) Copy `street.glb` and the manifest into `res://street/`; the editor imports `.glb` natively (Import dock → *Scene* with *Meshes → Generate LODs* off is fine). (2) glTF is right-handed Y-up metres, exactly Godot's convention, travel toward −Z: **no conversion**. (3) Drag the imported scene into a level; node names are `Footpath`, `Road`, `Frontage_L`, …; glTF `extras` appear as node metadata (`get_meta("extras")`). (4) `FileAccess.open("res://street/street.manifest.json", …)` + `JSON.parse_string` for the manifest; build `StaticBody3D` colliders from `colliders[]` and sample `lanes.stations` for lane following.

Engine specifics above were written from the engines' public documentation and are **not machine-verified** in this repository (no Unity/Unreal/Godot in CI); the README states the tested engine versions only after someone has tried them.

## 10. Versioning, determinism, licence

- `hero-street-manifest/1`, `hero-street-glb/1`, `hero-gameplay/1`, `hero-spawner-vectors/1` are independent tags; additive changes keep the tag, anything that changes meaning bumps it and `export.zip`'s `IMPORT.txt` says which tags it contains.
- **Determinism:** two builds of the same model version + observations in the same environment produce identical `street.glb`/manifest/xodr bytes (asserted by `tests/unit/test_export_determinism.py`: in-process twice, and in a subprocess with `PYTHONHASHSEED` 0 and 1). `build` records the library versions (`pygltflib`, `pillow`, `python`); a library upgrade may change bytes (never geometry) and is a deliberate, tested event (the hash baseline in `tests/fixtures/export/baseline_hashes.json` is regenerated in the same commit).
- **Licence / attribution:** the textures are derived from Google Static Maps / Street View imagery and OSM data. `attribution` is mandatory in any UI that shows the street; redistribution of the bundle needs the Google Maps Platform terms to be checked by the owner (`> Assumed:` the bundle is for personal/evaluation use).
