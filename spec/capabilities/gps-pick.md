# Capability: GPS Pick *(Phase 2)*

## What It Does
A player taps "Use my location" and is taken to the nearest already-generated street within
2 km of where they are, or told there is none yet.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Device position | lat/lon | browser geolocation (user permission) | yes |
| Ready segments | list | DB | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Nearest segment + distance | record | gallery → play |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Browser geolocation | one position fix (10 s timeout) | denied/timeout → "Location unavailable — pick a street below" |
| Public API | `GET /api/segments/nearest` | error → retry button |

## Business Rules
- Distance is great-circle distance to the segment midpoint; radius 2000 m
  (> **Assumed:** generous because segments are sparse in a prototype).
- Only `ready` segments qualify.
- Phase 1 ships the button as a labelled stub.

## Success Criteria
- [ ] `nearest` for a point 50 m from the seed midpoint returns the seed with `distance_m < 100`; for a point 5 km away returns `not_found` with the message "No street near you yet."
- [ ] Headless browser with a mocked geolocation at the seed midpoint: tapping "Use my location" lands on `/play/?segment=<seed slug>`; with geolocation denied the gallery shows "Location unavailable".
