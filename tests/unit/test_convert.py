"""Extraction -> ProjectedBatch (uses the real projection + coverage modules)."""

from __future__ import annotations

import math

import pytest

from hero.fusion.convert import (
    convert_overhead,
    convert_street_batch,
    overhead_to_batch,
    street_to_batch,
)
from hero.fusion.schemas import OverheadExtraction, StreetExtraction
from hero.fusion.views import OverheadWindow, StreetBatch
from hero.llm.client import LLMImage
from hero.model.refline import RectGrid
from hero.model.types import EntityType, FootpathState, ObservationKind, StripKind, ViewKind
from tests.fixtures.replay_llm import load_extraction
from tests.fixtures.street_fixtures import make_observation, straight_reference_line

REF = straight_reference_line(100.0)
IMG = LLMImage(data=b"", media_type="image/jpeg", caption="c", width_px=640, height_px=640)


def _batch(obs, batch_id="sv-test"):
    return StreetBatch(batch_id=batch_id, observations=list(obs), images=[IMG] * len(obs), captions=["c"] * len(obs))


def _det(local_id="d1", image_index=1, **over):
    d = {"local_id": local_id, "image_index": image_index, "type": "parked_vehicle", "subtype": "auto-rickshaw",
         "box_px": [140, 330, 160, 90], "touches_ground": True, "est_distance_m": None,
         "size_m": {"w": 1.4, "h": 1.7, "d": 2.6}, "orientation": "parallel", "attributes": {"colour": "yellow", "blocks_footpath": True},
         "description": "Yellow auto-rickshaw.", "confidence": 0.8}
    d.update(over)
    return d


def _frame(i=1, **over):
    f = {"image_index": i, "visibility_ok": True, "footpath_visible": True, "footpath_side": "left", "footpath_state": "broken"}
    f.update(over)
    return f


def _at(s, **kw):
    lat, lon = REF.point_at(s, 0.0)
    return dict(station_s=s, lat=lat, lon=lon, **kw)


def _fwd(obs_id="obs-1", s=10.0, **kw):
    return make_observation(obs_id=obs_id, heading_deg=0.0, view=ViewKind.forward, **_at(s), **kw)


# ---------------------------------------------------------------- street

def test_forward_detection_projects_ahead_of_the_camera_and_on_the_left():
    obs = _fwd(s=10.0)
    ex = StreetExtraction.model_validate({"frames": [_frame()], "detections": [_det()]})
    batch, dropped = convert_street_batch(_batch([obs]), ex, REF)
    assert dropped == []
    d = batch.detections[0]
    # bottom-centre pixel (220, 420): 100 px below the horizon -> range = 2.5 * hypot(320, -100) / 100
    rng = 2.5 * math.hypot(320, 100) / 100
    assert d.s == pytest.approx(10.0 + rng * math.cos(math.atan(100 / 320)), abs=0.3)
    assert d.t > 0, "pixel left of centre = left of travel"
    assert d.type is EntityType.parked_vehicle and d.subtype == "auto-rickshaw" and d.observation_id == "obs-1"
    assert d.key == "obs-1:d1" and d.crop_px == (140, 330, 160, 90) and d.raw_confidence == 0.8
    assert d.attributes == {"blocks_footpath": True, "colour": "yellow"}
    assert d.description == "Yellow auto-rickshaw." and d.weight > 0 and d.sigma_m > 0


def test_footprint_follows_orientation():
    obs = _fwd()
    def conv(orientation):
        ex = StreetExtraction.model_validate({"detections": [_det(orientation=orientation)]})
        return street_to_batch(_batch([obs]), ex, REF).detections[0]
    par, perp, unk = conv("parallel"), conv("perpendicular"), conv("unknown")
    assert (par.extent_s, par.extent_t, par.orientation_deg) == (2.6, 1.4, 0.0)
    assert (perp.extent_s, perp.extent_t, perp.orientation_deg) == (1.4, 2.6, 90.0)
    assert (unk.extent_s, unk.extent_t, unk.orientation_deg) == (2.6, 1.4, None)
    assert par.height_m == 1.7


