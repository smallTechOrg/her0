"""Ground texture plan: baked strip vs procedural, bin sources, seed, station ground_src."""

from __future__ import annotations

import pytest

from hero.export import load_gameplay
from hero.export.gameplay_types import GameplayV1
from hero.model import ground
from hero.model.compile import compile_scene
from hero.model.texture_types import FrontageStripRef, GroundStripRef, TextureRefs
from tests.fixtures.street_fixtures import make_fidelity_map, make_model, straight_reference_line

GP = GameplayV1.model_validate(load_gameplay())


def strip(sources, length=100.0):
    return GroundStripRef(url="/media/textures/s/ground_v1_abcd1234.jpg", size_px=(280, 1000), s_from=0.0, s_to=length,
                          m_per_px=0.1, bin_source=sources)


def plan_with(textures, length=100.0, slug="fixture-curved-street"):
    m = make_model(straight_reference_line(length), slug=slug)
    return compile_scene(m, observations={}, assets={}, tile=None, fidelity=make_fidelity_map(length, 1), slug_name="T",
                         city="C", textures=textures, gameplay=GP).plan


def test_without_textures_the_ground_is_procedural_and_nothing_is_blank():
    p = plan_with(None)
    g = p.ground_tex
    assert g.mode == "procedural" and g.strip is None and g.bin_m == 5.0
    assert g.bin_source == ["procedural"] * 20
    assert {st.ground_src for st in p.stations} == {"procedural"}


def test_baked_strip_keeps_the_bake_report_and_stations_follow_it():
    src = ["satellite_hires"] * 10 + ["interpolated"] * 5 + ["satellite_overview"] * 5
    p = plan_with(TextureRefs(ground=strip(src)))
    assert p.ground_tex.mode == "baked_strip" and p.ground_tex.strip.url.endswith("abcd1234.jpg")
    assert p.ground_tex.bin_source == src
    by_s = {st.s: st.ground_src for st in p.stations}
    assert by_s[0.0] == "satellite_hires" and by_s[47.5] == "satellite_hires" and by_s[50.0] == "interpolated"
    assert by_s[75.0] == "satellite_overview" and by_s[100.0] == "satellite_overview"


def test_bin_source_is_resized_to_the_bin_count():
    assert ground.bin_sources(100.0, TextureRefs(ground=strip(["satellite_hires"] * 3))) == \
        ["satellite_hires"] * 3 + ["procedural"] * 17
    assert len(ground.bin_sources(100.0, TextureRefs(ground=strip(["satellite_hires"] * 30)))) == 20
    assert len(ground.bin_sources(52.0, None)) == 11


def test_procedural_seed_is_stable_per_slug_and_int32():
    a, b = ground.procedural_seed("street-a"), ground.procedural_seed("street-b")
    assert a == ground.procedural_seed("street-a") != b and 0 <= a < 2 ** 31
    assert ground.procedural_seed("street-a", TextureRefs(procedural_seed=7)) == 7
    assert plan_with(None, slug="street-a").ground_tex.procedural_seed == a


def test_frontage_textures_are_attached_by_side():
    ref = FrontageStripRef(side="modelled", url="/media/textures/s/frontage_modelled_v1_aaaaaaaa.jpg", size_px=(1024, 128),
                           s_from=0.0, s_to=100.0, bin_source=["street_view"] * 20)
    p = plan_with(TextureRefs(frontage={"modelled": ref}))
    tex = {s.side: s.texture for s in p.frontage.sides}
    assert tex["modelled"] == ref and tex["opposite"] is None


@pytest.mark.parametrize("length", [100.0, 52.0, 5.0, 4.0])
def test_bin_count_matches_ceil_length_over_five(length):
    assert len(plan_with(None, length=length).ground_tex.bin_source) == -(-int(length) // 5)
