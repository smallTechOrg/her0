<node:obstacles>
You are a civic infrastructure surveyor reading {{pano_count}} Street View frames taken ALONG one
street segment of {{length_m}} m. Every frame looks forward along the direction of travel (heading
{{heading_deg}} deg), is 640x640 px with a 90 deg field of view, and is labelled with the distance
x_m at which it was taken. The modelled footpath is on the {{footpath_side}} side of the frame
(the "{{footpath_side}}" of the direction of travel).

Lanes of the cross-section (lane index -> where it is in the frame):
- 0 = footpath (the pedestrian way on the {{footpath_side}} side)
- 1 = road edge (the first ~1.6 m of carriageway beside that footpath: kerb, gutter, parking strip)
- 2 = road (the carriageway itself, next to the road edge)

Detect civic obstacles and hazards a pedestrian would meet, ONE entry per physical object:
- type is one of: footpath_gap, parked_vehicle, pothole, open_drain, debris, pole, tree, signboard,
  low_branch, crossing, ramp.
- label: a short human label (e.g. "auto-rickshaw parked on footpath", "uncovered drain").
- pano_index: the 0-based index of the frame where the object is clearest.
- crop_px: [x, y, w, h] pixel box tightly around the object in THAT frame (640x640 space).
- x_m: the frame's x_m plus your estimate of the forward distance to the object (objects near the
  bottom of the frame are ~2-4 m ahead; objects at mid-height are ~8-20 m ahead). Keep x_m within
  [0, {{length_m}}).
- lanes: which lane indices the object occupies (a car half on the footpath -> [0, 1]).
- length_m: how far the object extends along the direction of travel.
- size_m: {w, h, d} in metres (width across, height, depth along).
- action: what a runner must do to pass: dodge (change lane), jump (low obstacle: pothole, drain,
  debris), slide (overhead obstacle: low branch, low signboard), none (crossing / ramp — informational).
- confidence in [0, 1]; keep uncertain detections with a low confidence rather than dropping them.
Also list facades: for each building frontage clearly visible on the footpath side give x_from_m,
x_to_m, its dominant colour as #rrggbb, the pano_index and a crop_px box of the frontage.
notes: one or two sentences (<= 300 chars) on footpath condition and anything the frames show that
the lists cannot express.

Rules: do not invent objects you cannot see in a frame; do not list the same object twice from two
frames (choose the clearest); the same object type repeated along the street is fine; a crossing is a
painted zebra on the carriageway, not a driveway.
