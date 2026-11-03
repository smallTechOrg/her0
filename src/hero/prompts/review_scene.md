<node:review>
You are reviewing a generated 3D scene of a {{length_m}} m street segment against the imagery it was
built from. Attached: the top-down preview (satellite tile with the scene drawn: lanes, footpath
coverage colour bands — green intact / amber broken / red missing — building footprints in yellow,
objects as boxes labelled with their id and type, tokens as small yellow dots), the raw satellite tile,
and {{pano_count}} Street View frame(s) each labelled with its x_m.

Coordinate frame: x_m from START (0) to END ({{length_m}}); lane 0 = footpath ({{footpath_side}} side),
1 = road edge, 2 = road.

Current scene (object ids are what you patch):
{{scene_summary}}

Decide whether the scene materially matches the imagery. Only patch what the imagery contradicts:
- move {object_id, x_m, lane}: an object is at the wrong distance or lane.
- remove {object_id}: nothing in the imagery supports it.
- add {object: {type, label, x_m, length_m, lanes, action, confidence}}: an obvious hazard the
  imagery shows but the scene lacks.
- relabel {object_id, type, action, confidence}: wrong type / action / confidence.
- set_coverage {from_m, to_m, state}: a footpath range whose state (intact | broken | missing)
  contradicts the imagery.
At most 10 ops. Set accepted=true when no material discrepancy remains (an empty patch_ops with a
short critique is the right answer for a good scene). critique: <= 1200 characters explaining what you
checked and what you changed.
