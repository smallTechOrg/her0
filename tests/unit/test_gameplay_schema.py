"""gameplay.json (spec/portability.md section 5) validates against GameplayV1 and its committed JSON Schema is current."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from hero.export import GAMEPLAY_JSON, SCHEMAS_DIR, gameplay_sha256, load_gameplay
from hero.export.gameplay_types import TYPE_ORDER, GameplayV1


def test_gameplay_json_validates_and_has_the_spec_values():
    gp = GameplayV1.model_validate(load_gameplay())
    assert gp.schema_ == "hero-gameplay/1" and gp.version == 1
    assert (gp.runner.hit_damage, gp.runner.health_max, gp.runner.lane_tween_ms) == (10, 100, 150)
    assert (gp.runner.speed_start_mps, gp.runner.speed_max_mps) == (6.0, 12.0)
    assert tuple(gp.obstacle_types) == TYPE_ORDER
    assert gp.obstacle_types["pothole"].action == "jump" and gp.obstacle_types["low_branch"].action == "slide"
    assert gp.spawner.row_gap_min_m == 14.0 and gp.spawner.prng == "mulberry32" and gp.spawner.hash == "fnv1a32"
    assert sum(gp.spawner.default_spawn_hints.type_weights.values()) == pytest.approx(1.0)


def test_gameplay_sha256_is_of_the_file_bytes():
    assert gameplay_sha256() == hashlib.sha256(GAMEPLAY_JSON.read_bytes()).hexdigest()


def test_round_trip_keeps_wire_keys():
    gp = GameplayV1.model_validate(load_gameplay())
    dumped = json.loads(gp.model_dump_json(by_alias=True))
    assert dumped == load_gameplay()


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="hero-gameplay/2"),
    lambda d: d.update(extra_key=1),
    lambda d: d["obstacle_types"].pop("cone"),
    lambda d: d["runner"].update(speed_max_mps=1.0),
    lambda d: d["spawner"]["default_spawn_hints"]["type_weights"].update(cone=0.9),
    lambda d: d["spawner"]["lane_weights"]["0"].update(cone=-1.0),
    lambda d: d["lanes"].update(count=4),
])
def test_invalid_gameplay_is_rejected(mutate):
    d = copy.deepcopy(load_gameplay())
    mutate(d)
    with pytest.raises(ValidationError):
        GameplayV1.model_validate(d)


def test_committed_json_schema_is_current():
    committed = json.loads((SCHEMAS_DIR / "gameplay.schema.json").read_text())
    assert committed == GameplayV1.model_json_schema(by_alias=True)
    assert "schema" in committed["properties"] and committed["additionalProperties"] is False
