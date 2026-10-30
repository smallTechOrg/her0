## Phase 2.1 r2: realism, add-to-model, shared mesh export, gameplay as data

### What each piece added
- **Shared Python mesh builder + export** (`src/hero/export/`): deterministic `street.glb`, `street.manifest.json`, `gameplay.json` and a best-effort `street.xodr`, served from the API and loaded by the game. Contract: `spec/portability.md`.
- **Gameplay as data**: every rule in `src/hero/export/gameplay.json`; Python reference spawner (`gameplay_ref.py`) and JS spawner agree on committed vectors (`tests/fixtures/portability/`). Algorithm: `src/hero/export/SPAWNER_ALGORITHM.md`.
- **Dynamic obstacles** seeded by run number or `?seed=`.
- **Add to an existing model** (admin segment page): Fill gaps, Add more imagery, upload photos placed on the coverage map, Regenerate with what we have ($0), Generate.
- **Per-run Model dropdown**: default `claude-sonnet-5-5`; `claude-opus-5-5` verified.
- **Max quality preset**: ~12 m x 4 headings Street View, zoom 21 + overlapping zoom-20 crops, 3 extraction passes merged by agreement, up to 4 review rounds; estimate shown before launch; cached passes re-run for $0; a per-5 m before/after fidelity report per run.
- **Recompile tool** and README/spec updates (no engine-specific docs; no engine export UI).

### How to run
```bash
uv sync && uv run python -m hero.tools.vendor_three   # frontend/vendor/ is git-ignored
uv run hero-serve                                       # http://localhost:8711/
uv run python -m hero.tools.recompile --all             # zero spend
uv run pytest -q                                        # live tests are excluded by default
```
Note: `recompile --all` covers every street with a model or a Phase-1 scene, including the legacy off-road `100-feet-road-indiranagar-w` (no skip list in the tool).

### Evidence
- `docs/evidence/phase2_1/model_check_claude-opus-5-5.json` (Opus 5.5 structured-output check)
- `docs/evidence/phase2_1/{before,after}/`, `before_meta.json`, `after_meta.json`, `budget.json`, `rootcause_v2.json`, `visual_notes.md`

### Spend
- Opus verification: $0.0154 (one call, 2387 in / 292 out tokens; `cost_usd` 0.015388).
- Real generations in this phase's work: 0.

### Deferred / dropped
- Engine export UI (dropped by user decision; formats stay engine-neutral).
- OpenDRIVE (`xodr`) polish.
- Spawner test vectors beyond the committed set.
- v1/v2 scene polish.
- Opus price source: the price table uses $4/$20 per Mtok (`source_url` and verified date recorded in the check file); not independently re-verified beyond that.
