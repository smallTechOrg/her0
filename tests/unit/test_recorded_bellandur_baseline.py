"""The recorded real Bellandur v2 scene: root-cause baseline (> 0 hole bins per side) + the recorded model fixtures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from hero.model.scene_audit import audit_scene
from hero.model.scene_v2 import load_scene
from hero.model.types import CanonicalModel, Observation
from hero.tools import export_recorded

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "tests" / "fixtures" / "recorded"
SCENE = REC / "bellandur_scene_v2.json"
BELL = REC / "bellandur"
EVIDENCE = ROOT / "docs" / "evidence" / "phase2_1" / "rootcause_v2.json"


@pytest.fixture(scope="module")
def scene():
    return json.loads(SCENE.read_text())


@pytest.fixture(scope="module")
def report(scene):
    return audit_scene(scene)


def test_recorded_scene_is_a_valid_real_v2_scene(scene):
    assert scene["schema_version"] == 2
    load_scene(scene)  # validates against SceneV2
    assert scene["segment"]["slug"] == "bellandur-main-road-n-2"


def test_baseline_has_hole_bins_on_both_sides(report):
    assert report.totals["n_bins"] == 20
    assert report.totals["frontage_hole_bins"]["modelled"] > 0
    assert report.totals["frontage_hole_bins"]["opposite"] > 0
    assert report.hole_bins["modelled"] and report.hole_bins["opposite"]


def test_baseline_footpath_intervals_match_the_scene(scene, report):
    cov = scene["cross_section"]["footpath_coverage"]
    assert [(i["from_m"], i["to_m"], i["state"]) for i in report.footpath_intervals] == \
        [(c["from_m"], c["to_m"], c["state"]) for c in cov]
    assert report.footpath_state_m["encroached"] == pytest.approx(27.5)


def test_baseline_ground_strips_are_untextured_everywhere(report):
    assert {"footpath", "carriageway"} <= {x["kind"] for x in report.strips_without_texture}
    for side in ("modelled", "opposite"):
        assert len(report.ground_hole_bins[side]) == report.totals["n_bins"]


def test_baseline_street_gap_is_real_not_an_artifact(report):
    """Along-s evidence exists nearly everywhere (spec rule), but facades are set back from the street."""
    mod = report.frontage_coverage["modelled"]
    assert sum(r["evidence_hole"] for r in mod) == 0
    assert sum(r["covered_m"] for r in mod) > 90
    assert max(r["gap_to_street_m"] for r in mod) > 5.0


def test_committed_evidence_equals_a_fresh_audit(scene, report):
    committed = json.loads(EVIDENCE.read_text())
    fresh = report.to_dict()
    assert committed["source"] == {"file": SCENE.name, "sha256": hashlib.sha256(SCENE.read_bytes()).hexdigest()}
    fresh["source"] = committed["source"]
    assert committed == fresh


# ----------------------------------------------------------------------------- recorded model fixtures


def test_recorded_model_validates_and_matches_scene(scene):
    m = json.loads((BELL / "model_v1.json").read_text())
    CanonicalModel.model_validate(m)
    manifest = json.loads((BELL / "manifest.json").read_text())
    assert manifest["model_hash"] == scene["model"]["model_hash"]
    assert m["segment_slug"] == "bellandur-main-road-n-2" and m["version"] == scene["model"]["version"]
    assert len(m["entities"]) == 112


def test_recorded_observations_validate_and_images_exist_within_budget():
    obs = [Observation.model_validate(o) for o in json.loads((BELL / "observations.json").read_text())]
    m = json.loads((BELL / "model_v1.json").read_text())
    assert {o.id for o in obs} == set(m["fused_observation_ids"])
    total = 0
    for o in obs:
        p = BELL / "images" / Path(o.file_path).name
        assert p.is_file(), o.file_path
        raw = p.read_bytes()
        total += len(raw)
        assert hashlib.sha256(raw).hexdigest() == o.sha256 and len(raw) == o.bytes
        with Image.open(p) as im:
            assert im.size == (o.width_px, o.height_px)
    assert total <= 4_000_000
    assert {o.kind.value for o in obs} == {"satellite_overview", "satellite_crop", "streetview"}


# ----------------------------------------------------------------------------- export_recorded tool


def _tiny_db(tmp_path):
    src = tmp_path / "src"
    (src / "imagery").mkdir(parents=True)
    Image.new("RGB", (64, 64), (200, 10, 10)).save(src / "imagery" / "a.png")
    db = src / "h.db"
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    con.executescript("""
        create table segments (id text, slug text, name text, city text, start_lat real, start_lon real, end_lat real,
            end_lon real, footpath_side text, length_m real, heading_deg real);
        create table model_versions (segment_id text, version int, model_json text, fidelity_json text, model_hash text);
        create table observations (id text, segment_id text, obs_key text, kind text, source text, sha256 text,
            file_path text, media_type text, width_px int, height_px int, bytes int, lat real, lon real, heading_deg real,
            pitch_deg real, fov_deg real, zoom int, scale int, meters_per_px real, pano_id text, capture_date text,
            station_s real, offset_t real, view text, georef_json text, cost_usd real, acquired_run_id text, note text,
            created_at text);""")
    con.execute("insert into segments values ('s1','tiny','T','C',1,2,3,4,'left',50,90)")
    con.execute("insert into model_versions values ('s1',2,?,?,?)",
                (json.dumps({"fused_observation_ids": ["o1"], "entities": []}), "{}", "h" * 64))
    con.execute("insert into observations values ('o1','s1','k','satellite_crop','google_static_maps',?, 'imagery/a.png',"
                "'image/png',64,64,1,1,2,null,null,null,20,2,0.1,null,null,null,null,null,'{}',0,null,null,'2026-10-30 10:00:00')",
                ("0" * 64,))
    con.commit()
    con.close()
    return src


def test_export_recorded_roundtrip_and_leaves_source_untouched(tmp_path):
    src = _tiny_db(tmp_path)
    before = (src / "h.db").read_bytes()
    copy = tmp_path / "scratch" / "copy.db"
    export_recorded.backup_copy(src / "h.db", copy)
    out = tmp_path / "out"
    m = export_recorded.export_recorded(copy, src, "tiny", out, max_bytes=100_000)
    assert m["observations"] == 1 and m["model_version"] == 2
    obs = json.loads((out / "observations.json").read_text())
    assert obs[0]["media_type"] == "image/jpeg" and obs[0]["file_path"].endswith(".jpg")
    assert "original sha256 " + "0" * 64 in obs[0]["note"]
    assert (out / "images" / Path(obs[0]["file_path"]).name).is_file()
    assert json.loads((out / "model_v2.json").read_text())["fused_observation_ids"] == ["o1"]
    assert (src / "h.db").read_bytes() == before  # the original was only read
    # deterministic
    export_recorded.export_recorded(copy, src, "tiny", tmp_path / "out2", max_bytes=100_000)
    assert (out / "observations.json").read_bytes() == (tmp_path / "out2" / "observations.json").read_bytes()


def test_export_recorded_errors(tmp_path):
    src = _tiny_db(tmp_path)
    with pytest.raises(SystemExit, match="no segment"):
        export_recorded.export_recorded(src / "h.db", src, "nope", tmp_path / "o")
    with pytest.raises(SystemExit, match="cannot fit"):
        # a noisy image cannot fit in 10 bytes at any quality
        import os
        Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3)).save(src / "imagery" / "a.png")
        export_recorded.export_recorded(src / "h.db", src, "tiny", tmp_path / "o", max_bytes=10)
