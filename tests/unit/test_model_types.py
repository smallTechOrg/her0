"""hero.model.types / catalogue / ids / batch: the street-model contract objects."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hero.model import catalogue, ids
from hero.model.batch import ProjectedBatch, ProjectedDetection, StateSpan, StripSample, VisibleRegion
from hero.model.types import (
    CanonicalModel,
    CritiqueOp,
    DiffEntityRef,
    EntityState,
    EntityType,
    FidelityImproved,
    FootpathState,
    ModelDiff,
    ModelKind,
    RunReport,
    StripKind,
)
from pydantic import TypeAdapter

from tests.fixtures.street_fixtures import (
    FIXED_NOW,
    curved_reference_line,
    make_entity,
    make_fidelity_map,
    make_model,
    make_observation,
)


def test_model_json_uses_the_schema_key_and_round_trips_exactly():
    model = make_model(entities=[make_entity()], fused_observation_ids=["obs-0001"])
    dumped = json.loads(model.model_dump_json())
    assert dumped["schema"] == "street-model/1"
    assert "schema_id" not in dumped
    again = CanonicalModel.model_validate_json(model.model_dump_json())
    assert again == model
    assert again.model_dump_json() == model.model_dump_json()


def test_unknown_keys_are_rejected():
    data = json.loads(make_model().model_dump_json())
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        CanonicalModel.model_validate(data)
    data = json.loads(make_model().model_dump_json())
    data["profile"][0]["strips"][0]["colour"] = "red"
    with pytest.raises(ValidationError):
        CanonicalModel.model_validate(data)


def test_profile_must_tile_the_reference_line_from_zero():
    data = json.loads(make_model().model_dump_json())
    data["profile"][0]["s_from"] = 2.0
    with pytest.raises(ValidationError, match="start at s=0"):
        CanonicalModel.model_validate(data)
    data = json.loads(make_model().model_dump_json())
    data["profile"][-1]["s_to"] -= 5.0
    with pytest.raises(ValidationError, match="end at length_s"):
        CanonicalModel.model_validate(data)
    data = json.loads(make_model().model_dump_json())
    data["profile"][1]["s_from"] += 3.0
    with pytest.raises(ValidationError, match="contiguous"):
        CanonicalModel.model_validate(data)


def test_duplicate_entity_ids_and_legacy_kind_mismatch_are_rejected():
    e = make_entity()
    with pytest.raises(ValidationError, match="unique"):
        make_model(entities=[e, e])
    data = json.loads(make_model().model_dump_json())
    data["kind"] = "legacy"
    with pytest.raises(ValidationError, match="legacy"):
        CanonicalModel.model_validate(data)


def test_entity_id_must_look_like_an_entity_id_and_confidence_is_bounded():
    with pytest.raises(ValidationError):
        make_entity(entity_id="pole-1")
    with pytest.raises(ValidationError):
        type(make_entity()).model_validate({**make_entity().model_dump(), "confidence": 1.2})


def test_curved_fixture_model_is_valid_and_stats_count_removed_separately():
    removed = make_entity(entity_id="ent-0000000002", state=EntityState.removed, confidence=0.1)
    model = make_model(curved_reference_line(), entities=[make_entity(), removed], kind=ModelKind.fused, version=1)
    assert model.stats.entity_count == 1 and model.stats.removed_count == 1
    assert model.interval_at(0.0).s_from == 0.0
    assert model.interval_at(10_000.0) is model.profile[-1]
    assert model.entity("ent-0000000002").state is EntityState.removed
    assert model.entity("ent-missing") is None


def test_footpath_strip_lookup_and_missing_state_has_no_strip():
    model = make_model(states=[FootpathState.intact, FootpathState.missing])
    assert model.profile[0].footpath_strip("left") is not None
    assert model.profile[1].footpath_strip("left") is None
    assert model.profile[1].confidence_for("left") == 0.0
    assert model.profile[0].confidence_for("left") > 0.0


def test_critique_ops_are_a_discriminated_union():
    adapter = TypeAdapter(CritiqueOp)
    op = adapter.validate_python({"op": "nudge_entity", "entity_id": "ent-1", "ds_m": 1.0, "dt_m": -0.5})
    assert op.op == "nudge_entity"
    with pytest.raises(ValidationError):
        adapter.validate_python({"op": "delete_entity", "entity_id": "ent-1"})


def test_diff_and_report_json_use_spec_keys():
    diff = ModelDiff(from_version=1, to_version=2, is_empty=False,
                     fidelity={"improved": [FidelityImproved(s_from=0.0, s_to=100.0, **{"from": "L1"}, to="L2")],
                               "overall": (1.0, 2.0)})
    dumped = json.loads(diff.model_dump_json())
    assert dumped["fidelity"]["improved"][0]["from"] == "L1"
    assert ModelDiff.model_validate(dumped) == diff
    report = RunReport(run_id="r1", segment_slug="s", mode="generate", base_version=1, result_version=2,
                       version_created=True, summary="x")
    assert RunReport.model_validate_json(report.model_dump_json()) == report
    assert DiffEntityRef(id="ent-1", type="pole", s=1, t=2, confidence=0.5).description == ""


def test_fidelity_map_level_lookup_and_defaults():
    fid = make_fidelity_map(20.0, level=1)
    assert [b.s_to for b in fid.bins] == [5.0, 10.0, 15.0, 20.0]
    assert fid.level_at(0.0) == 1 and fid.level_at(99.0) == 1
    assert fid.overall.histogram_m["L1"] == 20.0


def test_observation_record_derives_url_and_street_level_flag():
    obs = make_observation()
    assert obs.url == "/media/imagery/obs-0001.jpg" and obs.is_street_level
    sat = make_observation(obs_id="obs-sat", kind="satellite_overview", station_s=None, view=None, zoom=19)
    assert not sat.is_street_level and sat.heading_deg is None
    with pytest.raises(ValidationError):
        type(obs).model_validate({**obs.model_dump(), "kind": "panorama"})


# ------------------------------------------------------------------ catalogue


def test_catalogue_covers_every_entity_type():
    assert set(catalogue.CATALOGUE) == set(EntityType)
    assert catalogue.entry("pothole").action == "jump"
    assert catalogue.entry(EntityType.low_branch).action == "slide"
    assert catalogue.entry("parked_vehicle").size_m == (1.8, 1.5, 4.2)
    assert catalogue.entry("crossing").emits_tokens and catalogue.entry("ramp").emits_tokens


def test_catalogue_unknown_type_raises_value_error():
    with pytest.raises(ValueError):
        catalogue.entry("spaceship")


def test_action_overrides_follow_the_catalogue_notes():
    assert catalogue.action_for("other", {"blocks_footpath": True}) == "dodge"
    assert catalogue.action_for("other", {}) == "none"
    assert catalogue.action_for("manhole", {"open": True}) == "jump"
    assert catalogue.action_for("manhole") == "none"
    assert catalogue.action_for("wall", in_lane_band=True) == "dodge"
    assert catalogue.action_for("wall", in_lane_band=False) == "none"
    assert catalogue.action_for("drain", {"covered": True}) == "none"
    assert catalogue.action_for("drain") == "jump"
    assert catalogue.emits_low_branch("tree", {"canopy_low": True})
    assert not catalogue.emits_low_branch("pole", {"canopy_low": True})


def test_two_wheeler_uses_the_smaller_default_size():
    assert catalogue.default_size_m("parked_vehicle", "two-wheeler") == (0.7, 1.2, 1.9)
    assert catalogue.default_size_m("railing", extent_length_s=6.0) == (0.1, 1.0, 6.0)


def test_legacy_mapping_renames_open_drain_and_drops_footpath_gap():
    assert catalogue.legacy_type("open_drain") is EntityType.drain
    assert catalogue.legacy_type("footpath_gap") is None
    assert catalogue.legacy_type("tree") is EntityType.tree
    with pytest.raises(ValueError):
        catalogue.legacy_type("unicorn")


# ------------------------------------------------------------------ ids


def test_entity_id_is_deterministic_and_input_sensitive():
    a = ids.entity_id("slug", "pole", "obs-1", 12.54, 5.86, 0)
    assert a == ids.entity_id("slug", EntityType.pole, "obs-1", 12.54, 5.86, 0)
    assert a.startswith("ent-") and len(a) == 14
    assert a == ids.entity_id("slug", "pole", "obs-1", 12.5, 5.9, 0)  # rounded to 0.1 m
    assert a != ids.entity_id("slug", "pole", "obs-1", 12.54, 5.86, 1)
    assert a != ids.entity_id("slug", "tree", "obs-1", 12.54, 5.86, 0)
    assert ids.entity_id("slug", "pole", "o", -0.04, 0.0) == ids.entity_id("slug", "pole", "o", 0.0, -0.04)


def test_entity_id_matches_the_documented_formula():
    import hashlib

    expect = "ent-" + hashlib.sha1(b"slug|pole|obs-1|12.5|5.9|0").hexdigest()[:10]
    assert ids.entity_id("slug", "pole", "obs-1", 12.5, 5.9, 0) == expect


def test_building_ids():
    assert ids.building_id(osm_way_id=1827364) == "bld-osm-1827364"
    v = ids.building_id(segment_slug="s", side="left", s_center=33.4)
    assert v.startswith("bld-v-") and len(v) == 14 and v == ids.building_id(segment_slug="s", side="left", s_center=33.2)
    with pytest.raises(ValueError):
        ids.building_id(segment_slug="s")


def test_observation_keys():
    k = ids.obs_key_google("streetview", {"pano": "abc", "heading": 10})
    assert len(k) == 64 and k == ids.obs_key_google("streetview", {"heading": 10, "pano": "abc"})
    assert ids.obs_key_upload("A" * 64) == "upload:" + "a" * 64
    with pytest.raises(ValueError):
        ids.obs_key_upload("xyz")
    assert ids.contradiction_id("type_conflict", "ent-1", "ent-2") == ids.contradiction_id("type_conflict", "ent-1", "ent-2")


# ------------------------------------------------------------------ batch


def test_batch_dataclasses_are_frozen_and_default_empty():
    batch = ProjectedBatch(batch_id="b1", observation_ids=("obs-1",))
    assert batch.detections == () and batch.visible == ()
    with pytest.raises(Exception):
        batch.batch_id = "x"  # type: ignore[misc]
    det = ProjectedDetection(key="obs-1:d1", observation_id="obs-1", type=EntityType.pole, subtype=None, s=1.0, t=2.0,
                             sigma_m=0.5, extent_s=0.3, extent_t=0.3, height_m=7.0, orientation_deg=None,
                             attributes={}, description="", raw_confidence=0.7, crop_px=None, weight=1.0)
    assert det.flags == ()
    StripSample(s=0.0, kind=StripKind.footpath, side="left", t_lo=5.0, t_hi=6.5, material="paver", condition="fair",
                weight=1.0, observation_id="obs-1")
    StateSpan(observation_id="obs-1", side="left", s_from=0.0, s_to=10.0, state=FootpathState.intact, weight=1.0)
    VisibleRegion(observation_id="obs-1", s_from=0.0, s_to=15.0, side="both", weight=1.0)
