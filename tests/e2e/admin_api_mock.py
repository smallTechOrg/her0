"""Deterministic admin API for the phone-viewport admin e2e tests.

The browser loads the REAL static frontend from ``live_server``; every ``/api/**`` call and every ``/media/**`` image
is fulfilled here with JSON shaped exactly per ``spec/api.md`` / ``spec/data.md`` (envelope ``{ok, data}``), so the
tests pass independently of the model/admin backend slices. ``MockApi.requests`` records ``(method, path, body)``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TILE = (Path(__file__).parent / "fixtures" / "tile_1280.png").read_bytes()
SLUG = "fixture-curved-street"
RUN_ID = "5a1f0000-0000-4000-8000-000000000002"
NOOP_RUN_ID = "5a1f0000-0000-4000-8000-000000000003"
LENGTH_S = 100.2
PASSWORD = "e2e-pass"
IMG_W, IMG_H = 640, 480


def _env(data):
    return {"ok": True, "data": data}


def _err(code, message, detail=None):
    return {"ok": False, "error": {"code": code, "message": message, "detail": detail}}


def obs(i, kind, view, station, new_in=1, fused=1, zoom=None):
    return {
        "id": f"obs-{i:03d}", "kind": kind, "source": "google_streetview" if kind == "streetview" else "google_static_maps",
        "url": f"/media/imagery/obs-{i:03d}.jpg?v=1", "lat": 12.9711, "lon": 77.6413, "heading_deg": 2.0, "pitch_deg": 0.0,
        "fov_deg": 70, "zoom": zoom, "view": view, "station_s": station, "offset_t": 0.0, "pano_id": f"pano{i}",
        "capture_date": "2023-04", "cost_usd": 0.007, "created_at": "2026-10-30T11:00:00Z", "fused_in_version": fused,
        "width_px": IMG_W, "height_px": IMG_H,
    }


def build_observations():
    out = [obs(0, "satellite_overview", None, 50.0, zoom=19)]
    i = 1
    for st in range(0, 101, 10):
        for view in ("forward", "back", "left", "right"):
            out.append(obs(i, "streetview", view, float(st), fused=1 if st < 60 else 2))
            i += 1
    out.append(obs(i, "streetview", "forward", 100.0, fused=None))  # an unfused one
    return list(reversed(out))


def build_bins(level_v2: bool):
    n = math.ceil(LENGTH_S / 5)
    bins = []
    for k in range(n):
        lo, hi = k * 5.0, min((k + 1) * 5.0, LENGTH_S)
        if level_v2:
            level = 3 if k < 5 else 2 if k < 18 else 1
        else:
            level = 0 if k < 2 else 1
        bins.append({
            "index": k, "s_from": lo, "s_to": round(hi, 1), "level": level, "views": 4 if level else 0, "stations": 2 if level > 1 else level,
            "sides": {"left": 1, "right": 1}, "overhead_hires": level > 0,
            "hint": f"Add a Street View frame facing left or right from another station within 15 m (bin {k}).",
        })
    hist = {"L0": 0.0, "L1": 0.0, "L2": 0.0, "L3": 0.0}
    for b in bins:
        hist[f"L{b['level']}"] += b["s_to"] - b["s_from"]
    score = round(sum(b["level"] * (b["s_to"] - b["s_from"]) for b in bins) / LENGTH_S, 2)
    return {"bin_m": 5.0, "length_s": LENGTH_S, "bins": bins,
            "overall": {"level": math.floor(score), "score": score, "histogram_m": {k: round(v, 1) for k, v in hist.items()}}}


TYPES = ["parked_vehicle", "tree", "pole", "drain", "bollard", "pothole", "signboard"]


def build_entities():
    out = []
    for k, t in enumerate(TYPES):
        out.append({"id": f"ent-{k:010d}", "type": t, "subtype": "auto-rickshaw" if k == 0 else None,
                    "state": "removed" if t == "signboard" else ("disputed" if t == "pole" else "present"),
                    "s": 8.0 + 13 * k, "t": 5.9 if k % 2 == 0 else -4.0, "confidence": round(0.9 - 0.1 * k, 2),
                    "description": f"A {t.replace('_', ' ')} about {8 + 13 * k} m from the start.", "evidence_count": 2 + (k % 3),
                    "first_seen_version": 1, "last_changed_version": 2})
    return out


def build_entity_detail(eid):
    ent = next(e for e in build_entities() if e["id"] == eid)
    evidence = [{
        "role": "supports", "weight": 0.9, "crop_px": [160, 120, 320, 240], "note": None, "added_in_version": 1,
        "observation": {"id": f"obs-{n:03d}", "kind": "streetview", "view": "forward", "url": f"/media/imagery/obs-{n:03d}.jpg?v=1",
                        "station_s": 10.0 * n, "heading_deg": 2.0, "capture_date": "2023-04", "width_px": IMG_W, "height_px": IMG_H},
    } for n in (1, 2)]
    if ent["state"] == "removed":
        evidence.append({"role": "contradicts", "weight": 0.5, "crop_px": None, "note": "visible region, no detection", "added_in_version": 2,
                         "observation": {"id": "obs-009", "kind": "streetview", "view": "back", "url": "/media/imagery/obs-009.jpg?v=1",
                                         "station_s": 30.0, "heading_deg": 182.0, "capture_date": "2023-04", "width_px": IMG_W, "height_px": IMG_H}})
    return {
        "entity": {**{k: v for k, v in ent.items() if k not in ("s", "t", "evidence_count")},
                   "position": {"s": ent["s"], "t": ent["t"]}, "attributes": {"colour": "yellow", "blocks_footpath": True}},
        "evidence": evidence,
        "contradictions": [{"id": "ctr-1", "kind": "presence_conflict", "entity_ids": [eid], "description": "Seen from the left, not from the right.",
                            "resolved_in_version": None, "opened_in_version": 2}] if ent["type"] == "parked_vehicle" else [],
        "history": [{"version": 1, "confidence": 0.7, "state": "present", "position": {"s": ent["s"], "t": ent["t"]}},
                    {"version": 2, "confidence": ent["confidence"], "state": ent["state"], "position": {"s": ent["s"], "t": ent["t"]}}],
    }


def build_diff(a, b):
    return {
        "from_version": a, "to_version": b, "is_empty": False,
        "entities": {
            "added": [{"id": "ent-0000000099", "type": "bollard", "s": 41.2, "t": 5.5, "confidence": 0.58, "description": "A white bollard by the kerb."}],
            "removed": [{"id": "ent-0000000006", "type": "signboard", "s": 86.0, "t": 6.0, "reason": "not seen in 2 later views; confidence 0.62 → 0.12"}],
            "revived": [],
            "changed": [{"id": "ent-0000000000", "fields": {"confidence": [0.74, 0.86], "position": [[12.4, 5.8], [12.5, 5.9]], "attributes": {"colour": ["white", "yellow"]}},
                         "reason": "2 more frames supported it"}],
            "unchanged": 4,
        },
        "profile": {"changed_intervals": [{"s_from": 22.0, "s_to": 31.0, "what": "footpath_state broken → missing"}]},
        "buildings": {"added": [], "changed": [], "removed": []},
        "fidelity": {"improved": [{"s_from": 0.0, "s_to": 100.2, "from": "L1", "to": "L2"}], "unchanged_len_m": 0.0, "overall": [1.0, 2.0]},
        "contradictions": {"opened": [], "resolved": []},
        "observations": {"fused_added": 22},
        "summary_lines": ["22 new photos fused (22 Street View)", "1 new object: 1 bollard", "Fidelity L1 → L2 over 100 m"],
    }


def build_model(legacy=False, head=2):
    versions = [
        {"version": 1, "kind": "fused", "parent_version": None, "run_id": "5a1f0000-0000-4000-8000-000000000001", "created_at": "2026-10-29T10:00:00Z",
         "entity_count": 6, "observation_count": 22, "fidelity_level": 1, "summary": "First fusion: 6 objects from 22 photos."},
        {"version": 2, "kind": "fused", "parent_version": 1, "run_id": RUN_ID, "created_at": "2026-10-30T11:02:11Z",
         "entity_count": 7, "observation_count": 44, "fidelity_level": 2, "summary": "Added 22 Street View photos; 1 new object; fidelity L1 → L2."},
    ][:head]
    pts = [{"lat": 12.97066 + 0.00045 * i, "lon": 77.6413 + 0.00001 * i, "s": round(i * 50.1, 1)} for i in range(3)]
    return {
        "slug": SLUG, "head_version": head, "legacy": legacy, "placement_verified": not legacy,
        "reference_line": {"source": "legacy_straight" if legacy else "osm_nominatim", "osm_way_id": None if legacy else 25173540,
                           "name": None if legacy else "100 Feet Road", "length_s": LENGTH_S, "points": pts},
        "fidelity": build_bins(head == 2),
        "counts": {"entities": 7, "removed": 1, "buildings": 3, "observations": 48, "fused_observations": 44},
        "versions": versions,
    }


def build_run(run_id=RUN_ID, noop=False):
    used = [{"id": f"obs-{i:03d}", "kind": "streetview", "view": "forward", "url": f"/media/imagery/obs-{i:03d}.jpg?v=1",
             "station_s": 10.0 * i, "capture_date": "2023-04", "new": i % 2 == 0} for i in range(1, 7)]
    return {
        "id": run_id, "segment": {"slug": SLUG, "name": "100 Feet Road, Indiranagar (fixture)"}, "status": "succeeded",
        "started_at": "2026-10-30T11:00:00Z", "finished_at": "2026-10-30T11:02:51Z", "inputs": {},
        "steps": [{"name": "plan_imagery", "status": "ok", "latency_ms": 12, "detail": "48 planned"},
                  {"name": "fuse", "status": "ok", "latency_ms": 4100, "detail": "merged"}],
        "imagery": {"satellite": None, "streetview": [], "streetview_status": "ok", "osm_available": True, "streetview_available": True,
                    "warnings": [], "observations": used},
        "layout": None, "obstacles": None, "iterations": [], "scene": None, "preview_url": None, "thumbnail_url": None,
        "cost": {"input_tokens": 51200, "output_tokens": 7400, "cost_usd": 0.0 if noop else 0.27, "calls": []},
        "google": {"requests": 0 if noop else 22, "cache_hits": 48 if noop else 26}, "error": None,
        "budget": {"spent_total_usd": 0.7, "budget_usd": 50.0},
        "mode": "generate", "base_version": 2 if noop else 1, "result_version": 2, "version_created": not noop, "report_available": True,
    }


def build_report(run_id=RUN_ID, noop=False):
    new = 0 if noop else 22
    return {
        "run_id": run_id, "segment_slug": SLUG, "mode": "generate", "base_version": 2 if noop else 1, "result_version": 2,
        "version_created": not noop, "started_at": "2026-10-30T11:00:00Z", "finished_at": "2026-10-30T11:02:51Z", "duration_s": 171.4,
        "images": {"planned": 48, "used_total": 48, "new": new, "cached": 48 - new, "fused_this_run": new,
                   "by_source": {"google_streetview": 44, "google_static_maps": 4, "upload": 0},
                   "by_kind": {"satellite_overview": 1, "satellite_crop": 3, "streetview": 44, "user_photo": 0},
                   "by_view": {"forward": 11, "back": 11, "left": 11, "right": 11}, "new_observation_ids": [], "skipped": []},
        "detections": {"new": [] if noop else [{"id": "ent-0000000099", "type": "bollard", "description": "A white bollard by the kerb.", "confidence": 0.58}],
                       "changed": [] if noop else [{"id": "ent-0000000000", "what": "confidence 0.74 → 0.86"}],
                       "removed": [], "contradicted": [], "unchanged_count": 7 if noop else 5},
        "profile_changes": [] if noop else ["22–31 m: footpath now 'missing' (was 'broken'), 4 frames agree"],
        "fidelity": {"before": {"overall": 2.0 if noop else 1.0, "histogram_m": {"L2": 100.2} if noop else {"L1": 100.2}},
                     "after": {"overall": 2.0, "histogram_m": {"L2": 100.2}},
                     "improved_intervals": [] if noop else [{"s_from": 0.0, "s_to": 100.2, "from": "L1", "to": "L2"}]},
        "confidence_changes": [] if noop else [{"entity_id": "ent-0000000000", "type": "parked_vehicle", "before": 0.74, "after": 0.86, "reason": "2 more supporting frames"}],
        "cost": {"anthropic_usd": 0.0 if noop else 0.27, "anthropic_calls": 0 if noop else 4, "input_tokens": 51200, "output_tokens": 7400,
                 "google_new_requests": new, "google_cached": 48 - new, "google_est_usd": 0.154 if not noop else 0.0,
                 "total_usd": 0.0 if noop else 0.424, "budget_remaining_usd": 49.2},
        "warnings": [], "degraded": False,
        "summary": ("Nothing new to fuse: all 48 images were already cached and fused. The model stays at v2." if noop
                    else "Run added 22 new Street View photos (26 of 48 images were already cached) and published model v2."),
        "suggestions": ["Metres 60–75 are the least certain: a close-up photo there would raise them to L3."],
    }


STATUS = {
    "budget_usd": 50.0, "spent_usd": 0.7, "remaining_usd": 49.3,
    "runs": {"total": 2, "succeeded": 2, "failed": 0, "failed_budget": 0}, "google": {"requests": 70, "cache_hits": 26, "est_cost_usd": 0.4},
    "models": {"vision": "claude-sonnet-5-5", "review": "claude-sonnet-5-5"}, "streetview_available_last": True,
    "seed_segment": {"name": "Seed", "city": "Bengaluru", "start": {"lat": 12.97, "lon": 77.64}, "end": {"lat": 12.971, "lon": 77.641}, "footpath_side": "left"},
    "active_run_id": None,
    "limits": {"max_images_per_run": 60, "sv_spacing_m": 10, "headings": ["forward", "back", "left", "right"],
               "sat_crop_zoom": 20, "sat_crop_spacing_m": 40, "batch_images": 8, "structured_mode": "json"},
}


class MockApi:
    """Playwright route handler for ``**/api/**`` and ``**/media/**``."""

    def __init__(self, *, legacy=False, head=2, active_run_id=None, post_status=None, entity_status=None, empty_model=False):
        self.requests: list[tuple[str, str, object]] = []
        self.legacy, self.head, self.active_run_id = legacy, head, active_run_id
        self.post_status = post_status or {}  # path suffix -> (status, body) override for POSTs
        self.entity_status = entity_status
        self.empty_model = empty_model
        self.observations = build_observations()

    def install(self, page):
        page.route("**/media/**", lambda r: r.fulfill(status=200, content_type="image/png", body=TILE))
        page.route("**/api/**", self._handle)

    def posts(self, suffix):
        return [b for (m, p, b) in self.requests if m == "POST" and p.endswith(suffix)]

    def _json(self, route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _handle(self, route):
        req = route.request
        u = urlparse(req.url)
        path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        body = None
        if req.method in ("POST", "PUT") and req.post_data:
            try:
                body = json.loads(req.post_data)
            except ValueError:
                body = req.post_data
        self.requests.append((req.method, path + (("?" + u.query) if u.query else ""), body))
        if req.headers.get("x-hero-admin-password") != PASSWORD and path.startswith("/api/admin"):
            return self._json(route, _err("unauthorized", "Wrong or missing admin password."), 401)
        for suffix, (st, b) in self.post_status.items():
            if req.method == "POST" and path.endswith(suffix):
                return self._json(route, b, st)
        prefix = f"/api/admin/segments/{SLUG}"
        if path == "/api/admin/status":
            return self._json(route, _env({**STATUS, "active_run_id": self.active_run_id}))
        if path == "/api/admin/segments":
            seg = {"slug": SLUG, "name": "100 Feet Road, Indiranagar (fixture)", "city": "Bengaluru", "length_m": LENGTH_S, "status": "ready",
                   "quality": "full", "walkability": None, "latest_run_id": RUN_ID,
                   "latest_run": {"id": RUN_ID, "status": "succeeded", "cost_usd": 0.27, "finished_at": "2026-10-30T11:02:51Z"},
                   "model": {"head_version": self.head, "fidelity_level": "L2" if self.head == 2 else "L1", "legacy": self.legacy, "photos": 48, "placement_verified": not self.legacy}}
            return self._json(route, _env([seg]))
        if path == "/api/admin/runs":
            return self._json(route, _env([{"id": RUN_ID, "segment": {"slug": SLUG, "name": "Fixture"}, "status": "succeeded", "cost_usd": 0.27,
                                            "started_at": "2026-10-30T11:00:00Z", "finished_at": "2026-10-30T11:02:51Z", "mode": "generate", "version_created": True}]))
        if path == f"/api/admin/runs/{RUN_ID}":
            return self._json(route, _env(build_run()))
        if path == f"/api/admin/runs/{NOOP_RUN_ID}":
            return self._json(route, _env(build_run(NOOP_RUN_ID, noop=True)))
        if path.endswith("/report") and path.startswith("/api/admin/runs/"):
            rid = path.split("/")[4]
            return self._json(route, _env(build_report(rid, noop=rid == NOOP_RUN_ID)))
        if path == f"{prefix}/model":
            m = build_model(self.legacy, self.head)
            if self.empty_model:
                m.update(head_version=None, versions=[], reference_line=None, fidelity={"bin_m": 5.0, "length_s": LENGTH_S, "bins": [], "overall": None},
                         counts={"entities": 0, "removed": 0, "buildings": 0, "observations": 0, "fused_observations": 0})
            return self._json(route, _env(m))
        if path.startswith(f"{prefix}/model/versions/"):
            n = int(path.rsplit("/", 1)[1])
            return self._json(route, _env({"version": n, "kind": "fused", "parent_version": n - 1, "run_id": RUN_ID, "created_at": "2026-10-30T11:02:11Z",
                                           "summary": "x", "model": {"profile": [
                                               {"id": "pi-0", "s_from": 0.0, "s_to": 22.0, "footpath_state": "intact"},
                                               {"id": "pi-1", "s_from": 22.0, "s_to": 31.0, "footpath_state": "broken"},
                                               {"id": "pi-2", "s_from": 31.0, "s_to": LENGTH_S, "footpath_state": "blocked"}]},
                                           "fidelity": build_bins(n == 2), "diff": None}))
        if path == f"{prefix}/model/diff":
            return self._json(route, _env(build_diff(int(q["from"]), int(q["to"]))))
        if path == f"{prefix}/model/coverage":
            cov = build_bins(int(q.get("version", self.head)) == 2)
            by_bin = [{"index": b["index"], "observation_ids": [f"obs-{b['index'] % 40 + 1:03d}"] if b["level"] else []} for b in cov["bins"]]
            return self._json(route, _env({**cov, "observations_by_bin": by_bin}))
        if path == f"{prefix}/model/entities":
            if self.entity_status:
                return self._json(route, _err("internal_error", "boom"), self.entity_status)
            return self._json(route, _env(build_entities()))
        if path.startswith(f"{prefix}/model/entities/"):
            return self._json(route, _env(build_entity_detail(path.rsplit("/", 1)[1])))
        if path == f"{prefix}/observations":
            return self._json(route, _env({"total": len(self.observations), "items": self.observations}))
        if path == f"{prefix}/placement-preview":
            return self._json(route, _env({"url": "/media/previews/placement.png?v=1", "placement_verified": True, "warnings": [],
                                           "reference_line": {"source": "osm_nominatim", "osm_way_id": 25173540, "name": "100 Feet Road",
                                                              "length_s": LENGTH_S, "extrapolated_m": {"start": 0.0, "end": 0.0}, "snap_m": 3.0}}))
        if path == f"{prefix}/imagery" and req.method == "POST":
            return self._json(route, _env({"run_id": RUN_ID, "status": "queued", "mode": "acquire"}), 202)
        if path == f"{prefix}/generate" and req.method == "POST":
            return self._json(route, _env({"run_id": RUN_ID, "status": "queued", "mode": "generate"}), 202)
        return self._json(route, _err("not_found", f"mock has no route for {req.method} {path}"), 404)
