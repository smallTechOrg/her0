"""hero.model.scene_audit + hero.tools.audit_scene on hand-made v2 scenes and a hand-made StreetPlan dict."""

from __future__ import annotations

import copy
import json

import pytest

from hero.model.scene_audit import audit_scene, make_bins
from hero.tools import audit_scene as audit_cli


def _station(s, *, fp=(5.0, 6.5), state="intact", tile=True):
    strips = [{"kind": "carriageway", "lo": -4.0, "hi": 5.0, "material": "asphalt", "height_m": 0.0, "confidence": 0.5}]
    return {"s": s, "lane_lat": [5.5, 4.0, 2.2],
            "footpath": None if fp is None else {"lo": fp[0], "hi": fp[1], "state": state, "material": "unknown",
                                                 "height_m": 0.15, "confidence": 0.5},
            "strips": strips, "ground_uv": [[0.1, 0.9], [0.9, 0.9]] if tile else None}


def v2_scene(*, buildings, objects=(), length=20.0, tile=True, fp_missing_at=()):
    sts = [_station(s, fp=None if s in fp_missing_at else (5.0, 6.5), tile=tile) for s in [0, 2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20]]
    sts = [x for x in sts if x["s"] <= length]
    return {
        "schema_version": 2,
        "segment": {"slug": "hand", "length_m": length, "footpath_side": "left"},
        "ground": {"tile_url": "/t.png" if tile else None},
        "corridor": {"step_m": 2.5, "ground_half_m": 30.0, "stations": sts},
        "cross_section": {"footpath_coverage": [
            {"from_m": 0.0, "to_m": 10.0, "state": "intact", "confidence": 0.5},
            {"from_m": 10.0, "to_m": length, "state": "broken", "confidence": 0.4}]},
        "buildings": buildings,
        "objects": list(objects),
    }


def bld(fp, id_="b"):
    return {"id": id_, "footprint": fp, "height_m": 9.0, "side": "modelled"}


# street-facing block on the modelled side covering s 0..10, lat 7..15 (edge of strips is 6.5 -> reach ok)
NEAR = bld([[0, 7], [10, 7], [10, 15], [0, 15]])


def test_make_bins_last_one_shorter():
    assert make_bins(12.0) == [(0.0, 5.0), (5.0, 10.0), (10.0, 12.0)]
    assert make_bins(10.0)[-1] == (5.0, 10.0)


def test_v2_footpath_intervals_and_states():
    r = audit_scene(v2_scene(buildings=[NEAR]))
    assert [(i["from_m"], i["to_m"], i["state"]) for i in r.footpath_intervals] == [(0.0, 10.0, "intact"), (10.0, 20.0, "broken")]
    assert r.footpath_state_m == {"broken": 10.0, "intact": 10.0}
    assert r.totals["footpath_non_intact_m"] == 10.0


def test_v2_frontage_holes_per_side_and_bin():
    r = audit_scene(v2_scene(buildings=[NEAR]))
    assert r.totals["n_bins"] == 4
    # modelled side: bins 0,1 covered at the street; 2,3 empty. opposite: nothing anywhere.
    assert r.hole_bins["modelled"] == [2, 3]
    assert r.hole_bins["opposite"] == [0, 1, 2, 3]
    row = r.frontage_coverage["modelled"][0]
    assert row["covered_m"] == pytest.approx(5.0) and row["street_facing_m"] == pytest.approx(5.0)
    assert row["building_m"] == pytest.approx(5.0) and row["hole"] is False
    assert row["lateral_depth_m"] == pytest.approx(8.0)  # 7..15
    assert row["gap_to_street_m"] == pytest.approx(0.5)  # 7.0 - 6.5


def test_v2_detached_block_is_a_hole_even_though_along_s_covered():
    far = bld([[0, 20], [20, 20], [20, 30], [0, 30]])  # 13.5 m set back from the street edge
    r = audit_scene(v2_scene(buildings=[far]))
    row = r.frontage_coverage["modelled"][1]
    assert row["covered_m"] == pytest.approx(5.0)
    assert row["evidence_hole"] is False and row["hole"] is True
    assert row["gap_to_street_m"] == pytest.approx(13.5)


