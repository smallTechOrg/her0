# Capability: Per-run model choice
## What It Does
Lets the admin pick, per run, which configured Claude model performs the vision and review passes (default `HERO_VISION_MODEL`, currently `claude-sonnet-5-5`; `claude-opus-5-5` offered once verified and priced), and records the choice everywhere cost is accounted.
## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| `model` | model id | admin Generate / Fetch + Generate / Fill gaps dropdown | no (default) |
| `src/hero/llm/models.json`, `prices.json` | config | repo | yes |
| verification evidence | JSON | `docs/evidence/phase2_1/model_check_claude-opus-5-5.json` | for enabling a non-default option |
## Outputs
| Output | Type | Destination |
|---|---|---|
| option list with enabled/disabled reason + prices | JSON | `GET /api/admin/models` → dropdown |
| `run.inputs.models`, `RunReport.model`, `llm_calls.model`, `model.chosen` log | records | run page, report, logs |
| cost estimate priced with the model | USD | fill-gaps preview, run cap guard |
## External Calls
| System | Operation | On Failure |
|---|---|---|
| Anthropic (workspace header kept) | **one** tiny real structured-output call via `check_structured --models claude-opus-5-5 --modes auto_tool --max-calls 1` (once, by the SM slice) | option stays listed but `enabled:false` with the error as `disabled_reason`; no retry loops; default untouched |
| Anthropic pricing docs / `claude-api` skill | look up the real per-token price (no guessing) | no price row ⇒ option not listed |
## Business Rules
- The default is `settings.vision_model`; the API and UI never change it silently; the UI never persists a non-default choice.
- A request for an unknown/disabled/unpriced model is rejected with 422 before any spend; absent `model` ⇒ default.
- One choice applies to both vision and review nodes of that run (> **Assumed**).
- `prices.json` rows added in Phase 2.1 carry `source_url` and `verified_on`; the budget guard (per-run and global) prices each call with the model actually used.
## Success Criteria
- [ ] The verification evidence file exists with `ok` true/false, the model, structured mode, tokens and USD from the app's own cost record; exactly one call was made (counter asserted by the tool).
- [ ] `GET /api/admin/models`: default selected and equal to `HERO_VISION_MODEL`; `claude-opus-5-5` present, `enabled` equals the evidence's `ok`, with `price_source_url`.
- [ ] A fake-LLM run with `model=claude-opus-5-5` records that id on `run.inputs.models`, `RunReport.model` and every `llm_calls` row, and its budget guard blocks a call that exceeds the cap at *that* model's price (two prices tested).
- [ ] 422 for an unknown id; 422 with the reason for a disabled id; omitted ⇒ default.
- [ ] Phone/desktop e2e: the dropdown renders next to each spend button with the default preselected and a disabled option shows its reason.
