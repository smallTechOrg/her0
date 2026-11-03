"""Texture bake (slice S1e): ground strip + frontage ribbons from stored observations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageStat, JpegImagePlugin

from hero.imagery import bake
from hero.model.street_plan_types import StreetPlan
from hero.model.texture_types import TextureRefs
from hero.model.types import CanonicalModel, Observation, ObservationKind
from hero.model.refline import ReferenceLine
from tests.fixtures.bake import synth
from tests.fixtures.street_fixtures import curved_reference_line, make_model

ROOT = Path(__file__).resolve().parents[2]
BELLANDUR = ROOT / "tests/fixtures/recorded/bellandur"
CONTRACT = ROOT / "tests/fixtures/export/contract_curved"


# --------------------------------------------------------------------------- helpers


def _valid_sources(refs: TextureRefs, n_bins: int) -> None:
    assert refs.ground is not None
    assert len(refs.ground.bin_source) == n_bins
    assert set(refs.ground.bin_source) <= {"satellite_hires", "satellite_overview", "interpolated", "procedural"}
    assert set(refs.frontage) == {"modelled", "opposite"}
    for f in refs.frontage.values():
        assert len(f.bin_source) == n_bins
        assert set(f.bin_source) <= {"user_photo", "street_view", "interpolated", "none"}


def _no_blank_no_flat(img: Image.Image, bins: int, length: float, vertical: bool) -> None:
    """No black texel anywhere and every 5 m bin carries real texture variation (never a flat fill)."""
    hist = img.convert("L").histogram()
    assert sum(hist[:5]) < 0.01 * img.width * img.height, "blank (black) texels found"   # real shadows may be near-black
    for i in range(bins):
        a, b = i * 5.0, min(length, (i + 1) * 5.0)
        if vertical:
            box = (0, int((length - b) / length * img.height), img.width, max(int((length - a) / length * img.height), int((length - b) / length * img.height) + 2))
        else:
            box = (int(a / length * img.width), 0, max(int(b / length * img.width), int(a / length * img.width) + 2), img.height)
        sd = max(ImageStat.Stat(img.crop(box)).stddev)
        assert sd > 2.5, f"bin {i} is flat (stddev {sd:.2f})"


@pytest.fixture(scope="module")
def bellandur(tmp_path_factory):
    d = tmp_path_factory.mktemp("bell")
    (d / "imagery").mkdir()
    for f in (BELLANDUR / "images").iterdir():
        shutil.copy(f, d / "imagery" / f.name)
    model = CanonicalModel.model_validate(json.loads((BELLANDUR / "model_v1.json").read_text()))
    obs = [Observation.model_validate(o) for o in json.loads((BELLANDUR / "observations.json").read_text())]
    return d, model, obs


@pytest.fixture()
def curved_case(tmp_path):
    ref = curved_reference_line(length_m=100.0)
    model = make_model(ref)
    geo = synth.tile_georef(ref, 50.0, 20, 1000)
    sat = synth.satellite_obs(ref, geo, tmp_path, "sat-hi", [(50.0, 0.0, "green")])
    sv = synth.render_wall_view(ref, tmp_path, "sv-1", s_c=40.0, t_c=3.0, heading_deg=(ref.bearing_deg(40.0) - 90.0) % 360.0,
                                wall_t=8.0, length=ref.length_s, size=200)
    return tmp_path, model, [sat, sv]


# --------------------------------------------------------------------------- real recorded street


def test_bellandur_full_bake(bellandur, tmp_path):
    d, model, obs = bellandur
    refs = bake.bake_textures(model, None, obs, d, out_dir=tmp_path / "tex")
    _valid_sources(refs, 20)
    g = refs.ground
    assert g.size_px == (280, 1000) and g.t_lo == -14.0 and g.t_hi == 14.0 and g.s_from == 0.0
    assert g.bin_source.count("satellite_hires") >= 18      # three zoom-20 crops cover the street
    assert g.url.startswith("/media/textures/bellandur-main-road-n-2/ground_v1_") and g.url.endswith(".jpg")
    for key, f in refs.frontage.items():
        assert f.side == key and f.size_px == (1000, 128)
        assert f.bin_source.count("street_view") >= 15        # left/right Street View crops along the whole street
        assert (tmp_path / "tex" / f.url.split("/media/textures/")[1]).is_file()
    # tints: fixed furniture only, never dynamic things
    types = {e.id: e.type.value for e in model.entities}
    assert refs.tints and all(types[i] not in ("parked_vehicle", "vendor_stall", "debris") for i in refs.tints)
    assert all(len(c) == 7 and c[0] == "#" for c in refs.tints.values())
    assert any(types[i] in ("pole", "tree") for i in refs.tints)
    # the stored file is exactly the declared image
    img = Image.open(tmp_path / "tex" / g.url.split("/media/textures/")[1])
    assert img.size == g.size_px and img.format == "JPEG"


def test_bellandur_images_have_no_gap_and_are_not_flat(bellandur):
    d, model, obs = bellandur
    gb = bake.bake_ground(model, None, obs, d)
    _no_blank_no_flat(gb.image, 20, 100.0, vertical=True)
    for side in ("modelled", "opposite"):
        fb = bake.bake_frontage(model, None, obs, d, side)
        _no_blank_no_flat(fb.image, 20, 100.0, vertical=False)


def test_bellandur_sparse_evidence_is_painted_not_blank(bellandur):
    d, model, obs = bellandur
    sparse = [o for o in obs if o.kind is ObservationKind.satellite_overview]
    sparse += [o for o in obs if o.kind is ObservationKind.streetview and (o.station_s or 0) < 30]
    gb = bake.bake_ground(model, None, sparse, d)
    assert set(gb.bin_source) == {"satellite_overview"}      # overview tile alone covers the band
    _no_blank_no_flat(gb.image, 20, 100.0, vertical=True)
    for side in ("modelled", "opposite"):
        fb = bake.bake_frontage(model, None, sparse, d, side)
        assert fb.bin_source[0] == "street_view" and fb.bin_source[-1] == "none"
        assert "interpolated" in fb.bin_source               # 30 m shoulder next to real evidence
        _no_blank_no_flat(fb.image, 20, 100.0, vertical=False)


def test_no_observations_is_fully_procedural_and_still_textured(bellandur):
    d, model, _obs = bellandur
    gb = bake.bake_ground(model, None, [], d)
    assert gb.bin_source == ["procedural"] * 20
    _no_blank_no_flat(gb.image, 20, 100.0, vertical=True)
    fb = bake.bake_frontage(model, None, [], d, "modelled")
    assert fb.bin_source == ["none"] * 20
    _no_blank_no_flat(fb.image, 20, 100.0, vertical=False)


def test_missing_image_file_is_skipped_not_fatal(bellandur, tmp_path):
    _d, model, obs = bellandur
    empty = tmp_path / "empty"
    empty.mkdir()
    refs = bake.bake_textures(model, None, obs, empty)   # no image files at all
    assert set(refs.ground.bin_source) == {"procedural"}
    assert refs.tints == {}


# --------------------------------------------------------------------------- partial coverage on the curved fixture


def test_partial_hires_coverage_interpolates_then_goes_procedural(tmp_path):
    ref = curved_reference_line(length_m=100.0)
    model = make_model(ref)
    geo = synth.tile_georef(ref, 20.0, 20, 600)      # ~44 m wide: covers roughly s 0-42 only
    obs = synth.satellite_obs(ref, geo, tmp_path, "sat-part", [(20.0, 0.0, "yellow")], station_s=20.0)
    gb = bake.bake_ground(model, None, [obs], tmp_path)
    src = gb.bin_source
    last_real = max(i for i, x in enumerate(src) if x == "satellite_hires")
    assert 3 <= last_real <= 12 and src[0] in ("satellite_hires", "interpolated")
    assert src[last_real + 1] == "interpolated"                       # shoulder next to real imagery
    first_proc = src.index("procedural")
    assert all(x == "interpolated" for x in src[last_real + 1:first_proc])
    assert (first_proc - last_real - 1) * 5 <= 30 and src[-1] == "procedural"   # nothing farther than 30 m is faked
    _no_blank_no_flat(gb.image, 20, ref.length_s, vertical=True)


def test_overview_is_the_base_layer_hires_wins_on_top(tmp_path):
    ref = curved_reference_line(length_m=100.0)
    model = make_model(ref)
    ov_geo = synth.tile_georef(ref, 50.0, 19, 800)
    ov = synth.satellite_obs(ref, ov_geo, tmp_path, "sat-ov", [(50.0, 0.0, "blue")], kind=ObservationKind.satellite_overview,
                             base=(100, 110, 120))
    hi_geo = synth.tile_georef(ref, 50.0, 20, 800)
    hi = synth.satellite_obs(ref, hi_geo, tmp_path, "sat-hi", [(50.0, 0.0, "green")], base=(200, 190, 180))
    both = bake.bake_ground(model, None, [ov, hi], tmp_path)
    only_ov = bake.bake_ground(model, None, [ov], tmp_path)
    assert "satellite_overview" in only_ov.bin_source and "satellite_hires" not in only_ov.bin_source
    def mean_at_centre(img):
        y = int(img.height * 0.5)
        return ImageStat.Stat(img.crop((0, y - 30, img.width, y + 30))).mean
    assert mean_at_centre(both.image)[0] > mean_at_centre(only_ov.image)[0] + 20      # brighter hires tile on top
    assert bake.bake_ground(model, None, [hi], tmp_path).bin_source[10] == "satellite_hires"


def test_pavement_and_asphalt_are_darkened_and_blended(tmp_path):
    ref = curved_reference_line(length_m=100.0)
    model = make_model(ref)
    geo = synth.tile_georef(ref, 50.0, 20, 1000)
    obs = synth.satellite_obs(ref, geo, tmp_path, "sat-w", [], base=(210, 210, 210))
    g = bake.bake_ground(model, None, [obs], tmp_path)
    W, H = g.image.size
    mpp = 28.0 / W

    def lum_at(t_lo, t_hi, s):
        x0, x1 = int((14 - t_hi) / mpp), int((14 - t_lo) / mpp)
        y = int((100.0 - s) / 100.0 * H)
        return ImageStat.Stat(g.image.convert("L").crop((x0, y - 8, x1, y + 8))).mean[0]

    road, foot, far = lum_at(-3, 3, 50), lum_at(5.3, 6.2, 50), lum_at(10, 12, 50)
    assert road < foot - 12 and road < far - 12      # carriageway reads as asphalt
    assert foot < 210                                # paving is graded (blended), not raw satellite


# --------------------------------------------------------------------------- determinism / storage


def test_jpeg_parameters_and_content_addressing(curved_case, tmp_path):
    d, model, obs = curved_case
    out = tmp_path / "tex"
    refs = bake.bake_textures(model, None, obs, d, out_dir=out)
    paths = [out / u.url.split("/media/textures/")[1] for u in [refs.ground, *refs.frontage.values()]]
    for p, ref_ in zip(paths, [refs.ground, *refs.frontage.values()]):
        data = p.read_bytes()
        assert p.name.endswith(f"_{bake.content_sha8(data)}.jpg")           # name = sha8 of the bytes
        assert data[:3] == b"\xff\xd8\xff" and b"Exif" not in data[:64] and b"ICC_PROFILE" not in data[:200]
        img = Image.open(p)
        assert img.format == "JPEG" and "progressive" not in img.info and img.size == ref_.size_px
        assert JpegImagePlugin.get_sampling(img) == 2                         # 4:2:0
        assert max(img.size) <= 1024 and len(data) < 400_000
    # idempotent: same input -> same refs, no rewrite
    mtimes = [p.stat().st_mtime_ns for p in paths]
    again = bake.bake_textures(model, None, obs, d, out_dir=out)
    assert again == refs and [p.stat().st_mtime_ns for p in paths] == mtimes
    # different evidence -> different content address; the old files stay (old scenes keep working)
    other = bake.bake_textures(model, None, obs[1:], d, out_dir=out)
    assert other.ground.url != refs.ground.url and all(p.exists() for p in paths)


def test_default_output_is_data_dir_textures(curved_case):
    d, model, obs = curved_case
    refs = bake.bake_textures(model, None, obs, d)
    assert (d / "textures" / model.segment_slug).is_dir()
    assert bake.load_texture(d, refs.ground).size == refs.ground.size_px


def test_bytes_identical_across_processes_and_hash_seeds(tmp_path):
    code = (
        "import sys, hashlib\n"
        "from pathlib import Path\n"
        "from hero.imagery import bake\n"
        "from tests.fixtures.bake import synth\n"
        "from tests.fixtures.street_fixtures import curved_reference_line, make_model\n"
        "d = Path(sys.argv[1]); ref = curved_reference_line(length_m=100.0); model = make_model(ref)\n"
        "geo = synth.tile_georef(ref, 50.0, 20, 800)\n"
        "o = [synth.satellite_obs(ref, geo, d, 'sat', [(50.0, 4.0, 'red')]),\n"
        "     synth.render_wall_view(ref, d, 'sv', s_c=40.0, t_c=3.0, heading_deg=(ref.bearing_deg(40.0) - 90.0) % 360.0, wall_t=8.0, length=ref.length_s, size=160)]\n"
        "r = bake.bake_textures(model, None, o, d)\n"
        "print(r.model_dump_json())\n"
    )
    outs = []
    for seed in ("1", "777"):
        d = tmp_path / f"p{seed}"
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT)}
        res = subprocess.run([sys.executable, "-c", code, str(d)], capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
        assert res.returncode == 0, res.stderr[-800:]
        outs.append(res.stdout.strip().splitlines()[-1])
    assert outs[0] == outs[1]
    a = {p.name: p.read_bytes() for p in (tmp_path / "p1/textures").rglob("*.jpg")}
    b = {p.name: p.read_bytes() for p in (tmp_path / "p777/textures").rglob("*.jpg")}
    assert a == b and len(a) == 3


# --------------------------------------------------------------------------- plan input / contract fixture / helpers


def _contract_model() -> tuple[CanonicalModel, StreetPlan]:
    raw = json.loads((CONTRACT / "plan.json").read_text())
    plan = StreetPlan.model_validate(raw)
    ref = ReferenceLine.from_latlons([(p.lat, p.lon) for p in plan.reference_line.points])
    model = make_model(ref, slug=plan.slug, side=plan.footpath_side, version=plan.model_version)
    return model, plan


def test_contract_curved_plan_as_object_dict_and_stations(tmp_path):
    model, plan = _contract_model()
    n_bins = len(plan.ground_tex.bin_source)
    ref_obj = bake.bake_textures(model, plan, [], tmp_path / "a")
    ref_dict = bake.bake_textures(model, json.loads((CONTRACT / "plan.json").read_text()), [], tmp_path / "b")
    ref_sta = bake.bake_textures(model, plan.stations, [], tmp_path / "c")
    assert ref_obj.ground.url == ref_dict.ground.url == ref_sta.ground.url
    _valid_sources(ref_obj, n_bins)
    assert ref_obj.ground.bin_source == ["procedural"] * n_bins
    assert ref_obj.procedural_seed == bake.procedural_seed_for(plan.slug)
    samples = bake.cross_section_samples(model, plan)
    assert len(samples) == len(plan.stations)
    kinds = {b.kind for _s, bs in samples for b in bs}
    assert {"footpath", "carriageway", "kerb"} <= kinds
    foot = [b for b in samples[4][1] if b.kind == "footpath" and b.state == "intact"][0]
    assert foot.t_lo > 0 and foot.t_hi > foot.t_lo        # left footpath: positive t (left of travel)
    # plan-driven wall distances are used for the frontage projection
    wall = bake._wall_t(model, plan, "left", "modelled")
    assert wall(1.0) == pytest.approx(8.2) and wall(10.0) == pytest.approx(10.0)
    wall_r = bake._wall_t(model, plan, "right", "opposite")
    assert wall_r(10.0) < -7


def test_cross_section_from_model_without_plan(bellandur):
    _d, model, _obs = bellandur
    samples = bake.cross_section_samples(model)
    assert samples[0][0] == 0.0 and samples[-1][0] == pytest.approx(model.reference_line.length_s)
    assert all(any(b.kind == "carriageway" for b in bs) for _s, bs in samples)


def test_stack_ribbons_and_uv_consistency():
    left = Image.new("RGB", (100, 128), (255, 0, 0))
    right = Image.new("RGB", (100, 128), (0, 0, 255))
    atlas = bake.stack_ribbons(left, right)
    assert atlas.size == (100, 256)
    u, v = bake.facade_uv(50.0, 6.0, 100.0, "left")
    assert atlas.getpixel((int(u * 100), int(v * 256))) == (255, 0, 0)
    u, v = bake.facade_uv(50.0, 6.0, 100.0, "right")
    assert atlas.getpixel((int(u * 100), int(v * 256))) == (0, 0, 255)


def test_procedural_patterns_are_deterministic_and_seed_dependent():
    model = make_model(curved_reference_line(length_m=100.0))
    samples = bake.cross_section_samples(model)
    a = bake.procedural_ground_image((140, 500), 100.0, samples, 5)
    assert a.tobytes() == bake.procedural_ground_image((140, 500), 100.0, samples, 5).tobytes()
    assert a.tobytes() != bake.procedural_ground_image((140, 500), 100.0, samples, 6).tobytes()
    f = bake.procedural_facade_image((500, 128), 100.0, "x", "left", 3)
    assert f.tobytes() == bake.procedural_facade_image((500, 128), 100.0, "x", "left", 3).tobytes()
    assert f.getextrema() != ((0, 0), (0, 0), (0, 0)) and max(ImageStat.Stat(f).stddev) > 8


def test_bad_side_rejected(bellandur):
    d, model, _ = bellandur
    with pytest.raises(ValueError):
        bake.bake_frontage(model, None, [], d, "middle")
