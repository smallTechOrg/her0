import math
from datetime import UTC, datetime

import pytest

from hero.model.priors import OsmBuilding, OsmContext, build_prior_model, osm_priors
from hero.model.types import CanonicalModel, FootpathState, ModelKind, StripKind
from tests.fixtures.street_fixtures import DEMO_ORIGIN, FIXED_NOW, _latlon, curved_reference_line, straight_reference_line


def build(ref=None, osm=None, side="left", source="osm_nominatim"):
    ref = ref or straight_reference_line(100.0)
    rm = ref.to_model(source=source, osm_way_id=1, name="Test Rd")
    return build_prior_model(segment_slug="seg-x", footpath_side=side, ref=ref, ref_model=rm, osm=osm, now=FIXED_NOW)


def strip(iv, kind, side):
    return next(s for s in iv.strips if s.kind is kind and s.side == side)


def test_default_template():
    m = build()
    assert m.kind is ModelKind.prior and m.version == 0 and m.parent_version is None and not m.legacy
    assert m.placement_verified is True and m.entities == [] and m.fused_observation_ids == []
    assert m.created_at == FIXED_NOW
    assert len(m.profile) == 4 and all(iv.length <= 30.0 + 1e-6 for iv in m.profile)
    iv = m.profile[0]
    cw = strip(iv, StripKind.carriageway, "centre")
    assert (cw.t_lo[0], cw.t_hi[0]) == (-3.0, 3.0)
    fl, fr = strip(iv, StripKind.footpath, "left"), strip(iv, StripKind.footpath, "right")
    assert (fl.t_lo[0], fl.t_hi[0]) == pytest.approx((3.15, 4.65))
    assert (fr.t_lo[0], fr.t_hi[0]) == pytest.approx((-4.65, -3.15))
    assert fl.weight == 0.15 and fl.confidence <= 0.14 + 1e-9
    k = strip(iv, StripKind.kerb, "left")
    assert k.t_hi[0] - k.t_lo[0] == pytest.approx(0.15)
    assert iv.footpath_state is FootpathState.unknown
    assert strip(iv, StripKind.forecourt, "left").t_lo[0] == pytest.approx(4.65)
    assert m.stats.entity_count == 0


def test_roundtrips_and_is_deterministic():
    a, b = build(), build()
    assert a.model_dump_json() == b.model_dump_json()
    assert CanonicalModel.model_validate_json(a.model_dump_json()) == a


def test_placement_verified_by_source():
    assert build(source="osm_overpass").placement_verified is True
    assert build(source="straight_chord").placement_verified is False
    assert build(source="legacy_straight").placement_verified is False


@pytest.mark.parametrize("tags,width", [({}, 6.0), ({"lanes": "4"}, 12.0), ({"lanes": "1"}, 5.0), ({"lanes": "8"}, 14.0),
                                         ({"width": "7.5"}, 7.5), ({"width": "9 m"}, 9.0), ({"width": "40"}, 14.0),
                                         ({"width": "2"}, 5.0), ({"width": "abc", "lanes": "3"}, 9.0),
                                         ({"width": "7", "lanes": "6"}, 7.0)])
def test_carriageway_width(tags, width):
    m = build(osm=OsmContext(highway_tags=tags))
    cw = strip(m.profile[0], StripKind.carriageway, "centre")
    assert cw.t_hi[0] - cw.t_lo[0] == pytest.approx(width)
    fl = strip(m.profile[0], StripKind.footpath, "left")
    assert fl.t_lo[0] == pytest.approx(width / 2 + 0.15)


def test_sidewalk_tags_raise_weight():
    iv = build(osm=OsmContext(highway_tags={"sidewalk": "left"})).profile[0]
    assert strip(iv, StripKind.footpath, "left").weight == 0.4
    assert strip(iv, StripKind.footpath, "right").weight == 0.15
    both = build(osm=OsmContext(highway_tags={"sidewalk": "both"})).profile[0]
    assert strip(both, StripKind.footpath, "right").weight == 0.4
    no = build(osm=OsmContext(highway_tags={"sidewalk": "no"})).profile[0]
    assert strip(no, StripKind.footpath, "left").weight == 0.15


