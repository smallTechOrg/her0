# Capability: Street Generation

## What It Does
An admin turns a real 50–100 m street segment (two endpoints + footpath side) into a
playable scene by fetching real satellite, street-level and map geometry, running the vision
and review passes (`spec/agent.md`), and persisting the scene — while watching every step,
its timing, cost, tile count, the per-pass scene graphs, the review iterations and the final
scene, under a hard spend limit.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Admin password | string (header) | admin UI | yes |
| Segment endpoints, name, city, footpath side | lat/lon pair, strings, enum | admin form (prefilled with the seed) | yes |
| Satellite tile | image | Google Static Maps (cached) | yes |
| Street View availability + panoramas | metadata + images | Google Street View Static (cached) | no (fallback) |
| Road/footway/building geometry | map data | Overpass / OSM (cached) | no (fallback) |
| Model slugs, budgets, iteration cap | config | env | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Scene JSON (`spec/data.md`) | document | `segments.scene_json`, run row |
| Run record: steps + timings, imagery refs, pass graphs, iterations, cost, tokens, Google counts, error | row(s) | `pipeline_runs`, `llm_calls`, `google_requests` |
| Thumbnail + per-iteration top-down previews, billboard/facade crops | images | data dir, served under `/media/` |
| Structured log lines per step and per model call | logs | stdout |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Google Static Maps | 1 satellite tile per segment (cache first) | run fails `upstream_error` with key/API-enablement hint |
| Google Street View Static | 1 metadata check, then ≤`HERO_STREETVIEW_SAMPLES` images (cache first) | metadata non-OK or image errors → satellite-only generation, warning recorded |
| Overpass (OSM) | 1 bbox query (cache first) | continue without OSM; buildings from vision; warning |
| Anthropic | pass A (1 call), pass B (1 call), review (≤2 calls); each with the workspace header | retries on transient errors; schema re-ask once; pass A failure → run fails; pass B failure → satellite-only; review failure → keep current scene |

## Business Rules
- Generation is admin-only; players never trigger it.
- One batched model call per artefact (tile, pano set, review preview) — never per-object calls.
- Re-generating a segment whose imagery is cached makes **zero** new Google requests; every
  attempt (hit or miss) is counted and shown as "N new / M cached".
- Budget: before every model call the guard checks `spent_total + estimate ≤ HERO_CLAUDE_BUDGET_USD`
  and `spent_run + estimate ≤ HERO_RUN_BUDGET_USD`; otherwise the run ends `failed_budget`
  without making the call. Starting a run with no remaining budget is refused.
- Cost per call = tokens × configured price table; unknown model → run refuses to start.
- Only one run at a time; a second request is refused with "a run is in progress".
- A segment's previous scene is replaced only by a successful run.
- Low-confidence detections are kept and labelled, never dropped silently.
- The run view exposes every intermediate artefact: imagery, annotated tile, pass A/B graphs,
  each iteration's preview + critique + patch, final scene JSON, per-call cost table.
- Segment length must be 30–200 m (address-created segments are 100–200 m: 50–100 m either way of the address); otherwise creation is refused with the reason.

## Success Criteria
- [ ] Generating the seed segment with real keys ends `succeeded`, `quality ∈ {full, satellite_only}`,
      with ≥ 1 footpath coverage range tiling `[0, length_m]`, ≥ 1 building, ≥ 1 hazard object,
      every object having `latlon` and `tile_px` inside the tile, and the scene validating against the schema.
- [ ] The run record has a step list containing at least `resolve_geometry, fetch_satellite, verify_streetview, detect_layout, assemble_scene, review_iteration_1, finalize`, each with `latency_ms > 0`.
- [ ] `llm_calls` for the run: 2–4 rows, each `cost_usd > 0`, `cost_usd == price(input_tokens, output_tokens)` for its model; `pipeline_runs.cost_usd` equals their sum.
- [ ] Second generation of the same segment: `google_requests` new = 0, cached ≥ attempts of the first run.
- [ ] With `HERO_CLAUDE_BUDGET_USD` set below the first call's estimate (test setting), the run ends `failed_budget` with zero `llm_calls` rows and no Anthropic request made.
- [ ] A wrong admin password gets `unauthorized`; a second generate during a run gets `run_in_progress`.
- [ ] When the Street View metadata returns non-OK for a point with no coverage, the run still succeeds with `quality = satellite_only` and a warning.
- [ ] Each review iteration's critique is non-empty and its patch has ≤ 10 ops; iteration count ≤ cap.
- [ ] A `pipeline.step` log line and an `llm.call` log line with `cost_usd` appear for the run.
- Longer segments scale the imagery: the satellite tile drops one zoom level above 110 m, and Street View frames are
  sampled about every 40 m (max 7), so a 200 m segment is described from more than three photos.
