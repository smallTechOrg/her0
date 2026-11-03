# Capability: Model Inspection *(Phase 2)*

## What It Does
Lets the admin see what the street model knows, how well, and from which images: version history, a fidelity/coverage map along the street
(with "what image would improve this"), an entity list with click-through evidence, version-to-version diffs, the run report for each run,
the imagery inventory, and a way to fetch more imagery and generate again.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Admin password | header | admin UI | yes |
| Segment slug, version numbers, filters | query | admin UI | yes |
| Model versions, observations, run reports | rows | DB via `/api/admin/…` (`spec/api.md`) | yes |
| Imagery-plan form values | JSON | admin UI | for "Add more imagery" |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Segment page: header badge, version history, coverage map, entities + evidence drawer, diff, imagery grid, add-imagery form, reference-line view | screen | admin |
| Run page additions: mode chip, "Model v1 → v2", run report, images used (new/cached) | screen | admin |
| New acquire-only / generate runs | requests | `imagery-acquisition`, `model-fusion` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Admin API (`/api/admin/segments/{slug}/model*`, `/observations`, `/imagery`, `/generate`, `/runs/{id}/report`) | read + start runs | inline error with retry; never a blank panel |

## Business Rules
- Read-only except the two run-starting actions; both show the same 409/402 reasons as Generate and disable themselves while a run is active.
- Everything shown is traceable: an entity row opens its evidence (the images with the detection box), its confidence history across versions, and its contradictions.
- The coverage map is derived data from `FidelityMap` (one cell per 5 m, `data-level`), never recomputed in the browser; clicking a cell shows the next-level hint and the photos covering it; clicking can prefill the `s` range of the add-imagery form.
- Legacy and map-only (L0 prior) models are labelled as such everywhere; a `removed` entity is shown struck-through with its reason, not hidden.
- Upload UI is a labelled stub in Phase 2 (`data-stub="true"`).

## Success Criteria
- [ ] `GET …/model` for a fixture segment with versions 0–2 returns `versions` of length 3 in ascending order with `fidelity_level`, `summary`, a head `fidelity` whose `bins` tile `[0, length_s]`, and a `reference_line.points` list of ≤ 60 points; for a legacy segment the first call imports v0 and a second call returns the same version list (no duplicate import).
- [ ] `…/model/diff?from=1&to=2` equals the stored `diff_json` of version 2; `…/entities/{id}` returns evidence items each with an `observation` whose `url` returns 200 under `/media/`.
- [ ] Playwright (390×844, fixture DB seeded with two versions through the real merge engine and recorded extractions — no LLM): the segment page shows `[data-testid=versions]` with 2 rows, `[data-testid=coverage]` with `ceil(length/5)` cells each carrying a `data-level`, clicking an L1 cell shows a hint text and ≥ 1 thumbnail; the entities table lists ≥ 5 rows; opening one shows ≥ 1 evidence thumbnail and a description; the diff panel for v1→v2 lists at least one added entity and an "L1 → L2" fidelity line; the add-imagery form changes its "≈ N new images" estimate when a heading is unchecked.
- [ ] Playwright: the run page for the fixture generate run shows the run report summary text, an Images table with `new` and `cached` numbers equal to the report JSON, and the "Model v1 → v2" link to the segment page; for a no-op run it shows "No change — model stays v2".
- [ ] "Fetch images only" POSTs `/imagery` with the form's plan (asserted on the request body via route interception) and navigates to the run page; the Upload control is disabled, captioned "Coming in Phase 3".
- [ ] A wrong password on any of these endpoints returns 401 `unauthorized` (integration test).
