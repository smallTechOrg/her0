# AI-Native Design

Outcome of the lens: **AI capabilities are core.** The product does not exist without the
vision-driven reconstruction; the game itself is conventional engineering.

## Opportunities considered

| Opportunity | Decision | Rationale |
|---|---|---|
| Satellite tile → layout scene graph | **AI (vision)** | No deterministic way to read footpath condition, trees, building edges from pixels |
| Street View → obstacles, facades, poles, parked cars | **AI (vision)** | same |
| Reviewing the generated scene against imagery | **AI (reflection)** | catches mislocated objects; measurable quality gain per brief |
| Walkability score | **no AI** | deterministic formula over the scene — auditable, reproducible |
| Lane mapping, token placement, collision, scoring | **no AI** | pure geometry/game logic |
| GPS nearest-segment pick | **no AI** | haversine over a small table |
| Admin correction | **human-in-the-loop (#13)**, no LLM | admin edits replace detections; no re-spend |
| Free-form ReAct agent with fetch/render tools | **rejected** | brief binds "one batched call per artefact, never per-object loops"; step order is fixed and cost must be predictable, so a fixed graph with one bounded reflection loop beats a free tool loop |

## Patterns (catalogue: `../patterns/agentic-ai.md`)

- **#1 Prompt chaining** — fixed step sequence: layout pass → obstacle pass → assemble → review.
- **#5 Tool use (deterministic, system-side)** — the renderer (`imagery.preview`) is the "tool"
  the review loop calls between LLM turns; the model never calls tools itself (structured
  output via forced tool-use is a formatting mechanism, not agency).
- **#4 Reflection** — bounded critique-and-correct loop (max `HERO_MAX_REVIEW_ITERATIONS`=2).
- **#12 Exception handling / recovery** — Street View absent → satellite-only; Overpass down →
  vision-only geometry; pass B invalid twice → generic obstacles off, scene still ships with
  `quality` downgraded; budget hit → labelled failure, never silent.
- **#16 Resource-aware** — batched calls, content-addressed imagery cache (zero Google re-spend),
  per-call USD accounting, per-run and global budget guard, model slug per pass.
- **#18 Guardrails** — every model output validated against the Pydantic schema (tool input
  schema); numeric fields clamped to segment bounds; one re-ask on validation failure with
  the validation error quoted; confidences ∈ [0,1].
- **#19 Evaluation/monitoring** — `llm_calls` table + structured log line per call; the
  integration gate asserts structure of real outputs.
- **#13 Human-in-the-loop** — Phase 2 admin correction (`spec/capabilities/admin-correction.md`).

## State

`hero.pipeline.state.PipelineState` (dataclass, serialisable to the run row):

```
run_id, segment (Segment), settings snapshot (models, budgets, zoom, samples)
frame: LocalFrame            # origin lat/lon, heading_deg, length_m  (hero.scene.geo)
osm: OsmGeometry | None      # ways in bbox, projected to the local frame
satellite: TileRef           # path, center, zoom, scale, size, meters_per_px, bounds
streetview: list[PanoRef]    # [] if unavailable; each: path, lat/lon, heading, x_m along
streetview_available: bool
layout: LayoutGraph | None   # pass A output (typed)
obstacles: ObstacleGraph|None# pass B output (typed)
scene: Scene | None          # assembled, then patched per iteration
iterations: list[ReviewIteration]   # n, preview_path, critique, patch_ops, accepted
steps: list[StepRecord]      # name, status, started_at, latency_ms, detail
cost: CostLedger             # input_tokens, output_tokens, cost_usd, google_requests, google_cache_hits
warnings: list[str]
error: PipelineError | None  # code, message, step
```

## Steps (nodes), in order

| # | step | does | LLM | on failure |
|---|---|---|---|---|
| 1 | `resolve_geometry` | length/bearing/local frame; Overpass bbox query (cached) | no | Overpass error → `osm=None`, warning, continue |
| 2 | `fetch_satellite` | Static Maps satellite tile centred on segment midpoint (cached) + georeference | no | fail run (`upstream_error`) — no ground, no game |
| 3 | `verify_streetview` | 1 metadata call at segment start (free); sets `streetview_available` | no | non-OK → `False`, warning "satellite-only" |
| 4 | `fetch_streetview` | `HERO_STREETVIEW_SAMPLES` images at 0/mid/end m, heading = segment bearing, fov 90, pitch 0, 640×640 (cached) | no | skipped if unavailable; partial failures drop that pano |
| 5 | `detect_layout` | **one** call: raw tile + annotated tile (segment line, 10 m ticks, footpath-side arrow, OSM overlay if present) → `LayoutGraph` | A | schema invalid → one re-ask; still invalid → fail run |
| 6 | `detect_obstacles` | **one** call: all panos (each labelled with its x_m and heading) → `ObstacleGraph` | B | skipped if no panos; invalid twice → `obstacles=None`, warning, quality `satellite_only` |
| 7 | `assemble_scene` | merge → `Scene`: cross-section lanes from layout widths, footpath coverage, buildings (OSM footprints preferred, vision boxes otherwise), objects with lane/action mapping, tokens on intact footpath + crossings, crops → billboard files, `latlon` + `tile_px` for every object, walkability | no | any exception → fail run |
| 8 | `review_iteration_N` | render top-down preview (tile + scene drawn) → **one** call with preview + tile + panos → `{accepted, critique, patch_ops[]}`; apply patch; re-render; stop when `accepted` or N = max | R | invalid patch → ignore patch, mark iteration `rejected`, continue; LLM error → stop loop, keep current scene, warning |
| 9 | `finalize` | validate Scene, compute `generation.quality` + `confidence_overall`, write thumbnail preview, persist scene on segment, run `succeeded` | no | fail run |

