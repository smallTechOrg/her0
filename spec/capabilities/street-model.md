# Capability: Street Model *(Phase 2)*

## What It Does
Keeps, per street segment, one canonical, standardised, versioned model — a real-road reference line, a cross-section
profile that varies along the street (including the traced footpath), typed entities and buildings with stable ids,
per-fact provenance/confidence, fidelity levels, immutable version snapshots and diffs — and compiles any version
deterministically into the game scene, so the street gets better with every image instead of being rebuilt each run.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Road polyline (clipped to the segment) | WGS84 polyline | OSM via Nominatim reverse / Overpass | no (fallback: straight chord, flagged) |
| Projected evidence batches | `ProjectedBatch` list | `model-fusion` | for fused versions |
| Observations (georeferenced images) | `Observation` rows | `imagery-acquisition` | yes for fidelity/coverage |
| Phase-1 scene (legacy import) | Scene v1 | `segments.scene_json` | only for legacy segments |
| Previous model version | `CanonicalModel` | `model_versions` | no (v0 prior otherwise) |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `CanonicalModel` snapshot per version (v0 `prior`/`legacy`, then `fused`) | document | `model_versions.model_json` |
| `FidelityMap` (L0–L3 per 5 m bin) | document | `model_versions.fidelity_json`, API |
| `ModelDiff` vs the parent version | document | `model_versions.diff_json`, API |
| Compiled Scene v2 (curved corridor, variable cross-section, entities, buildings, `about`) — Phase 2.1 r2: lite Scene v3 + `StreetPlan` + export bundle (`street-export.md`) | document | `model_versions.scene_json`, `segments.scene_json`, `export_json` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| none (pure code; DB only through `hero.db.model_repo`) | — | — |

## Business Rules
- **Coordinates**: `(s, t)` along/left-positive-lateral of the reference line (`spec/data.md` → Frames); the reference line is fixed
  at the segment's first fused version and inherited thereafter; only a legacy → fresh lineage transition may set a new one.
- **Never drop knowledge**: no function deletes an entity; contradicted entities become `disputed` or `removed` (kept, same id, revivable).
- **Stable identity**: ids are minted once, deterministically; the same evidence merged onto the same base yields the same ids.
- **Idempotent merge**: an observation already in `fused_observation_ids` contributes nothing; re-merging the same batch is a no-op.
- **Fidelity is monotone**: it is computed from fused observations only, as a function of the *set*; adding observations never lowers any bin's level.
- **Versions are immutable** and dense (`0,1,2…`); a version is created only if the merged model's hash differs from its parent's.
- **Compile is pure**: the same model + observations + assets always give a byte-identical scene; the footpath lane centre follows the traced
  footpath strip at every 2.5 m station; lane centres keep ≥ 1.4 m separation; the last 6 m blend toward the first station for a seamless loop.
- **Legacy**: a Phase-1 scene imports as v0 (`kind=legacy`, `placement_verified=false`, all bins L0, entities with no provenance, description
  "legacy, placement unverified"); its scene stays v1 and keeps playing; a first fusion run on it starts a fresh lineage (nothing is merged from legacy entities).
- **Prior (L0)**: a segment with no street-level evidence still has a model: reference line + OSM-template profile + OSM buildings, flagged map-only.
- Scope: both sides of the street are modelled; only the modelled footpath side drives lanes, tokens and walkability.

## Success Criteria
- [ ] `ReferenceLine` on a circular-arc fixture (radius 80 m, 100 m long, built in the test): `to_st(*point_at(s, t))` returns `(s, t)` within 1 cm for 200 random `(s, t)` with `|t| ≤ 15` (seeded RNG); `length_s` within 0.05 % of the arc length.
- [ ] `clip_road` on a polyline longer than the window returns `length_s` within 0.5 m of the requested `length_m` centred on the projection of the centre (±0.5 m), oriented to the travel bearing; on a polyline 20 m short of the window it reports `extrapolated_m` summing to 20 ± 0.5.
- [ ] `project_street_detection` on a synthetic frame (camera at a known `(s, t)`, a ground point placed by `point_at`, projected into pixels by the inverse maths in the test) recovers `(s, t)` within 0.2 m for ranges 3–25 m, returns `None` beyond 30 m, and applies pitch correctly (±5° round trip).
- [ ] Merge property tests (fixed seeds, ≥ 50 generated cases each): (a) idempotence `merge(merge(m,B),B)` equals `merge(m,B)` ignoring `version/run_id/created_at`; (b) ids stable: every entity id of the base exists in the result; (c) no entity is ever removed from `entities`; (d) more supporting detections never lower confidence; a visible absence never raises it; (e) order of batches does not change the result beyond 1e-9.
- [ ] Entity resolution: two detections of the same type 0.8 m apart merge into one entity; 6 m apart stay two; same place different type with both ≥ 0.5 opens a `type_conflict` and keeps both.
- [ ] Absence: an entity with confidence 0.6 inside the visible region of 3 later observations with no detection ends `removed` only if confidence < 0.15 and ≥ 2 contradicting observations; it stays in the model and a later supporting detection restores `present` with the same id.
- [ ] `compute_fidelity`: one forward frame → the covered bins are L1, others L0; frames from two stations plus a left-facing and a right-facing frame → L2; adding a `user_photo` → L3 on its bins; for every pair of observation sets `A ⊆ B`, `level_B(bin) ≥ level_A(bin)` (property test).
- [ ] `diff_models(m, m)` → `is_empty`; v1→v2 fixture diff lists the exact added/changed entity ids, the improved bins and `summary_lines` containing the added count.
- [ ] `compile_scene` determinism: two calls and a call on a JSON round-tripped model give byte-identical `model_dump_json()`; on the curved fixture, for every station `lane_lat[0]` equals the footpath strip centre within 0.01 m and `lane_lat[0]−lane_lat[1] ≥ 1.4`; the constant-`lat` curve-length property of `spec/data.md` holds within 1 cm; the compiled scene validates against `SceneV2` and `load_scene` returns it.
- [ ] `import_legacy(example_scene)` → `kind="legacy"`, `placement_verified=false`, version 0, every fidelity bin L0, one entity per example object with `provenance == []`.
- [ ] DB: `upsert_observation` twice with the same `(segment_id, obs_key)` returns the same row and `created=False`; `publish_version` with `version != head+1` raises; a forced exception inside `publish_version` leaves `segments.scene_json` and `model_versions` unchanged (transaction test).
- [ ] `uv run alembic upgrade head` on a fresh DB and on a copy of the Phase-1 dev DB both end at revision `0002`; Phase-1 rows are untouched.
