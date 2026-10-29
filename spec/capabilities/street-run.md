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
- Lane change by horizontal swipe or ⇠/⇢; jump by swipe-up/⇡; slide by swipe-down/⇣. Inputs
  during a tween are queued (one deep), not dropped.
- The segment loops seamlessly until the first crash; speed ramps per loop within set bounds
  (constants in `spec/ui.md`).
- An object is cleared only by its `action`: `jump` objects while airborne, `slide` objects
  while sliding, `dodge` objects only by not being in any of its `lanes`; `none` objects never
  crash. Overlap in a shared lane without the right action = crash.
- Tokens are collected by passing through them in their lane (jumping through counts).
- Score = round(distance_m) + 50 × tokens collected (consistent with server validation).
- Every scene object is drawn: billboard image when present, otherwise a labelled box;
  low-confidence objects carry a visible "?" marker so the player knows it is uncertain.
- The gallery is real: every `ready` street appears as a card; a single street is a valid gallery.
- Phase 1 stub: "Use my location" (disabled, captioned) — see `gps-pick`.
- Performance: no post-processing, flat/Lambert shading, textures ≤ 1024 px (tile) and ≤ 256 px
  (billboards/facades), object count ≤ 40 + tokens ≤ 12 per loop, geometry reused across loops.

## Success Criteria
- [ ] Headless browser (phone viewport 390×844) opens `/`, sees the street card with its name and thumbnail, taps it, taps to start, and the HUD distance increases above 20 m within 5 s.
- [ ] Driving arrow-key inputs: ArrowLeft/ArrowRight change the HUD lane indicator; ArrowUp sets the runner state to jumping; ArrowDown to sliding (observable via the page's debug state handle, `window.__hero.state`).
- [ ] With the canonical example scene served via test route interception: giving no input produces a crash on `o1` (parked_vehicle, lanes 0–1 at 12.5 m) and the results show `crashed_into_type = parked_vehicle`; moving to lane 2 before 12 m and jumping at `o3` (pothole, 40 m) survives past 45 m; passing `o5` (crossing, `action = none`) never crashes.
- [ ] Against the real generated street: the run ends in a crash within 90 s without input, and the results overlay shows a numeric score and distance.
- [ ] Scene with a missing `billboard_url` renders the labelled fallback box (no console errors, no missing-texture crash).
- [ ] Fetching a non-existent slug shows the error state text "Couldn't load this street" and a working "Back to gallery" link.
- [ ] Frame rate check (manual, test-handoff): ≥ 30 fps on a mid-range Android phone over LAN.
