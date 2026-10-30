# AI-Native Design

Outcome of the lens: **AI capabilities are core, and in Phase 2 they become cumulative.** The
product does not exist without vision-driven reconstruction; the game itself is conventional
engineering. Phase 2's design rule: **Claude = perception, code = geometry, memory and merging.**
The model reads pixels and writes descriptions; everything that has a right answer (projection to
`(s, t)`, entity resolution, confidence arithmetic, fidelity, diffs, compilation) is deterministic
code over a persistent canonical model, so a second run *adds* knowledge instead of replacing it.

## Opportunities considered

| Opportunity | Decision | Rationale |
|---|---|---|
| Rectified satellite strips → footpath/kerb/carriageway traces, footpath continuity, overhead objects | **AI (vision), 1 batched call** | reading pavement condition and edges from pixels has no deterministic equivalent; rectification makes the output (pixel polylines) exactly convertible to `(s, t)` by code |
| Street View frames (4 headings × dense stations) → typed entities, attributes, facades, descriptions | **AI (vision), few batched calls (≤ 8 frames each)** | no deterministic way to name/describe poles, parked autos, drains; one call per batch, never per object |
| Pixel → ground position of a detection | **no AI** | pinhole + flat-ground maths with the frame's heading/pitch/fov (`hero.model.projection`); the model's own distance guess is only a fallback/cross-check |
| Reference line, clipping, strip edges from pixel polylines, OSM priors | **no AI** | geometry from OSM + deterministic transforms |
| Entity resolution across runs, confidence update, contradiction handling | **no AI (evidence arithmetic)** | auditable, reproducible, idempotent; an LLM "merger" would be non-deterministic and could silently drop supported entities |
| Confidence calibration | **no AI (monotone table)** | model-reported confidences are over-confident; a fixed shrink is testable |
| Critique of the model against imagery | **AI (reflection, ≤ `HERO_MAX_REVIEW_ITERATIONS`=1)** | catches mislocated footpaths/objects that no rule can see; bounded and evidence-weighted so it cannot overrule real support |
| Fidelity levels, coverage map, diffs, run report, "About this street" text | **no AI (templates)** | deterministic, free, testable by substring; the *descriptions of things* come from perception, the *summaries of the model* come from code |
| Walkability score, lanes, tokens, collision, scoring | **no AI** | pure formulas / game logic |
| GPS nearest-segment pick (Phase 3) | **no AI** | haversine |
| Admin correction (Phase 3) | **human-in-the-loop (#13), no LLM** | human edits enter the model as `human` evidence |
| Free-form ReAct agent that decides which images to fetch next | **rejected** | cost must be predictable; the imagery plan is explicit and admin-controlled; the fidelity map already says exactly which image would help (`hint`) |

## Patterns (catalogue: `../patterns/agentic-ai.md`)

- **#8 Memory management** — the **canonical street model** is the long-term memory: stable entity
  ids, per-fact provenance, confidence, version snapshots. `observations` + `observation_extractions`
  are the episodic memory (replayable without the LLM). A run reads memory (`load_base_model`,
  `fused_observation_ids`), writes it (`merge`, `publish`) and never overwrites it.
- **#1 Prompt chaining** — fixed step sequence: acquire → overhead perception → street-level
  perception → project → merge → critique → publish.
- **#2 Routing (static)** — observation kind selects the prompt/schema: `satellite_*` →
  `perceive_overhead`, `streetview|user_photo` → `perceive_streetlevel`.
- **#5 Tool use (deterministic, system-side)** — the rectifier, the projector, the renderer and the
  merger are the tools between model turns; the model never calls tools itself (the forced/auto tool
  in the structured-output modes is a formatting mechanism, not agency).
- **#4 Reflection** — one bounded critique pass over a top-down render of the **model** (not the old
  scene) beside the satellite crops. Output ops are applied as low-weight `critique` evidence.
- **#12 Exception handling / recovery** — per-frame and per-batch failures degrade (frame skipped, batch
  skipped, warning, `degraded: true` in the report); Overpass down → OSM priors absent; Nominatim/Overpass
  both down → `straight_chord` reference line, `placement_verified=false`, hard warning; no Street View →
  overhead-only model (L0 + hires overhead); budget hit → labelled `failed_budget`, **partial merges are
  not published**; nothing new → explicit no-op.
