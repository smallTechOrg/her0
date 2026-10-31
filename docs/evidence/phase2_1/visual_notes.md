# Phase 2.1 visual verification notes (S5b)

Shots: `before/` (Phase-2 canvas renderer, recorded v2 Bellandur scene and the s4 curved fixture) and `after/` (glb game:
`__hero.source == "glb"` on every shot, see `after_meta.json`). 390x844, software GL, run distances 0/20/40/70 m, driven
deterministically by `tests/e2e/visual/capture.py` (`--label after`). Bellandur AFTER is served by a scratch copy of
`data/hero.db` (alembic upgrade + `hero.tools.recompile --slug bellandur-main-road-n-2` on the copy, real satellite
ground + Street View frontage textures). The curved AFTER is the committed curved StreetPlan fixture built by the real
exporter (procedural textures, no observations) and served by route interception. Automated stats: `tests/e2e/visual/judge.py`
(AFTER: 8/8 pass; BEFORE: fails on black sky / frontage holes). Budgets: `budget.json` (max 41 draw calls, 8.7k triangles,
2 glb textures; the game's `perf.textures` of 3 includes the 64 px AO blob).

## What changed in this pass
- Frontage: a confident wall/gate that sat off the local building line (e.g. a 2 m wall 9 m in front of a 16 m facade) no
  longer carves a notch/wedge. It is merged into the median building line of +-3 cells and takes the lowest neighbouring
  building height and colour (marked interpolated); a wall/gate already on the line is raised to the same skyline.
  (`src/hero/model/frontage.py`)
- Kerb: isolated single-station spikes of the kerb line / footpath outer edge are replaced by their 5-station median, which
  removed the zig-zag white kerb at ~50 m on Bellandur. (`src/hero/model/street_plan.py`)
- Furniture: pole shaft capped at 0.26 m diameter; drain/manhole decals capped (1.4 m / 0.9 m) and drawn dark metal instead
  of taking the surrounding crop tint; kerb cut / ramp decals capped. (`src/hero/export/mesh_furniture.py`)
- Ground: vertex tints are near-white multipliers so condition reads as a subtle shift rather than a hard-edged brown slab;
  procedural "missing/broken footpath" tones are less saturated. Anisotropy 8. (`mesh_ground.py`, `imagery/tints.py`, `glb_loader.js`)
- capture.py: own server on 8737, recompile on the copy, curved served from the v3 pipeline build, `__hero.source` recorded, hit
  flash hidden and the start scrim made transparent for the shot only.

## Judgement per brief issue (AFTER)
1. Footpath/road surface: fixed in substance. Real satellite texture on footpath and road (Bellandur), procedural paver/asphalt
   (curved), a visible lighter kerb strip between them, no flat slab. Imperfect: Bellandur ground is a magnified ~10 cm/px
   satellite crop, so it is soft and blotchy (tree shadows read as dark smears); the kerb is a plain light strip, not a
   detailed profile.
2. Entities: fixed. Poles, trees, bins are small meshes, no photo billboards, no "?" boxes, no big labels. Imperfect: trees are
   two low-poly blobs on a stick; a utility pole 1.6 to 2 m beside the camera path still reads big in the frame (outside the
   0.8 to 1.5 m clearance fade by design).
3. Sky/horizon/fog: fixed. Gradient sky, haze horizon, fog colour equals the horizon colour, no black. Distant buildings
   fade into the haze.
4. Frontage: mostly fixed. Both sides tiled with contiguous facades, no black gaps or see-through wedges (judge: hole fraction
   <= 3.3 %). Imperfect: Bellandur facades carry vertical stretch/smear streaks where Street View coverage is thin and the
   texture was filled from neighbours; some facades are dark and low contrast; the lateral steps between neighbouring
   building lines remain visible as stepped walls. The opposite side is mostly outside the portrait field of view.
5. No baked vehicles / non-footpath objects: fixed (dynamic_context only; none in the glb).

## Known remaining flaws (honest list)
- Where the footpath is genuinely missing (curved, from ~72 m) the plan draws bare earth flush with the road; it is a
  horizontally cut, flat-looking brown patch with a crisp start edge. Data-correct but not pretty.
- At 0 m the Bellandur frame has a dark band at the bottom: the loop's previous-copy ground (the shaded end of the street)
  shows behind the start line.
- Facade streaking and ground blur are limited by source texture resolution (frontage 1000 x 128 px per 100 m).
- Hit flash and the start scrim were hidden for the evidence frames; in the live game they tint the screen briefly.
