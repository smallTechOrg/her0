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
them change accordingly. The Phase-2 stub "Upload a photo — Coming in Phase 3" was retired in Phase 2.1; the performance numbers in "Corridor (Phase 2…)" below (≤ 90 / 60 000) are superseded by the Phase-2.1 render budget.)

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
- Status bar: spent / budget with a progress bar, remaining · "global cap $50.00" (no per-run cap), runs succeeded/failed, Google
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
  changes table; cost box (Anthropic USD and call count, no cap shown; Google new/cached, total); warnings; `suggestions`.
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
   **Upload a photo** — real since Phase 2.1 (`## Phase 2.1` below); the Phase-2 stub (`data-testid="upload-stub"`) no longer exists.
7. **Reference line**: overview tile with the traced line (from the placement preview), source/way/length, warnings.

## Phase 2.1 — realism + add-to-model *(supersedes the conflicting statements above; everything not mentioned is unchanged)*

### r2 changes to this section (authoritative)
- **Where the look comes from.** Ground, kerb, frontage and furniture below are now **built in Python and loaded from `street.glb`** (`spec/portability.md` §3); the visual rules in "Game visuals" still describe what the player must see, but the JS no longer builds them. JS keeps: sky gradient, fog, lights, AO blob, camera rig, runner, spawned obstacles (instanced), HUD, the camera-clearance clip (a dithered shader discard inside 1.5 → 0.8 m of the camera on the glb materials — replaces per-entity fading, since furniture is merged per category), loop chaining (3 copies), and the loading/error states.
- **Loading state.** While the glb + manifest load the Ready overlay shows "Building the street…" with a thin progress bar (`data-testid="street-loading"`); on failure "Couldn't load the street model. [Try again]" (`data-testid="street-error"`), never a different-looking fallback for a v3 street. v1/v2 streets load instantly as before (legacy geometry).
- **Attribution.** The About panel ends with the manifest `attribution.lines` (small text, `data-testid="attribution"`).
- **Debug handle.** `window.__hero.source` is `"glb"` or `"legacy"`; `perf` adds `primitives`.
- **Admin additions (r2).** (1) **Model dropdown** (`data-testid="model-select"`, component `model_select.js`) beside every spend button that starts an LLM run — *Generate*, *Fetch + Generate*, *Fill gaps → Fetch + Generate* — listing `GET /api/admin/models`: options show the label and the price per Mtok in the title; the default is preselected; a disabled option is greyed with its `disabled_reason` as tooltip; the chosen value is sent as `model` and shown in the cost estimate and on the run page ("Model: Sonnet 5.5"). Never remembers a non-default across reloads. (2) ~~Download for Unity / Unreal / Godot button~~ **REMOVED (SQ, user scope change): the admin UI has no engine-export button or `export_button.js`; the backend export endpoints stay (the game uses `model.glb` / the manifest).**

### Game visuals (scene v3; `frontend/js/game/*`) — supersedes "Corridor (Phase 2…)" and the v1 "Running" description for v3 scenes

