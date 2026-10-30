"""analyze_gaps: evidence vs rendered per 5 m bin, counts, interpolation entries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hero.model.gap_types import GapReport
from hero.model.gaps import analyze_gaps, bins_of
from hero.model.texture_types import GroundStripRef, TextureRefs
from hero.model.types import (
    Building,
    CanonicalModel,
    FootpathState,
    ObservationKind,
    ProfileInterval,
    ViewKind,
)
from tests.fixtures.street_fixtures import (
    make_fidelity_map,
    make_model,
    make_observation,
    make_strips,
    straight_reference_line,
)

ROOT = Path(__file__).resolve().parents[2]


def bld(bid, s0, s1, t0, t1, side="left", h=9.0, source="osm") -> Building:
    return Building(id=bid, source=source, side=side, footprint=[(s0, t0), (s1, t0), (s1, t1), (s0, t1)], height_m=h)


def street(buildings=(), fused=(), **kw) -> CanonicalModel:
    m = make_model(straight_reference_line(100.0), fused_observation_ids=list(fused), **kw)
    return m.model_copy(update={"buildings": list(buildings)})


def obs_map(*obs):
    return {o.id: o for o in obs}


def compiled(model, fid):
    """Compile for real: the blank counts of the report are taken from the plan, and re-derived independently here."""
    from hero.model.compile import compile_scene
    from tests.unit.test_compile_bellandur_no_holes import independent_blanks

    res = compile_scene(model, observations={}, assets={}, tile=None, fidelity=fid, slug_name="T", city="C")
    assert res.gaps.blank_after_compile.model_dump() == independent_blanks(res.plan)
    return res.gaps


def test_short_fidelity_map_is_an_error_not_level_zero():
    with pytest.raises(ValueError, match="fidelity map has 10 bin"):
        analyze_gaps(street(), {}, make_fidelity_map(50.0, 1), None)


def test_bins_tile_and_blank_counts_are_zero_on_a_bare_prior_model():
    r = compiled(street(), make_fidelity_map(100.0, 0))
    assert isinstance(r, GapReport) and len(r.bins) == 20 and r.bin_m == 5.0
    assert r.bins[0].s_from == 0 and r.bins[-1].s_to == 100.0
    assert r.blank_total() == 0
    assert r.uncovered.frontage_modelled == 20 and r.uncovered.frontage_opposite == 20
    assert r.uncovered.ground == 20 and r.uncovered.furniture == 20
    for b in r.bins:
        assert b.frontage.modelled.evidence == "none" and b.frontage.modelled.rendered == "prior"
        assert b.ground.rendered == "procedural" and b.hint


def test_last_bin_is_shorter_and_bins_helper_agrees():
    m = make_model(straight_reference_line(52.0))
    r = analyze_gaps(m, {}, make_fidelity_map(52.0, 1), None)
    assert [b.s_to for b in r.bins][-1] == 52.0 and len(r.bins) == 11
    assert [(b.s_from, b.s_to) for b in r.bins] == bins_of(52.0)


def test_footprints_give_osm_evidence_and_neighbours_fill_the_rest():
    m = street([bld("b1", 0, 30, 9, 20), bld("b2", 60, 100, -12, -20, side="right")])
    r = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    mod = [b.frontage.modelled for b in r.bins]
    opp = [b.frontage.opposite for b in r.bins]
    assert [x.evidence for x in mod[:6]] == ["osm"] * 6 and all(x.rendered == "osm" for x in mod[:6])
    assert all(x.evidence == "none" for x in mod[6:])
    assert mod[6].rendered == "interpolated"            # within 30 m of the footprint
    assert mod[-1].rendered == "prior"                  # nothing within 30 m
    assert [x.evidence for x in opp[12:]] == ["osm"] * 8
    assert opp[11].rendered == "interpolated" and opp[0].rendered == "prior"
    assert r.uncovered.frontage_modelled == 14 and r.uncovered.frontage_opposite == 12
    assert r.blank_after_compile.frontage_modelled == 0


def test_vision_building_renders_as_vision():
    m = street([bld("b1", 0, 100, 9, 20, source="vision")])
    r = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    assert {b.frontage.modelled.rendered for b in r.bins} == {"vision"}


def test_a_footprint_far_from_the_road_is_not_evidence():
    m = street([bld("far", 0, 100, 40, 50)])
    r = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    assert all(b.frontage.modelled.evidence == "none" for b in r.bins)


def test_photo_evidence_uses_the_side_the_camera_looks_at():
    o = make_observation(obs_id="obs-left", station_s=50.0, view=ViewKind.left)
    m = street(fused=["obs-left"])
    r = analyze_gaps(m, obs_map(o), make_fidelity_map(100.0, 1), None)
    assert [b.index for b in r.bins if b.frontage.modelled.evidence == "photo"] == [9, 10]
    assert all(b.frontage.opposite.evidence == "none" for b in r.bins)
    assert r.bins[9].frontage.modelled.rendered == "prior"  # a photo alone gives no geometry
    f = make_observation(obs_id="obs-fwd", station_s=10.0, view=ViewKind.forward)
    r2 = analyze_gaps(street(fused=["obs-fwd"]), obs_map(f), make_fidelity_map(100.0, 1), None)
    assert [b.index for b in r2.bins if b.frontage.opposite.evidence == "photo"] == [2, 3, 4]
    m2 = street([bld("b1", 40, 60, 9, 20)], fused=["obs-left"])
    assert analyze_gaps(m2, obs_map(o), make_fidelity_map(100.0, 1), None).bins[9].frontage.modelled.evidence == "osm+photo"


def test_ground_evidence_overview_hires_and_none():
    ref = straight_reference_line(100.0)
    lat, lon = ref.point_at(50, 0)
    tile = make_observation(obs_id="tile", kind=ObservationKind.satellite_overview, zoom=19, station_s=None, view=None,
                            size_px=(1280, 1280), lat=lat, lon=lon)
    fid = make_fidelity_map(100.0, 1)
    hires = fid.model_copy(update={"bins": [b.model_copy(update={"overhead_hires": b.index < 4}) for b in fid.bins]})
    r = analyze_gaps(street(fused=["tile"]), obs_map(tile), hires, None)
    assert [b.ground.evidence for b in r.bins[:5]] == ["satellite_hires"] * 4 + ["satellite_overview"]
    assert r.uncovered.ground == 0
    r2 = analyze_gaps(street(), obs_map(tile), fid, None)  # tile not fused into the model
    assert {b.ground.evidence for b in r2.bins} == {"none"}


def test_ground_rendered_comes_from_the_bake_report():
    src = ["satellite_hires"] * 8 + ["interpolated"] * 4 + ["procedural"] * 8
    tex = TextureRefs(ground=GroundStripRef(url="/media/textures/x/ground_v1_aaaaaaaa.jpg", size_px=(280, 1000),
                                            s_from=0, s_to=100, m_per_px=0.1, bin_source=src))
    r = analyze_gaps(street(), {}, make_fidelity_map(100.0, 1), tex)
    assert [b.ground.rendered for b in r.bins] == src
    grounds = [i for i in r.interpolations if i.what == "ground_texture"]
    assert [(i.s_from, i.s_to, i.method) for i in grounds] == [(40.0, 60.0, "neighbours"), (60.0, 100.0, "procedural")]
    assert grounds[0].from_s == [40.0, 60.0]


def _profile(kind: str) -> list[ProfileInterval]:
    out = []
    for i in range(4):
        state = FootpathState.unknown if kind in ("unknown",) else FootpathState.intact
        strips = [] if kind == "unknown" else make_strips("left", weight=2.0 if kind == "traced" else 0.1, tag=str(i))
        out.append(ProfileInterval(id=f"pi-{i}", s_from=25.0 * i, s_to=25.0 * (i + 1), footpath_state=state,
                                   state_votes={} if kind == "unknown" else {"intact": 0.3}, strips=strips))
    return out


@pytest.mark.parametrize("kind,evidence,rendered", [("traced", "traced", "traced"), ("weak", "state_only", "state_only"),
                                                    ("unknown", "prior", "prior")])
def test_footpath_evidence_and_rendering(kind, evidence, rendered):
    m = street().model_copy(update={"profile": _profile(kind)})
    r = compiled(m, make_fidelity_map(100.0, 1))
    assert {(b.footpath.evidence, b.footpath.rendered) for b in r.bins} == {(evidence, rendered)}
    assert r.blank_after_compile.footpath == 0


def test_unknown_interval_next_to_a_known_one_is_interpolated():
    prof = _profile("traced")
    prof[1] = ProfileInterval(id="pi-1", s_from=25.0, s_to=50.0, footpath_state=FootpathState.unknown)
    r = analyze_gaps(street().model_copy(update={"profile": prof}), {}, make_fidelity_map(100.0, 1), None)
    assert [b.footpath.rendered for b in r.bins[5:10]] == ["interpolated"] * 5
    fp = [i for i in r.interpolations if i.what == "footpath_state"]
    assert [(i.s_from, i.s_to, i.method) for i in fp] == [(25.0, 50.0, "neighbours")]
    assert fp[0].side == "left"


def test_furniture_evidence_follows_street_level_coverage():
    assert {b.furniture.evidence for b in analyze_gaps(street(), {}, make_fidelity_map(100.0, 0), None).bins} == {"none"}
    r = analyze_gaps(street(), {}, make_fidelity_map(100.0, 1), None)
    assert {b.furniture.evidence for b in r.bins} == {"street_level"}
    assert not [i for i in r.interpolations if i.what == "furniture_unknown"]


def test_frontage_interpolation_entries_name_physical_sides_and_anchors():
    m = street([bld("a", 0, 20, 9, 15), bld("b", 60, 80, 9, 15)])
    r = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    fr = [i for i in r.interpolations if i.what == "frontage" and i.side == "left"]
    blend = next(i for i in fr if i.method == "neighbours" and i.s_from == 20.0)
    assert blend.s_to == 60.0 and blend.from_s == [20.0, 60.0] and "blended between 20 m and 60 m" in blend.note
    tail = next(i for i in fr if i.s_from == 80.0)
    assert tail.method == "neighbours" and tail.s_to == 100.0 and "copied" in tail.note
    right = [i for i in r.interpolations if i.what == "frontage" and i.side == "right"]
    assert [(i.s_from, i.s_to, i.method) for i in right] == [(0.0, 100.0, "prior")]


def test_every_non_evidence_bin_is_covered_by_an_interpolation_entry():
    m = street([bld("a", 0, 20, 9, 15), bld("b", 60, 80, 9, 15)])
    r = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    for side, phys in (("modelled", "left"), ("opposite", "right")):
        for b in r.bins:
            if getattr(b.frontage, side).rendered in ("interpolated", "prior"):
                assert any(i.what == "frontage" and i.side == phys and min(i.s_to, b.s_to) - max(i.s_from, b.s_from) > 1e-6 for i in r.interpolations), (side, b.index)


def test_report_is_deterministic_and_round_trips():
    m = street([bld("a", 0, 20, 9, 15)])
    a = analyze_gaps(m, {}, make_fidelity_map(100.0, 1), None)
    b = analyze_gaps(CanonicalModel.model_validate_json(m.model_dump_json()), {}, make_fidelity_map(100.0, 1), None)
    assert a.model_dump_json() == b.model_dump_json()
    assert GapReport.model_validate_json(a.model_dump_json()) == a


def test_hints_name_the_missing_side():
    r = analyze_gaps(street([bld("a", 0, 100, 9, 15)]), {}, make_fidelity_map(100.0, 1), None)
    assert "right-hand frontage" in r.bins[3].hint and "15-20 m" in r.bins[3].hint


@pytest.mark.parametrize("name", ["bellandur", "curved"])
def test_recorded_and_curved_models_have_no_blank_bins(name):
    from tests.fixtures import make_model_fixtures as mk

    scene = json.loads((ROOT / "tests" / "fixtures" / "scene_v3" / f"{name}.json").read_text())
    assert scene["schema_version"] == 3
    res = mk.build_v3(mk.build())[name]
    r = res.gaps
    assert r.blank_total() == 0 and len(r.bins) == 20
    assert all(b.ground.rendered and b.frontage.modelled.rendered and b.frontage.opposite.rendered and b.footpath.rendered
               for b in r.bins)
    assert res.scene.about.interpolated == [i for i in r.interpolations if i.what != "furniture_unknown"]
