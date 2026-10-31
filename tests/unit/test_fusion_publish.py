"""hero.fusion.publish / assets / steps helpers: pure logic of the fusion orchestrator (no network, no LLM, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PIL import Image

from hero.fusion import assets as assets_mod
from hero.fusion import publish as pub
from hero.fusion.state import CritiqueIteration, FusionState
from hero.imagery.osm import OsmGeometry, OsmWay
from hero.model.scene_v3 import SceneV3
from hero.model.types import (
    Billboard,
    EntityState,
    EntityType,
    FootpathState,
    ModelKind,
    ObservationKind,
    ViewKind,
)
from tests.fixtures.street_fixtures import (
    make_entity,
    make_model,
    make_observation,
    straight_reference_line,
)

REF = straight_reference_line(100.0)


def prior(**kw):
    return make_model(REF, **kw)


def fused(base, *, entities, fused_ids=("obs-0001",), states=None, version=1):
    kw = dict(kind=ModelKind.fused, version=version, entities=list(entities), fused_observation_ids=list(fused_ids),
              run_id="run-1", parent_version=base.version, states=states)
    return make_model(REF, **kw)


def street_obs(oid="obs-0001", s=10.0, view=ViewKind.forward, heading=0.0):
    return make_observation(obs_id=oid, station_s=s, view=view, heading_deg=heading, lat=12.9706, lon=77.6413)


# ------------------------------------------------------------------ consistency_check


def test_consistency_check_flags_objects_outside_the_street_frame_without_removing_them():
    far = make_entity(entity_id="ent-00000000f1", s=140.0, t=5.0)
    wide = make_entity(entity_id="ent-00000000f2", s=20.0, t=45.0)
    model = fused(prior(), entities=[far, wide, make_entity(entity_id="ent-00000000f3", s=30.0)])

    out, warnings = pub.consistency_check(model)

    assert len(out.entities) == 3, "the check never deletes"
    assert any("outside the street frame" in w and "ent-00000000f1" in w and "ent-00000000f2" in w for w in warnings)


def test_consistency_check_flags_two_present_objects_of_one_type_on_top_of_each_other():
    a = make_entity(entity_id="ent-00000000a1", s=10.0, t=5.5)
    b = make_entity(entity_id="ent-00000000a2", s=10.2, t=5.6)
    _, warnings = pub.consistency_check(fused(prior(), entities=[a, b]))
    assert any("possible duplicate" in w for w in warnings)


def test_parked_vehicle_on_an_intact_footpath_opens_a_footpath_state_conflict_once():
    car = make_entity(entity_id="ent-00000000c1", type=EntityType.parked_vehicle, s=12.0, t=5.8)
    model = fused(prior(), entities=[car], states=[FootpathState.intact])
    before_votes = dict(model.profile[0].state_votes)

    out, warnings = pub.consistency_check(model)

    conflicts = [c for c in out.contradictions if c.kind.value == "footpath_state_conflict"]
    assert len(conflicts) == 1 and conflicts[0].entity_ids == ["ent-00000000c1"] and conflicts[0].is_open
    assert conflicts[0].observation_ids == ["obs-0001"]
    assert any("sits on the footpath" in w for w in warnings)
    assert out.profile[0].state_votes["intact"] < before_votes["intact"], "the state's vote is damped"
    again, _ = pub.consistency_check(out)
    assert len([c for c in again.contradictions if c.kind.value == "footpath_state_conflict"]) == 1, "same id, not duplicated"


@pytest.mark.parametrize("state", [FootpathState.encroached, FootpathState.blocked, FootpathState.broken,
                                   FootpathState.missing])
def test_vehicle_on_a_footpath_whose_state_already_explains_it_is_consistent(state):
    car = make_entity(entity_id="ent-00000000c2", type=EntityType.parked_vehicle, s=12.0, t=5.8)
    out, _ = pub.consistency_check(fused(prior(), entities=[car], states=[state]))
    assert out.contradictions == []


def test_vehicle_clear_of_the_footpath_or_removed_is_not_a_conflict():
    clear = make_entity(entity_id="ent-00000000c3", type=EntityType.parked_vehicle, s=12.0, t=1.0)
    gone = make_entity(entity_id="ent-00000000c4", type=EntityType.parked_vehicle, s=40.0, t=5.8, state=EntityState.removed)
    out, _ = pub.consistency_check(fused(prior(), entities=[clear, gone], states=[FootpathState.intact]))
    assert out.contradictions == []


# ------------------------------------------------------------------ assemble_version


def bundle_for(candidate, base, observations=None, **kw):
    obs = observations if observations is not None else {"obs-0001": street_obs()}
    return pub.assemble_version(candidate=candidate, base=base, observations=obs, assets=kw.pop("assets", {}),
                                segment_name="Test street", city="Bengaluru",
                                models={"vision": "m-v", "review": "m-r"}, review_iterations=1, warnings=["w1"])


def test_assemble_version_compiles_a_valid_scene_diff_and_summary():
    base = prior()
    candidate = fused(base, entities=[make_entity(entity_id="ent-00000000e1", s=30.0, t=5.5)])

    bundle = bundle_for(candidate, base)

    assert bundle.changed and bundle.quality in ("full", "satellite_only", "generic")
    assert isinstance(bundle.scene, SceneV3) and bundle.scene.model.version == 1  # v3-lite; assets come with the export
    assert bundle.scene.generation.models == {"vision": "m-v", "review": "m-r"}
    assert bundle.scene.generation.review_iterations == 1 and "w1" in bundle.scene.generation.warnings
    assert bundle.model.fidelity == bundle.fidelity_after
    assert bundle.fidelity_after.overall.score > bundle.fidelity_before.overall.score == 0.0
    assert any(b.level >= 1 for b in bundle.fidelity_after.bins)
    assert bundle.diff.from_version == 0 and bundle.diff.to_version == 1
    assert [e.id for e in bundle.diff.entities.added] == ["ent-00000000e1"]
    assert "Model v1" in bundle.summary and "fidelity L" in bundle.summary
    create = bundle.to_create()
    assert create.version == 1 and create.kind == "fused" and create.parent_version == 0
    assert create.scene["schema_version"] == 3 and create.run_id == "run-1"


def test_assemble_version_detects_no_change():
    base = prior(version=1, kind=ModelKind.fused, fused_observation_ids=["obs-0001"], run_id="r0")
    candidate = base.model_copy(update={"version": 2, "parent_version": 1, "run_id": "r1"})

    bundle = bundle_for(candidate, base)

    assert bundle.changed is False and bundle.diff.is_empty


def test_assemble_version_is_deterministic():
    base = prior()
    candidate = fused(base, entities=[make_entity(entity_id="ent-00000000e1", s=30.0)])
    a, b = bundle_for(candidate, base), bundle_for(candidate, base)
    assert a.scene.model_dump_json() == b.scene.model_dump_json()
    assert a.summary == b.summary


def test_prior_fallback_scene_is_labelled_degraded_and_generic():
    scene = pub.prior_fallback_scene(prior(), {}, segment_name="Test street", city="Bengaluru",
                                     warnings=["degraded: failed"])
    assert scene.about.degraded and scene.generation.quality.value == "generic"
    assert scene.about.fidelity.level == "L0" and "degraded: failed" in scene.generation.warnings


def test_best_tile_is_the_oldest_overview():
    old = make_observation(obs_id="ov-1", kind=ObservationKind.satellite_overview, station_s=50.0, zoom=19)
    new = old.model_copy(update={"id": "ov-2", "created_at": datetime(2026, 11, 1, tzinfo=UTC)})
    crop = make_observation(obs_id="cr-1", kind=ObservationKind.satellite_crop, station_s=10.0, zoom=20)
    assert pub.best_tile({"ov-2": new, "cr-1": crop, "ov-1": old}).id == "ov-1"
    assert pub.best_tile({"cr-1": crop}) is None


# ------------------------------------------------------------------ crops


def test_cut_model_crops_writes_version_suffixed_urls_and_skips_missing_images(tmp_path):
    (tmp_path / "imagery").mkdir()
    Image.new("RGB", (640, 640), (90, 120, 150)).save(tmp_path / "imagery" / "obs-0001.jpg")
    ok = make_entity(entity_id="ent-00000000b1", s=20.0).model_copy(
        update={"billboard": Billboard(observation_id="obs-0001", crop_px=(100, 100, 120, 160))})
    missing = make_entity(entity_id="ent-00000000b2", s=30.0).model_copy(
        update={"billboard": Billboard(observation_id="obs-9999", crop_px=(0, 0, 50, 50))})
    removed = make_entity(entity_id="ent-00000000b3", s=40.0, state=EntityState.removed).model_copy(
        update={"billboard": Billboard(observation_id="obs-0001", crop_px=(0, 0, 50, 50))})
    tiny = make_entity(entity_id="ent-00000000b4", s=50.0).model_copy(
        update={"billboard": Billboard(observation_id="obs-0001", crop_px=(0, 0, 3, 3))})
    model = fused(prior(), entities=[ok, missing, removed, tiny])

    urls = assets_mod.cut_model_crops(model, {"obs-0001": street_obs()}, data_dir=tmp_path, version=3)

    assert urls == {"ent-00000000b1": f"/media/crops/{model.segment_slug}/ent-00000000b1.png?v=3"}
    img = Image.open(tmp_path / "crops" / model.segment_slug / "ent-00000000b1.png")
    assert img.mode == "RGBA" and max(img.size) <= 256


def test_save_thumbnail_downsizes(tmp_path):
    path = assets_mod.save_thumbnail(Image.new("RGB", (1600, 900), (1, 2, 3)), tmp_path / "p" / "thumb.png")
    assert max(Image.open(path).size) == assets_mod.THUMB_PX


# ------------------------------------------------------------------ osm adapter + state


def way(wid, kind, pts, **tags):
    return OsmWay(id=wid, kind=kind, tags={k.replace("_", ":") if k == "building_levels" else k: v for k, v in tags.items()},
                  points=[(0.0, 0.0)] * len(pts), latlons=pts)


def test_osm_context_adapter_projects_buildings_and_keeps_the_nearest_road_tags():
    from hero.fusion.steps import osm_context_from

    lat0, lon0 = REF.point_at(0, 0)
    east = lambda s, t: REF.point_at(s, t)  # noqa: E731
    road = way(1, "highway", [east(0, 0), east(100, 0)], highway="primary", name="100 Feet Road", lanes="2")
    side = way(2, "highway", [east(0, 80), east(100, 80)], highway="service")
    near = way(3, "building", [east(10, -12), east(20, -12), east(20, -20), east(10, -20), east(10, -12)],
               building="yes", building_levels="3")
    far = way(4, "building", [east(10, 200), east(20, 200), east(20, 210), east(10, 200)], building="yes")
    foot = way(5, "footway", [east(0, 6), east(100, 6)], highway="footway")
    osm = OsmGeometry(bbox=(0, 0, 1, 1), ways=[road, side, near, far, foot])

    ctx = osm_context_from(osm, REF, "100 Feet Road")

    assert ctx.highway_tags["name"] == "100 Feet Road"
    assert [b.osm_way_id for b in ctx.buildings] == [3] and ctx.buildings[0].storeys == 3
    assert len(ctx.buildings[0].footprint) == 4, "the closing point is dropped"
    assert all(abs(t) < 25 for _s, t in ctx.buildings[0].footprint)
    assert len(ctx.footway_polylines) == 1


def test_state_json_pieces_are_serialisable():
    st = FusionState.__new__(FusionState)
    it = CritiqueIteration(n=1, accepted=False, critique="check the pole", ops=[{"op": "flag_entity"}], applied=2,
                           preview_path="previews/r/critique1.png", preview_url="/media/previews/r/critique1.png")
    js = it.to_json()
    assert js["patch_ops"] == [{"op": "flag_entity"}] and js["applied"] is True and js["applied_count"] == 2
    assert CritiqueIteration(n=2, accepted=True, critique="").to_json()["applied"] is False
    assert FusionState.warn is not None and st is not None


def test_core_strips_that_overlap_are_flagged_but_the_priors_backdrop_strips_are_not():
    from hero.model.types import Condition, Material, Strip, StripKind

    base = prior()
    assert not any("strips overlap" in w for w in pub.consistency_check(base)[1]), \
        "the prior's forecourt/verge backdrops overlap the footpath by design"
    iv = base.profile[0]
    clash = Strip(id="sp-clash", kind=StripKind.parking, side="left", material=Material.asphalt, condition=Condition.fair,
                  height_m=0.0, t_lo=(4.0, 4.0), t_hi=(6.0, 6.0), weight=0.2, confidence=0.2, description="", provenance=[])
    bad = base.model_copy(update={"profile": [iv.model_copy(update={"strips": [*iv.strips, clash]}), *base.profile[1:]]})

    _, warnings = pub.consistency_check(bad)

    assert any(w.startswith("consistency: strips overlap in 0-") for w in warnings)
