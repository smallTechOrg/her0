# Capability: Street Run

## What It Does
A player opens the site on a phone or desktop browser, picks a generated street from the
gallery, and runs the reconstructed corridor endlessly — three real lanes, swipe/arrow lane
change, jump and slide — collecting civic tokens and crashing on real-image-derived hazards.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Ready segments list | list | public API | yes |
| Scene JSON for the chosen street | document (`spec/data.md`) | public API | yes |
| Ground tile, billboard and facade images | images | `/media/` | yes (billboards optional per object) |
| Touch swipes / arrow keys | input | player | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Run outcome: score, distance, tokens, loops, crashed-into object, duration | record | `walkability-report` (posted at crash) |
| 30+ fps rendering of the corridor | screen | player |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Public API | list segments; fetch scene | gallery/play error state with retry; never a blank screen |

## Business Rules
- Three lanes map to the real cross-section: footpath (0), road edge (1), road (2); the
  runner starts in lane 0 (footpath).
- **Phase 2 (scene v2): the corridor follows the real street.** The path bends with the traced reference line (`scene.path`), the
  cross-section varies along the street (`scene.corridor.stations`: footpath width/offset/state, kerb, carriageway), and the lane centres
  follow it (`lane_lat[]` per 2.5 m station): lane 0 runs along the middle of the traced footpath, wandering, narrowing and breaking
  with it. Positions are `(s, lat)` mapped through the path (`spec/data.md` → Scene JSON v2); a scene with `schema_version: 1` still
  plays on the straight Phase-1 corridor with identical rules. Health, tokens, hazards, collision, loop and scoring rules are unchanged;
  `scenery: true` entities (outside every lane band) are drawn but never collide. The camera follows the runner's heading along the path.
  The segment still loops: copies are chained end-to-start with the path's end pose (rigid transform), and the last 6 m of the profile are
  pre-blended so the seam is smooth. Panels: the **About this street** panel and model line (`street-description`).
- Lane change by horizontal swipe or ⇠/⇢; jump by swipe-up/⇡; slide by swipe-down/⇣. Inputs
  during a tween are queued (one deep), not dropped.
- The runner has 100 health. Each hazard hit costs 10 (constants `HEALTH_MAX`, `HIT_DAMAGE`), then 1.5 s of invulnerability (`INVULN_S`, runner blinks); a run also starts with 3 s of grace (`START_GRACE_S`) so objects wrapping over the loop seam cannot hit at t=0 and each object damages at most once per loop pass. The run ends only when health reaches 0; `crashed_into` is the object that took it to 0.
- The segment loops seamlessly until health reaches 0; speed ramps per loop within set bounds
  (constants in `spec/ui.md`).
- An object is cleared only by its `action`: `jump` objects while airborne, `slide` objects
  while sliding, `dodge` objects only by not being in any of its `lanes`; `none` objects never
  crash. Overlap in a shared lane without the right action = crash.
- Tokens are collected by passing through them in their lane (jumping through counts).
- Score = round(distance_m) + 50 × tokens collected (consistent with server validation).
- **Phase 2.1 r2 (supersedes the next bullet and the labelled-box/billboard criteria for v3 streets):** a v3 street is the loaded `street.glb` (+ manifest + `gameplay.json`); no billboard or labelled box is drawn; vehicles etc. are spawned dynamically per `gameplay-data.md`; v1/v2 streets keep the legacy rendering. See `scene-realism.md`, `street-export.md`.
- (legacy v1/v2 only) Every scene object is drawn: billboard image when present, otherwise a labelled box;
  low-confidence objects carry a visible "?" marker so the player knows it is uncertain.
- The gallery is real: every `ready` street appears as a card; a single street is a valid gallery.
- Phase 1/2 stub: "Use my location" (disabled, captioned "Coming in Phase 3") — see `gps-pick`.
- Performance: no post-processing, flat/Lambert shading, textures ≤ 1024 px (tile) and ≤ 256 px
  (billboards/facades), object count ≤ 40 + tokens ≤ 12 per loop, geometry reused across loops.
  Phase 2 budget (measured by `window.__hero.perf`): ≤ 90 draw calls and ≤ 60 000 triangles per frame; ground/strips/buildings merged per material class; tokens instanced.

## Success Criteria
- [ ] Headless browser (phone viewport 390×844) opens `/`, sees the street card with its name and thumbnail, taps it, taps to start, and the HUD distance increases above 20 m within 5 s.
- [ ] Driving arrow-key inputs: ArrowLeft/ArrowRight change the HUD lane indicator; ArrowUp sets the runner state to jumping; ArrowDown to sliding (observable via the page's debug state handle, `window.__hero.state`).
- [ ] With the canonical example scene served via test route interception: giving no input produces a first hit on `o1` (parked_vehicle, lanes 0–1 at 12.5 m, health 100→90, run continues) and the run ends only when health reaches 0 (10 hits); moving to lane 2 before 12 m and jumping at `o3` (pothole, 40 m) survives past 45 m; passing `o5` (crossing, `action = none`) never crashes.
- [ ] Against the real generated street: the run ends at health 0 within 180 s without input, and the results overlay shows a numeric score and distance.
- [ ] Scene with a missing `billboard_url` renders the labelled fallback box (no console errors, no missing-texture crash).
- [ ] Fetching a non-existent slug shows the error state text "Couldn't load this street" and a working "Back to gallery" link.
- [ ] Frame rate check (manual, test-handoff): ≥ 30 fps on a mid-range Android phone over LAN.
- [ ] **Curved fixture street** (`tests/e2e/fixtures/scene_curved_v2.json`, a reference line turning ≥ 25° over 100 m with a footpath that
      narrows from 2.2 m to 1.2 m, shifts 1.0 m laterally and has a `missing` gap): in lane 0 with no input for 12 s the runner's
      `__hero.state.footpath_offset_m` (|lane-0 centre − footpath centre| at the runner's station) is ≤ 0.05 at every 250 ms sample, `__hero.state.on_strip == "footpath"`
      at ≥ 95 % of samples outside the `missing` gap, the runner's world heading changes by ≥ 20° during the first loop, and the runner's world position stays within 0.1 m of
      `path(s) + lane_lat[0](s)·normal(s)` computed from the fixture JSON in the test.
- [ ] `__hero.perf.draw_calls ≤ 90` and `triangles ≤ 60000` on the curved fixture after 3 s of play; no console errors.
- [ ] A scene with `schema_version: 1` (the Phase-1 fixture) still passes every Phase-1 criterion above, unchanged.
- [ ] Opening a v2 street shows the model badge on Ready and the About panel (see `street-description` criteria).
