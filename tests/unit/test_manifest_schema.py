"""ManifestV1 (spec/portability.md section 4): the stand-in manifest validates; the committed JSON Schema is current."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hero.export import SCHEMAS_DIR, gameplay_sha256, load_gameplay
from hero.export.manifest_types import SPAWN_TYPES, ManifestV1

BUNDLE = Path(__file__).resolve().parents[1] / "fixtures" / "export" / "contract_curved"


def manifest_dict() -> dict:
    return json.loads((BUNDLE / "street.manifest.json").read_text())


def test_standin_manifest_validates_and_keeps_key_order():
    d = manifest_dict()
    m = ManifestV1.model_validate(d)
    assert list(d) == ["schema", "build", "segment", "frame", "reference_line", "lanes", "cross_section", "frontage", "entities",
                       "colliders", "tokens", "spawn_hints", "interpolated", "loop", "mesh_stats", "attribution", "gameplay",
                       "gameplay_sha256"]
    assert m.schema_ == "hero-street-manifest/1" and m.frame.travel_at_s0 == "-Z"
    assert list(m.dump()) == list(d)
    assert m.gameplay == load_gameplay() and m.gameplay_sha256 == gameplay_sha256()
    assert tuple(m.spawn_hints.type_weights) == SPAWN_TYPES


def test_lanes_cross_section_and_reference_line_are_index_aligned():
    m = ManifestV1.model_validate(manifest_dict())
    assert len(m.reference_line.points) == len(m.lanes.stations) == len(m.cross_section.stations) == 25
    assert m.reference_line.points[0].x == 0.0 and m.reference_line.points[0].z == 0.0
    missing = [st.s for st in m.lanes.stations if st.footpath is None]
    assert missing == [30.0, 32.5, 35.0]  # footpath missing -> lane 0 has no walkable surface
    assert all(st.lane_t[0] > st.lane_t[1] > st.lane_t[2] for st in m.lanes.stations)  # left footpath: t decreasing outward->road


def test_no_dynamic_things_in_colliders_or_entities():
    m = ManifestV1.model_validate(manifest_dict())
    dynamic = {"parked_vehicle", "vendor_stall", "speed_breaker", "pothole", "other"}
    assert not dynamic & {c.type for c in m.colliders}
    assert not dynamic & {e.type for e in m.entities}
    assert any(c.id.startswith("gap-") for c in m.colliders)
    assert {c.action for c in m.colliders} <= {"dodge", "jump", "slide", "none"}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="hero-street-manifest/2"),
    lambda d: d.update(surprise=1),
    lambda d: d["spawn_hints"]["type_weights"].update(cone=0.9),
    lambda d: d["spawn_hints"]["type_weights"].pop("stall"),
    lambda d: d["frontage"]["left"].pop(1),
    lambda d: d["lanes"]["stations"].pop(),
    lambda d: d.update(gameplay_sha256="xyz"),
    lambda d: d["entities"].append(copy.deepcopy(d["entities"][0])),
])
def test_invalid_manifest_is_rejected(mutate):
    d = manifest_dict()
    mutate(d)
    with pytest.raises(ValidationError):
        ManifestV1.model_validate(d)


def test_committed_json_schema_is_current():
    committed = json.loads((SCHEMAS_DIR / "manifest.schema.json").read_text())
    assert committed == ManifestV1.model_json_schema(by_alias=True)
    assert committed["properties"]["schema"]["const"] == "hero-street-manifest/1"
    assert committed["additionalProperties"] is False