def test_mapped_footway_replaces_offsets():
    ref = straight_reference_line(100.0)
    way = [_latlon(DEMO_ORIGIN, -6.0, y) for y in (0.0, 50.0, 100.0)]  # 6 m west = left of north travel
    pri = osm_priors(ref, OsmContext(footway_polylines=[way]))
    assert pri.footpath_centre_t["left"] == pytest.approx(6.0, abs=0.05) and pri.footpath_centre_t["right"] is None
    m = build(ref, OsmContext(footway_polylines=[way]))
    fl = strip(m.profile[0], StripKind.footpath, "left")
    assert (fl.t_lo[0], fl.t_hi[0]) == pytest.approx((5.25, 6.75), abs=0.05)
    assert fl.weight == 0.4
    v = strip(m.profile[0], StripKind.verge, "left")
    assert v.t_lo[0] == pytest.approx(3.15) and v.t_hi[0] == pytest.approx(fl.t_lo[0])
    assert strip(m.profile[0], StripKind.footpath, "right").weight == 0.15


def test_footway_inside_kerb_clamped_and_far_or_off_segment_ignored():
    ref = straight_reference_line(100.0)
    near = [_latlon(DEMO_ORIGIN, -1.0, y) for y in (0.0, 100.0)]
    m = build(ref, OsmContext(footway_polylines=[near]))
    assert strip(m.profile[0], StripKind.footpath, "left").t_lo[0] == pytest.approx(3.15)
    far = [_latlon(DEMO_ORIGIN, -80.0, y) for y in (0.0, 100.0)]
    off = [_latlon(DEMO_ORIGIN, -6.0, y) for y in (300.0, 400.0)]
    pri = osm_priors(ref, OsmContext(footway_polylines=[far, off]))
    assert pri.footpath_centre_t == {"left": None, "right": None}


def test_profile_tiling_edge_lengths():
    for length in (5.0, 29.0, 30.0, 61.0, 100.2):
        m = build(straight_reference_line(length))
        assert m.profile[0].s_from == 0 and m.profile[-1].s_to == pytest.approx(m.reference_line.length_s)
        assert all(iv.length <= 30.0 + 1e-6 for iv in m.profile)
        assert len(m.profile) == math.ceil(length / 30.0)


def test_curved_line_and_right_side():
    m = build(curved_reference_line(), side="right")
    assert m.footpath_side == "right" and len(m.profile) == 4
    ids = [s.id for iv in m.profile for s in iv.strips]
    assert len(ids) == len(set(ids))


def test_buildings():
    sq = [(10.0, 9.0), (28.0, 9.0), (28.0, 21.0), (10.0, 21.0), (10.0, 9.0)]
    right = [(0.0, -9.0), (20.0, -9.0), (20.0, -15.0)]
    off = [(500.0, 9.0), (510.0, 9.0), (510.0, 15.0)]
    osm = OsmContext(buildings=[OsmBuilding(7, sq, 9.6, 3), OsmBuilding(3, right, 6.0), OsmBuilding(9, off),
                                OsmBuilding(11, sq[:2]), OsmBuilding(12, sq, 0.0)])
    m = build(osm=osm)
    assert [b.id for b in m.buildings] == ["bld-osm-3", "bld-osm-7"]
    b7 = m.buildings[1]
    assert b7.side == "left" and len(b7.footprint) == 4 and b7.height_m == 9.6 and b7.storeys == 3
    assert b7.source == "osm"
    assert m.buildings[0].side == "right"
    assert m.stats.building_count == 2


def test_invalid_inputs():
    with pytest.raises(ValueError):
        build(side="centre")
    ref = straight_reference_line(50.0)
    with pytest.raises(ValueError):
        build_prior_model(segment_slug="", footpath_side="left", ref=ref,
                          ref_model=ref.to_model(source="osm_nominatim", osm_way_id=None, name=None), osm=None,
                          now=datetime(2026, 1, 1, tzinfo=UTC))
