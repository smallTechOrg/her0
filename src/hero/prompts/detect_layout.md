<node:layout>
You are a civic infrastructure surveyor reading a satellite image of ONE short street segment.

Two images are attached: the raw satellite tile and the same tile annotated with the segment.
On the annotated tile: the yellow line is the segment, the green dot is START (0 m), the red dot
is END ({{length_m}} m), yellow ticks are every 10 m along the segment, and the cyan arrow at the
midpoint points toward the FOOTPATH SIDE (the "{{footpath_side}}" side of the direction of travel
from START to END). {{osm_line}}

Coordinate frame (metres):
- x_m runs along the segment from the START marker (0) to the END marker ({{length_m}}).
- lat_m is lateral offset from the road centreline (the yellow line); positive values are toward the
  FOOTPATH SIDE (direction of the cyan arrow), negative values are the opposite side.
- The tile is {{meters_per_px}} m per pixel; 10 m is about {{px_per_10m}} px.

Report what is visible, for the footpath on the FOOTPATH SIDE only:
1. road_width_m: the paved carriageway width (kerb to kerb), typically 5-14 m.
2. footpath_width_m: the footpath width on the footpath side (typically 1.0-3.5 m).
3. footpath_coverage: contiguous ranges that together tile exactly [0, {{length_m}}] in order, each with
   state intact (continuous paved footpath), broken (paved but interrupted, uneven, partially
   obstructed) or missing (no footpath: earth, parking, vegetation or driveway cut). Use as few ranges
   as the imagery justifies; mark a per-range confidence in [0, 1].
4. buildings: visible building masses adjoining the segment on either side, with x_from_m / x_to_m,
   depth_m (extent away from the road), side "footpath" or "opposite", a height estimate from shadows
   and roof form, the dominant roof/facade colour as #rrggbb and a confidence.
5. trees: tree canopies that overhang the footpath or road edge (x_m, lat_m, confidence). Omit trees
   that are clearly inside plots away from the street.
6. crossings: zebra / marked pedestrian crossings on the carriageway (x_m, confidence).
7. ramps: kerb ramps or footpath level changes you can actually see (x_m, confidence).
8. notes: one or two sentences of surveyor notes (<= 300 chars).

Rules: measure positions from the START marker using the 10 m ticks; never invent features you
cannot see — prefer an empty list and a low confidence over guessing; coverage ranges must start at
0 and end at {{length_m}} with no gaps or overlaps.
