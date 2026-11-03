# Capability: Dynamic obstacles
## What It Does
Keeps vehicles, stalls, carriageway debris and other movable things out of the baked course (they stay in the model as `dynamic_context`) and spawns generic, fair, reproducible obstacles during each run.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| `scene.dynamic_context.spawn_hints` | type weights, density | compiled scene v3 | no (defaults) |
| static hazards and footpath presence | `objects[]`, `path`/`corridor` | scene | yes |
| run number or `?seed=` | int | localStorage counter / URL | yes (derived) |
## Outputs
| Output | Type | Destination |
|---|---|---|
| obstacles per loop `{id,type,lane,x_m,length_m,action,size_m}` | list | `Runner.activeObstacles()` → collision + dynamic layer |
| `window.__hero.dynamic` | `{run_number, seed, obstacles, rules}` | e2e/debug |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| none | pure client-side | n/a |
## Business Rules
- Fairness rules, ramp, type mix and seeding are **data** in `gameplay.json → spawner` and the algorithm is `spec/portability.md` §7 (single source; Python reference `hero.export.gameplay_ref`, JS `spawner.js` must equal it — `gameplay-data.md`). The roadmap r1 table is rationale only. Inputs come from the manifest (`spawn_hints`, colliders, footpath presence), not the scene.
- A spawned obstacle is never in the model or the scene; health (100, −10 per hit), tokens and lane logic are unchanged; crashing into one records its type in `game_runs.crashed_into_type`.
- Same `(slug, run number or seed, loop)` ⇒ same obstacles; vehicle-heavy `dynamic_context` ⇒ vehicle-heavy mix.
## Success Criteria
- [ ] Property test: 1 000 seeds × 10 loops × lengths 40–200 m — every row has ≥ 1 lane with no dodge obstacle within ±6 m (static included), ≤ 2 occupied lanes, ≤ 1 blocked lane in loops 0–1, row gap ≥ 14 m, action gap ≥ 9 m, nothing in the first 30 m of loop 0.
- [ ] Determinism and difference across run numbers asserted.
- [ ] With `weights = {parked_car: 0.5, two_wheeler: 0.4, …}` ≥ 60 % of 2 000 sampled obstacles are vehicles; with the default they are < 60 %.
- [ ] JS spawner output equals the committed Python-generated vectors exactly (`tests/e2e/test_spawner_vectors.py`); Python reference equals them too (`tests/unit/test_gameplay_ref.py`).
- [ ] Recompiled Bellandur: 0 `parked_vehicle` in `manifest.colliders`/`manifest.entities`/glb furniture extras, ≥ 35 in `scene.dynamic_context.by_type.parked_vehicle.count`.
- [ ] e2e: a run on the curved fixture shows spawned obstacles (`__hero.dynamic.obstacles.length > 0` within 40 m), a hit lowers health by 10, and `?seed=7` twice gives identical `obstacles`.