Budget guard wraps every LLM step: before the call, `BudgetGuard.check(run_id, est_usd)`
where `est_usd` = price of (image tokens estimate + prompt tokens, assumed max output 4 k
tokens); if `spent_total + est > HERO_CLAUDE_BUDGET_USD` or `spent_run + est > HERO_RUN_BUDGET_USD`
→ run ends `failed_budget` **before** the request. After the call the real usage is priced
and recorded. The admin API also refuses to start a run when `remaining_usd <= 0` (402).

## Edges

Linear 1→2→3→(4)→5→(6)→7→8…→9. Conditional edges: 3 decides 4 and 6; 8 loops at most
`max_iterations` times and exits early on `accepted=true`. No other branching.

## Patch operations (review output contract)

`patch_ops: list[{op, target, …}]` with `op ∈ {move, remove, add, relabel, set_coverage}`:
`move {object_id, x_m, lane}`, `remove {object_id}`, `add {object: Object}`,
`relabel {object_id, type, action, confidence}`, `set_coverage {from_m, to_m, state}`.
Applied by `hero.pipeline.review.apply_patch(scene, ops)` which validates and clamps.

## Error handler

Every step runs inside `runner.run_step(name, fn)`: records `started`, times it, catches
`UpstreamError | ValidationError | BudgetExceeded | Exception`, writes `StepRecord`, logs
`pipeline.step`, and either continues (if the step declares `recoverable=True`) or sets
`state.error` and jumps to a terminal `fail(run)` that persists partial outputs (imagery
refs, any graphs so far) so the admin can inspect what happened.

## Finalize

`segments.scene_json ← scene`, `segments.status = ready`, `segments.quality`,
`segments.latest_run_id`, `pipeline_runs.{scene_json, iterations_json, steps_json, cost_usd,
input_tokens, output_tokens, google_requests, google_cached_hits, status, finished_at}`.

## Concurrency

One worker thread (`registry.start_run`), single-flight: a second generate request while a
run is `queued|running` → 409 `run_in_progress`. LLM calls are sequential by design. Google
fetches within step 4 run sequentially (≤3 requests; no need for parallelism).

## Assembly (pseudocode)

```python
def run_pipeline(run_id: str) -> None:
    state = load_state(run_id)
    guard = BudgetGuard(settings, repo)
    llm = AnthropicClient(settings, guard, repo)        # workspace header, retries, pricing
    steps = [
        Step("resolve_geometry", resolve_geometry, recoverable=False),
        Step("fetch_satellite",  fetch_satellite,  recoverable=False),
        Step("verify_streetview", verify_streetview, recoverable=True),
        Step("fetch_streetview", fetch_streetview, recoverable=True, when=lambda s: s.streetview_available),
        Step("detect_layout",    detect_layout(llm), recoverable=False),
        Step("detect_obstacles", detect_obstacles(llm), recoverable=True, when=lambda s: bool(s.streetview)),
        Step("assemble_scene",   assemble_scene,   recoverable=False),
    ]
    for step in steps:
        if not run_step(state, step): return fail(state)
    for n in range(1, settings.max_review_iterations + 1):
        ok = run_step(state, Step(f"review_iteration_{n}", review_once(llm, n), recoverable=True))
        if not ok or state.iterations[-1].accepted: break
    run_step(state, Step("finalize", finalize, recoverable=False)) or fail(state)
```

## Prompts (files in `src/hero/prompts/`)

- `detect_layout.md` — role: civic infrastructure surveyor; inputs: raw tile, annotated tile,
  segment length, footpath side, OSM summary; output schema `LayoutGraph` (`spec/data.md`);
  rules: metres along the segment from the START marker, coverage ranges must tile [0,length],
  mark `confidence` per item, prefer "unknown" over guessing.
- `detect_obstacles.md` — inputs: each pano with its `x_m` and heading; output `ObstacleGraph`;
  rules: estimate `x_m` = pano `x_m` + forward distance, `lane` by position in the cross-section,
  `crop_px` boxes in the pano's pixel space, facade colours as hex per building span.
- `review_scene.md` — inputs: preview, tile, panos, current scene summary (objects with ids);
  output `{accepted, critique, patch_ops}`; rules: only patch what the imagery contradicts,
  at most 10 ops, accept when no material discrepancy remains.

Each prompt carries a node tag (`<node:layout>`, `<node:obstacles>`, `<node:review>`) in its
first line so an optional stub client (unit tests only) branches on tags, never prose.