def test_missing_size_falls_back_to_catalogue_defaults():
    ex = StreetExtraction.model_validate({"detections": [_det(size_m=None, type="tree", subtype=None)]})
    d = street_to_batch(_batch([_fwd()]), ex, REF).detections[0]
    assert (d.extent_t, d.height_m) == (0.8, 5.0)


def test_local_id_collisions_across_batches_give_distinct_keys_and_in_batch_repeats_get_a_suffix():
    a, b = _fwd("obs-a", 10.0), _fwd("obs-b", 40.0)
    ex = StreetExtraction.model_validate({"detections": [_det("d1")]})
    ka = street_to_batch(_batch([a]), ex, REF).detections[0].key
    kb = street_to_batch(_batch([b]), ex, REF).detections[0].key
    assert ka != kb
    twice = StreetExtraction.model_validate({"detections": [_det("d1"), _det("d1", box_px=[300, 330, 50, 60])]})
    keys = [d.key for d in street_to_batch(_batch([a]), twice, REF).detections]
    assert keys == ["obs-a:d1", "obs-a:d1#2"]


def test_undroppable_vs_droppable_detections_are_counted_with_reasons():
    obs = [_fwd("obs-1", 10.0), _fwd("obs-2", 20.0)]
    ex = StreetExtraction.model_validate({
        "frames": [_frame(1), _frame(2, visibility_ok=False)],
        "detections": [_det("ok"),
                       _det("badidx", image_index=7),
                       _det("hidden", image_index=2),
                       _det("far", box_px=[300, 318, 20, 4], touches_ground=True, est_distance_m=None),   # on the horizon, no estimate
                       _det("toofar", box_px=[300, 300, 20, 20], touches_ground=False, est_distance_m=80.0)]})
    batch, dropped = convert_street_batch(_batch(obs), ex, REF)
    assert [d.key for d in batch.detections] == ["obs-1:ok"]
    reasons = {x.key: x.reason for x in dropped}
    assert set(reasons) == {"7:badidx", "2:hidden", "obs-1:far", "obs-1:toofar"}
    assert "out of range" in reasons["7:badidx"] and "visibility" in reasons["2:hidden"]


def test_estimated_distance_fallback_is_flagged():
    ex = StreetExtraction.model_validate({"detections": [_det(touches_ground=False, est_distance_m=8.0)]})
    d = street_to_batch(_batch([_fwd()]), ex, REF).detections[0]
    assert "estimated_range" in d.flags


def test_upload_without_pose_drops_instead_of_raising():
    up = make_observation(obs_id="obs-up", kind=ObservationKind.user_photo, heading_deg=None, fov_deg=None, station_s=10.0)
    ex = StreetExtraction.model_validate({"detections": [_det()]})
    batch, dropped = convert_street_batch(_batch([up]), ex, REF)
    assert batch.detections == () and "projection failed" in dropped[0].reason


def test_frames_become_state_spans_per_view_and_side():
    obs = [_fwd("obs-f", 10.0), make_observation(obs_id="obs-b", heading_deg=180.0, view=ViewKind.back, **_at(50.0)),
           make_observation(obs_id="obs-r", heading_deg=90.0, view=ViewKind.right, **_at(70.0))]
    ex = StreetExtraction.model_validate({"frames": [
        _frame(1, footpath_state="broken"),
        _frame(2, footpath_state="missing", footpath_side="right"),
        _frame(3, footpath_visible=False, footpath_side="none", footpath_state="unknown")]})
    batch = street_to_batch(_batch(obs), ex, REF)
    spans = {(s.observation_id, s.side): (s.s_from, s.s_to, s.state) for s in batch.states}
    assert spans == {("obs-f", "left"): (10.0, 22.0, FootpathState.broken),
                     ("obs-b", "right"): (38.0, 50.0, FootpathState.missing)}


