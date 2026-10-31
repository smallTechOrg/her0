"""Python reference spawner (spec/portability.md section 7): primitives, committed vectors, determinism, adapter."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from hero.export.gameplay import gameplay_sha256, load_gameplay
from hero.export.gameplay_ref import (Mulberry32, SpawnInput, StaticBlocker, fnv1a32, generate_loop, generate_loops,
                                      spawn_input_from_dict, spawn_input_from_manifest, spawn_input_to_dict, x_mm)
from hero.tools.make_spawner_vectors import build_bulk, build_vectors, dumps

VEC_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "portability"
GP = load_gameplay()
DEFAULT_W = GP.spawner.default_spawn_hints.type_weights


def _inp(**kw) -> SpawnInput:
    base = dict(slug="s", seed=1, length_m=100.0, density=0.5, type_weights=DEFAULT_W)
    base.update(kw)
    return SpawnInput(**base)


# --- primitives -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [("", 0x811C9DC5), ("a", 0xE40C292C), ("foobar", 0xBF9CF968)])
def test_fnv1a32_known_vectors(text, expected):
    assert fnv1a32(text.encode()) == expected


def test_fnv1a32_key_matches_independent_js_value():
    # value cross-checked against an independent JS implementation (Math.imul based)
    assert fnv1a32(b"vector-street|1|0") == 3832741913


def test_mulberry32_matches_independent_js_values_and_range():
    r = Mulberry32(fnv1a32(b"vector-street|1|0"))
    got = [r.next() for _ in range(3)]
    assert got == [0.819844521349296, 0.7175282428506762, 0.556170312454924]
    r = Mulberry32(12345)
    xs = [r.next() for _ in range(5000)]
    assert all(0.0 <= v < 1.0 for v in xs) and 0.45 < sum(xs) / len(xs) < 0.55


def test_mulberry32_seed_is_reduced_to_uint32():
    assert Mulberry32(5).next() == Mulberry32(5 + 2**32).next()


def test_x_mm_rounds_half_up_with_floor_not_python_round():
    assert x_mm(0.0005) == 1 and x_mm(2.5) == 2500 and x_mm(1.0004) == 1000
    assert x_mm(0.0015) == 2  # round() would give banker's/float artefacts; floor(x*1000+0.5) is the rule


# --- committed vectors ------------------------------------------------------------------------------------------

def test_committed_vectors_equal_recomputed():
    committed = json.loads((VEC_DIR / "spawner_vectors.json").read_text())
    assert committed["schema"] == "hero-spawner-vectors/1"
    assert committed["gameplay_sha256"] == gameplay_sha256(), "gameplay.json changed: regenerate with hero.tools.make_spawner_vectors"
    assert committed == json.loads(dumps(build_vectors()))
    assert (VEC_DIR / "spawner_vectors.json").read_text() == dumps(build_vectors())


def test_committed_bulk_vectors_equal_recomputed_and_small():
    path = VEC_DIR / "spawner_vectors_bulk.json"
    committed = json.loads(path.read_text())
    assert committed["gameplay_sha256"] == gameplay_sha256()
    assert path.read_text() == dumps(build_bulk())
    assert path.stat().st_size <= 200_000
    assert len(committed["cases"]) == 150  # 50 seeds x 3 streets


def test_vectors_independently_regenerate_from_inputs_in_file():
    # recompute from the *input* stored in the file (what a port does), not from the generator's case list
    for fname in ("spawner_vectors.json", "spawner_vectors_bulk.json"):
        doc = json.loads((VEC_DIR / fname).read_text())
        for c in doc["cases"]:
            inp = spawn_input_from_dict(c["input"])
            assert [s.as_list() for s in generate_loops(GP, inp, doc["loops"])] == c["expected"], c["name"]


def test_vector_file_covers_the_required_matrix():
    doc = json.loads((VEC_DIR / "spawner_vectors.json").read_text())
    cases = doc["cases"]
    assert len(cases) >= 8
    ins = [c["input"] for c in cases]
    assert {1, 7, 424242} <= {i["seed"] for i in ins}
    assert {60.0, 100.0, 200.0} <= {i["length_m"] for i in ins}
    assert {0.0, 0.5, 1.0} <= {i["density"] for i in ins}
    assert any(i["footpath_missing"] for i in ins) and any(i["static"] for i in ins) and any(i["keepout"] for i in ins)
    assert any(i["type_weights"]["parked_car"] >= 0.5 for i in ins)
    loops_seen = {row[0] for c in cases for row in c["expected"]}
    assert loops_seen == {0, 1, 2}
    types_seen = {row[3] for c in cases for row in c["expected"]}
    assert types_seen == set(GP.obstacle_types)
    assert any(len([r for r in c["expected"] if r[0] == 2 and r[1] == k]) == 2 for c in cases for k in range(40))  # two-obstacle rows


# --- determinism / sensitivity ----------------------------------------------------------------------------------

def test_same_slug_seed_gives_identical_layout_and_loops_are_independent_streams():
    a = generate_loops(GP, _inp(seed=9, length_m=200.0), 4)
    assert a == generate_loops(GP, _inp(seed=9, length_m=200.0), 4)
    # loop k does not depend on earlier loops being generated
    assert generate_loop(GP, _inp(seed=9, length_m=200.0), 3) == [s for s in a if s.loop == 3]


def test_different_seeds_and_slugs_differ():
    layouts = {tuple(s.as_list() for s in generate_loops(GP, _inp(seed=k, length_m=200.0, density=1.0), 3)).__repr__() for k in range(1, 21)}
    assert len(layouts) == 20
    assert generate_loop(GP, _inp(slug="a", length_m=200.0), 1) != generate_loop(GP, _inp(slug="b", length_m=200.0), 1)


def test_density_zero_spawns_fewer_than_density_one():
    n0 = sum(len(generate_loops(GP, _inp(seed=k, length_m=200.0, density=0.0), 5)) for k in range(1, 80))
    n1 = sum(len(generate_loops(GP, _inp(seed=k, length_m=200.0, density=1.0), 5)) for k in range(1, 80))
    assert n1 > n0 * 1.2


def test_difficulty_ramps_with_loop():
    inp = _inp(length_m=200.0, density=1.0)
    per_loop = Counter()
    for k in range(1, 200):
        for s in generate_loops(GP, SpawnInput(**{**inp.__dict__, "seed": k}), 6):
            per_loop[s.loop] += 1
    assert per_loop[0] < per_loop[3] < per_loop[5]


def test_vehicle_heavy_hints_give_vehicle_majority_and_default_does_not():
    heavy = {"parked_car": 0.6, "two_wheeler": 0.34, "cone": 0.01, "barricade": 0.01, "pothole": 0.01, "stall": 0.02, "low_branch": 0.01}
    vehicles = {"parked_car", "two_wheeler"}

    def share(w):
        c = Counter()
        for k in range(1, 150):
            for s in generate_loops(GP, _inp(seed=k, length_m=200.0, density=1.0, type_weights=w), 4):
                c[s.type in vehicles] += 1
        return c[True] / (c[True] + c[False])

    assert share(heavy) > 0.55  # lane 0 cannot hold cars, so the share is capped below 1
    assert share(DEFAULT_W) < share(heavy) - 0.1


def test_zero_weights_missing_keys_behave_as_zero():
    a = generate_loops(GP, _inp(seed=3, length_m=200.0, type_weights={}), 3)
    b = generate_loops(GP, _inp(seed=3, length_m=200.0, type_weights={t: 0.0 for t in GP.obstacle_types}), 3)
    assert a == b and a


# --- geometry rules ---------------------------------------------------------------------------------------------

def test_no_spawn_in_free_start_and_seam_zone_and_inside_short_street():
    for k in range(1, 100):
        for s in generate_loops(GP, _inp(seed=k, length_m=100.0, density=1.0), 3):
            lo = GP.spawner.free_start_loop0_m if s.loop == 0 else GP.spawner.free_start_later_m
            assert lo * 1000 <= s.x_mm <= (100.0 - GP.spawner.seam_free_m) * 1000 + 1
    assert generate_loop(GP, _inp(length_m=30.0), 0) == []  # whole street is the free start


def test_keepout_blocks_everything_inside():
    inp = _inp(seed=2, length_m=200.0, density=1.0, keepout=((0.0, 200.0),))
    assert generate_loops(GP, inp, 5) == []


def test_footpath_missing_everywhere_forbids_lane_zero():
    inp = _inp(length_m=200.0, density=1.0, footpath_missing=((0.0, 200.0),))
    out = [s for k in range(1, 60) for s in generate_loops(GP, SpawnInput(**{**inp.__dict__, "seed": k}), 4)]
    assert out and all(s.lane != 0 for s in out)


def test_footpath_missing_margin_uses_longest_obstacle_length():
    # missing range starts 5 m after x: x + 4.2 (longest) + 2 (margin) = 6.2 > 5 => lane 0 still forbidden there
    inp = _inp(seed=1, length_m=200.0, density=1.0)
    for s in generate_loops(GP, inp, 3):
        x = s.x_mm / 1000.0
        near = SpawnInput(**{**inp.__dict__, "footpath_missing": ((x + 5.0, x + 8.0),)})
        again = [t for t in generate_loop(GP, near, s.loop) if t.row == s.row]
        assert all(t.lane != 0 for t in again)


def test_static_blocker_in_same_lane_is_never_overlapped():
    st = StaticBlocker(50.0, 52.0, (1,), "dodge")
    for k in range(1, 200):
        for s in generate_loops(GP, _inp(seed=k, length_m=200.0, density=1.0, static=(st,)), 4):
            if s.lane == 1:
                ln = GP.obstacle_types[s.type].length_m
                x = s.x_mm / 1000.0
                assert not (x + 0.0005 < 52.0 and x + ln - 0.0005 > 50.0)  # strict overlap forbidden (mm rounding slack)


# --- manifest adapter -------------------------------------------------------------------------------------------

def _manifest_dict():
    stations = [{"s": 2.5 * i, "footpath": None if 4 <= i <= 6 or i == 10 else {"t_lo": 0, "t_hi": 1}} for i in range(13)]
    return {
        "segment": {"slug": "m-street", "length_s": 30.2},
        "lanes": {"stations": stations},
        "colliders": [
            {"type": "pole", "s": 12.2, "length_m": 0.6, "lanes": [0], "action": "dodge"},
            {"type": "crossing", "s": 20.0, "length_m": 3.0, "lanes": [1, 2], "action": "none"},
            {"type": "ramp", "s": 25.0, "length_m": 2.0, "lanes": [0], "action": "none"},
            {"type": "gap", "s": 5.0, "length_m": 2.5, "lanes": [0], "action": "jump"},
        ],
        "spawn_hints": {"density": 0.7, "type_weights": dict(DEFAULT_W)},
    }


def test_spawn_input_from_manifest_merges_ranges_and_derives_static_and_keepout():
    inp = spawn_input_from_manifest(_manifest_dict(), seed=4)
    assert (inp.slug, inp.seed, inp.length_m, inp.density) == ("m-street", 4, 30.2, 0.7)
    assert inp.footpath_missing == ((8.75, 16.25), (23.75, 26.25))  # stations 4,5,6 merge; 10 separate
    assert [(b.s0, b.s1, b.lanes, b.action) for b in inp.static] == [(12.2, 12.2 + 0.6, (0,), "dodge"), (5.0, 7.5, (0,), "jump")]
    assert inp.keepout == ((14.0, 29.0), (19.0, 33.0))


def test_spawn_input_dict_round_trip():
    inp = spawn_input_from_manifest(_manifest_dict(), seed=4)
    assert spawn_input_from_dict(json.loads(json.dumps(spawn_input_to_dict(inp)))) == inp
