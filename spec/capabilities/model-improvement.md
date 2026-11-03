# Capability: Model improvement (add to an existing model)
## What It Does
Lets the admin see where a generated street model is weakest and raise it with one click: a priced "fill gaps" plan, manual extra imagery, real photo uploads placed on the street, and a free rebuild from stored data.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| segment slug | string | admin segment page | yes |
| fill-gaps options | `{target_level, max_images, max_usd, s_range}` | admin form | no (defaults) |
| photo files + placement | jpeg/png, `s`, `side`, `facing`/`heading_deg`, `captured_at` | admin upload form (real file input, map click) | for uploads |
| regenerate mode | `compile` \| `remerge` | admin card | no (`compile`) |
| model | model id | dropdown next to Fetch + Generate / Fill gaps → Fetch + Generate (`model-choice.md`) | no (default `HERO_VISION_MODEL`) |
## Outputs
| Output | Type | Destination |
|---|---|---|
| improve plan (weakest intervals, hints, action availability, how-to steps) | JSON | `GET …/improve-plan` → panel |
| fill-gaps preview (images, cached vs new, USD estimate) | JSON | `POST …/fill-gaps/preview` → inline table |
| new observations (`user_photo`) / acquired images | rows | `observations` |
| recompiled scene v3 (+ gap report) and rebuilt export bundle (glb, manifest, xodr) | derived columns + files | `model_versions`, `segments`, `data/exports/` |
| new model version (after Generate or `remerge` with changes) | row | `model_versions` |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| Google Static Maps / Street View | only on Fill gaps / Add imagery start (never on preview, upload, regenerate) | per-image skip, run report warning |
| Anthropic | only on Generate after new unfused observations (existing fusion) | existing fusion error handling; never on regenerate |
## Business Rules
- Preview and regenerate never spend; every spend button shows an estimate first and is disabled while a run is active or the estimate exceeds the remaining budget.
- An upload is evidence only after Generate fuses it; until then it is shown as "uploaded · not yet fused".
- One photo facing a side on a bin that is already L2 on both sides lifts it to L3; the upload response says what the photo can reach.
- Regenerate with what we have: `cost_usd == 0.0`, 0 Google requests, 0 Anthropic calls, always; missing extractions are listed, not fetched.
- The panel always shows the same five how-to steps as the README.
## Success Criteria
- [ ] `GET …/improve-plan` on a model with an L1 stretch returns that stretch in `weakest` with a non-empty `hint`.
- [ ] `fill-gaps/preview` returns ≥ 1 planned image with `est.total_usd > 0` and makes zero HTTP calls (guarded fakes).
- [ ] Uploading a jpeg with `s=42, side=left, facing=left, captured_at=2024-02` creates an `observations` row with `source=upload`, `view=left`, `station_s≈42`, `capture_date="2024-02"`.
- [ ] `POST …/regenerate` returns `cost_usd: 0.0` and the segment's scene `schema_version` is 3 afterwards.
- [ ] Phone-viewport e2e: the panel, the file input, the map click filling `s`, the plan preview with a cost estimate and no `data-testid="upload-stub"`.