def test_both_sides_and_unknown_state_and_clamping():
    ex = StreetExtraction.model_validate({"frames": [_frame(1, footpath_side="both", footpath_state="intact"),
                                                    _frame(2, footpath_state="unknown")]})
    batch = street_to_batch(_batch([_fwd("obs-1", 95.0), _fwd("obs-2", 50.0)]), ex, REF)
    assert {(s.side, s.s_from, s.s_to) for s in batch.states} == {("left", 95.0, REF.length_s), ("right", 95.0, REF.length_s)}


def test_facade_spans_the_projected_box_width_and_carries_its_crop():
    obs = make_observation(obs_id="obs-l", heading_deg=270.0, view=ViewKind.left, **_at(30.0))
    ex = StreetExtraction.model_validate({"facades": [{"image_index": 1, "box_px": [100, 60, 440, 300], "side": "left",
                                                      "colour_hex": "#c9b79c", "storeys": 3, "use": "shops", "description": "Shops.",
                                                      "confidence": 0.7}]})
    f = street_to_batch(_batch([obs]), ex, REF).facades[0]
    assert f.side == "left" and f.colour_hex == "#c9b79c" and f.storeys == 3 and f.crop_px == (100, 60, 440, 300)
    assert 0.0 <= f.s_from < f.s_to <= 100.0 and f.s_to - f.s_from >= 2.0 and f.weight > 0
    assert abs((f.s_from + f.s_to) / 2 - 30.0) < 15


def test_facade_that_cannot_be_projected_falls_back_to_a_window_around_the_camera():
    obs = _fwd("obs-1", 40.0)
    ex = StreetExtraction.model_validate({"facades": [{"image_index": 1, "box_px": [0, 100, 100, 215], "side": "left",
                                                      "description": "Far shops.", "confidence": 0.5}]})
    f = street_to_batch(_batch([obs]), ex, REF).facades[0]
    assert (f.s_from, f.s_to) == (35.0, 45.0)


def test_visible_regions_cover_only_usable_frames_and_batch_ids_are_preserved():
    obs = [_fwd("obs-1", 10.0), _fwd("obs-2", 60.0)]
    ex = StreetExtraction.model_validate({"frames": [_frame(1), _frame(2, visibility_ok=False)]})
    batch = street_to_batch(_batch(obs, "sv-xyz"), ex, REF)
    assert batch.batch_id == "sv-xyz" and batch.observation_ids == ("obs-1", "obs-2")
    assert {v.observation_id for v in batch.visible} == {"obs-1"}
    assert all(0 <= v.s_from < v.s_to <= 100 for v in batch.visible)


def test_empty_extraction_gives_an_empty_but_valid_batch_and_conversion_is_deterministic():
    obs = [_fwd()]
    batch = street_to_batch(_batch(obs), StreetExtraction.model_validate({}), REF)
    assert batch.detections == batch.states == batch.facades == () and batch.observation_ids == ("obs-1",)
    ex = StreetExtraction.model_validate({"frames": [_frame()], "detections": [_det()]})
    assert street_to_batch(_batch(obs), ex, REF) == street_to_batch(_batch(obs), ex, REF)


# ---------------------------------------------------------------- overhead

def _windows(grids=None, zoom=19):
    grids = grids or [RectGrid(s_top=50.0, s_bottom=0.0, t_left=14.0, t_right=-14.0, m_per_px=0.1),
                      RectGrid(s_top=100.0, s_bottom=50.0, t_left=14.0, t_right=-14.0, m_per_px=0.1)]
    sat = make_observation(obs_id="obs-sat", kind=ObservationKind.satellite_overview, station_s=None, zoom=zoom)
    img = LLMImage(data=b"", media_type="image/png", caption="c", width_px=280, height_px=500)
    return [OverheadWindow(index=i, observation=sat, grid=g, image=img, caption="c") for i, g in enumerate(grids, 1)]