- **#16 Resource-aware optimisation** — images content-addressed and deduped by `(pano_id, heading)`
  before paying; fused observations never re-sent; ≤ 8 frames per call; per-run image cap
  (`HERO_MAX_IMAGES_PER_RUN`), per-run Claude cap (`HERO_RUN_BUDGET_USD`), global hard stop
  (`HERO_CLAUDE_BUDGET_USD`); the batch planner estimates cost before sending and drops the
  lowest-priority batches (far-from-centre stations first) with a report line instead of failing.
- **#18 Guardrails** — every model output validated against a Pydantic schema; one re-ask quoting the
  error; coordinates clamped to the window; ranges > 30 m rejected; critique ops bounded
  (±3 m nudges, ±0.15 confidence); **no code path deletes an entity**; contradictions are stored, never
  silently resolved; low confidence is shown, not hidden.
- **#19 Evaluation and monitoring** — `llm_calls` + `llm.call`/`model.merge`/`model.published` logs;
  extraction replay (`ReplayLLM`) makes merge/fidelity/compile regression-testable with zero spend;
  the run report is the per-run evaluation artefact (images, new/changed/removed, fidelity before→after, cost).
- **#14 Knowledge retrieval (light)** — OSM (road geometry, lanes/width tags, sidewalks, building
  footprints/heights) is retrieved context that seeds the prior and constrains perception.
- **#13 Human-in-the-loop** — Phase 2: uploaded photos are first-class observations (API real, UI stub);
  Phase 3: admin correction as `human` evidence.

## Principles of the merge (the heart of "cumulative")

1. **Evidence, not opinions.** Every claim on an entity/strip/building points at observation ids
   (`provenance`). A claim with no provenance can only be a `prior` or `legacy`.
2. **Support and absence are asymmetric.** A supporting detection adds `w·0.75^k·logit(p)`; a *visible*
   absence subtracts only `0.5·v`. Entities fade on repeated contradiction (`removed` at confidence < 0.15
   with ≥ 2 contradicting observations) but stay in the snapshot and can be revived under the same id.
3. **Contradictions stay visible** (`type_conflict`, `presence_conflict`, `position_conflict`,
   `attribute_conflict`, `footpath_state_conflict`) on the admin page and in the run report.
