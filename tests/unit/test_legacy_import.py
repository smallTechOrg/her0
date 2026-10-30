"""hero.model.legacy.import_legacy: Phase-1 scene (v1) -> canonical model v0 (kind legacy)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hero.model.compile import compile_scene
from hero.model.fidelity import compute_fidelity
from hero.model.legacy import import_legacy
from hero.model.types import CanonicalModel, EntityType, FootpathState, ModelKind
from hero.scene.schema import Scene
from tests.fixtures.street_fixtures import FIXED_NOW

EXAMPLE = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "scene_example.json"
SLUG = "100-feet-road-indiranagar-w"


@pytest.fixture()
def scene() -> Scene:
    return Scene.model_validate(json.loads(EXAMPLE.read_text()))


@pytest.fixture()
def model(scene) -> CanonicalModel:
    return import_legacy(scene, segment_slug=SLUG, footpath_side="left", now=FIXED_NOW)


def test_v0_kind_legacy_flags(model):
    assert model.kind is ModelKind.legacy and model.legacy and model.version == 0 and model.parent_version is None
    assert not model.placement_verified and model.run_id is None and model.created_at == FIXED_NOW
    assert model.reference_line.source == "legacy_straight" and model.segment_slug == SLUG
    assert model.fused_observation_ids == [] and model.contradictions == []
    assert model.reference_line.length_s == pytest.approx(74.6, abs=0.5)


def test_every_fidelity_bin_is_l0(model):
    fm = model.fidelity
    assert fm is not None and {b.level for b in fm.bins} == {0} and fm.overall.level == 0
    assert len(fm.bins) == math.ceil(model.reference_line.length_s / 5.0)
    assert fm == compute_fidelity(model, {})


def test_profile_tiles_the_line_with_the_v1_coverage_states(model):
    assert model.profile[0].s_from == 0.0 and model.profile[-1].s_to == pytest.approx(model.reference_line.length_s)
    assert all(a.s_to == pytest.approx(b.s_from) for a, b in zip(model.profile, model.profile[1:]))
    assert [(iv.s_from, iv.s_to, iv.footpath_state) for iv in model.profile][:2] == [
        (0.0, 22.0, FootpathState.intact), (22.0, 31.0, FootpathState.missing)]
    assert model.profile[2].footpath_state is FootpathState.broken
    missing = model.profile[1]
    assert missing.footpath_strip("left") is None  # missing => no footpath strip
    fp = model.profile[0].footpath_strip("left")
    assert fp is not None and (fp.t_lo[0], fp.t_hi[0]) == (4.5, 6.3)  # road half 4.5 m, 1.8 m footpath
    assert all("legacy, placement unverified" in iv.description for iv in model.profile)


def test_footpath_gap_object_is_not_an_entity_other_objects_map_over(model, scene):
    types = sorted(e.type.value for e in model.entities)
    assert types == ["crossing", "low_branch", "parked_vehicle", "pothole"]
    assert len(model.entities) == len(scene.objects) - 1
    e = [x for x in model.entities if x.type is EntityType.parked_vehicle][0]
    assert e.position.s == 12.5 and e.provenance == [] and e.position_sigma_m == 1.5
    assert e.first_seen_version == e.last_changed_version == 0
    assert "legacy, placement unverified" in e.description and e.attributes["legacy_id"] == "o1"
    assert e.confidence == pytest.approx(0.0 + [o for o in scene.objects if o.id == "o1"][0].confidence, abs=1e-3)


def test_open_drain_is_renamed_to_drain():
    raw = json.loads(EXAMPLE.read_text())
    raw["objects"][2]["type"] = "open_drain"
    m = import_legacy(Scene.model_validate(raw), segment_slug=SLUG, footpath_side="left", now=FIXED_NOW)
    drain = [e for e in m.entities if e.type is EntityType.drain]
    assert len(drain) == 1 and drain[0].attributes["covered"] is False


def test_right_side_mirrors_lateral_positions(scene):
    left = import_legacy(scene, segment_slug=SLUG, footpath_side="left", now=FIXED_NOW)
    right = import_legacy(scene, segment_slug=SLUG, footpath_side="right", now=FIXED_NOW)
    lt = {e.attributes["legacy_id"]: e.position.t for e in left.entities}
    rt = {e.attributes["legacy_id"]: e.position.t for e in right.entities}
    assert lt.keys() == rt.keys() and all(rt[k] == pytest.approx(-lt[k]) for k in lt)
    fp = right.profile[0].footpath_strip("right")
    assert fp is not None and fp.t_hi[0] == -4.5 and fp.t_lo[0] == -6.3
    assert right.footpath_side == "right" and right.reference_line.points[0].s == 0.0


def test_buildings_get_vision_style_ids_and_sides(model, scene):
    assert len(model.buildings) == len(scene.buildings) == 1
    b = model.buildings[0]
    assert b.id.startswith("bld-v-") and b.source == scene.buildings[0].source and b.side in ("left", "right")
    assert b.height_m == scene.buildings[0].height_m and "legacy" in b.description


def test_import_is_deterministic_and_ids_are_stable(scene):
    a = import_legacy(scene, segment_slug=SLUG, footpath_side="left", now=FIXED_NOW)
    b = import_legacy(Scene.model_validate_json(scene.model_dump_json()), segment_slug=SLUG, footpath_side="left",
                      now=FIXED_NOW)
    assert a.model_dump_json() == b.model_dump_json()
    assert len({e.id for e in a.entities}) == len(a.entities)
    other = import_legacy(scene, segment_slug="other-slug", footpath_side="left", now=FIXED_NOW)
    assert {e.id for e in other.entities}.isdisjoint({e.id for e in a.entities})


def test_stats_and_json_round_trip(model):
    assert model.stats == model.computed_stats() and model.stats.entity_count == 4 and model.stats.observation_count == 0
    assert CanonicalModel.model_validate_json(model.model_dump_json()) == model


def test_invalid_side_is_rejected(scene):
    with pytest.raises(ValueError):
        import_legacy(scene, segment_slug=SLUG, footpath_side="centre", now=FIXED_NOW)


def test_a_legacy_model_compiles_to_a_flagged_scene_v2(model):
    sc = compile_scene(model, observations={}, assets={}, tile=None, fidelity=model.fidelity,
                       slug_name="100 Feet Road", city="Bengaluru")
    assert sc.about.legacy and not sc.about.placement_verified and sc.about.fidelity.level == "L0"
    assert not sc.about.degraded and "legacy, placement unverified" in sc.about.summary
    assert any(o.type == "footpath_gap" for o in sc.objects)  # the missing interval comes back as a gap
    assert {o.entity_id for o in sc.objects if o.entity_id} == {e.id for e in model.entities}