def test_v2_walls_count_and_short_walls_do_not_fill_a_bin():
    wall = {"type": "wall", "x_m": 0.0, "length_m": 5.0, "lateral_m": 7.0, "size_m": {"w": 0.3, "h": 2.0, "d": 5.0}}
    stub = {"type": "wall", "x_m": 5.0, "length_m": 1.0, "lateral_m": 7.0, "size_m": {"w": 0.3, "h": 2.0, "d": 1.0}}
    r = audit_scene(v2_scene(buildings=[], objects=[wall, stub]))
    rows = r.frontage_coverage["modelled"]
    assert rows[0]["wall_m"] == pytest.approx(5.0) and rows[0]["hole"] is False
    assert rows[1]["covered_m"] == pytest.approx(1.0) and rows[1]["hole"] is True


def test_v2_ground_strips_are_flat_and_listed_without_texture():
    r = audit_scene(v2_scene(buildings=[NEAR]))
    kinds = {x["kind"] for x in r.strips_without_texture}
    assert {"carriageway", "footpath"} <= kinds
    assert all("flat" in x["reason"] for x in r.strips_without_texture)
    assert all(row["hole"] for row in r.ground_coverage["modelled"])  # surface strips carry no photo texture
    row = r.ground_coverage["modelled"][0]
    assert row["flat_surface_m"] > 0 and row["tile_m"] > 0  # tile only visible beside the strips


def test_v2_missing_footpath_and_no_tile_reported():
    r = audit_scene(v2_scene(buildings=[NEAR], tile=False, fp_missing_at=(5, 7.5)))
    assert any(x["kind"] == "footpath_missing" for x in r.strips_without_texture)
    assert all("no ground tile" in x["reason"] for x in r.strips_without_texture if x["kind"] == "carriageway")
    assert r.ground_coverage["modelled"][0]["tile_m"] == 0.0


def test_audit_is_pure_and_deterministic():
    sc = v2_scene(buildings=[NEAR])
    before = copy.deepcopy(sc)
    a, b = audit_scene(sc).to_dict(), audit_scene(sc).to_dict()
    assert a == b and sc == before
    assert json.loads(json.dumps(a)) == a


def test_rejects_non_scene():
    with pytest.raises(ValueError):
        audit_scene({"hello": "world"})
    with pytest.raises(TypeError):
        audit_scene(42)


# ----------------------------------------------------------------------------- StreetPlan (duck-typed)


def plan_dict(*, holes=False, length=10.0, ground=("satellite_hires", "interpolated")):
    seg = lambda a, b, **kw: {"s_from": a, "s_to": b, "lat_from": 8.0, "lat_to": 8.0, "height_m": 8.0, "kind": "building",  # noqa: E731
                              "colour_hex": "#aabbcc", "storeys": 2, "source": "osm", "interpolated": False,
                              "confidence": 0.8, **kw}
    mod = [seg(0.0, length)]
    opp = [seg(0.0, 4.0), seg(6.0, length, source="interpolated", interpolated=True)] if holes else \
          [seg(0.0, 4.0), seg(4.0, length, source="interpolated", interpolated=True)]
    sts = [{"s": s, "lane_lat": [5.0, 3.0, 1.0], "kerb": {"lat": 4.0, "height_m": 0.15, "width_m": 0.2},
            "footpath": None if s == 5.0 else {"lo": 4.2, "hi": 5.7, "state": "intact", "confidence": 0.7},
            "strips": [], "ground_src": ground[0] if s < 5 else ground[1]} for s in (0.0, 2.5, 5.0, 7.5, 10.0)]
    return {"plan": "street-plan/1", "slug": "hand-plan", "footpath_side": "right", "length_m": length,
            "stations": sts,
            "ground_tex": {"mode": "procedural", "bin_m": 5.0, "bin_source": list(ground)},
            "frontage": {"step_m": 2.5, "sides": [{"side": "modelled", "texture": None, "segments": mod},
                                                  {"side": "opposite", "texture": None, "segments": opp}]}}


