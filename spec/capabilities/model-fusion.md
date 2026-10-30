# Capability: Model Fusion *(Phase 2)*

## What It Does
A generation run ingests the imagery plan, sends only the not-yet-fused observations to Claude in a few batched vision calls,
projects the detections geometrically into the street's `(s, t)` frame, merges them with evidence arithmetic into the canonical
model (updating confidences, keeping contradictions visible, never dropping supported entities), critiques the result once
against the imagery, and publishes the next model version with a plain-language run report — so each run raises fidelity and an
unchanged input costs nothing.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Segment, mode (`generate` \| `acquire`), optional imagery-plan overrides, optional run budget | request | admin API | yes |
| Base model (head version, or v0 prior / legacy-fresh) | `CanonicalModel` | `street-model` | yes |
| Unfused observations (+ stored-but-unmerged extractions) | rows | `imagery-acquisition`, `observation_extractions` | yes |
| OSM context (building footprints, road tags, sidewalks) | map data | Overpass (cached) | no |
| Model slug(s), structured mode, batch size, budgets, review cap | config | env | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| New model version (snapshot, fidelity, diff, compiled scene, summary) — or an explicit no-op | rows | `model_versions`, `segments.scene_json` |
| Raw perception outputs per call | rows | `observation_extractions` |
| Run record: steps + timings, calls, cost, Google counts, critique iterations | rows | `pipeline_runs`, `llm_calls`, `google_requests` |
| **Run report**: images used (new/cached/by source/by heading), new/changed/removed/contradicted entities, profile changes, fidelity before→after, confidence changes, cost, warnings, plain-language summary and suggestions | document | `run_reports` |
| Entity crops (immutable, version-suffixed URLs), thumbnail | images | `data/crops/<slug>/`, `data/previews/<run_id>/` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Anthropic | 1 overhead call (≤ 4 rectified windows), `ceil(n/8)` street-level calls, ≤ `HERO_MAX_REVIEW_ITERATIONS` critique calls — each with the `anthropic-workspace-id` header, structured output in the configured mode | retries on transient errors; schema re-ask once; a failed batch is skipped and its observations stay unfused for the next run; critique failure keeps the candidate; budget stop = `failed_budget`, nothing published |
| Google / Nominatim / Overpass | via `imagery-acquisition` | see that capability |

## Business Rules
- **Claude perceives, code decides.** Detections come back as pixel boxes/polylines plus descriptions; positions are computed by `hero.model.projection` (ground-plane projection with the frame's heading/pitch/fov, rectified-strip inverse mapping) — the model's own distance guess is only a fallback and a cross-check.
- **Tell the model the geometry**: each frame's caption states view, station, heading relative to travel, and which side the modelled footpath is on; rectified windows state their axes and scale and carry s/t tick marks.
- **Fewer, richer calls**: ≤ `HERO_PERCEPTION_BATCH_IMAGES` (8) frames per call, contiguous stations grouped; one overhead call; never one call per object.
- **Cumulative**: only observations absent from `fused_observation_ids` are sent; identical cached input ⇒ **no-op** (no Claude call, `$0.00`, no new version, report says "no new images"). More/different images ⇒ new version with a diff.
- **Merge** (`spec/data.md` → Evidence rules): type + `(s,t)` proximity + size gating, precision-weighted position, log-odds confidence with calibration, support weight decays per repeated support, absence is weaker than presence, contradictions stored and listed, nothing deleted, `removed` entities revivable.
- **Critique**: one bounded reflection over a top-down render of the model vs. satellite crops; ops apply as low-weight evidence (≤ 0.15 confidence, ≤ 3 m nudge) and cannot delete.
- **Budget**: per-run cap `HERO_RUN_BUDGET_USD` (default $1.00, overridable per run within 0.05–5) and the global `HERO_CLAUDE_BUDGET_USD` guard every call; the batch planner trims low-priority batches to stay ≤ 80 % of the remaining run cap and reports it; a run is refused (402) when the global budget is spent.
- **Atomic publish**: model version + scene + segment pointer change in one transaction; a failed or budget-stopped run never replaces a published scene; a segment with no scene yet gets the labelled L0 prior scene instead of nothing.
- **Descriptive output**: perception asks for a description of every strip/entity/facade; the run report is deterministic text over structured data (no LLM) so it is reproducible and free.
- **Model slug**: `HERO_VISION_MODEL`/`HERO_REVIEW_MODEL`, structured output in `forced_tool | auto_tool | json_text` (`auto` falls back when a slug rejects forced tool choice); defaults per the P0 spike (`spec/roadmap.md`).

## Success Criteria
- [ ] With `ReplayLLM` (recorded extractions) and the recorded Google transport, `run_fusion` on the curved fixture segment ends `succeeded`, creates version 1 with `parent_version=0`, `entity_count ≥ 5`, `fidelity_level ≥ 1`, one `run_reports` row, one `observation_extractions` row per call, steps containing `acquire_imagery, select_unfused, perceive_streetlevel, merge, publish, report`, each `latency_ms > 0`.
- [ ] Re-running with identical inputs: `version_created=false`, `ReplayLLM` received 0 calls, `llm_calls` rows for the run = 0, `cost_usd == 0`, `google_requests` new = 0, `model_versions` count unchanged, report summary contains "no new images".
- [ ] Adding observations (left/right headings) and running again creates version 2 with `diff.fidelity.improved` non-empty, every version-1 entity id still present in version 2, and `diff.observations.fused_added == number of new observations`.
- [ ] A recorded extraction batch with `local_id` collisions across batches does not double-count: evidence is unique per `(observation_id, detection_key)`.
- [ ] Budget: with `HERO_RUN_BUDGET_USD=0.01` the run ends `failed_budget` before the first request (`ReplayLLM` calls == 0), publishes nothing, leaves observations stored and the segment's scene unchanged; with a cap that fits only one of three batches the report lists `budget_trimmed`.
- [ ] A street-level batch that fails schema validation twice is skipped: its observations remain unfused, a warning is in the report, the run still `succeeded` with the other batches merged; the next run picks them up.
- [ ] `AnthropicClient` in `auto` mode, against a fake SDK that returns HTTP 400 "tool_choice" for forced calls, succeeds via `auto_tool`, and via `json_text` when the fake returns text only; the `anthropic-workspace-id` header is present on all requests in all three modes (asserted on the fake's recorded requests).
- [ ] The critique step: ops outside the bounds (|nudge| > 3 m) are skipped and listed; an op naming an unknown entity id is skipped; applying any ops never reduces `len(entities)`.
- [ ] Logs: a `pipeline.step` line per step, an `llm.call` line with `cost_usd` per call, `model.merge` and `model.published` lines with counts for the run.
- [ ] `RunReport.cost.anthropic_usd == SUM(llm_calls.cost_usd)` for the run and `images.new + images.cached == images.used_total == images.planned − len(images.skipped)`; `summary` and `suggestions` are non-empty strings; `fidelity.before/after` match the stored fidelity of the parent/new versions.
