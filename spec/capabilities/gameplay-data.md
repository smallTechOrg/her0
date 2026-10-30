# Capability: Gameplay as data
## What It Does
Holds every gameplay rule (lane centres, health/damage, invulnerability, start grace, token and scoring rules, speed ramp, dynamic-obstacle spawner parameters) in one versioned `gameplay.json`, read by the compile, the Python reference spawner and the JS runner/spawner, so a port in another engine can reimplement the game byte-for-byte.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| `gameplay.json` | `hero-gameplay/1` | `src/hero/export/gameplay.json` | yes |
| `spawn_hints`, colliders, footpath presence | manifest fields | `street.manifest.json` | no (legacy scenes use `default_spawn_hints`) |
| run number / `?seed=` | int | localStorage / URL | yes (derived) |
## Outputs
| Output | Type | Destination |
|---|---|---|
| spawned obstacles `(loop, row, lane, type, x_mm, variant)` | list | JS runner + dynamic layer; Python reference |
| `gameplay_sha256` | string | manifest, glb `asset.extras`, test vectors |
| `spawner_vectors.json` / `_bulk.json` | test vectors | `tests/fixtures/portability/` |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| none | pure | `gameplay.json` failing schema validation stops server startup (clear message) |
## Business Rules
- Contract, PRNG (`mulberry32`/`fnv1a32`), numeric rules, algorithm and vector format: `spec/portability.md` §5–§7. The fairness rule table of roadmap r1 is superseded by it.
- No gameplay number is hard-coded in JS or Python outside `gameplay.json` (grep tests). Changing the file changes `gameplay_sha256`; the vectors must be regenerated in the same commit.
- Exactly 8 PRNG draws per slot regardless of geometry; floats are binary64 using `+ − * /`, comparisons, `floor`, `min`, `max` only; positions are exported as integer millimetres `floor(x·1000+0.5)`.
## Success Criteria
- [ ] Python reference output equals the committed vectors for ≥ 8 cases (loops 0–2); recomputing is stable; vector `gameplay_sha256` matches the file.
- [ ] The JS spawner equals the same vectors and the bulk file exactly (Chromium via Playwright).
- [ ] Fairness properties hold on the reference for 1 000 seeds × 10 loops × lengths 40–200 m.
- [ ] `GameplayV1` rejects unknown keys, missing `obstacle_types`, weights summing wrong, lane gaps < `min_footpath_to_edge_m`; the committed JSON Schema equals the generated one.
- [ ] Compile derives lane centres from `gameplay.lanes`: changing `road_edge_inset_m` in a test copy moves `lanes.stations[].lane_t[1]` by exactly that amount.
