# Capability: Walkability Report

## What It Does
After a crash, the player sees their score and best-score status, the street's walkability
score card, a satellite overlay of the detected hazards at their real coordinates and the list
of what was detected; the run is logged and the per-street best score is persisted.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Run outcome | record (`spec/api.md` POST runs body) | `street-run` | yes |
| Scene walkability block, objects, tokens, ground tile + `tile_px` | document | scene already loaded | yes |
| Street run history (Phase 3) | list | public API | no |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `game_runs` row | record | DB |
| `segments.best_score / best_distance_m` updated when beaten | fields | DB; gallery card |
| Results overlay: score, best status, walkability card, hazard map, detected list | screen | player |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Public API | post run; (Phase 3) fetch top runs | results still render from local scene data with "Score couldn't be saved" notice |

## Business Rules
- Walkability is computed deterministically from the scene (`spec/data.md` → walkability);
  the same scene always yields the same card. It is computed at generation time and stored in
  the scene; the results screen reads it, never recomputes.
- Hazard map markers are placed at `tile_px` over the real satellite tile; the crashed-into
  object is highlighted; markers are colour-coded by action; tapping shows label + confidence.
- Low-confidence detections are shown with a "low confidence" label; the card states
  "includes low-confidence detections" when any hazard is below the threshold.
- Best score per street = max score over its logged runs; no accounts, no names.
- Run validation (`spec/data.md`) rejects impossible scores; rejection is logged and the
  client shows the notice but still displays the local result.
- Phase 2 (scene v2): the same formula runs on the compiled scene — only `intact` footpath counts as covered (`broken`, `blocked`,
  `encroached`, `missing`, `unknown` do not) — and the card adds the model line (fidelity level, photo count, last update), an
  "About this street" button, and a note "based on a single-viewpoint (L1) model; treat as indicative" when the overall fidelity is below L2.
  The hazard map draws the curved `path` polyline (from `path.samples[].tile_px`) instead of a straight segment line.
- Phase 3: results also list the street's top 5 runs.

## Success Criteria
- [ ] `compute_walkability` on the canonical example scene returns `footpath_coverage_pct = 29.5`, `hazard_count = 4`, `crossing_present = true`, `score = 50`; on a scene with full intact coverage, a crossing and zero hazards returns `score = 100`; with no footpath, no crossing and 10+ hazards returns `0`.
- [ ] Posting a valid run returns `is_new_best = true` the first time and `false` for a lower score; the gallery entry's `best_score` reflects the max; after a process restart the value is unchanged (state-survival test).
- [ ] Posting `score > distance_m + 50*tokens + 1` is rejected with `validation_error`.
- [ ] In the headless browser after a crash: the results overlay shows "Walkability", the coverage percentage text, hazard count, a crossing yes/no, and a canvas overlay element; the detected-objects list has one row per hazard in the scene.
- [ ] A hazard with `confidence < 0.6` renders the text "low confidence" in its row.
