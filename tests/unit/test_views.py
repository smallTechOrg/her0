"""Street batches, overhead windows, captions and prompts."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest
from PIL import Image

from hero.fusion import views
from hero.fusion.views import (
    build_overhead_windows,
    build_street_batches,
    overhead_prompt,
    plan_window_grids,
    street_prompt,
)
from hero.model.types import ObservationKind, ViewKind
from hero.prompts import render_prompt
from tests.fixtures.street_fixtures import (
    curved_reference_line,
    make_observation,
    straight_reference_line,
)

TILE = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "tile_1280.png"


def _sv(tmp_path, n, s, view=ViewKind.forward, heading=0.0):
    o = make_observation(obs_id=f"obs-{n:03d}", station_s=s, view=view, heading_deg=heading)
    f = tmp_path / o.file_path
    f.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 640), (n * 10 % 255, 90, 90)).save(f, format="JPEG")
    return o


@pytest.fixture
def ref():
    return straight_reference_line(100.0)


# ---------------------------------------------------------------- street batches

def test_street_batches_are_contiguous_windows_of_at_most_max_images(tmp_path, ref):
    obs = [_sv(tmp_path, i, i * 8.0) for i in range(10)]
    batches = build_street_batches(obs, ref, data_dir=tmp_path, max_images=4)
    assert [len(b.observations) for b in batches] == [4, 4, 2]
    stations = [o.station_s for b in batches for o in b.observations]
    assert stations == sorted(stations)
    assert all(len(b.images) == len(b.captions) == len(b.observations) for b in batches)


def test_street_batches_are_deterministic_for_any_input_order(tmp_path, ref):
    obs = [_sv(tmp_path, i, (i % 5) * 10.0, view=list(ViewKind)[i % 4]) for i in range(12)]
    a = build_street_batches(obs, ref, data_dir=tmp_path, max_images=5)
    shuffled = obs[:]
    random.Random(3).shuffle(shuffled)
    b = build_street_batches(shuffled, ref, data_dir=tmp_path, max_images=5)
    assert [x.batch_id for x in a] == [x.batch_id for x in b]
    assert [[o.id for o in x.observations] for x in a] == [[o.id for o in x.observations] for x in b]
    assert len({x.batch_id for x in a}) == len(a)


def test_street_batch_captions_state_id_view_station_heading_and_forward_convention(tmp_path, ref):
    o = _sv(tmp_path, 1, 20.0, view=ViewKind.right, heading=90.0)
    cap = build_street_batches([o], ref, data_dir=tmp_path, max_images=8)[0].captions[0]
    for needle in ("Image 1 of 1", "obs-001", "view: right", "s=20.0 m", "+90 deg relative to travel",
                   "right of travel", "forward = direction of travel", "640x640"):
        assert needle in cap, needle
    img = build_street_batches([o], ref, data_dir=tmp_path, max_images=8)[0].images[0]
    assert img.caption == cap and (img.width_px, img.height_px) == (640, 640) and img.media_type == "image/jpeg"
    assert img.data[:2] == b"\xff\xd8"


def test_relative_heading_follows_the_curve(tmp_path):
    ref = curved_reference_line()
    o = make_observation(station_s=60.0, heading_deg=ref.bearing_deg(60.0) + 10)
    assert views.relative_heading_deg(o, ref) == pytest.approx(10.0, abs=1e-6)
    assert views.relative_heading_deg(make_observation(station_s=None), ref) is None


def test_non_street_and_unstationed_observations_are_excluded(tmp_path, ref):
    sat = make_observation(obs_id="obs-sat", kind=ObservationKind.satellite_overview, station_s=None, zoom=19)
    nostation = make_observation(obs_id="obs-ns", station_s=None)
    good = _sv(tmp_path, 1, 5.0)
    batches = build_street_batches([sat, nostation, good], ref, data_dir=tmp_path, max_images=8)
    assert [o.id for b in batches for o in b.observations] == [good.id]
    assert build_street_batches([], ref, data_dir=tmp_path, max_images=8) == []


def test_street_batches_error_paths(tmp_path, ref):
    with pytest.raises(ValueError):
        build_street_batches([], ref, data_dir=tmp_path, max_images=0)
    missing = make_observation(obs_id="obs-missing", station_s=1.0)
    with pytest.raises(FileNotFoundError):
        build_street_batches([missing], ref, data_dir=tmp_path, max_images=8)


def test_street_prompt_lists_every_frame_and_the_footpath_side(tmp_path, ref):
    obs = [_sv(tmp_path, i, i * 10.0) for i in range(3)]
    batch = build_street_batches(obs, ref, data_dir=tmp_path, max_images=8)[0]
    text = street_prompt(batch, ref, footpath_side="left")
    assert text.startswith("<node:streetlevel>")
    assert "{{" not in text
    for i in (1, 2, 3):
        assert f"image {i}:" in text
    assert "station s=20.0 m" in text and "footpath is on the left side" in text


# ---------------------------------------------------------------- overhead

def test_window_grids_tile_the_street_and_keep_full_size():
    g = plan_window_grids(100.0)
    assert [(w.s_bottom, w.s_top) for w in g] == [(0.0, 50.0), (50.0, 100.0)]
    assert g[0].size_px == (280, 500) and g[0].t_left == 14 and g[0].t_right == -14
    g = plan_window_grids(120.0)
    assert [(w.s_bottom, w.s_top) for w in g] == [(0.0, 50.0), (50.0, 100.0), (70.0, 120.0)]
    short = plan_window_grids(30.0)
    assert [(w.s_bottom, w.s_top) for w in short] == [(0.0, 30.0)]


def test_window_grids_cap_at_four_keeping_the_centre():
    g = plan_window_grids(400.0)
    assert [w.s_bottom for w in g] == [100.0, 150.0, 200.0, 250.0]
    assert all(w.s_top - w.s_bottom == 50.0 for w in g)
    mid = sum((w.s_bottom + w.s_top) / 2 for w in g) / 4
    assert abs(mid - 200.0) <= 60.0
    with pytest.raises(ValueError):
        plan_window_grids(0.0)


def _tile_obs(tmp_path, ref, *, obs_id="obs-tile", zoom=19, kind=ObservationKind.satellite_overview, centre=None, mpp=0.15):
    lat, lon = centre or ref.point_at(ref.length_s / 2, 0.0)
    o = make_observation(obs_id=obs_id, kind=kind, station_s=None, zoom=zoom, lat=lat, lon=lon, size_px=(1280, 1280))
    o = o.model_copy(update={"meters_per_px": mpp, "georef": {"center_lat": lat, "center_lon": lon, "zoom": zoom, "scale": 2,
                                                                "size_px": [1280, 1280], "meters_per_px": mpp}})
    dest = tmp_path / o.file_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(TILE, dest)
    return o


def test_overhead_windows_rectify_the_covering_tile_with_axes_in_the_caption(tmp_path):
    ref = straight_reference_line(100.0)
    o = _tile_obs(tmp_path, ref)
    wins = build_overhead_windows([o], ref, data_dir=tmp_path)
    assert [w.index for w in wins] == [1, 2]
    w = wins[0]
    assert w.image.media_type == "image/png" and (w.image.width_px, w.image.height_px) == (280, 500)
    assert Image.open(__import__("io").BytesIO(w.image.data)).size == (280, 500)
    cap = w.caption
    for needle in ("Image 1 of 2", "up = direction of travel", "image-left = left of travel", "s_top=50.0", "s_bottom=0.0",
                   "t_left=14.0", "t_right=-14.0", "metres_per_px=0.100", "280x500", "ticks"):
        assert needle in cap, needle
    prompt = overhead_prompt(wins, ref, footpath_side="right")
    assert prompt.startswith("<node:overhead>") and "{{" not in prompt and "image 2:" in prompt and "right side" in prompt


def test_overhead_prefers_the_finer_tile_and_skips_windows_nobody_covers(tmp_path):
    ref = straight_reference_line(100.0)
    coarse = _tile_obs(tmp_path, ref, obs_id="obs-a", mpp=0.15)
    fine = _tile_obs(tmp_path, ref, obs_id="obs-b", zoom=20, kind=ObservationKind.satellite_crop, mpp=0.07,
                     centre=ref.point_at(25.0, 0.0))
    # the z20 crop fully covers window 1 only; window 2 falls back to the overview that covers it fully
    picked = [w.observation.id for w in build_overhead_windows([coarse, fine], ref, data_dir=tmp_path)]
    assert picked == ["obs-b", "obs-a"]
    far = _tile_obs(tmp_path, ref, obs_id="obs-far", centre=(ref.point_at(0, 0)[0] + 0.05, ref.point_at(0, 0)[1]))
    assert build_overhead_windows([far], ref, data_dir=tmp_path) == []
    assert build_overhead_windows([], ref, data_dir=tmp_path) == []


def test_overhead_windows_on_a_curve_stay_in_the_requested_grid(tmp_path):
    ref = curved_reference_line()
    wins = build_overhead_windows([_tile_obs(tmp_path, ref)], ref, data_dir=tmp_path)
    assert len(wins) >= 1 and all(w.grid.m_per_px == 0.1 for w in wins)


# ---------------------------------------------------------------- prompts

def test_all_three_prompts_carry_their_node_tag_on_the_first_line():
    first = {n: render_prompt(n, **{k: "x" for k in ("n_windows", "n_images", "length_m", "footpath_side", "window_table",
                                                     "frame_table", "n_entities", "model_summary", "max_ops")}).splitlines()[0]
             for n in ("perceive_overhead", "perceive_streetlevel", "critique_model")}
    assert first == {"perceive_overhead": "<node:overhead>", "perceive_streetlevel": "<node:streetlevel>",
                     "critique_model": "<node:critique>"}


def test_prompts_are_geometry_aware():
    street = render_prompt("perceive_streetlevel", n_images=8, length_m="100", footpath_side="left", frame_table="T")
    for needle in ("heading relative to travel", "station", "forward = direction of travel", "image_index", "visibility_ok",
                   "touches_ground", "description"):
        assert needle in street, needle
    over = render_prompt("perceive_overhead", n_windows=2, length_m="100", footpath_side="left", window_table="T")
    for needle in ("UP in the image is the DIRECTION OF TRAVEL", "metres_per_px", "tick", "tile", "unknown"):
        assert needle in over, needle
