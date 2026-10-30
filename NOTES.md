# Build journal (harness-improvement log)

Run start: 2026-10-29 ~19:00 local. Platform: Claude Code (VS Code extension), bypass permissions.

- **Intake**: AskUserQuestion worked for all 7 rounds. Free-text answers in "Other" carried key scope decisions (admin-only generation, no mid-run tagging) — the intake should expect that and read Other text carefully.
- **Key check**: Anthropic org-level key rejected with 400 "not scoped to a workspace" → added `HERO_ANTHROPIC_WORKSPACE_ID` and the `anthropic-workspace-id` header. Lesson: the key-validation script should detect this error class and ask for the workspace ID instead of a new key.
- **Session restart** mid-intake (user closed VS Code) was harmless because nothing was in flight; cwd resets to $HOME per Bash call — always use absolute paths.

## Phase 1 build (2026-10-29)

- **Shared test DB clobbered by parallel generators.** Several code-generator slices ran
  `pytest` concurrently; every session's `tests/conftest.py` unlinks and recreates the same
  `data/hero_test.db`, so sibling sessions saw `no such table` / `disk I/O error` mid-run and
  reported spurious failures. Lesson: `conftest.py` should derive the test DB name from a
  worker/session id (e.g. `hero_test_<pid>.db`) or the orchestrator must set a per-slice
  `HERO_TEST_DATABASE_URL`; otherwise force serial test runs (one gate at a time).
- **`.env` drifted from `.env.example` after the spec landed.** `HERO_ADMIN_PASSWORD` was
  introduced by the spec after intake; `.env` lacked it, and the scaffold generator appended
  a generated value itself. Lesson: intake should re-check `.env` against `.env.example`
  once the spec is written, or the scaffold gate should report newly required vars to the
  user instead of inventing values.
- **Spec example data was internally inconsistent.** The hand-written example scene had
  lat/lon values that did not agree with the local-frame x/y numbers; only a unit test
  (`tests/unit/test_geo.py`) caught it. Lesson: spec-writer should compute worked examples
  with code (a small script) rather than by hand.
- **overpass-api.de is flaky** (406 / 502 / timeouts within one afternoon). The pipeline
  degrades to no-OSM and records the error; the live imagery test skips on Overpass failure
  rather than failing. Keep that degrade path, and consider a mirror URL fallback.
- **Model slug has a known expiry.** The Anthropic SDK emits a deprecation warning for
  `claude-sonnet-4-5` (EOL 2026-11-30). The slug validated at intake, but the harness should
  record the EOL date next to the validated model name so the user is warned before it breaks.
- **Task briefs drifted from the spec.** One brief said the scene is served on
  `/api/segments/{slug}` while `spec/api.md` says `/scene`; generators correctly followed the
  spec. Lesson: orchestrator briefs should point at spec sections (file + heading) instead
  of restating contracts, so there is only one source of truth.
- **Rate-limit death mid-phase, then resume.** The first project-builder run died on an API
  rate limit after all five slices landed but before commit, leaving everything uncommitted
  and the fix-bundle status unknown. Resume worked by trusting disk state (git status, test
  run) instead of the dead run's last message. Lesson: commit + push after each VERIFIED
  slice (WIP commits on the feature branch), so a dead orchestrator loses nothing.
- **Gate output was not persisted, so a flake could not be diagnosed.** The e2e
  `test_generated_street_appears_in_the_gallery_and_plays_to_a_crash_with_a_results_card`
  failed once on a float-vs-int format mismatch (`100%` vs `100.0%`, fixed in the test), failed
  again once with an unseen message, then passed. Each re-run costs a real generation. Lesson:
  always tee gate output to a file on the first run, and have `test_db` keep the DB (or dump
  spend) on failure; the test DB is deleted at session end so per-session spend is unreadable.
- **A newer model slug validated on a plain call but broke the pipeline.** `claude-sonnet-5-5`
  returns 200 for `messages.create` yet 400s on forced `tool_choice` ("type tool and any are not
  supported for this model"). Lesson: slug validation must exercise the same call shape the app
  uses (forced tool-use + image), not a bare "hi". Default stays `claude-sonnet-4-5`; README
  documents the 2026-11-30 EOL.
- **Org-level key needs the workspace header in every ad-hoc script**, not just the app client.

## Phase 1 playtest feedback (2026-10-30)

- **Instant game-over on first hit felt broken.** On the real street the first hazard (a low branch over the footpath) ended the run seconds after start, so the player saw nothing of the street. Fix: 100 health, 20 per hit, 1.2 s invulnerability, one hit per object per loop. Lesson: the intake "reliability/difficulty" questions should ask how forgiving collisions are; for a civic-awareness game the street must be seen, so default to health, not one-hit death. Specs that mandate "run ends on first crash" should be questioned when the product goal is exploration.
- Existing e2e tests encoded the instant-crash rule and had to be rewritten; keep game-rule constants in the spec so tests derive from them.

## Address-based segment creation (2026-10-30)

- **Intake missed that admins don't know lat/lon.** The Phase 1 create form asked for four coordinates. Lesson: for any "location" input, the intake/spec should ask "how will the person enter this?" and default to address/place search with candidate confirmation, never raw coordinates.
- **Geocoders disagree.** Google resolved "100 Feet Road, Bengaluru" to Koramangala, Nominatim to Indiranagar. Showing candidates plus a "check on map" link beats picking one silently.
- **Overpass was down again (3 mirrors timed out).** Nominatim `reverse` with `polygon_geojson=1` returns the road's LineString and is a far more dependable source for road direction; Overpass is now only a fallback with a short timeout. Lesson: don't make a user-facing step depend on Overpass.
- **Sequential vs parallel:** Nominatim's policy is ~1 req/s, so candidate lookups are sequential and cached.

## P0 structured-output spike (2026-10-30)

- `check_keys`: PASS. `check_structured` on the real `StreetExtraction` schema: `claude-sonnet-4-5` ok in forced_tool/auto_tool/json_text; `claude-sonnet-5-5` FAILS forced_tool (HTTP 400), ok in `auto_tool` ($0.010866) and `json_text` ($0.009936). Total spend $0.044103.
- Decision: `DEFAULT_VISION_MODEL` and `DEFAULT_REVIEW_MODEL` = `claude-sonnet-5-5` (use `auto_tool`/`json_text`; `HERO_STRUCTURED_MODE=auto` falls back). `claude-sonnet-4-5` EOL is 2026-11-30.
