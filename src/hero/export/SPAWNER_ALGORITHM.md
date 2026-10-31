# Spawner algorithm - resolved pseudocode (companion to spec/portability.md section 7)

Normative = `spec/portability.md` section 7. This file only pins the points section 7 leaves open (marked **[R]**).
Reference implementation: `src/hero/export/gameplay_ref.py`. Vectors: `tests/fixtures/portability/spawner_vectors{,_bulk}.json`.
`gp` = parsed `gameplay.json`; `sp = gp.spawner`; `OT = gp.obstacle_types`; `TYPE_ORDER` = the 7 keys of `OT` in file order
(`parked_car, two_wheeler, cone, barricade, pothole, stall, low_branch`). All numbers are IEEE doubles read from the JSON as-is.

## A. Primitives

```
MASK = 0xFFFFFFFF
imul(a,b)  = (a*b) mod 2^32                      (JS: Math.imul(a,b)>>>0)
fnv1a32(str): h=0x811C9DC5; for each UTF-8 byte b: h=(h^b)&MASK; h=imul(h,0x01000193); return h
key(slug,seed,L) = slug + "|" + decimal(seed) + "|" + decimal(L)
mulberry32(a):  next():
    a = (a + 0x6D2B79F5) & MASK
    t = a
    t = imul(t ^ (t>>>15), t | 1)
    t = (t ^ ((t + imul(t ^ (t>>>7), t | 61)) & MASK)) & MASK
    return ((t ^ (t>>>14)) & MASK) / 4294967296.0
x_mm(x)  = floor(x*1000.0 + 0.5)                 (integer; Math.floor / math.floor, never round())
```

## B. Inputs

`SpawnInput = {slug, seed, length_m, density, type_weights{7 keys}, footpath_missing[[s0,s1]], static[{s0,s1,lanes[],action}], keepout[[a,b]]}`.
`tw[type]` = `type_weights[type]`, missing => 0.0. `static` only holds colliders whose action is not `none`.
Intervals are closed-form doubles; `overlap([a0,a1],[b0,b1])` is **strict**: `a0 < b1 and a1 > b0`.
**[R]** `dist([a0,a1],[b0,b1]) = max(a0 - b1, b0 - a1)` (negative or zero when they overlap/touch; evaluate as written).

## C. pick(lane, u)

```
items = [t for t in TYPE_ORDER if t in sp.lane_weights[str(lane)]]       # base weight > 0, TYPE_ORDER order
w_i   = base_i * (1.0 + sp.type_boost * tw[t_i])
total = ((w_0 + w_1) + w_2) + ...                                         # left to right
x = u * total
for i in order: x = x - w_i; if x < 0: return t_i
return last item
```

## D. allowed(type, lane, x, row)

`row` = list of already-placed entries of the *current row* (each has `lane, type, x` and the same `x`). `len = OT[type].length_m`; `act = OT[type].action`.
```
1. for st in static: if lane in st.lanes and overlap([x, x+len], [st.s0, st.s1]): return false
2. **[R8]** zone = [x - sp.gap_zone_m, x + zlen + sp.gap_zone_m], zlen = max(len, max(OT[r.type].length_m for r in row))
   (section 7 says `x + len`; for the first obstacle of a row `row` is empty so it is identical. For B the zone must cover A as well,
   otherwise a static blocker seen by A's longer zone but not B's could give a 3-lane dodge wall. Deliberate, tested deviation.)
   S_all   = {lane} U {r.lane for r in row} U {l for st in static if overlap(zone,[st.s0,st.s1]) for l in st.lanes}
   S_dodge = {l for st in static if st.action=="dodge" and overlap(zone,[st.s0,st.s1]) for l in st.lanes}
             U {r.lane for r in row if OT[r.type].action=="dodge"}  U ({lane} if act=="dodge")
3. if L <= sp.clear_lane_until_loop:  if |S_all| > 2: return false
   elif act == "dodge":               if |S_dodge| > 2: return false
4. if act != "dodge":     # [R] BOTH sources use the action_gap test: reject if dist(...) <= sp.action_gap_m   (<=, i.e. exactly 9 m rejects)
      for st in static: if st.action != "dodge" and lane in st.lanes and dist([x,x+len],[st.s0,st.s1]) <= sp.action_gap_m: return false
      for o in emitted_this_loop (all rows so far, incl. this row's A): if OT[o.type].action != "dodge" and o.lane == lane
              and dist([x,x+len],[o.x, o.x+OT[o.type].length_m]) <= sp.action_gap_m: return false
5. return true
```
`emitted_this_loop` = obstacles already appended to `out` in this `generate_loop` call (rows of earlier slots and A of this row).
Note: in step 3 for `L > clear_lane_until_loop` a non-dodge type is not checked against the lane-count (only steps 1, 4 apply).