Reading order of what the player sees (every item is judged from the AFTER screenshots at 0/20/40/70 m):
- **Ground.** Footpath and road are the baked road-aligned satellite strip (`ground_tex.strip`) draped by `(lat, s)`, darkened/desaturated so it reads as pavement and asphalt (footpath ×0.85, road ×0.6, plus a state tint: intact none, broken warm brown 35 %, blocked/encroached amber 35 %, missing earth with the texture hidden), with a procedural pattern (paver grid / concrete speckle / asphalt grain from `procedural_seed`, 128 px tile, repeated) wherever `ground_src == "procedural"` or `ground_tex.mode == "procedural"`. **Never a flat slab.** A **kerb** (`corridor.stations[].kerb`) is a raised step (`height_m`, 0.2 m wide top in a lighter grey `#c8c8c8`, vertical face one shade darker) between footpath and carriageway; it drops to the road level where `height_m == 0` (missing footpath, kerb cut, ramp). The carriageway shows lane-less asphalt (no painted lanes unless the strip contains them).
- **Street View backdrop (slice SV; `pano_backdrop.js`) — the street *looks like Street View* when the export stitched panoramas.** `GET /api/segments/{slug}/panoramas.json` (`hero-panoramas/1`, built by Regenerate / recompile from the stored Street View frames: every frame of one `pano_id` re-projected onto a 2048×1024 equirect) lists the panoramas with their `s`/`t`. The play page projects the panorama nearest to the *camera* onto the street geometry (`Road`, `Footpath*`, `Kerb`, `Ground_*`, `Frontage_*`, `Frontage_Caps` get one shared `mat_pano` ShaderMaterial; projection centre = `world(s, t)` at `camera_height_m` 2.5 m, bearing frame = the copy's yaw) and a dome around the camera (`PanoSky`) shows the same sphere; moving along the street cross-fades to the next panorama over the middle 40 % of the gap, like stepping in Street View. The gradient sky is hidden and the fog backs off (`near 110, far 300`) because the photo has its own haze; furniture, tokens, the avatar and the dynamic obstacles are still lit models on top. Textures load lazily (the two ahead are prefetched, LRU of 6). Without panoramas (404) nothing changes and the baked look below applies. `window.__hero.pano` = `{count, current, next, mix, cached}` or `null`.
- **Frontage.** On both sides one continuous wall ribbon from `frontage.sides[].segments` (extruded to `height_m` at `lat_from → lat_to`), with the baked frontage texture mapped by `(s, height/12 m)` and tinted toward `colour_hex` (40 %) where `bin_source` is `interpolated`/`none`; steps in `lat` between segments are closed by a perpendicular face; wall/gate segments are low (≤ 2.4 m) and show the sky above. A roof cap and a darker parapet line hide the open top. No isolated quads, no holes; nothing in-world says "low fidelity" (that lives in the About panel).
- **Furniture.** Small low-poly meshes from `furniture[]`: tree = trunk cylinder + 2 stacked canopy blobs (`canopy_hex` or `#2f7d3a`), pole = thin tapered cylinder (+ cross-arm for `variant: utility`), bin/bollard/bus-stop/signboard = simple boxes/cylinders, railing = posts + rail along `length_m`, drain/manhole/crossing/ramp/kerb-cut = flat decals on the ground (crossing = white stripes). Each type is **size-capped** (`MAX_SIZE_M`), tinted by `colour_hex`, never a photo billboard, never a "?" tag. Anything within **1.5 m** of the camera path or the runner is faded out (scaled to 0 over 1.5 → 0.8 m) so nothing can occlude the view; camera near plane 0.3 m.
- **Atmosphere.** Sky = vertical gradient (zenith `#5f9bd6` → horizon `#dbe6f0`) as `scene.background` (0 draw calls); distance fog colour = horizon colour (`near 30 m, far 90 m`), camera far 100 m, so far geometry and spawning obstacles dissolve into haze (no pop-in); hemisphere + one soft directional light (warm, from the left-front); a soft **ground-AO blob** under the runner (radial-gradient quad, 1 draw call); the camera sits 3.8 m high, 6.6 m behind, looks 12 m ahead (so the footpath ahead is readable).
- **Text.** No in-world text sprites and no big labels; the only text on screen is the HUD and, when the runner is **hit**, a small toast with the obstacle's name (`hud.js`, 1.2 s, bottom-centre, 16 px). Low-confidence objects are not marked in-world; confidence is communicated in the About panel and the results card.
- **Dynamic obstacles** (generic low-poly, flat-shaded, instanced): parked car (box body + cabin, 4.2×1.5×1.8 m, random muted colour from a 6-colour palette by obstacle id), two-wheeler (low box + handlebar, 1.9×1.1×0.7), cone (0.4 m cone, orange/white band), barricade (two posts + red/white rail, 1.6 m wide), pothole (dark disc decal, 1.0 m), vendor stall (1.5×2.0×1.5 box with an awning colour), low branch (green slab at 1.4–2.0 m above the footpath, hanging into lane 0 → `slide`). Each model is one `InstancedMesh`. They carry no text.
- **HUD and controls unchanged:** distance, score, tokens, health bar, lane indicator, About button; health 100, −10 per hit, 1.5 s invulnerability, 3 s start grace.
- **Performance** — see `spec/architecture.md` → *Render budget* (≤ 60 draw calls / 60 000 triangles / 8 textures / 24 MB); `window.__hero.perf` gains `textures`, `texture_mb`, `geometries`; `window.__hero.dynamic` = `{run_number, seed, obstacles, rules}` (read-only).

### About this street panel — additions (`scene.about`, scene v3)
After the fidelity bar and "Least certain": **"Filled in from neighbouring stretches"** — the `about.interpolated` list, one line each ("Right-hand frontage, 40–60 m — no photo or building outline; blended from 35 m and 60 m"), preceded by `gap_summary`; **"Seen here, spawned during play"** — `dynamic_summary`
("35 parked vehicles, 3 vendor stalls… appear as moving-around obstacles, not at their real spots"). Both sections are omitted when empty. For v1/v2 scenes the panel is unchanged. Low-confidence information previously shown in-world now lives here and in the results card.

### Results card — additions
"Hazards" lists **static** hazards only and says so ("static hazards on the footpath: 6; obstacles in your run were generated for play"); the crash line shows the spawned obstacle's name when you crashed into one. `walkability` card unchanged.

### Admin segment page — "How to improve this model" panel (`data-testid="improve"`; the first section under the header, above Version history)
Header row: current model badge + `scene v3`/`scene v2 (stale — Regenerate to upgrade)`. Then, top to bottom:
1. **Where it is weakest** (`data-testid="improve-weakest"`): the coverage bar (same component as Coverage, compact, `data-level` cells) with the ≤ 5 weakest intervals listed underneath ("40–60 m · L1 · single viewpoint, no right-facing photo → *Add a Street View frame facing right, or upload one*", each with two buttons **Fill this stretch** (pre-sets `s_range` in the plan preview) and **Upload here** (pre-sets `s` in the upload form)). Empty state: "Every stretch is at the target level."
2. **Four actions** as cards (`data-testid="improve-action-<id>"`), each with a one-line "what it does / what it costs":
   - **Fill gaps** — *"Plans the photos that would help most. Shows a preview first."* Button **Preview plan** → inline table (kind, view, station, "new/cached", reason), totals "n new · m cached · ≈ $0.19 Google · ≈ $0.31 Claude (estimate)", a warning chip when `will_exceed_budget` (estimate above the remaining global budget)/`trimmed`; the estimate itself never blocks, then **Fetch + Generate** and **Fetch images only** (disabled with the reason until a preview exists and when a run is active).
   - **Add more imagery** — the existing form (spacing, headings, crop zoom, optional range with live estimate) moved into this card.
   - **Upload photos** — the real upload form (below).
   - **Regenerate with what we have** — *"Rebuilds the street from photos and detections already stored. Costs $0: no Google, no Claude."* Radio `Rebuild scene only (default)` / `Also re-merge stored detections` with a hint when `missing_extractions > 0`; button **Regenerate** → inline result "Done — scene v3, 0 Google requests, 0 Claude calls, $0.00; 25 m interpolated; 41 objects now spawn dynamically" (or "Nothing changed").
3. **How to add to an existing model** (`<details open data-testid="improve-help">`): the five numbered steps from `improve-plan.steps` plus two sentences on cost and on what each fidelity level means. Same text as the README section.
All four cards share one **busy** state (a run in progress disables every spend button and links to the run view).

### Upload photos form (`data-testid="upload-form"`; replaces the stub)
- A real `<input type="file" accept="image/jpeg,image/png" multiple data-testid="upload-file">` plus a drop zone; each chosen file becomes a **row** with a thumbnail (object URL), file name, size (rejected inline above `upload_max_mb`), and per-row fields: **Position `s`** (number, m, 0–length; filled by clicking the map; "click the map" hint until set), **Side of the road** (`left`/`right`/`centre`, labelled with compass words when known: "west side (left of travel)"), **Facing** (select: *along the street ahead*, *back*, *the left-hand buildings*, *the right-hand buildings*, *custom heading…* → compass number), **Date taken** (month input, optional; prefilled from the file's `lastModified`, labelled "taken", editable), remove button.
- **Placement map** (`data-testid="upload-map"`, `placement_map.js`): the overview tile with the traced reference line (from the placement preview, or the straight line over the satellite thumbnail when absent) rendered on a `<canvas>`, 10 m ticks, the footpath side marked; below it the coverage cells again so weak stretches can be clicked. **Click on the map** → snaps to the nearest point on the line → fills `s` (and `side` from which side of the line you clicked) of the selected row; existing observations are dots (user photos blue); the selected row's marker is accent-coloured and draggable. Keyboard: arrows move the selected marker ±1 m.
- **Upload N photos** → sequential `POST …/observations/upload`; per-row status (queued/uploading/done "placed at 42 m · can lift 40–45 m to L3"/error text); afterwards the imagery grid refreshes (new rows say "uploaded · not yet fused") and a **Generate now** button appears (costs shown; or "Fuse later").
- Empty/loading/error states per `ui-ux.md`; 4xx messages from the API shown verbatim on the row.

### Admin additions elsewhere
Segment table (`/admin/`): model cell shows `v1 · L1 · scene v2` with a "stale" tag when `scene_schema < 3`. Run view: unchanged. The imagery grid filter gains `user_photo`.

### Max quality preset *(slice SQ; `quality_fields.js`, `GET /api/admin/quality-preset`)*
A **Max quality** checkbox (`max-quality-fill`, `max-quality-imagery`; default OFF) on the Fill-gaps card and the Add-imagery form. Checking it (a) fills the editable run-time fields from the server preset: Street View spacing 5 m (Google has a panorama only every ~10 m, so this catches every panorama; stations on the same panorama are fetched once) x 8 headings (4 cardinal + 4 diagonal) x 3 camera tilts (`pitches_deg` 0 / -30 down at the road, kerbs and potholes / +25 up at facades, signs and wires) x 2 fields of view (`fovs_deg` 90 wide / 50 zoomed detail) = 48 frames per panorama (`frames_per_station` = headings x tilts x fovs), satellite crop zoom 21 every 10 m plus an overlapping second tile set at zoom 20 (`sat_extra_zoom`, crops centred on the seams of the primary set), `max_images` 2000 (the cap of every imagery form and API is 1-2000), **extraction passes 3** per frame batch, **review rounds up to 4** (stop early when a round applies <= 1 change); (b) switches the shared model dropdown to `claude-opus-5-5` **only if that model is enabled** (otherwise a note says so and the chosen model is kept); the user can switch back, and unchecking restores the model the preset replaced. Leaving it unchecked never touches the model and the requests are exactly what they were. No per-run dollar cap: the global `HERO_CLAUDE_BUDGET_USD` hard stop, the estimate shown before launch (Fill-gaps preview `est.*`; Add-imagery `data-testid="estimate-claude"` from `POST …/imagery/estimate`) and the per-run cost report stay. Both estimates include the passes and review rounds (an upper bound: early stop and the extraction cache only make it cheaper). The Fill-gaps preview also sends `model` and is asked again whenever the model dropdown or a quality field changes.
**Final estimate, typical 100 m segment at max quality** (`hero.model.improve.estimate_cost`, prices from `src/hero/llm/prices.json`; ~11 panoramas x 48 = 528 Street View frames + 19 close-up crops + 1 overview, all new, 3 passes, 4 review rounds): **Opus 5.5 ~ $17.1** ($3.74 Google + $13.35 Claude); **Sonnet 5.5 ~ $10.4** ($3.74 Google + $6.68 Claude). The Add-imagery estimate snaps planned stations to the stored panoramas (`PANO_SNAP_M`) so frames already stored and frames shared by two stations are not counted twice. When a run finds nothing new it says so in its warnings (add headings, tilts or zoomed views). Upper bounds; a re-run of work already stored costs $0.

## 6. Correction — `/admin/correct.html?segment=<slug>` *(Phase 3, `data-hero-page="admin-correct"`)*

Satellite tile with the segment line; footpath coverage ranges as draggable coloured bars
along the segment; objects as draggable markers (drag = move `s`, lane selector, delete,
relabel type/action); "Add object" at a clicked point. Save → `PUT …/corrections` (edits the canonical model as
`human` evidence and publishes a version of kind `edit`); shows
"Saved — walkability recalculated, $0.00 spent". Reset to the last generated version.