def test_plan_with_tiled_frontage_has_no_holes():
    r = audit_scene(plan_dict())
    assert r.kind == "street_plan" and r.slug == "hand-plan"
    assert r.hole_bins == {"modelled": [], "opposite": []}
    assert r.ground_hole_bins == {"modelled": [], "opposite": []}
    assert r.side_map == {"modelled": "right", "opposite": "left"}
    assert r.frontage_coverage["opposite"][1]["interpolated_m"] == pytest.approx(5.0)
    assert r.totals["blank_bins"] == {"modelled": 0, "opposite": 0}


def test_plan_footpath_intervals_from_stations_and_missing():
    r = audit_scene(plan_dict())
    states = [(i["state"], i["from_m"], i["to_m"]) for i in r.footpath_intervals]
    assert states == [("intact", 0.0, 3.75), ("missing", 3.75, 6.25), ("intact", 6.25, 10.0)]


def test_plan_gap_in_frontage_is_found():
    r = audit_scene(plan_dict(holes=True))
    assert r.hole_bins["modelled"] == []
    assert r.hole_bins["opposite"] == [0, 1]  # 4..6 m uncovered touches both 5 m bins
    assert r.frontage_coverage["opposite"][0]["covered_m"] == pytest.approx(4.0)
    assert r.frontage_coverage["opposite"][1]["covered_m"] == pytest.approx(4.0)


def test_plan_blank_ground_bin_is_a_ground_hole_but_painted_is_not():
    r = audit_scene(plan_dict(ground=("none", "procedural")))
    assert r.ground_hole_bins["modelled"] == [0] and r.ground_hole_bins["opposite"] == [0]
    assert [x["source"] for x in r.strips_without_texture] == ["none", "procedural"]


def test_plan_accepts_pydantic_like_object():
    class Boxed:
        def __init__(self, d):
            self.d = d

        def model_dump(self, mode="python"):
            return self.d

    assert audit_scene(Boxed(plan_dict())).to_dict() == audit_scene(plan_dict()).to_dict()


def test_plan_dict_validates_as_real_street_plan_types_where_possible():
    """The hand-made dict's frontage/ground/station shapes are the real StreetPlan sub-models' shapes."""
    from hero.model.street_plan_types import FrontageSidePlan, GroundTexPlan, PlanStation

    d = plan_dict()
    GroundTexPlan.model_validate(d["ground_tex"])
    for side in d["frontage"]["sides"]:
        FrontageSidePlan.model_validate(side)
    for st in d["stations"]:
        PlanStation.model_validate(st)


# ----------------------------------------------------------------------------- CLI


def test_cli_prints_and_asserts(tmp_path, capsys):
    ok = tmp_path / "ok.plan.json"
    ok.write_text(json.dumps(plan_dict()))
    bad = tmp_path / "bad.plan.json"
    bad.write_text(json.dumps(plan_dict(holes=True)))
    assert audit_cli.main([str(ok), "--assert-no-holes"]) == 0
    assert "frontage holes  0/2" in capsys.readouterr().out
    assert audit_cli.main([str(bad), "--assert-no-holes"]) == 1
    assert "HOLES" in capsys.readouterr().err
    out = tmp_path / "r.json"
    assert audit_cli.main([str(bad), "--out", str(out)]) == 0
    saved = json.loads(out.read_text())
    assert saved["source"]["file"] == "bad.plan.json" and saved["hole_bins"]["opposite"] == [0, 1]


def test_cli_rejects_garbage_and_needs_one_input(tmp_path, capsys):
    junk = tmp_path / "junk.json"
    junk.write_text("{}")
    assert audit_cli.main([str(junk)]) == 2
    with pytest.raises(SystemExit):
        audit_cli.main([])


def test_cli_slug_reads_stored_scene_read_only(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "h.db"
    con = sqlite3.connect(db)
    con.execute("create table segments (slug text, scene_json text)")
    con.execute("insert into segments values (?, ?)", ("hand", json.dumps(v2_scene(buildings=[NEAR]))))
    con.commit()
    con.close()
    monkeypatch.setenv("HERO_DATABASE_URL", f"sqlite:///{db}")
    assert audit_cli.main(["--slug", "hand"]) == 0
    with pytest.raises(SystemExit):
        audit_cli.main(["--slug", "nope"])
