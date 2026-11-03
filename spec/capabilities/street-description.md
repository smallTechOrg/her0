# Capability: Street Description *(Phase 2)*

## What It Does
Makes the street explain itself in plain language everywhere: every strip, entity and building carries a short description and
an evidence line, every run produces a human-readable report, and players see an "About this street" panel and a results line stating
the fidelity level, how many photos built it, when it was updated and which parts are least certain. A weak or map-only street is
shown as a clearly-labelled low-fidelity sketch, never a silent blank.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Canonical model + fidelity + diff | documents | `street-model` | yes |
| Per-item `description` text from perception | strings | `model-fusion` | no (templates fill gaps) |
| Run facts (images, costs, changes) | structs | `model-fusion` | for reports |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `scene.about` (summary, fidelity label + histogram, photo counts, capture dates, last updated, low-confidence parts, improve hint, flags) | block | Scene v2 → game |
| `objects[].description`, `.evidence`, `buildings[].description` | strings | Scene v2 → game results list, admin |
| `version_summary` paragraph | string | `model_versions.summary`, version history |
| `RunReport.summary` + `suggestions` | strings | `run_reports`, run page |
| Fallback scene for a segment with no usable evidence (L0 prior) | Scene v2, `degraded: true` | `segments.scene_json` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| none — deterministic templates in `hero.model.describe` / `hero.model.report` (item descriptions come from the perception call that already ran) | — | — |

## Business Rules
- All summary text is **template-generated from structured data** (no extra LLM call): same input ⇒ same sentence; numbers in the text always equal the numbers in the structured fields.
- Required phrases in `about.summary`: `fidelity L{n}`, the fidelity label, `{N} photos`, `model v{version}`, the street name and its length; plus the `low_confidence_parts` list when any, and "legacy, placement unverified" for legacy models / "map-only sketch" for L0 priors.
- A low-confidence part = a 5 m-aligned span (merged when adjacent) where fidelity is L0/L1, or any entity with confidence < 0.6, or an open contradiction.
- Description fallbacks (when perception gave none): strips and intervals from `describe.strip_text/interval_text` (e.g. "Paver footpath about 1.5 m wide, broken from 22 to 31 m"); entities from catalogue label + attributes.
- Player copy is plain language: no ids, no jargon beyond "fidelity" (explained in the panel legend).
- The default (fallback) scene is playable and labelled: "Map-only sketch: no street photos yet" in the About panel, gallery chip and results line.

## Success Criteria
- [ ] `about_summary` for the fixture model v2 contains `fidelity L2`, `48 photos`, `model v2`, the street name; for the legacy v0 import it contains "legacy, placement unverified"; for the prior it contains "map-only sketch"; two calls return identical strings.
- [ ] Numbers in `about` match the data: `photos.total == street_view + satellite + user == count of fused observations`; `fidelity.histogram_m` sums to `length_m ± 0.01`.
- [ ] Every object in a compiled fixture scene has a non-empty `description` and an `evidence.summary`; every `profile` interval description is non-empty (fallback templates exercised by a model with empty perception descriptions).
- [ ] Publishing a failed first generation (all perception batches invalid) yields a v2 scene with `about.degraded == true`, `generation.quality == "generic"`, status `ready`, and the About panel text "Map-only sketch".
- [ ] Playwright (390×844, curved fixture scene): the Ready overlay shows the model badge; clicking `[data-testid=about-open]` opens `[data-testid=about-panel]` containing "fidelity L2", "photos", "Least certain" and the improve hint; the panel closes with Esc; after a crash the results card shows the model line and an About button.
- [ ] A v1 scene (legacy) opens the About panel with the first-generation notice and no console errors.
