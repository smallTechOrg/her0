"""Load + validate ``gameplay.json`` (spec/portability.md section 5) as ``GameplayV1``, with cross-field checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hero.export import GAMEPLAY_JSON
from hero.export.gameplay_types import TYPE_ORDER, GameplayV1


def gameplay_sha256(path: Path | None = None) -> str:
    return hashlib.sha256((path or GAMEPLAY_JSON).read_bytes()).hexdigest()


def validate_gameplay(data: dict) -> GameplayV1:
    """Schema validation (``GameplayV1``) plus the semantic rules the spawner's guarantees rely on."""
    gp = GameplayV1.model_validate(data)
    sp, ot = gp.spawner, gp.obstacle_types
    max_len = max(t.length_m for t in ot.values())
    problems: list[str] = []
    if sp.row_gap_base_m < sp.row_gap_min_m:
        problems.append("row_gap_base_m must be >= row_gap_min_m")
    if sp.row_gap_min_m - max_len <= sp.gap_zone_m:
        problems.append("row_gap_min_m - longest obstacle must exceed gap_zone_m (zones of consecutive rows must not overlap)")
    if sp.free_start_later_m + sp.seam_free_m <= sp.action_gap_m:
        problems.append("free_start_later_m + seam_free_m must exceed action_gap_m (action spacing across the loop seam)")
    for lane, table in sp.lane_weights.items():
        if not any(ot[t].action == "dodge" for t in table):
            problems.append(f"lane {lane} has no dodge type (second-obstacle fallback needs one)")
    for tname, w in sp.default_spawn_hints.type_weights.items():
        if tname not in ot or w < 0:
            problems.append(f"default_spawn_hints.{tname} invalid")
    if sp.window_ahead_m <= sp.window_behind_m:
        problems.append("window_ahead_m must exceed window_behind_m")
    if sp.variants < 2:
        problems.append("variants must be >= 2")
    if problems:
        raise ValueError("invalid gameplay.json: " + "; ".join(problems))
    assert tuple(ot) == TYPE_ORDER
    return gp


def load_gameplay(path: Path | None = None) -> GameplayV1:
    """Parse + validate the shipped ``gameplay.json`` (or ``path``)."""
    return validate_gameplay(json.loads((path or GAMEPLAY_JSON).read_bytes()))
