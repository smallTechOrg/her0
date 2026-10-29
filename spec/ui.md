# UI

Three surfaces, one visual system (dark background, high-contrast type ≥16 px, one accent
colour for tokens/primary actions, one warning colour for hazards, one muted colour for
low-confidence and stubs). Portrait phone first; desktop is the same layout centred at
max-width 480 px for play, full-width for admin. All interactive elements are real buttons
with focus rings; `prefers-reduced-motion` disables the camera bob and tween easing only
(gameplay motion is the product). Every page root carries `data-hero-page`.

Stubs: a stub is a visibly disabled control with a `title` and inline caption
"Coming in Phase 2" and `data-stub="true"`. Never a dead button.

## 1. Gallery — `/` (`data-hero-page="gallery"`)

- Header: "#hero" wordmark + one-line tagline "Run a real street. See how walkable it is."
- Primary action row: **Use my location** — Phase 1: stub ("Coming in Phase 2: picks the
  street you're standing on"). Phase 2: real; asks geolocation, shows "Finding the nearest
  street…", then jumps to play or shows "No street near you yet — pick one below".
- Street cards (one per ready segment): satellite thumbnail with hazard overlay, name, city,
  length, walkability score badge (`50/100`), best score ("Best 1,240" or "No runs yet"),
  quality tag ("satellite-only" shown when not `full`). Tap → `/play/?segment=<slug>`.
- States: loading skeleton cards; empty "No streets yet — an admin generates them at /admin/";
  error "Couldn't load streets — retry" with retry button.
- Footer link: "Admin" → `/admin/`.

## 2. Play — `/play/?segment=<slug>` (`data-hero-page="play"`)

**Loading** — full-screen panel: street name, "Building the street from satellite + street
view…", progress text for scene fetch → textures (real progress: n/total textures loaded).
Error state: "Couldn't load this street" + reason + "Back to gallery".

**Ready** — "Tap to run" overlay with the control hints: swipe ⇠ ⇢ lane, ⇡ jump, ⇣ slide
(desktop: arrow keys). Street name and "Lanes: footpath · road edge · road".

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
> round(distance_m) + 50 × tokens) are the Phase 1 tuning; adjust in Phase 2 after play-testing.

**Results** (overlay, scrollable) — in order:
1. Score, distance, loops, "New best!" badge or "Best: N".
2. **Walkability card** for the street: footpath coverage % (bar), hazard count, crossing
   present (yes/no), score /100, quality tag + "low-confidence detections included" note when
   any hazard `< 0.6`.
3. **Hazard map**: the satellite tile (canvas) with the segment line, markers at every
   hazard's `tile_px` coloured by action (jump/slide/dodge), token markers, and a highlight
   on the object you crashed into. Tapping a marker shows its label + confidence.
4. **Detected objects** list: type, label, lane, action, confidence (low-confidence rows
   labelled "low confidence"), "from street view" when a source crop exists (thumbnail).
5. Phase 2: top-5 runs for this street.
6. Buttons: **Run again** (primary), **Back to gallery**.
Error state if posting the run fails: results still show from local data with a notice
"Score couldn't be saved — check your connection".

## 3. Admin — `/admin/` (`data-hero-page="admin"`)

- Password gate: single password field + "Enter"; stored in `sessionStorage` for the tab;
  wrong → "Wrong password" inline. Validated via `GET /api/admin/status`.
- Status bar: spent / budget with a progress bar, remaining, runs succeeded/failed, Google
  requests + cache hits, models in use, "Street View last check: available/unavailable".
- Segment table: name, city, length, status chip, quality, latest run (status, cost, link),
  **Generate** button (disabled with reason when a run is in progress or budget is exhausted).
- "New segment" form prefilled with the seed (name, city, start lat/lon, end lat/lon,
  footpath side) → Create. Validation messages inline.
- **Seed city** button — Phase 1 stub ("Coming in Phase 2"). Phase 2: real.
- Recent runs list (status, segment, cost, duration) → run view.

## 4. Pipeline view — `/admin/run.html?id=<run_id>` (`data-hero-page="admin-run"`)

Polls every 1.5 s until terminal; shows "Live" indicator while running.
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
- **Correct detected lines** — Phase 1 stub ("Coming in Phase 2"). Phase 2: → `/admin/correct.html?segment=<slug>`.

## 5. Correction — `/admin/correct.html?segment=<slug>` *(Phase 2, `data-hero-page="admin-correct"`)*

Satellite tile with the segment line; footpath coverage ranges as draggable coloured bars
along the segment; objects as draggable markers (drag = move `x_m`, lane selector, delete,
relabel type/action); "Add object" at a clicked point. Save → `PUT …/corrections`; shows
"Saved — walkability recalculated, $0.00 spent". Reset to last generated scene.
