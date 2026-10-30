# UI

Three surfaces, one visual system (dark background, high-contrast type ≥16 px, one accent
colour for tokens/primary actions, one warning colour for hazards, one muted colour for
low-confidence and stubs). Portrait phone first; desktop is the same layout centred at
max-width 480 px for play, full-width for admin. All interactive elements are real buttons
with focus rings; `prefers-reduced-motion` disables the camera bob and tween easing only
(gameplay motion is the product). Every page root carries `data-hero-page`.

Stubs: a stub is a visibly disabled control with a `title` and inline caption
"Coming in Phase 3" and `data-stub="true"`. Never a dead button. (Phase 2 renumbered the old Phase 2
— seeded city, GPS pick, admin correction — to Phase 3; every stub caption and the e2e assertions on
them change accordingly. New Phase-2 stub: "Upload a photo — Coming in Phase 3" on the segment page.)

**Descriptive by default (Phase 2).** Every object, strip and building the UI shows carries its short
natural-language `description` and an evidence line ("Seen in 3 Street View frames, 2 within 10 m").
Low-fidelity or legacy material is never silent: it carries a plain label ("map-only sketch",
"legacy, placement unverified", "seen from one place only").

## 1. Gallery — `/` (`data-hero-page="gallery"`)

- Header: "#hero" wordmark + one-line tagline "Run a real street. See how walkable it is."
- Primary action row: **Use my location** — Phase 1/2: stub ("Coming in Phase 3: picks the
  street you're standing on"). Phase 3: real; asks geolocation, shows "Finding the nearest
  street…", then jumps to play or shows "No street near you yet — pick one below".
- Street cards (one per ready segment): satellite thumbnail with hazard overlay, name, city,
  length, walkability score badge (`50/100`), best score ("Best 1,240" or "No runs yet"),
  quality tag ("satellite-only" shown when not `full`), and (Phase 2) a **fidelity chip** from
  `model`: `L2 · 48 photos` (`data-fidelity="L2"`), or "map-only sketch" for L0 / "legacy · placement
  unverified" for imported Phase-1 streets. Tap → `/play/?segment=<slug>`.
- States: loading skeleton cards; empty "No streets yet — an admin generates them at /admin/";
  error "Couldn't load streets — retry" with retry button.
- Footer link: "Admin" → `/admin/`.

## 2. Play — `/play/?segment=<slug>` (`data-hero-page="play"`)

**Loading** — full-screen panel: street name, "Building the street from satellite + street
view…", progress text for scene fetch → textures (real progress: n/total textures loaded).
Error state: "Couldn't load this street" + reason + "Back to gallery".

**Ready** — "Tap to run" overlay with the control hints: swipe ⇠ ⇢ lane, ⇡ jump, ⇣ slide
(desktop: arrow keys). Street name and "Lanes: footpath · road edge · road". Phase 2 adds one
line under the name — the model badge, e.g. "Fidelity L2 · 48 photos · updated 30 Oct 2026" — and a
secondary button **About this street** (`data-testid="about-open"`) that opens the About panel
(below) without starting the run.

**About this street panel** (`data-testid="about-panel"`, reachable from Ready, from the HUD pause
icon and from Results) — plain language, from `scene.about`, in this order: the `summary` sentence(s)
("This street is modelled at fidelity L2 (multi-view) from 48 photos … model v2 …"); a fidelity
bar over the street length coloured L0–L3 with the legend; "Photos: 44 Street View, 4 satellite,
0 uploaded · captured 2023-04 to 2024-02 · last updated 30 Oct 2026"; **"Least certain"** list
(`low_confidence_parts`, each "60–75 m: 2 objects below 60 % confidence"); the `improve_hint`; for
legacy/L0: the labelled warning ("Legacy scene, placement unverified" / "Map-only sketch: no street photos
yet"). For a v1 scene: "This street was built with the first-generation model (3 photos); its position
was not verified." The panel traps focus and closes with Esc / Close.

**Corridor (Phase 2, scene v2).** The street bends with the real road: the ground is a strip draped
along `path` (satellite texture via `corridor.stations[].ground_uv`), the footpath is drawn as its
traced strip with its real width and offset (colour by `footpath.state`: intact grey, broken
brown-grey, blocked/encroached amber, missing = gap showing earth), kerb ribs at strip steps,
carriageway darker. The three lanes follow `lane_lat[]` per station — **the footpath lane runs down the
middle of the traced footpath**. The camera follows the runner along the path (heading = `θ(s)`),
looking ~9 m ahead; lane tweening is in `lat`. Buildings are extruded from their `[s, lat]` footprints
(facade crop on the street-facing wall). Entities are billboards/boxes at `(s, lateral_m)` yawed to the
path; `scenery: true` entities are drawn dimmed and never hit. Performance budget (asserted in e2e via
`window.__hero.perf`): ≤ 90 draw calls and ≤ 60 000 triangles per frame, textures ≤ 1024 px (tile) /
256 px (crops), strips/buildings/ground merged into one mesh per material class, tokens `InstancedMesh`,
geometry shared by the chained copies; target 30+ fps on a mid-range Android phone (manual check).

`window.__hero` (read-only debug handle, drives the e2e tests) gains, for v2 scenes: `state.s` (wrapped station),
`state.lat_m` (current visual lateral), `state.footpath_centre_m` and `state.footpath_offset_m` (|lane-0 centre − footpath centre| at
the runner's station; `null` inside a `missing` gap), `state.on_strip` (`"footpath" | "kerb" | "carriageway" | "gap" | …` — the strip under the
runner's `lat_m`), `state.world` (`{x, z}`), `state.heading_deg`, `state.model` (`{version, fidelity, photos}`), and `perf` (`{draw_calls, triangles, fps_avg}`
sampled from `renderer.info`). Input is still real swipes/keys — no setters.

**Running** — Three.js canvas full-viewport. HUD top: distance (m), score, tokens; a small
lane indicator (which of footpath / road edge / road). Camera third-person behind and above a
capsule runner. Ground: satellite tile textured plane (the real tile, draped via
`ground.frame_to_px`, repeated per loop). Buildings: flat-shaded boxes from `buildings[]`,
facade crop on the street-facing face when `facade_url` exists, else `color_hex`. Objects:
billboard quads (always facing the camera) textured from `billboard_url`, sized `size_m`;
without a billboard, a coloured box with the `label` as a sprite. Low-confidence objects get a
translucent outline ("?" badge). Tokens: spinning accent-coloured coins. Crossing/ramp drawn
as flat strips. Lane change: 150 ms tween; jump: 500 ms airtime (clears `jump` objects);
slide: 600 ms (clears `slide` objects). Speed starts 6 m/s, +0.1 m/s per loop, max 12 m/s.
Segment loops seamlessly (x wraps at `length_m`; loops counter). Collision: runner box
overlaps an object box in a shared lane and the required action is not active → crash.
Crash: 400 ms stumble + screen flash, then Results. Reduced-motion: no flash.

> **Assumed:** mechanics constants above (speeds, timings, token value 50, score =
> round(distance_m) + 50 × tokens) are the Phase 1 tuning; adjust in Phase 3 after play-testing.

**Results** (overlay, scrollable) — in order:
1. Score, distance, loops, "New best!" badge or "Best: N".
2. **Walkability card** for the street: footpath coverage % (bar), hazard count, crossing
   present (yes/no), score /100, quality tag + "low-confidence detections included" note when
   any hazard `< 0.6`.
3. **Hazard map**: the satellite tile (canvas) with the segment line, markers at every
   hazard's `tile_px` coloured by action (jump/slide/dodge), token markers, and a highlight
   on the object you crashed into. Tapping a marker shows its label + confidence.
4. **Detected objects** list: type, label, lane, action, confidence (low-confidence rows
   labelled "low confidence"), "from street view" when a source crop exists (thumbnail); Phase 2 adds
   each object's `description` and evidence line, and the footpath-state list from `cross_section.footpath_coverage`
   (six states, `unknown` shown as "not yet observed").
5. **Model line + About** (Phase 2): "Model v2 · fidelity L2 · 48 photos · updated 30 Oct 2026" with an
   **About this street** button, and the "Least certain" parts in one sentence.
6. Phase 3: top-5 runs for this street.
7. Buttons: **Run again** (primary), **Back to gallery**.
Error state if posting the run fails: results still show from local data with a notice
"Score couldn't be saved — check your connection".

## 3. Admin — `/admin/` (`data-hero-page="admin"`)

- Password gate: single password field + "Enter"; stored in `sessionStorage` for the tab;
  wrong → "Wrong password" inline. Validated via `GET /api/admin/status`.
- Status bar: spent / budget with a progress bar, remaining, runs succeeded/failed, Google
  requests + cache hits, models in use, "Street View last check: available/unavailable".
- Segment table: name, city, length, status chip, quality, **model** (`v2 · L2`, "legacy" tag for imported
  Phase-1 streets), latest run (status, cost, link), an **Open** link to the segment page
  (`/admin/segment.html?slug=…`), **Generate** button (disabled with reason when a run is in progress or
  budget is exhausted). Generate uses the default imagery plan; the plan can be changed on the segment page.
- "New segment" form is address-based: address or place name (prefilled "100 Feet Road, Indiranagar"; a pasted
  "lat, lon" also works), city, "metres either way" (50–100, default 50) → **Find location** lists geocoder candidates
  (label, coordinates, provider, road direction, "check on map" link). Picking one fills the footpath-side choice
  (named by compass side, e.g. "west side (left of travel)") and the road direction (editable; required when none was
  found). **Create segment** is enabled only once a candidate and a valid half-length exist. No lat/lon fields.
- After **Create segment** (address flow) the page offers **Check placement** → `POST …/placement-preview`:
  shows the overview tile with the traced reference line, 10 m ticks and footpath side, plus
  "Road geometry: OpenStreetMap way 25173540 (100 Feet Road) · line snapped 3 m from the address" or a red
  "Road geometry unavailable — using a straight line; placement not verified". The admin confirms the line lies on the
  road before spending on Generate.
- **Seed city** button — Phase 1/2 stub ("Coming in Phase 3"). Phase 3: real.
- Recent runs list (status, mode `generate|acquire`, segment, cost, duration, `new version`/`no change`) → run view.

## 4. Pipeline view — `/admin/run.html?id=<run_id>` (`data-hero-page="admin-run"`)

Polls every 2 s until terminal; shows "Live" indicator while running.
- Header: segment name, status chip (`queued/running/succeeded/failed/failed_budget`), elapsed.
- Cost panel: input/output tokens, USD this run, budget bar (spent total / budget), Google
  requests "N new / M cached", per-call table (step, model, tokens, USD, latency).
- **Steps timeline**: every step with status icon, latency, detail text — appearing live.
- **Imagery**: satellite tile (with annotated version toggle), Street View frames with their
  x_m/heading captions, "Street View unavailable → satellite-only" notice when applicable.
- **Scene graph**: tabs "Layout (pass A)", "Obstacles (pass B)" raw JSON (collapsible, monospaced).
- **Review iterations**: per iteration: top-down preview image, accepted?, critique text,
  patch ops table (op, target, values), "applied"/"rejected".
- **Final scene**: top-down preview (final), scene JSON (collapsible), **Open in game** link,
  walkability summary.
- Error panel (when failed): code, message, step, "what to do" sentence.
- **Correct detected lines** — Phase 1/2 stub ("Coming in Phase 3"). Phase 3: → `/admin/correct.html?segment=<slug>`.

**Phase-2 run view (runs with `mode`)** — same page, extra sections (the Phase-1 sections that have no data are hidden, not empty):
- Header: segment name, status chip, **mode chip** (`generate` / `acquire`), "Model v1 → v2" (links to the segment page) or "No change — model stays v2".
- **Run report** (`data-testid="run-report"`, from `GET …/report`): the plain-language `summary`; an Images table
  (total / new / cached; by source; by heading `forward back left right`); **What changed**: new / changed / removed /
  contradicted entities (each with its description and confidence, link to its evidence on the segment page);
  profile changes; **Fidelity before → after** as two stacked bars with the improved intervals listed; confidence
  changes table; cost box (Anthropic USD vs run cap, Google new/cached, total); warnings; `suggestions`.
- **Images used**: thumbnail grid (`imagery.observations`), badge `new` / `cached`, caption "forward · 20 m · 2023-04".
- Steps timeline, cost panel, critique iterations (preview + critique + ops), error panel: as Phase 1.

## 5. Segment page — `/admin/segment.html?slug=<slug>` *(Phase 2, `data-hero-page="admin-segment"`)*

One page that answers "what does the model know, how well, and from what". Loading/empty/error/populated states.
Sections, top to bottom:
1. **Header**: name, city, length, status, **model badge** (`v2 · L2 · 48 photos`), legacy/placement-unverified warning
   when applicable, buttons **Generate** and **Open in game**.
2. **Version history** (`data-testid="versions"`): newest first; each row = version, kind, date, run link, entity count,
   fidelity, the one-paragraph `summary`; two selectors + **Compare** → the diff panel; the latest two are pre-selected.
3. **Coverage / fidelity map** (`data-testid="coverage"`): a horizontal strip along `s` (one cell per 5 m,
   `data-level="L0..L3"`, colours L0 grey, L1 amber, L2 green, L3 blue), s-ticks every 10 m, a second row of footpath-state
   cells (intact/broken/missing/blocked/encroached/unknown colours), entity markers (shape by class, opacity = confidence).
   Clicking a cell opens a popover: level, views/stations/sides, `hint` ("what image would improve this"), thumbnails of
   the covering photos. Legend and a histogram ("60 m L1 · 40 m L2").
4. **Entities** (`data-testid="entities"`): table (type, `s`, side, confidence bar, state, description, evidence count);
   filters type/state/min confidence; row click → **evidence drawer**: description, attributes, history sparkline of
   confidence per version, contradictions, and the supporting images as thumbnails with the detection box drawn from
   `crop_px` (click = full image); `removed` entities are shown struck-through with the reason.
5. **Diff** (`data-testid="diff"`): for the selected pair — summary lines, added / removed / changed entities (field-level
   before → after), profile changes, fidelity improvements, contradictions opened/resolved.
6. **Imagery** (`data-testid="imagery"`): observation grid with filters (kind, view), counts, "fused in v2" / "not yet fused"
   badges. **Add more imagery** form (`data-testid="add-imagery"`): spacing slider (4–40 m, default from status.limits),
   heading checkboxes (forward/back/left/right), satellite crop zoom (19/20), optional `s` range (prefilled by clicking a coverage
   cell), live estimate "≈ 22 new images ≈ $0.15 Google" (client-side from the plan maths + cached count from the observations list),
   buttons **Fetch images only** (`POST …/imagery`) and **Fetch + Generate** (`POST …/generate` with the same plan; the run view opens).
   **Upload a photo** — stub ("Coming in Phase 3", `data-stub="true"`).
7. **Reference line**: overview tile with the traced line (from the placement preview), source/way/length, warnings.

## 6. Correction — `/admin/correct.html?segment=<slug>` *(Phase 3, `data-hero-page="admin-correct"`)*

Satellite tile with the segment line; footpath coverage ranges as draggable coloured bars
along the segment; objects as draggable markers (drag = move `s`, lane selector, delete,
relabel type/action); "Add object" at a clicked point. Save → `PUT …/corrections` (edits the canonical model as
`human` evidence and publishes a version of kind `edit`); shows
"Saved — walkability recalculated, $0.00 spent". Reset to the last generated version.
