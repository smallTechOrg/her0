# Capability: Street export (shared mesh, glTF, manifest, OpenDRIVE)
## What It Does
Builds, once and deterministically in Python, a low-poly textured 3D street from a model version and publishes it as `street.glb` + `street.manifest.json` + `street.xodr` (+ `gameplay.json`) that the browser game loads and that Unity, Unreal or Godot can import.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| canonical model version | `CanonicalModel` | `model_versions` | yes |
| compiled `StreetPlan` + lite scene + gap report | pure compile output | `hero.model.compile_scene` | yes |
| observations / baked textures | files + rows | data dir, `data/textures/` | no (procedural fill) |
| `gameplay.json` | rules as data | `src/hero/export/gameplay.json` | yes |
## Outputs
| Output | Type | Destination |
|---|---|---|
| `street.glb` | glTF 2.0 binary (metres, +Y up, named nodes, PBR, baked UVs, `extras`) | `data/exports/<slug>/`, `GET …/model.glb` |
| `street.manifest.json` | JSON `hero-street-manifest/1` | `GET …/manifest.json` |
| `street.xodr` | OpenDRIVE 1.7 (best effort) | `GET …/street.xodr` |
| `export.zip` | deterministic zip + `IMPORT.txt` | admin "Download for Unity / Unreal / Godot" |
| `export_json` | `ExportRecord` | `model_versions` |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| none (disk only; network clients disabled in tests) | | glb over `HERO_EXPORT_MAX_GLB_MB` or failed validation ⇒ export fails with `export_too_large`/`export_invalid`, the previous scene and bundle stay served |
## Business Rules
- Contract (frame, node names, `extras`, manifest fields, budgets): `spec/portability.md` §2–§5, §8. One fact, one place.
- Same model version + observations + pinned library versions ⇒ identical bytes (sha256); the library's serialiser is never trusted (own canonical writer).
- Geometry is never mirrored; there is never a hole between wall and wall; nothing evidence-less is drawn as "blank" — gaps are interpolated/procedural and listed in `manifest.interpolated`.
- Dynamic-context entities (vehicles, stalls, carriageway debris) never appear in the glb, manifest `entities` or `colliders`.
- Textures derive from Google imagery: the manifest carries the attribution and a licence notice; the README says so.
- Serving: ETag + 304, immutable for `?v=<sha8>` URLs; export is built in publish/recompile, lazily once if missing.
## Success Criteria
- [ ] `build_street_mesh` twice (and in two subprocesses with different `PYTHONHASHSEED`) ⇒ equal `sha256(write_glb(...))`.
- [ ] glb: exactly the contract node names, ≤ 14 primitives, ≤ 12 000 triangles, ≤ 2 textures, ≤ 1.5 MB; `pygltflib` reload and `validate_glb` clean; Khronos validator 0 errors when available.
- [ ] Mesh coverage: per 5 m bin both frontages present; per station the ground nodes tile wall-to-wall (Bellandur + curved fixture).
- [ ] `Road` bounds length ≈ `length_s` ± 0.5 m; `extras` entity ids/(s,t) equal `manifest.entities`.
- [ ] `parse_xodr(build_xodr(...))` reproduces the reference line and strip edges within 5 cm.
- [ ] `GET model.glb` → 200, `model/gltf-binary`, ETag; repeat with `If-None-Match` → 304; `?v=` URL has `immutable`; `export.zip` has 5 entries and is byte-identical on repeat.
- [ ] `recompile --all` on a db copy: second run reports `unchanged` and identical `mesh_sha256`; 0 Google / 0 Anthropic requests.