## E. generate_loop(gp, inp, L)  (L = 0,1,2,...)

```
 1. rng    = mulberry32(fnv1a32(key(slug, seed, L)))
 2. cursor = sp.free_start_loop0_m if L == 0 else sp.free_start_later_m
 3. limit  = inp.length_m - sp.seam_free_m
 4. gap    = max(sp.row_gap_min_m, sp.row_gap_base_m - sp.row_gap_per_loop_m * L)       # L as double
 5. p      = min(sp.spawn_p_max, sp.spawn_p_base + sp.spawn_p_per_loop * L) * (sp.density_floor + sp.density_gain * inp.density)
 6. maxlen = max(OT[t].length_m for t in TYPE_ORDER)
 7. row = -1; out = []
 8. repeat:
 9.    d0..d7 = eight consecutive rng() draws (ALWAYS 8, before any decision)
10.    x = cursor + d0 * sp.jitter_max_m
11.    if x > limit: STOP (return out)
12.    row = row + 1;  cursor = x + gap
13.    if not (d1 < p): continue
14.    if any(a <= x and x <= b for [a,b] in keepout): continue
15.    cands = [0,1,2]; remove 0 if any [s0,s1] in footpath_missing has  s0 < x + maxlen + sp.footpath_missing_margin_m  and  s1 > x - sp.footpath_missing_margin_m
16.    if cands empty: continue
17.    lane_a = cands[floor(d3 * len(cands))]; type_a = pick(lane_a, d5); var_a = floor(d7 * sp.variants)
18.    two = (L >= sp.second_obstacle_from_loop) and (d2 < sp.second_obstacle_prob)
19.    if not allowed(type_a, lane_a, x, []): continue            # row number is still consumed
20.    append A = (L,row,lane_a,type_a,x_mm(x),var_a)
21.    if two:
22.       cb = cands without lane_a
23.       if cb non-empty:
24.          lane_b = cb[floor(d4 * len(cb))]; type_b = pick(lane_b, d6)
25.          if OT[type_a].action != "dodge" and OT[type_b].action != "dodge":
                type_b = first t in TYPE_ORDER within lane_b's table with OT[t].action == "dodge"
26.          var_b = (var_a + sp.variants // 2) mod sp.variants        # integer floor division
27.          if allowed(type_b, lane_b, x, [A]): append B = (L,row,lane_b,type_b,x_mm(x),var_b)    # else only B dropped
```
Resolved points: **[R1]** `row` increments once per examined slot (steps 11-12), including slots skipped by steps 13-19, so row numbers have holes;
ids are `dyn-<L>-<row>-<lane>`. **[R2]** the stop test (11) uses the unrounded double `x`; an obstacle may extend beyond `limit` by its length.
**[R3]** output order = emission order (A before B, ascending row); `x_mm` of A and B are equal. **[R4]** keepout test (14) is closed on both ends.
**[R5]** `cands[floor(d3*n)]`: `d3 < 1` so the index is always in range. **[R6]** `allowed` for A uses an empty `row`; the action-gap list (step D4)
contains obstacles of earlier rows only at that point. **[R7]** `seed` is a non-negative integer printed in decimal (may exceed 2^32; it is only hashed as text).

## F. Deriving the input from a manifest (`spawn_input_from_manifest`)

`slug = manifest.segment.slug`, `length_m = manifest.segment.length_s`, `density`/`type_weights` = `manifest.spawn_hints`.
`footpath_missing`: for each `lanes.stations[i]` with `footpath == null`: `[s - 1.25, s + 1.25]`; sorted by s; merge a range into the previous one when `lo <= prev_hi + 1e-9`.
`static`: each collider with `action != "none"`: `{s0: s, s1: s + length_m, lanes, action}` (in manifest order).
`keepout`: each collider with `type in {crossing, ramp}`: `[s - sp.keepout_margin_m, s + length_m + sp.keepout_margin_m]`.
