"""Generate the committed spawner test vectors: ``uv run python -m hero.tools.make_spawner_vectors``.

Writes ``tests/fixtures/portability/spawner_vectors.json`` (named cases, loops 0-2) and ``spawner_vectors_bulk.json``
(50 seeds x 3 streets x loops 0-2). ``build_vectors()``/``build_bulk()`` are pure so tests recompute and compare.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hero.export.gameplay import gameplay_sha256, load_gameplay
from hero.export.gameplay_ref import generate_loops, spawn_input_from_dict

SCHEMA = "hero-spawner-vectors/1"
LOOPS = 3
VECTOR_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "portability"

DEFAULT_W = {"parked_car": 0.2, "two_wheeler": 0.2, "cone": 0.15, "barricade": 0.1, "pothole": 0.15, "stall": 0.1, "low_branch": 0.1}
VEHICLE_W = {"parked_car": 0.5, "two_wheeler": 0.3, "cone": 0.04, "barricade": 0.04, "pothole": 0.04, "stall": 0.04, "low_branch": 0.04}
SLUG = "vector-street"
BLOCKERS = [{"s0": 60.0, "s1": 60.6, "lanes": [0], "action": "dodge"}]
MIXED = [{"s0": 35.5, "s1": 37.5, "lanes": [1], "action": "dodge"}, {"s0": 90.25, "s1": 91.25, "lanes": [2], "action": "jump"},
         {"s0": 120.0, "s1": 121.0, "lanes": [0], "action": "slide"}]


def _case(name, seed, length, density, weights, missing=(), static=(), keepout=()):
    return {"name": name, "input": {
        "slug": SLUG, "seed": seed, "length_m": float(length), "density": float(density), "type_weights": dict(weights),
        "footpath_missing": [list(r) for r in missing], "static": [dict(s) for s in static], "keepout": [list(k) for k in keepout]}}


def _cases() -> list[dict]:
    return [
        _case("plain-100m-seed1", 1, 100.0, 0.5, DEFAULT_W, [[40.0, 52.5]], BLOCKERS, [[78.0, 90.0]]),
        _case("bare-60m-seed1-d0", 1, 60.0, 0.0, DEFAULT_W),
        _case("bare-60m-seed7-d1", 7, 60.0, 1.0, DEFAULT_W),
        _case("bare-100m-seed7-d05", 7, 100.0, 0.5, DEFAULT_W),
        _case("bare-200m-seed424242-d1", 424242, 200.0, 1.0, DEFAULT_W),
        _case("vehicles-200m-seed1-d1", 1, 200.0, 1.0, VEHICLE_W),
        _case("vehicles-100m-seed424242-d05", 424242, 100.0, 0.5, VEHICLE_W),
        _case("missing-footpath-200m-seed7", 7, 200.0, 1.0, DEFAULT_W, [[20.0, 70.0], [110.0, 112.5]]),
        _case("static-mixed-200m-seed424242", 424242, 200.0, 1.0, DEFAULT_W, [], MIXED),
        _case("keepout-100m-seed1-d1", 1, 100.0, 1.0, DEFAULT_W, [], [], [[30.0, 45.0], [60.0, 75.0]]),
        _case("everything-200m-seed7-d1", 7, 200.0, 1.0, VEHICLE_W, [[150.0, 160.0]], MIXED, [[100.0, 108.0]]),
        _case("everything-60m-seed424242-d0", 424242, 60.0, 0.0, DEFAULT_W, [[40.0, 45.0]], BLOCKERS, [[50.0, 52.0]]),
        _case("seed0-150m", 0, 150.0, 0.75, DEFAULT_W, [], BLOCKERS),
        _case("seed-above-2pow32", 4294967297, 150.0, 1.0, DEFAULT_W),
    ]


def _expected(gp, inp: dict) -> list:
    return [s.as_list() for s in generate_loops(gp, spawn_input_from_dict(inp), LOOPS)]


def build_vectors() -> dict:
    gp = load_gameplay()
    cases = _cases()
    for c in cases:
        c["expected"] = _expected(gp, c["input"])
    return {"schema": SCHEMA, "gameplay_sha256": gameplay_sha256(), "loops": LOOPS, "cases": cases}


def build_bulk() -> dict:
    gp = load_gameplay()
    streets = [("bulk-60m", 60.0, DEFAULT_W, [], [], []),
               ("bulk-100m", 100.0, VEHICLE_W, [[40.0, 52.5]], BLOCKERS, [[78.0, 90.0]]),
               ("bulk-200m", 200.0, DEFAULT_W, [[20.0, 70.0]], MIXED, [[140.0, 150.0]])]
    cases = []
    for sname, length, w, miss, st, ko in streets:
        for seed in range(1, 51):
            c = _case(f"{sname}-seed{seed}", seed, length, 0.8, w, miss, st, ko)
            c["expected"] = _expected(gp, c["input"])
            cases.append(c)
    return {"schema": SCHEMA, "gameplay_sha256": gameplay_sha256(), "loops": LOOPS, "cases": cases}


def dumps(doc: dict) -> str:
    """Compact, stable text: one case per line."""
    j = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=True)  # noqa: E731
    head = f'{{"schema":{j(doc["schema"])},"gameplay_sha256":{j(doc["gameplay_sha256"])},"loops":{doc["loops"]},"cases":[\n'
    return head + ",\n".join(j(c) for c in doc["cases"]) + "\n]}\n"


def main() -> int:
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    for name, doc in (("spawner_vectors.json", build_vectors()), ("spawner_vectors_bulk.json", build_bulk())):
        text = dumps(doc)
        (VECTOR_DIR / name).write_text(text, encoding="utf-8")
        print(f"wrote {VECTOR_DIR / name} ({len(text)} bytes, {len(doc['cases'])} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