def test_recorded_overhead_fixture_converts_to_strips_states_objects_and_visibility():
    ex = OverheadExtraction.model_validate(load_extraction("overhead_curved"))
    batch, dropped = convert_overhead(_windows(), ex, REF)
    assert dropped == []
    assert batch.observation_ids == ("obs-sat",) and batch.batch_id.startswith("ov-")
    assert all(s.observation_id == "obs-sat" for s in batch.strips + batch.states)
    fp = [s for s in batch.strips if s.kind is StripKind.footpath]
    assert fp and all(s.side == "left" and s.t_lo == pytest.approx(5.0, abs=0.11) and s.t_hi == pytest.approx(6.5, abs=0.11) for s in fp)
    assert all(abs(s.s / 2.5 - round(s.s / 2.5)) < 1e-6 for s in batch.strips)
    states = sorted(batch.states, key=lambda s: s.s_from)
    # window 1 rows 0-320 intact -> s 50..18 ; rows 320-500 broken -> s 18..0
    assert (states[0].s_from, states[0].s_to, states[0].state) == (0.0, 18.0, FootpathState.broken)
    missing = [s for s in states if s.state is FootpathState.missing]
    assert [(m.s_from, m.s_to) for m in missing] == [(74.0, 86.0)]
    assert [(v.s_from, v.s_to, v.side) for v in batch.visible] == [(0.0, 50.0, "both"), (50.0, 100.0, "both")]
    assert {v.weight for v in batch.visible} == {0.7}


def test_overhead_objects_land_at_the_box_centre_with_pixel_extent():
    ex = OverheadExtraction.model_validate(load_extraction("overhead_curved"))
    batch = overhead_to_batch(_windows(), ex, REF)
    tree = next(d for d in batch.detections if d.type is EntityType.tree)
    # box [55,120,40,40] -> centre (75, 140) -> s = 50 - 14 = 36, t = 14 - 7.5 = 6.5
    assert (tree.s, tree.t) == (pytest.approx(36.0), pytest.approx(6.5))
    assert (tree.extent_s, tree.extent_t) == (pytest.approx(4.0), pytest.approx(4.0))
    assert tree.flags == ("overhead",) and tree.key.startswith("obs-sat:w1o")
    assert len({d.key for d in batch.detections}) == len(batch.detections)


def test_hires_crop_gets_full_visibility_weight_and_bad_window_index_is_dropped():
    ex = OverheadExtraction.model_validate({"windows": [
        {"image_index": 1, "objects": []}, {"image_index": 5, "objects": []}]})
    batch, dropped = convert_overhead(_windows(zoom=20), ex, REF)
    assert {v.weight for v in batch.visible} == {1.0}
    assert [d.key for d in dropped] == ["window:5"]


def test_overhead_object_outside_the_street_is_dropped_and_nothing_to_convert_is_empty():
    far = RectGrid(s_top=130.0, s_bottom=80.0, t_left=14.0, t_right=-14.0, m_per_px=0.1)
    ex = OverheadExtraction.model_validate({"windows": [{"image_index": 1, "objects": [
        {"type": "tree", "box_px": [50, 10, 20, 20], "confidence": 0.5}, {"type": "pole", "box_px": [50, 400, 10, 10], "confidence": 0.5}]}]})
    batch, dropped = convert_overhead(_windows([far]), ex, REF)
    assert [d.type for d in batch.detections] == [EntityType.pole]
    assert len(dropped) == 1 and "outside" in dropped[0].reason
    assert [(v.s_from, v.s_to) for v in batch.visible] == [(80.0, REF.length_s)]
    empty = overhead_to_batch(_windows(), OverheadExtraction.model_validate({}), REF)
    assert empty.detections == empty.strips == empty.states == ()
