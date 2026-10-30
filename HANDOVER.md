# #hero: handover (end of Phase 2.1, 2026-10-31)

Mobile-browser endless runner for smallTech's "#" line. The course is a real 50–100 m street reconstructed from Google satellite tiles, Street View and OpenStreetMap. Street generation is admin-only and uses Claude vision. Claude budget is $50 (global hard stop).

Branch `feature/hero-20261029-1915-v0.1`, PR #1 against `main`. Last commit `419dc31`.

## Where we are
- **Phase 1:** playable real street, health system (100 hp, 10 per hit, 1.5 s invulnerability, 3 s start grace), address-based segment creation (50–100 m either side).
- **Phase 2:** canonical street model (OpenDRIVE-style refline in an (s, t) frame, typed entities with stable IDs, observations as records, fidelity L0–L3 per 5 m bin, merge/diff/versions, run reports).
- **Phase 2.1:** deterministic Python mesh builder producing `street.glb` + manifest, scene schema v3, satellite-textured footpath/kerb/road, continuous frontage, gap filling, dynamic obstacles (cars etc. are not baked in; a seeded spawner places them per run), "How to improve this model" admin panel, photo upload, model dropdown (Sonnet 5.5 default, Opus 5.5 option), Max quality preset, per-run cost cap removed.
- **Latest user verdict:** better, but **not realistic enough**. That is the main goal for next work.

## Not in git: you must recreate
`data/` is gitignored (157 MB: `hero.db`, `imagery/`, `crops/`, `previews/`, `exports/`). `.env` and `frontend/vendor/` are also ignored.
- Local machine holds the real data. For a cloud session either copy `data/` over, or regenerate (costs Claude and Google credits).
- `uv run python -m hero.tools.vendor_three` restores `frontend/vendor/`.
- Env var names are in `.env.example`. Needed: `HERO_ANTHROPIC_API_KEY`, `HERO_ANTHROPIC_WORKSPACE_ID` (without it the API returns 400 "not scoped to a workspace"), `HERO_GOOGLE_MAPS_API_KEY`, `HERO_ADMIN_PASSWORD`, `HERO_CLAUDE_BUDGET_USD=50`. Never commit keys.
- Cloud session cannot test on the user's phone or local server. The user tests locally.

## Run it
```
uv sync
uv run alembic upgrade head          # head = 0003
uv run python -m hero.tools.recompile --all   # $0; rebuilds scene v3 + glb from stored models
uv run hero-serve                    # http://localhost:8711 (play at /play, admin at /admin)
uv run pytest tests/unit tests/integration    # 1838 passed, 7 skipped at 419dc31
uv run ruff check src tests
```
`live` pytest marker (real API calls) is excluded by default. Playwright e2e was not fully re-run after the last Python fixes.

## Segments in the local db
- `bellandur-main-road-n-2`: the real generated model (model v1, scene v3 + glb). Use this for realism work.
- `100-feet-road-indiranagar-canonical-demo-e`: demo street, not generated from real imagery.
- `bellandur-main-road-n`, `100-feet-road-indiranagar-w`: legacy Phase 1 streets, play as stored, off-road or poor. Candidates for deletion.

## Key gotchas
- Anthropic structured output works on Sonnet/Opus 5.5 via `auto_tool` or `json_text`, not forced `tool_choice`. `claude-sonnet-4-5` retires 2026-11-30.
- `src/hero/llm/prices.json`: the last builder changed Sonnet 5.5 from $3/$15 to $2/$10 per Mtok (Opus 5.5 is $4/$20). **Unverified; check against the pricing page.** It only affects cost estimates and the budget counter.
- Geocoding: Nominatim (about 1 request/s, cached) is primary for road heading; Overpass is flaky and kept as a short-timeout fallback.
- Cache busting: local JS/CSS use `?v=3`; a middleware sets `no-cache` on non-API paths. Hard-refresh after frontend changes.
- Test data dir is now isolated in `tests/conftest.py` (session temp `HERO_DATA_DIR`). Earlier test runs left stray `data/exports/fixture-curved-street`; safe to delete.
- Harness friction is journalled in `NOTES.md`. macOS: use `sed -i ''`.

## Known gaps / next work (priority order)
1. **Realism** (user's top ask). Observed issues: ground is soft/blotchy, trees are two blobs on a stick, facades streak where Street View coverage is thin, a pole near the camera can look oversized. Levers: Max quality preset on Bellandur with Opus 5.5 (denser Street View, 3 passes, up to 4 review rounds; estimate $1.74 per 100 m, never run for real yet), user photos (lift to L3), better tree/furniture meshes, better ground texture blending, facade textures from multiple frames. Look at `docs/evidence/phase2_1/{before,after}/` and `visual_notes.md`.
2. Not yet verified for real: Max quality run, Opus generation, photo upload against the real API, 30 fps on a real phone.
3. `remerge` regenerate mode (replay stored extractions) is unimplemented and returns 422; Regenerate only recompiles.
4. Flaky e2e test `test_real_fixture_run_page_shows_the_run_report`; live UI coverage assertion not re-run on the real street.
5. Portability: only data formats are kept engine-neutral (`spec/portability.md`). No Unity/Unreal/Godot export UI by decision. An OpenDRIVE writer and `export.zip` endpoint exist in code, unused by the UI.
6. Phase 3 (deferred): seeded city, GPS "street I'm standing on", admin correction of detected lines, per-street run history, delete legacy Phase 1 pipeline.
7. Licensing unchecked: Street View imagery for 3D modelling, Google Photorealistic 3D Tiles in Bengaluru.

## Where to read
`spec/` (architecture, data, api, agent, ui, roadmap, portability), `README.md` (section "Phase 2.1: improve an existing model"), `docs/evidence/` (gates, placement, phase2_1 screenshots and notes, Opus check), `docs/evidence/phase2_1/pr_body.md`.
