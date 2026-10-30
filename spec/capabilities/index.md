# Capabilities

| Capability | Phase | Status |
|---|---|---|
| [street-generation](street-generation.md) — admin generates a street segment, with run view and budget guard | 1 (legacy one-shot pipeline); superseded by the Phase-2 capabilities below | real (legacy); `POST …/generate` now runs `model-fusion` |
| [street-run](street-run.md) — player picks a street from the gallery and runs it; Phase 2: the corridor follows the traced road and footpath | 1, extended in 2 | real (gallery shows every ready street; GPS pick stubbed) |
| [walkability-report](walkability-report.md) — post-run score card, hazard overlay, best score, run log | 1, extended in 2 | real (run history list added in Phase 3) |
| [street-model](street-model.md) — canonical, versioned, standardised street model: reference line, cross-section profile, typed entities with stable ids, provenance, fidelity levels, diffs, deterministic compile to the game scene | **2** | real |
| [imagery-acquisition](imagery-acquisition.md) — dense, cached, cost-guarded Street View + close-up satellite acquisition into georeferenced observations; "fetch more"; photo upload record | **2** | real (upload UI = labelled stub) |
| [model-fusion](model-fusion.md) — batched perception → projection → evidence-grounded merge → critique → new model version + run report; no-op when nothing is new | **2** | real |
| [model-inspection](model-inspection.md) — admin segment page: version history, fidelity/coverage map, entities with evidence, diffs, run report, add-imagery | **2** | real |
| [street-description](street-description.md) — descriptions everywhere: About-this-street panel, results model line, run narrative, labelled low-fidelity fallback | **2** | real |
| [seed-city](seed-city.md) — 5–20 seeded segments for one city | 3 | stub button |
| [gps-pick](gps-pick.md) — "the street I'm standing on" | 3 | stub button |
| [admin-correction](admin-correction.md) — admin fixes detected lines/objects on a map, no re-spend (now edits the canonical model as human evidence) | 3 | stub button |
