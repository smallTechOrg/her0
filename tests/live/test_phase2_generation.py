"""Phase-2 live gate: exactly two real full generations + one free no-op run on the 100 Feet Road demo street, asserting every
Phase-2 success criterion (spec/roadmap.md) and the observability gate. REAL spend (about $0.3-0.6 per generation)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from .conftest import (GATE_JSON, MAX_LLM_RUNS, PLACEMENT_PNG, RUN_CAP_USD, TOTAL_CAP_USD, angle_from_north, data)

pytestmark = pytest.mark.live

OSM_SOURCES = ("osm_nominatim", "osm_overpass")


def _bins(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return snap["version"]["fidelity"]["bins"]


def _share(bins: list[dict[str, Any]], pred) -> float:
    return sum(1 for b in bins if pred(b["level"])) / len(bins)


def _length(snap: dict[str, Any]) -> float:
    return snap["version"]["model"]["reference_line"]["length_s"]


# ---------------------------------------------------------------- placement (before any generation)


def test_placement_criteria_hold_before_generation(demo_street: dict[str, Any]):
    ref = demo_street["reference_line"]
    assert ref["source"] in OSM_SOURCES
    assert demo_street["head_reference_source"] in OSM_SOURCES
    assert ref["snap_m"] <= 25.0
    assert abs(ref["length_s"] - 100.0) <= 3.0
    assert demo_street["angle_from_north_deg"] <= 25.0
    assert demo_street["placement_verified"] is True
    assert PLACEMENT_PNG.exists() and PLACEMENT_PNG.stat().st_size > 10_000  # the orchestrator views it


# ---------------------------------------------------------------- run 1


def test_run1_observations_and_images(gate: dict[str, Any]):
    s = gate["run1"]
    sv = [o for o in s["observations"] if o["kind"] == "streetview"]
    assert len({o["id"] for o in sv}) >= 12, f"only {len(sv)} Street View observations (need >= 12)"
    assert all(o["capture_date"] for o in sv), "every Street View observation needs a capture date"
    kinds = [o["kind"] for o in s["observations"]]
    assert kinds.count("satellite_overview") >= 1
    assert kinds.count("satellite_crop") >= 2
    by_view = s["report"]["images"]["by_view"]
    assert by_view.get("left", 0) == 0 and by_view.get("right", 0) == 0, by_view  # run 1 asked for forward+back only


def test_run1_fidelity_bins_l1_none_l2(gate: dict[str, Any]):
    s = gate["run1"]
    bins = _bins(s)
    assert len(bins) == math.ceil(_length(s) / 5.0 - 1e-9)
    assert _share(bins, lambda lv: lv == 1) >= 0.80, [b["level"] for b in bins]
    assert not any(b["level"] >= 2 for b in bins), "no bin may be L2+ without direct side views"


def test_run1_entities_have_descriptions_and_evidence(gate: dict[str, Any]):
    ents = gate["run1"]["entities"]
    assert len(ents) >= 5, f"only {len(ents)} entities"
    for e in ents:
        assert e["description"].strip(), e
        assert e["evidence_count"] >= 1, e


def test_run1_footpath_strip_covers_half_the_length_with_sane_width(gate: dict[str, Any]):
    model = gate["run1"]["version"]["model"]
    side = model["footpath_side"]
    length = model["reference_line"]["length_s"]
    covered = 0.0
    for iv in model["profile"]:
        for strip in iv["strips"]:
            if strip["kind"] != "footpath" or strip["side"] != side:
                continue
            width = ((strip["t_hi"][0] - strip["t_lo"][0]) + (strip["t_hi"][1] - strip["t_lo"][1])) / 2.0
            if 0.8 <= width <= 5.0:
                covered += iv["s_to"] - iv["s_from"]
    assert covered >= 0.5 * length, f"modelled-side footpath strip covers {covered:.1f} of {length:.1f} m"


def test_run1_cost_within_cap(gate: dict[str, Any]):
    s = gate["run1"]
    assert 0 < s["anthropic_usd"] <= RUN_CAP_USD, s["anthropic_usd"]
    assert s["anthropic_calls"] >= 1
    assert s["report"]["version_created"] and s["report"]["result_version"] == 1
    assert s["report"]["cost"]["anthropic_usd"] == pytest.approx(s["anthropic_usd"], abs=1e-6)


# ---------------------------------------------------------------- run 2


def test_run2_version_parent_and_entity_ids_retained(gate: dict[str, Any]):
    r1, r2 = gate["run1"], gate["run2"]
    assert r2["report"]["version_created"] and r2["report"]["result_version"] == 2
    assert r2["version"]["version"] == 2 and r2["version"]["parent_version"] == 1
    missing = {e["id"] for e in r1["entities"]} - {e["id"] for e in r2["entities"]}
    assert not missing, f"v1 entity ids missing from v2: {sorted(missing)}"


def test_run2_google_requests_hit_the_cache_for_run1_images(gate: dict[str, Any]):
    r1, r2 = gate["run1"], gate["run2"]
    i1, i2 = r1["report"]["images"], r2["report"]["images"]
    assert i2["new"] <= i2["planned"] - i1["new"], (i2, i1)
    assert i2["cached"] >= i1["new"], (i2, i1)
    assert r2["google_new"] == r2["report"]["cost"]["google_new_requests"]


def test_run2_fidelity_rises_to_l2(gate: dict[str, Any], admin):
    r1, r2 = gate["run1"], gate["run2"]
    s1 = r1["version"]["fidelity"]["overall"]["score"]
    s2 = r2["version"]["fidelity"]["overall"]["score"]
    assert s2 - s1 >= 0.5, f"overall fidelity {s1} -> {s2}"
    assert _share(_bins(r2), lambda lv: lv >= 2) >= 0.80, [b["level"] for b in _bins(r2)]
    diff = data(admin.get(f"/api/admin/segments/{gate['slug']}/model/diff", params={"from": 1, "to": 2}))
    assert diff["fidelity"]["improved"], diff["fidelity"]


def test_run2_an_entity_has_two_supporting_observations(gate: dict[str, Any], admin):
    best = 0
    for e in gate["run2"]["entities"]:
        detail = data(admin.get(f"/api/admin/segments/{gate['slug']}/model/entities/{e['id']}", params={"version": 2}))
        support = {x["observation"]["id"] for x in detail["evidence"]
                   if x["role"] == "supports" and x["observation"] is not None}
        best = max(best, len(support))
        if best >= 2:
            break
    assert best >= 2, "no entity has >= 2 supporting observations in v2"


def test_run2_cost_within_cap(gate: dict[str, Any]):
    assert 0 < gate["run2"]["anthropic_usd"] <= RUN_CAP_USD, gate["run2"]["anthropic_usd"]


# ---------------------------------------------------------------- run 3 (no-op) and totals


def test_run3_is_a_free_noop(gate: dict[str, Any]):
    r3 = gate["run3"]
    assert r3["report"]["version_created"] is False
    assert r3["anthropic_usd"] == 0.0 and r3["anthropic_calls"] == 0
    assert r3["report"]["cost"]["anthropic_usd"] == 0.0
    assert r3["google_new"] == 0 and r3["report"]["cost"]["google_new_requests"] == 0
    assert r3["head"]["head_version"] == 2, "a no-op run must leave the model at v2"


def test_total_spend_and_exactly_two_full_generations(gate: dict[str, Any]):
    from tests import live_gate

    total = gate["total_usd"]
    print(f"\n[gate] TOTAL Anthropic spend (app llm_calls): ${total:.4f}")
    assert total <= TOTAL_CAP_USD, total
    assert gate["status"]["spent_usd"] == pytest.approx(total, abs=1e-4), "app-wide llm_calls total differs from the 3 runs"
    assert live_gate.LLM_RUNS == MAX_LLM_RUNS == 2
    full = [k for k in ("run1", "run2", "run3") if gate[k]["anthropic_calls"] > 0]
    assert full == ["run1", "run2"], full


def test_evidence_json_written(gate: dict[str, Any]):
    assert GATE_JSON.exists()
    doc = json.loads(GATE_JSON.read_text())
    for key in ("run1", "run2", "run3"):
        run = doc[key]
        assert {"report", "anthropic_usd", "google", "fidelity", "entity_counts"} <= set(run)
        assert {"new", "cached"} <= set(run["google"])
        assert {"before", "after"} <= set(run["fidelity"])
    assert doc["calibration"]["params"] and doc["calibration"]["confidence"]
    assert doc["total_anthropic_usd"] <= TOTAL_CAP_USD
    assert angle_from_north(doc["placement"]["mean_bearing_deg"]) <= 25.0


# ---------------------------------------------------------------- observability gate


def _events(path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def test_server_log_has_the_observability_events(gate: dict[str, Any]):
    events = _events(gate["server_log"])
    names = {}
    for e in events:
        names.setdefault(e.get("event"), []).append(e)
    assert names.get("pipeline.step"), "no pipeline.step line in the server log"
    llm = names.get("llm.call")
    assert llm, "no llm.call line in the server log"
    assert all("cost_usd" in e for e in llm), "an llm.call line lacks cost_usd"
    assert names.get("model.merge"), "no model.merge line in the server log"
    assert names.get("model.published"), "no model.published line in the server log"
    # the logged LLM spend is the app's llm_calls spend
    assert sum(e["cost_usd"] for e in llm) == pytest.approx(gate["total_usd"], abs=1e-4)
    assert len(names["model.published"]) >= 2