4. **Self-consistency checks** (`consistency_check`, deterministic, after the merge, before the critique):
   a. every entity `(s,t)` inside `[0,length_s] × [−30,30]`; b. no two `present` entities of the same type
   closer than their gate (else merge them or flag); c. profile intervals tile `[0,length_s]` and strips don't overlap;
   d. a `parked_vehicle`/`bin`/`vendor_stall` whose extent overlaps the modelled footpath strip must be consistent with an
   `encroached|blocked|broken` state there (else flag a `footpath_state_conflict` and lower the state's vote);
   e. range disagreement (ground-plane vs model estimate) flagged on the evidence; f. frames reported
   `visibility_ok=false` contribute no absence evidence. Failures are warnings + contradictions, never aborts.
5. **Redundancy is the cross-check.** Dense stations and four headings mean the same object appears in 2–4 frames;
   independent supports raise confidence, and a one-frame-only detection stays visibly low-confidence.
6. **Calibrated, bounded confidence** (`calibrate_confidence`, `data.md`) — raw model confidences are shrunk
   before use; the critique may move confidence by ≤ 0.15 total.

## State

`hero.fusion.state.FusionState` (dataclass; serialisable pieces persisted on the run row after each step):

```
run_id, segment (SegmentRecord), mode: "generate"|"acquire", settings snapshot (models, budgets, plan, structured_mode)
ref: ReferenceLine; ref_model: ReferenceLineModel
base: CanonicalModel            # v0 prior|legacy, or head fused; "fresh lineage" flag when the base is legacy
osm: OsmGeometry | None; osm_context: OsmContext | None
plan: ImageryPlan; planned: list[PlannedImage]; plan_info
acquired: AcquireResult | None
observations: dict[str, Observation]       # every observation of the segment (id → record)
unfused: list[Observation]                 # not in base.fused_observation_ids
overhead_windows: list[OverheadWindow]; street_batches: list[StreetBatch]
extractions: list[ExtractionRecord]        # persisted as observation_extractions
projected: list[ProjectedBatch]
candidate: CanonicalModel | None; merge_log: list[ChangeRecord]
fidelity_before/after: FidelityMap; diff: ModelDiff | None
critique_iterations: list[CritiqueIteration]
scene: SceneV2 | None; version: int | None; report: RunReport | None
steps: list[StepRecord]; cost: CostLedger; warnings: list[str]; degraded: bool; error: PipelineError | None
```

## Steps (nodes), in order

| # | step | does | LLM | on failure |
|---|---|---|---|---|
| 1 | `load_base_model` | head version (or lazily import the Phase-1 scene as v0 `legacy`; legacy ⇒ fresh lineage), load all observations | no | fail run |
| 2 | `resolve_reference_line` | inherit the parent's reference line; else `fetch_road_polyline` → `clip_road` (or `straight_chord`); store the v0 `prior` model if the segment has no versions | no | polyline unavailable → `straight_chord` + hard warning (`placement_verified=false`); never fails |
| 3 | `fetch_osm_context` | Overpass bbox (existing `fetch_osm`) → `OsmContext` (buildings projected to `(s,t)`, highway tags, sidewalks) | no | recoverable: `osm=None`, warning |
| 4 | `plan_imagery` | `merge_plan(default, body overrides)` → `plan_images` → mark planned images already stored as cached | no | invalid plan → 422 before the run starts |
| 5 | `acquire_imagery` | metadata (free) → dedupe → image fetch → `upsert_observation`; stops at `max_images` NEW images | no | per-frame failures → `skipped[]`; overview tile failing = run fails (`upstream_error`) unless an overview observation already exists. **mode=acquire ends here** with a report |
| 6 | `select_unfused` | `unfused = observations − base.fused_observation_ids` (overview counts as an overhead observation until fused); `replayable` = the unfused ones whose extraction is already stored in `observation_extractions` (an earlier run paid for it but ended before `publish`) — they skip 8–9 and re-enter at step 10 | no | **both empty ⇒ `fusion.noop`: skip 7–14, write the no-op report, run `succeeded`** |
| 7 | `prepare_views` | rectify overhead windows (50 m of `s` × `t∈[−14,14]` at 0.1 m/px, with s/t ticks), build street batches (contiguous station windows, ≤ `HERO_PERCEPTION_BATCH_IMAGES`, frame geometry in each caption), estimate cost, trim to the run cap | no | budget plan trims batches; none left ⇒ fail `failed_budget` |
| 8 | `perceive_overhead` | **one** call, ≤ 4 windows → `OverheadExtraction`; project polylines/runs/objects to `(s,t)` | A (vision) | invalid twice → skip overhead, warning, profile stays prior (L0) |
| 9 | `perceive_streetlevel` | `ceil(n/8)` calls → `StreetExtraction`; sequential, resumable per batch | B (vision) | a batch invalid twice or failing upstream → skipped, its observations stay **unfused** (retried next run), warning |
| 10 | `project_detections` | `project_street_detection` per detection; `street_to_batch`/`overhead_to_batch` → `ProjectedBatch` (+ `visible_regions`) | no | detection that can't be projected is dropped and counted |
| 11 | `merge` | `merge_batches(base, projected, …)` → candidate model, `ChangeRecord`s, contradictions | no | exception ⇒ fail run, nothing published |
| 12 | `consistency_check` | rules 4a–f above | no | warnings only |
| 13 | `critique_N` | render the candidate (`hero.fusion.render.render_model_topdown`: strips, entities, reference line over the best hi-res crop/overview) → **one** call with the render + crops + ≤ 2 street frames → `CritiqueResult`; `apply_critique` | R (reflection) | LLM error ⇒ stop loop, keep candidate, warning; `accepted=true` or N = max ⇒ stop |
| 14 | `publish` | `compute_fidelity` before/after, `diff_models`, cut crops (`assets`), `compile_scene`, `publish_version` (one transaction), thumbnail | no | any exception ⇒ run `failed`, segment unchanged |
| 15 | `report` | `build_run_report` → `run_reports`; `pipeline_runs` finalised | no | report failure is logged, run still `succeeded` |

Budget guard wraps every LLM step exactly as in Phase 1 (`check_before_call` before each request,
`record` after); the per-run cap is `HERO_RUN_BUDGET_USD` (default **1.00**). The batch planner in
step 7 prevents the typical failure: it sums the per-batch estimates and, if the total would exceed
80 % of the remaining run cap, drops batches farthest from the segment centre first and records
`budget_trimmed: n batches` in the report. A hard `BudgetExceeded` mid-run ends `failed_budget`
**without publishing** (observations acquired so far remain, extractions already paid for remain in
`observation_extractions` and are re-used on the next run: `select_unfused` skips observations whose
extraction is stored but replays them through `project_detections`/`merge` instead of calling the model).

## Edges

Linear 1→2→3→4→5→6→7→8→9→10→11→12→(13)→14→15. Conditional edges: 5 ends the run in `acquire` mode;
6 jumps to 15 (no-op); 8 is skipped when no unfused overhead observation exists; 9 is skipped when no
unfused street-level observation exists; 13 loops at most `HERO_MAX_REVIEW_ITERATIONS` times and exits
on `accepted=true`.

## Structured-output modes and the model-slug investigation

`vision_json` supports `forced_tool`, `auto_tool`, `json_text` (`spec/architecture.md` → Stack → LLM).
In `auto` mode a slug that rejects forced `tool_choice` (HTTP 400, e.g. `claude-sonnet-5-5`) is retried
once in `auto_tool`; if the answer has no `tool_use` block, in `json_text` (the reply is parsed from the first
JSON object, fenced blocks tolerated, then schema-validated; one repair re-ask quoting the error). The workspace
header is added by the client constructor, so it is on every request in every mode. The orchestrator's
P0 spike (`check_structured`) decides the default slug (`roadmap.md` → Phase 2 → P0). Mode and slug are
logged on each `llm.call` (`structured_mode`) and stored in `run.inputs`.

## Error handler

Every step runs inside `fusion.runner.run_step(state, ctx, Step(name, fn, recoverable, when))` — the same
contract as Phase 1: record `started`, time it, catch `UpstreamError | SchemaValidationError | BudgetExceeded |
Exception`, write the `StepRecord`, log `pipeline.step`, continue if `recoverable`, else set `state.error` and
jump to `fail(state)`, which persists partial outputs (observation ids, extractions, warnings, the partial report
with `degraded: true`). A failed run never replaces a published scene; **if the segment has no published scene yet,
`fail` publishes the v0 prior scene** (`compile_scene` on the prior, `degraded: true`, `quality=generic`) so a
generation that produced nothing still yields a descriptive, clearly-labelled low-fidelity street, never a blank.

## Finalize

`publish_version` writes `model_versions` (+`scene_json`, `fidelity_json`, `diff_json`, `summary`), updates
`segments.scene_json/status/quality/latest_run_id/thumbnail_path` in the same transaction; then
`pipeline_runs.{imagery_json, iterations_json, steps_json, cost_usd, tokens, google_*, status, finished_at}`
and `run_reports`.

## Concurrency

One worker thread (`registry.start_run`), single-flight, as Phase 1: a second generate/imagery request while a
run is `queued|running` → 409. LLM calls and Google fetches are sequential by design (Street View metadata is
free; ≤ 60 image fetches ≈ 1–2 minutes). Perception batches are independent but run sequentially to keep the
cost cap exact.

## Assembly (pseudocode)

```python
def run_fusion(run_id: str, *, llm=None, google=None) -> PipelineRunRecord:
    state = load_state(run_id)                     # mode, plan overrides from run.inputs
    ctx = build_context(run_id, llm=llm, google=google)   # BudgetGuard, AnthropicClient(workspace header), GoogleImagery, caches
    graph = [
        Step("load_base_model",        load_base_model,        recoverable=False),
        Step("resolve_reference_line", resolve_reference_line, recoverable=True),   # falls back to straight_chord itself
        Step("fetch_osm_context",      fetch_osm_context,      recoverable=True),
        Step("plan_imagery",           plan_imagery,           recoverable=False),
        Step("acquire_imagery",        acquire_imagery,        recoverable=False),
    ]
    for step in graph:
        if not run_step(state, ctx, step): return fail(state)
    if state.mode == "acquire": return finish_report(state, ctx)
    if not run_step(state, ctx, Step("select_unfused", select_unfused, recoverable=False)): return fail(state)
    if not state.unfused and not state.replayable: return finish_noop(state, ctx)
    for step in [
        Step("prepare_views",        prepare_views,        recoverable=False),
        Step("perceive_overhead",    perceive_overhead,    recoverable=True,  when=lambda s: s.has_overhead),
        Step("perceive_streetlevel", perceive_streetlevel, recoverable=True,  when=lambda s: s.has_street),
        Step("project_detections",   project_detections,   recoverable=False),
        Step("merge",                merge,                recoverable=False),
        Step("consistency_check",    consistency_check,    recoverable=True),
    ]:
        if not run_step(state, ctx, step): return fail(state)
    for n in range(1, settings.max_review_iterations + 1):
        before = len(state.critique_iterations)
        if not run_step(state, ctx, Step(f"critique_{n}", critique_once(n), recoverable=True)): return fail(state)
        if len(state.critique_iterations) == before or state.critique_iterations[-1].accepted: break
    for step in [Step("publish", publish, recoverable=False), Step("report", report, recoverable=True)]:
        if not run_step(state, ctx, step): return fail(state)
    return succeed(state, ctx)
```

## Prompts (files in `src/hero/prompts/`, loaded with `render_prompt`, `{{placeholders}}`)

- `perceive_overhead.md` (`<node:overhead>`) — role: civic infrastructure surveyor reading straightened
  top-down windows; states the axes (up = travel, left = left), the metres-per-pixel and the drawn ticks;
  asks for strip edges as pixel polylines per strip kind and side, `footpath_runs` that tile the visible rows,
  overhead objects, and a **rich description** for every item (material, colour, condition, what a pedestrian
  experiences); "prefer `unknown` to guessing"; output = `OverheadExtraction`.
- `perceive_streetlevel.md` (`<node:streetlevel>`) — inputs: ≤ 8 frames, each captioned with observation id
  (never shown to the model as an id to echo; `image_index` only), view (`forward|back|left|right`), station,
  camera heading relative to travel and "the modelled footpath is on the {{footpath_side}} of travel";
  asks for one detection per physical object per frame (`touches_ground`, `est_distance_m`, size, orientation,
  attributes, ≥ 1 sentence `description`), facade spans, footpath state per frame, and `visibility_ok=false`
  when the view is blocked (bus, glare) — "do not list what you cannot see; do not invent"; output = `StreetExtraction`.
- `critique_model.md` (`<node:critique>`) — inputs: model render, hi-res crops, ≤ 2 street frames, a compact
  entity/strip summary with ids; "only flag what the imagery contradicts; ≤ 8 ops; accept when no material
  discrepancy"; output = `CritiqueResult`.

Each prompt carries a node tag in its first line so `ReplayLLM` (tests only) branches on tags, never prose.

## Confidence calibration

`calibrate_confidence` is a fixed monotone shrink (`data.md`). Its quality is tracked, not assumed: the
`tests/live` gate writes, per entity type, the mean raw vs merged confidence and the share of entities with
≥ 2 supports into `docs/evidence/phase2-gate.json`; recalibrate (one constant table) once labelled corrections
from Phase 3 exist.

## Legacy (Phase 1) graph

`resolve_geometry → fetch_satellite → verify_streetview → fetch_streetview → detect_layout → detect_obstacles →
assemble_scene → review_iteration_N → finalize` (`hero.pipeline.*`, prompts `detect_layout.md`, `detect_obstacles.md`,
`review_scene.md`) is unchanged, unit-tested, and **not reachable from the API** after Phase 2. Its patch-op contract
(`move|remove|add|relabel|set_coverage`) is superseded by `CritiqueOp` for the model; Phase 3 admin corrections use
model edits. Scheduled for deletion in Phase 3.
