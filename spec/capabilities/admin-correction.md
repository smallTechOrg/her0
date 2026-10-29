# Capability: Admin Correction *(Phase 2)*

## What It Does
The admin corrects what was detected — footpath coverage ranges, object positions/lanes/types,
deletions, additions — directly on the satellite tile, and the scene is updated with no new
imagery or model spend.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Patch operations | list (ops contract in `spec/agent.md`) | correction UI | yes |
| Current scene | document | DB | yes |
| Admin password | header | admin UI | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Updated scene (recomputed `lat_m`, `latlon`, `tile_px`, tokens, walkability) | document | `segments.scene_json` |
| Correction record | row | `segment_corrections` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| none (no Google, no model) | — | — |

## Business Rules
- Corrections apply on top of the latest generated scene; "Reset" restores it.
- Applying a correction is deterministic: same ops + same scene → same result.
- A corrected object gets `confidence = 1.0` and `source` preserved; added objects have
  `source = null`, `billboard_url = null` (labelled box in game).
- Manual work stays small: drag, lane select, type select, delete, add — nothing else.
- Phase 1 ships the "Correct detected lines" button as a labelled stub.

## Success Criteria
- [ ] `PUT corrections` with `[{op: "remove", object_id: "<a hazard>"}]` reduces `walkability.hazard_count` by 1, logs a `segment_corrections` row, creates no `llm_calls`/`google_requests` rows, and the scene still validates.
- [ ] `set_coverage` making the whole segment `intact` raises `footpath_coverage_pct` to 100 and removes `footpath_gap` objects.
- [ ] Headless browser: dragging an object marker and saving shows "Saved — walkability recalculated, $0.00 spent" and the play scene reflects the new `x_m`.
