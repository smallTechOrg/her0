# Capability: Seed City *(Phase 2)*

## What It Does
The admin seeds one city with 5–20 curated segments from a checked-in seed list and generates
the pending ones serially, watching the running spend, with a hard stop at the budget.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Seed list (name, city, endpoints, footpath side) | file in the project | repo (`src/hero/seeds/bengaluru.json`, 8 segments) | yes |
| Admin password | header | admin UI | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Created `draft` segments | rows | DB |
| One pipeline run per pending segment, sequential | runs | `street-generation` |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| (via `street-generation`) | each segment generated as in Phase 1 | a failed segment is marked `failed` and the batch continues; `failed_budget` stops the batch |

## Business Rules
- Seeding is idempotent: existing slugs are skipped and reported as `existing`.
- "Generate all pending" processes `draft` and `failed` segments one at a time, oldest first;
  the admin page shows which is running and the running total.
- The batch stops at the first `failed_budget`.
- > **Assumed:** seed list = 8 Bengaluru segments around Indiranagar / Koramangala / MG Road
  chosen for Street View coverage; exact endpoints are recorded in the seed file by the
  Phase 2 builder after checking Street View metadata for each start point.

## Success Criteria
- [ ] `POST /api/admin/seed` twice returns `created = 8` then `created = 0, existing = 8`.
- [ ] Generate-pending with a test budget allowing exactly one run produces one `succeeded` run and the batch halts with the remaining segments still `draft`; the admin status shows `remaining_usd ≤ 0`-driven stop reason.
- [ ] Gallery lists every `ready` segment with thumbnails after the batch.
