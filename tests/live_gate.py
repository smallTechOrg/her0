"""Phase-2 live gate fixtures and helpers (slice S7), registered globally as a pytest plugin by tests/conftest.py
so ``generated_segment`` resolves from tests/e2e as well as tests/live (one module, one session-scoped ``gate``: never
two real generations). The ``live_server`` override that logs to docs/evidence stays in tests/live/conftest.py.

Original notes: Phase-2 live gate fixtures (slice S7). REAL spend: the orchestrator runs ``uv run pytest tests/live -q -s``.

Session fixtures, in dependency order:

- ``live_server`` (overridden in tests/live/conftest.py): stdout goes to ``docs/evidence/phase2-server.log``.
- ``admin``: an ``httpx.Client`` with the admin password header against ``live_server``.
- ``demo_street``: creates the 100 Feet Road demo street through the address flow (``hero.tools.create_demo_street``),
  saves ``docs/evidence/placement.png`` and asserts the placement criteria BEFORE any generation can start.
- ``gate``: exactly two real full generations (run 1 forward+back, run 2 +left/right) and one free no-op run, with a
  module-level counter (``LLM_RUNS``) that aborts the session if a third LLM-calling run is attempted. Collects
  everything the assertions need and writes ``docs/evidence/phase2-gate.json``.
- ``generated_segment``: the slug of the v2 street after the generations (used by tests/e2e/*_live.py when they are
  run together with tests/live, e.g. ``pytest tests/live tests/e2e -m live``).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from hero.config import PROJECT_ROOT, get_settings
from hero.model import calibrate

EVIDENCE_DIR = PROJECT_ROOT / "docs" / "evidence"
SERVER_LOG = EVIDENCE_DIR / "phase2-server.log"
GATE_JSON = EVIDENCE_DIR / "phase2-gate.json"
PLACEMENT_PNG = EVIDENCE_DIR / "placement.png"
ADMIN_HEADER = "X-Hero-Admin-Password"

RUN1_IMAGERY = {"headings": ["forward", "back"], "sv_spacing_m": 10}
RUN2_IMAGERY = {"headings": ["forward", "back", "left", "right"], "sv_spacing_m": 10}
RUN_CAP_USD = 1.00
TOTAL_CAP_USD = 2.50
RUN_TIMEOUT_S = 15 * 60

# ----------------------------------------------------------------- the "never a third LLM run" counter
MAX_LLM_RUNS = 2
LLM_RUNS = 0


def start_generation(admin: httpx.Client, slug: str, imagery: dict[str, Any], *, llm_expected: bool) -> str:
    """POST generate. ``llm_expected`` runs count against ``MAX_LLM_RUNS``; the third aborts the whole pytest session."""
    global LLM_RUNS
    if llm_expected:
        LLM_RUNS += 1
        if LLM_RUNS > MAX_LLM_RUNS:
            pytest.exit(f"Phase-2 live gate: a {LLM_RUNS}rd LLM-calling run was attempted (max {MAX_LLM_RUNS}); "
                        "aborting the session to protect the real spend.", returncode=3)
    body: dict[str, Any] = {"imagery": imagery}
    resp = admin.post(f"/api/admin/segments/{slug}/generate", json=body)
    assert resp.status_code == 202, f"generate failed: {resp.status_code} {resp.text}"
    return resp.json()["data"]["run_id"]


# ----------------------------------------------------------------- helpers


def data(resp: httpx.Response) -> Any:
    assert resp.status_code == 200, f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text[:500]}"
    body = resp.json()
    assert body.get("ok"), body
    return body["data"]


def mean_bearing_deg(points: list[dict[str, float]]) -> float:
    """Length-weighted circular mean bearing (degrees clockwise from north) of a lat/lon polyline."""
    sx = sy = 0.0
    for a, b in zip(points, points[1:]):
        dy = (b["lat"] - a["lat"]) * 111_320.0
        dx = (b["lon"] - a["lon"]) * 111_320.0 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
        n = math.hypot(dx, dy)
        if n:
            sx += dx
            sy += dy  # summing displacement vectors == length-weighted mean direction
    return math.degrees(math.atan2(sx, sy)) % 360.0


def angle_from_north(bearing: float) -> float:
    # axial: a road running south is as north-aligned as one running north
    b = bearing % 180.0
    return min(b, 180.0 - b)


def wait_run(admin: httpx.Client, run_id: str, timeout_s: float = RUN_TIMEOUT_S) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        detail = data(admin.get(f"/api/admin/runs/{run_id}"))
        step = (detail["steps"] or [{}])[-1].get("name") if detail["steps"] else None
        if (detail["status"], step) != last:
            last = (detail["status"], step)
            print(f"  run {run_id[:8]}: {detail['status']} {step or ''}", flush=True)
        if detail["status"] not in ("queued", "running"):
            return detail
        time.sleep(3)
    raise AssertionError(f"run {run_id} did not finish within {timeout_s:.0f} s")


def snapshot(admin: httpx.Client, slug: str, run_id: str, label: str) -> dict[str, Any]:
    """Everything the assertions need about the state right after ``run_id`` finished."""
    detail = wait_run(admin, run_id)
    assert detail["status"] == "succeeded", f"{label} did not succeed: {detail['status']} {detail.get('error')}"
    report = data(admin.get(f"/api/admin/runs/{run_id}/report"))
    head = data(admin.get(f"/api/admin/segments/{slug}/model"))
    n = head["head_version"]
    version = data(admin.get(f"/api/admin/segments/{slug}/model/versions/{n}"))
    entities = data(admin.get(f"/api/admin/segments/{slug}/model/entities", params={"version": n}))
    observations = data(admin.get(f"/api/admin/segments/{slug}/observations", params={"limit": 1000}))["items"]
    by_type: dict[str, int] = {}
    for e in entities:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    return {"label": label, "run_id": run_id, "detail": detail, "report": report, "head": head, "version": version,
            "entities": entities, "entity_counts": {"total": len(entities), "by_type": by_type},
            "observations": observations,
            "anthropic_usd": detail["cost"]["cost_usd"], "anthropic_calls": len(detail["cost"]["calls"]),
            "google_new": detail["google"]["requests"], "google_cached": detail["google"]["cache_hits"]}


def calibration_table() -> dict[str, Any]:
    """The confidence-calibration and range-weight tables the merge used (``hero.model.calibrate.PARAMS``)."""
    return {
        "params": dict(calibrate.PARAMS),
        "confidence": [{"raw": r, "calibrated": round(calibrate.calibrate_confidence(r), 4)}
                       for r in (0.0, 0.25, 0.5, 0.75, 1.0)],
        "range_weight": [{"range_m": r, "weight": round(calibrate.range_weight(r), 4)} for r in (5, 10, 15, 20, 25, 30, 40)],
        "position_sigma_m": [{"range_m": r, "sigma": round(calibrate.sigma_for_range(r, False), 4)} for r in (5, 10, 20, 30)],
    }


def run_record(snap: dict[str, Any]) -> dict[str, Any]:
    fid = snap["report"]["fidelity"]
    return {
        "run_id": snap["run_id"], "report": snap["report"],
        "anthropic_usd": snap["anthropic_usd"], "anthropic_calls": snap["anthropic_calls"],
        "google": {"new": snap["google_new"], "cached": snap["google_cached"]},
        "fidelity": {"before": fid["before"], "after": fid["after"],
                     "before_to_after_overall": [fid["before"]["overall"], fid["after"]["overall"]]},
        "entity_counts": snap["entity_counts"], "head_version": snap["head"]["head_version"],
    }


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def server_log(live_server: str) -> Path:
    return SERVER_LOG


@pytest.fixture(scope="session")
def admin(live_server: str):
    password = get_settings().admin_password.get_secret_value()
    assert password, "HERO_ADMIN_PASSWORD must be set in .env"
    with httpx.Client(base_url=live_server, timeout=120.0, headers={ADMIN_HEADER: password}) as client:
        yield client


@pytest.fixture(scope="session")
def demo_street(admin: httpx.Client) -> dict[str, Any]:
    """Create the demo street through the address flow and verify the placement BEFORE any generation can run."""
    from hero.tools.create_demo_street import create_demo_street

    result = create_demo_street(admin, out_path=PLACEMENT_PNG, say=lambda m: print(f"[demo street] {m}", flush=True))
    ref = result.placement["reference_line"]
    head = data(admin.get(f"/api/admin/segments/{result.slug}/model"))
    bearing = mean_bearing_deg(head["reference_line"]["points"])
    info = {"slug": result.slug, "created": result.created, "image": str(result.image_path), "reference_line": ref,
            "placement_verified": result.verified, "problems": result.problems, "mean_bearing_deg": round(bearing, 2),
            "angle_from_north_deg": round(angle_from_north(bearing), 2), "head_reference_source": head["reference_line"]["source"]}
    assert result.image_path.exists() and result.image_path.stat().st_size > 10_000, "placement.png missing or tiny"
    assert ref["source"] in ("osm_nominatim", "osm_overpass"), f"reference line source {ref['source']!r} is not OSM road geometry"
    assert ref["snap_m"] <= 25.0, f"line snaps {ref['snap_m']} m from the geocoded centre (max 25 m)"
    assert abs(ref["length_s"] - 100.0) <= 3.0, f"reference length {ref['length_s']} m is not 100 +- 3 m"
    assert angle_from_north(bearing) <= 25.0, f"mean bearing {bearing:.1f} deg is more than 25 deg from north"
    assert not result.problems, result.problems
    return info


@pytest.fixture(scope="session")
def gate(admin: httpx.Client, demo_street: dict[str, Any], server_log: Path):
    """The three runs (2 full generations + 1 free no-op), then ``phase2-gate.json``. Runs once per session."""
    slug = demo_street["slug"]
    print(f"\n[gate] run 1: {RUN1_IMAGERY}", flush=True)
    snap1 = snapshot(admin, slug, start_generation(admin, slug, RUN1_IMAGERY, llm_expected=True), "run1")
    assert snap1["report"]["version_created"], "run 1 produced no model version; not spending on run 2"
    print(f"[gate] run 1 done: ${snap1['anthropic_usd']:.4f}; run 2: {RUN2_IMAGERY}", flush=True)
    snap2 = snapshot(admin, slug, start_generation(admin, slug, RUN2_IMAGERY, llm_expected=True), "run2")
    print(f"[gate] run 2 done: ${snap2['anthropic_usd']:.4f}; run 3 (no-op)", flush=True)
    snap3 = snapshot(admin, slug, start_generation(admin, slug, RUN2_IMAGERY, llm_expected=False), "run3")
    status = data(admin.get("/api/admin/status"))
    total = snap1["anthropic_usd"] + snap2["anthropic_usd"] + snap3["anthropic_usd"]
    out = {
        "slug": slug, "placement": demo_street,
        "run1": run_record(snap1), "run2": run_record(snap2), "run3": run_record(snap3),
        "total_anthropic_usd": round(total, 4), "app_spent_usd": status["spent_usd"],
        "llm_runs": LLM_RUNS, "calibration": calibration_table(),
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    GATE_JSON.write_text(json.dumps(out, indent=2, default=str))
    print(f"[gate] total Anthropic spend ${total:.4f}; evidence -> {GATE_JSON}", flush=True)
    return {"slug": slug, "run1": snap1, "run2": snap2, "run3": snap3, "total_usd": total, "status": status,
            "out": out, "server_log": server_log}


@pytest.fixture(scope="session")
def generated_segment(gate) -> str:
    """Slug of the demo street at model v2 after the two real generations."""
    assert gate["run2"]["head"]["head_version"] == 2
    return gate["slug"]
