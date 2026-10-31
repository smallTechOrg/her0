"""The recompiled recorded Bellandur street (and the curved fixture) have zero blank 5 m bins per side."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hero.model.compile import compile_scene
from hero.model.scene_audit import audit_scene
from hero.model.scene_v2 import SceneV2
from hero.model.street_plan_types import StreetPlan
from hero.model.texture_types import GroundStripRef, TextureRefs
from hero.model.types import CanonicalModel, FidelityMap, Observation, ObservationKind
from hero.tools import audit_scene as audit_cli

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "tests" / "fixtures" / "recorded"
BELL = REC / "bellandur"
PLAN_DIR = ROOT / "tests" / "fixtures" / "plan"


@pytest.fixture(scope="module")
def inputs():
    model = CanonicalModel.model_validate_json((BELL / "model_v1.json").read_text())
    obs = {o["id"]: Observation.model_validate(o) for o in json.loads((BELL / "observations.json").read_text())}
    fid = FidelityMap.model_validate_json((BELL / "fidelity_v1.json").read_text())
    tile = next(o for o in obs.values() if o.kind is ObservationKind.satellite_overview)
    return model, obs, fid, tile


@pytest.fixture(scope="module")
def result(inputs):
    model, obs, fid, tile = inputs
    return compile_scene(model, observations=obs, assets={}, tile=tile, fidelity=fid, slug_name="Bellandur Main Road",
                         city="Bengaluru")


def independent_blanks(plan) -> dict[str, int]:
    """Blank 5 m bins counted straight off the plan (written separately from ``hero.model.gaps.blank_from_plan``)."""
    n = math.ceil(plan.length_m / 5.0 - 1e-9)
    out = dict.fromkeys(("ground", "frontage_modelled", "frontage_opposite", "footpath"), 0)
    for i in range(n):
        lo, hi = 5.0 * i, min(plan.length_m, 5.0 * (i + 1))
        in_bin = [st for st in plan.stations if lo <= st.s < hi or (i == n - 1 and st.s == hi)]
        if not in_bin or not all(st.ground_src and st.ground_src != "none" for st in in_bin):
            out["ground"] += 1
        if not in_bin:
            out["footpath"] += 1
        for sd in plan.frontage.sides:
            if covered_len(sd.segments, lo, hi) < (hi - lo) - 1e-3:
                out["frontage_" + sd.side] += 1
    return out


def covered_len(segs, lo, hi):
    return sum(max(0.0, min(g.s_to, hi) - max(g.s_from, lo)) for g in segs)


def test_baseline_v2_scene_has_holes_but_the_recompiled_plan_has_none(result):
    base = audit_scene(json.loads((REC / "bellandur_scene_v2.json").read_text()))
    assert base.hole_bins["modelled"] and base.hole_bins["opposite"]
    rep = audit_scene(result.plan)
    assert rep.kind == "street_plan" and rep.totals["n_bins"] == 20
    assert rep.hole_bins == {"modelled": [], "opposite": []}
    assert rep.ground_hole_bins == {"modelled": [], "opposite": []}
    assert rep.totals["blank_bins"] == {"modelled": 0, "opposite": 0}


def test_every_5m_bin_of_both_frontages_is_tiled_by_segments(result):
    for side in result.plan.frontage.sides:
        for i in range(20):
            lo, hi = 5.0 * i, min(100.0, 5.0 * (i + 1))
            assert covered_len(side.segments, lo, hi) == pytest.approx(hi - lo, abs=1e-6), (side.side, i)


def test_every_ground_bin_has_a_source_and_gap_report_has_zero_blanks(result):
    assert len(result.plan.ground_tex.bin_source) == 20 and all(result.plan.ground_tex.bin_source)
    g = result.gaps
    derived = independent_blanks(result.plan)
    assert derived == dict.fromkeys(derived, 0), "the plan itself has no blank bin"
    assert g.blank_after_compile.model_dump() == derived and g.blank_total() == 0
    assert len(g.bins) == 20 and all(b.ground.rendered and b.footpath.rendered for b in g.bins)
    assert all(b.frontage.modelled.rendered in ("osm", "vision", "interpolated", "prior") for b in g.bins)


def test_interpolated_bins_are_listed_in_the_scene_about(result):
    listed = result.scene.about.interpolated
    assert listed and result.scene.about.gap_summary
    for side, phys in (("modelled", "left"), ("opposite", "right")):
        for b in result.gaps.bins:
            if getattr(b.frontage, side).rendered in ("interpolated", "prior"):
                assert any(i.what == "frontage" and i.side == phys and min(i.s_to, b.s_to) - max(i.s_from, b.s_from) > 1e-6
                           for i in listed)


def test_with_baked_textures_the_plan_still_has_no_holes(inputs):
    model, obs, fid, tile = inputs
    src = ["satellite_hires"] * 6 + ["satellite_overview"] * 8 + ["interpolated"] * 3 + ["procedural"] * 3
    tex = TextureRefs(ground=GroundStripRef(url="/media/textures/b/ground_v1_aaaaaaaa.jpg", size_px=(280, 1000), s_from=0,
                                            s_to=100, m_per_px=0.1, bin_source=src))
    r = compile_scene(model, observations=obs, assets={}, tile=tile, fidelity=fid, slug_name="B", city="C", textures=tex)
    rep = audit_scene(r.plan)
    assert not any(rep.hole_bins.values()) and not any(rep.ground_hole_bins.values())
    assert r.plan.ground_tex.mode == "baked_strip" and r.plan.ground_tex.bin_source == src
    assert [b.ground.rendered for b in r.gaps.bins] == src


def test_dynamic_split_on_the_recorded_street(result, inputs):
    model = inputs[0]
    assert not {o.type for o in result.plan.objects} & {"parked_vehicle", "vendor_stall"}
    assert result.scene.dynamic_context.by_type["parked_vehicle"].count >= 30
    assert result.plan.dynamic_context.spawn_hints.density > 0.5
    assert sum(result.plan.dynamic_context.spawn_hints.type_weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert model.model_dump_json() == CanonicalModel.model_validate_json((BELL / "model_v1.json").read_text()).model_dump_json()


@pytest.mark.parametrize("name", ["bellandur", "curved"])
def test_audit_tool_on_the_committed_plans_asserts_no_holes(name, capsys):
    path = PLAN_DIR / f"{name}.plan.json"
    assert StreetPlan.model_validate_json(path.read_text()).length_m == pytest.approx(100.0, abs=0.01)
    assert audit_cli.main([str(path), "--assert-no-holes"]) == 0
    assert "frontage holes  0/20" in capsys.readouterr().out


def test_committed_bellandur_plan_matches_a_fresh_compile(result):
    committed = json.loads((PLAN_DIR / "bellandur.plan.json").read_text())
    assert committed == json.loads(result.plan.model_dump_json())


def test_v2_compile_of_the_same_model_is_still_available(inputs):
    from hero.model.compile import compile_scene_v2

    model, obs, fid, tile = inputs
    v2 = compile_scene_v2(model, observations=obs, assets={}, tile=tile, fidelity=fid, slug_name="B", city="C")
    assert isinstance(v2, SceneV2) and any(o.type == "parked_vehicle" for o in v2.objects)


def _max_slope(plan):
    return max(abs(g.lat_to - g.lat_from) / (g.s_to - g.s_from) for sd in plan.frontage.sides for g in sd.segments)


def test_fresh_bellandur_frontage_wall_line_is_smooth(result):
    from hero.model.frontage import MAX_SLOPE

    assert _max_slope(result.plan) <= MAX_SLOPE + 1e-6
    for sd in result.plan.frontage.sides:  # still zero holes: contiguous tiling of [0, length]
        assert sd.segments[0].s_from == 0.0 and sd.segments[-1].s_to == pytest.approx(100.0, abs=1e-6)
        assert all(a.s_to == pytest.approx(b.s_from, abs=1e-6) for a, b in zip(sd.segments, sd.segments[1:]))
        # no sky notch: a wall/gate never stands lower than the lowest building of its side (it was merged into the line)
        low = min(g.height_m for g in sd.segments if g.kind == "building")
        assert all(g.height_m >= low - 1e-6 for g in sd.segments if g.source == "entity")


@pytest.mark.parametrize("name", ["bellandur", "curved"])
def test_committed_plans_have_no_steep_frontage_segments(name):
    from hero.model.frontage import MAX_SLOPE

    plan = StreetPlan.model_validate_json((PLAN_DIR / f"{name}.plan.json").read_text())
    assert _max_slope(plan) <= MAX_SLOPE + 1e-6


def test_smooth_evidence_snaps_outliers_and_keeps_entities():
    from hero.model.frontage import EvCell, SideEvidence, cell_grid, smooth_evidence

    def cell(kind, a, b):
        return EvCell(kind, "entity" if kind != "building" else "osm", a, b, 5.0, "#aaaaaa", 0, None, None, 0.9)

    lats = [10.0, 10.0, 28.0, 10.2, 10.0]
    cells = [cell("building", v, v) for v in lats]
    cells[1] = cell("wall", 3.0, 3.0)  # a confident wall far off the building line is merged into the line (no wedge)
    cells[0] = cell("building", 10.0, 22.0)  # steep edge within a cell -> flattened
    ev = SideEvidence(tuple(cells), (2.5,) * 5, tuple(cell_grid(12.5)))
    out = smooth_evidence(ev).cells
    assert out[1].kind == "wall" and out[1].snapped and out[1].lat_from == out[1].lat_to == pytest.approx(10.0, abs=0.3)
    on_line = cell("wall", 10.4, 10.4)
    ev2 = SideEvidence(tuple([cells[0], cells[1], on_line, cells[3], cells[4]]), (2.5,) * 5, tuple(cell_grid(12.5)))
    assert smooth_evidence(ev2).cells[2].lat_from == 10.4                       # on the line: lateral kept
    assert out[2].snapped and out[2].lat_from == out[2].lat_to < 12.0
    assert not out[3].snapped
    assert abs(out[0].lat_to - out[0].lat_from) <= 0.25 * 2.5 + 1e-9
