"""``hero.export.gameplay`` loader + semantic validation of gameplay.json (spec/portability.md section 5)."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from hero.export import GAMEPLAY_JSON
from hero.export import gameplay_sha256 as pkg_sha
from hero.export.gameplay import gameplay_sha256, load_gameplay, validate_gameplay
from hero.export.gameplay_types import TYPE_ORDER, GameplayV1


def _raw() -> dict:
    return json.loads(GAMEPLAY_JSON.read_bytes())


def test_load_returns_validated_model_with_spawner_values():
    gp = load_gameplay()
    assert isinstance(gp, GameplayV1)
    assert tuple(gp.obstacle_types) == TYPE_ORDER
    assert gp.spawner.row_gap_min_m == 14.0 and gp.spawner.variants == 6 and gp.spawner.type_boost == 3.0
    assert gp.spawner.lane_weights["0"]["low_branch"] == 1.5
    assert "pothole" not in gp.spawner.lane_weights["0"]  # footpath never gets a pothole


def test_sha256_matches_file_bytes_and_package_helper():
    assert gameplay_sha256() == hashlib.sha256(GAMEPLAY_JSON.read_bytes()).hexdigest() == pkg_sha()


def test_load_from_explicit_path(tmp_path):
    p = tmp_path / "gp.json"
    d = _raw()
    d["spawner"]["variants"] = 4
    p.write_text(json.dumps(d))
    assert load_gameplay(p).spawner.variants == 4
    assert gameplay_sha256(p) != gameplay_sha256()


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["spawner"].update(row_gap_base_m=10.0), "row_gap_base_m"),
    (lambda d: d["spawner"].update(row_gap_min_m=9.0, row_gap_base_m=26.0), "gap_zone_m"),
    (lambda d: d["spawner"].update(free_start_later_m=0.0, seam_free_m=1.0), "action_gap_m"),
    (lambda d: d["spawner"].update(window_ahead_m=5.0), "window_ahead_m"),
    (lambda d: d["spawner"].update(variants=1), "variants"),
    (lambda d: d["spawner"]["lane_weights"].update({"0": {"low_branch": 1.5}}), "no dodge type"),
])
def test_semantic_rules_reject_bad_files(mutate, needle):
    d = copy.deepcopy(_raw())
    mutate(d)
    with pytest.raises(ValueError, match=needle):
        validate_gameplay(d)


def test_schema_errors_still_surface():
    d = _raw()
    d["spawner"]["prng"] = "xorshift"
    with pytest.raises(ValueError):
        validate_gameplay(d)
    d = _raw()
    d["surprise"] = 1
    with pytest.raises(ValueError):
        validate_gameplay(d)


def test_shipped_file_is_semantically_valid_and_guarantees_hold_numerically():
    gp = load_gameplay()
    sp = gp.spawner
    max_len = max(t.length_m for t in gp.obstacle_types.values())
    # consecutive rows are >= row_gap_min apart start-to-start; their +-gap_zone windows must not overlap
    assert sp.row_gap_min_m - max_len > sp.gap_zone_m
    # reachability at top speed: 14 m gap / (12 m/s * 0.5 s) -> >= 2 lanes of travel
    assert 1 + int(sp.row_gap_min_m // (gp.runner.speed_max_mps * 0.5)) >= 2
    # max spawn probability stays a probability
    assert sp.spawn_p_max * (sp.density_floor + sp.density_gain) <= 1.0
