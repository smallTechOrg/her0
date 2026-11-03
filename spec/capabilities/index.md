# Capabilities

| Capability | Phase | Status |
|---|---|---|
| [street-generation](street-generation.md) — admin generates a street segment, with run view and budget guard | 1 (legacy one-shot pipeline); superseded by the Phase-2 capabilities below | real (legacy); `POST …/generate` now runs `model-fusion` |
| [street-run](street-run.md) — player picks a street from the gallery and runs it; Phase 2: the corridor follows the traced road and footpath | 1, extended in 2 | real (gallery shows every ready street; GPS pick stubbed) |
| [walkability-report](walkability-report.md) — post-run score card, hazard overlay, best score, run log | 1, extended in 2 | real (run history list added in Phase 3) |
| [street-model](street-model.md) — canonical, versioned, standardised street model: reference line, cross-section profile, typed entities with stable ids, provenance, fidelity levels, diffs, deterministic compile to the game scene | **2** | real |
| [imagery-acquisition](imagery-acquisition.md) — dense, cached, cost-guarded Street View + close-up satellite acquisition into georeferenced observations; "fetch more"; photo upload record | **2**, extended in 2.1 | real (upload UI real since 2.1; uploads accept `s`/`side`/`facing`/`captured_at`) |
| [model-fusion](model-fusion.md) — batched perception → projection → evidence-grounded merge → critique → new model version + run report; no-op when nothing is new | **2** | real |
| [model-inspection](model-inspection.md) — admin segment page: version history, fidelity/coverage map, entities with evidence, diffs, run report, add-imagery | **2** | real |
| [street-description](street-description.md) — descriptions everywhere: About-this-street panel, results model line, run narrative, labelled low-fidelity fallback | **2** | real |
| [model-improvement](model-improvement.md) — "How to improve this model": weakest stretches, priced Fill gaps, real photo upload placed on the map, free Regenerate with what we have | **2.1** | real (retires the upload stub) |
| [dynamic-obstacles](dynamic-obstacles.md) — vehicles/stalls/debris stay in the model as `dynamic_context`; the game spawns seeded, fair, generic obstacles | **2.1** | real |
| [scene-realism](scene-realism.md) — gap-free compile (textures, kerb, continuous frontage, furniture, interpolation labelled) + sky/fog/camera inside a render budget; Scene v3 + recompile migration | **2.1** | real |
| [street-export](street-export.md) — shared deterministic Python mesh builder → `street.glb` (glTF 2.0), manifest, best-effort OpenDRIVE, served + downloadable for Unity/Unreal/Godot; the browser loads the same glb | **2.1 r2** | real |
| [gameplay-data](gameplay-data.md) — gameplay rules + seeded spawner parameters as `gameplay.json`, Python reference spawner and shared test vectors (Python + JS) | **2.1 r2** | real |
| [model-choice](model-choice.md) — per-run Claude model dropdown (Sonnet 5.5 default, Opus 5.5 after one verified call + sourced price), recorded on runs/reports/calls | **2.1 r2** | real |
| [seed-city](seed-city.md) — 5–20 seeded segments for one city | 3 | stub button |
| [gps-pick](gps-pick.md) — "the street I'm standing on" | 3 | stub button |
| [admin-correction](admin-correction.md) — admin fixes detected lines/objects on a map, no re-spend (now edits the canonical model as human evidence) | 3 | stub button |
