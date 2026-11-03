# Capability: Scene realism and gap-free compile
## What It Does
Compiles a model into a plan whose ground, kerb, frontage and furniture are textured or procedurally filled with no blank stretch, bakes it into one low-poly glTF street (`street-export.md`), and renders that glb in the browser with sky, fog and a readable camera inside a fixed draw-call budget.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| canonical model version | `CanonicalModel` | `model_versions` | yes |
| observations (satellite crops, Street View/user photos) | files + rows | `observations`, data dir | no (procedural fallback) |
| `gameplay.json` | rules | `src/hero/export/` | yes |
## Outputs
| Output | Type | Destination |
|---|---|---|
| lite Scene v3 (`about.interpolated`, `dynamic_context`, `assets`) | JSON | `scene_json`, `GET …/scene` |
| `StreetPlan` (internal) | pure data | mesh builder, manifest builder |
| baked textures | JPEG | `data/textures/<slug>/` (embedded in the glb) |
| gap report | JSON | `gaps_json`, `GET …/gaps` |
| `street.glb` + manifest | files | `street-export.md` |
| rendered game frame | canvas | player |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| none (bake reads stored files only) | | missing/corrupt image ⇒ that bin is painted procedural + warning; compile and export never fail for texture reasons |
## Business Rules
- Every 5 m bin has ground texture (real, interpolated or procedural — painted into the atlas) and frontage on both sides; every non-evidence bin is listed in `about.interpolated`; low fidelity is never drawn in-world.
- Fixed furniture = footpath-attached items (`spec/data.md` → Runtime classification); sizes capped per type; the camera-clearance clip removes anything within 1.5 m of the camera path (shader, since furniture is merged).
- Geometry comes from the glb only (v3 streets); legacy v1/v2 streets keep the old JS geometry.
- Render budget: ≤ 60 draw calls, ≤ 60 000 triangles, ≤ 8 textures, ≤ 24 MB texture memory (`spec/architecture.md` → Render budget r2).
- Compile and mesh build are pure and deterministic; a recompile of an unchanged model is byte-identical.
## Success Criteria
- [ ] `audit_scene` on the Bellandur plan/manifest reports 0 blank ground and frontage bins on both sides; on the recorded v2 scene it reports > 0 hole bins.
- [ ] `compile_scene` twice / after JSON round-trip is byte-identical; mesh coverage test (`test_mesh_coverage`) passes on both fixtures.
- [ ] Phone-viewport e2e at 0/20/40/70 m on both scenes: `source == "glb"`, `perf.draw_calls ≤ 60`, `triangles ≤ 60000`, `textures ≤ 8`, `texture_mb ≤ 24`.
- [ ] AFTER screenshots (from the glb-loaded game): textured footpath + kerb + road, frontage both sides, sky/horizon, no "?" box, no in-world label, no baked vehicle, no object larger than its cap (orchestrator reads them).
