"""Street View panoramas for the play page: stitching pinhole frames into an equirect, the per-street build."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from hero.imagery import panorama as pm
from hero.model.refline import ReferenceLine
from hero.model.types import Observation, ObservationKind, ObservationSource

RED, GREEN, BLUE, YELLOW = (220, 30, 30), (30, 200, 40), (30, 40, 220), (230, 220, 20)


def _frame(colour, heading, pitch=0.0, fov=90.0, size=64):
    return Image.new("RGB", (size, size), colour), heading, pitch, fov


def _px(img: Image.Image, bearing_deg: float, elev_deg: float):
    u = (bearing_deg % 360) / 360.0
    v = 0.5 - elev_deg / 180.0
    return img.getpixel((int(u * img.width) % img.width, min(img.height - 1, int(v * img.height))))


def _close(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_four_level_frames_land_on_their_compass_bearings():
    pano = pm.stitch_panorama([_frame(RED, 0), _frame(GREEN, 90), _frame(BLUE, 180), _frame(YELLOW, 270)], width=256)
    assert pano.size == (256, 128)
    assert _close(_px(pano, 0, 0), RED) and _close(_px(pano, 90, 0), GREEN)
    assert _close(_px(pano, 180, 0), BLUE) and _close(_px(pano, 270, 0), YELLOW)
    # straight up is uncovered by level 90 degree frames: filled with the colour below it, never black
    up = _px(pano, 0, 80)
    assert sum(up) > 60


def test_overlapping_frames_blend_with_feathered_edges():
    pano = pm.stitch_panorama([_frame(RED, 0), _frame(GREEN, 60)], width=256)
    mid = _px(pano, 30, 0)                    # seen by both frames, 15 degrees inside each one's edge
    assert mid[0] > 60 and mid[1] > 60, mid
    assert _close(_px(pano, -20, 0), RED) and _close(_px(pano, 80, 0), GREEN)


def test_a_tilted_frame_covers_the_ground_and_a_zoomed_frame_wins_where_it_overlaps():
    level = _frame((100, 100, 100), 0)
    down = _frame(BLUE, 0, pitch=-45.0)
    zoom = _frame(RED, 0, fov=30.0)
    pano = pm.stitch_panorama([level, down, zoom], width=256)
    assert _close(_px(pano, 0, -60), BLUE)            # only the tilted frame sees 60 degrees down
    assert _close(_px(pano, 0, 0), RED)               # the zoomed frame outweighs the wide one at the centre
    assert _close(_px(pano, 35, 20), (100, 100, 100))  # outside the zoomed (and tilted) frames the wide one shows


def test_stitching_nothing_is_an_error():
    with pytest.raises(ValueError):
        pm.stitch_panorama([])


# ------------------------------------------------------------------ build


def _ref() -> ReferenceLine:
    return ReferenceLine.from_latlons([(12.9229, 77.6725), (12.9229, 77.6734)])   # ~100 m due east


def _obs(data_dir: Path, pano_id: str, heading: float, *, lat=12.9229, lon=77.6729, pitch=0.0, fov=90.0,
         station_s=None, colour=(90, 120, 150)) -> Observation:
    img = Image.new("RGB", (32, 32), colour)
    (data_dir / "imagery").mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"{pano_id}:{heading}:{pitch}:{fov}".encode()).hexdigest()
    path = data_dir / "imagery" / f"{name}.jpg"
    img.save(path, "JPEG")
    return Observation(
        id=f"obs-{name[:8]}", segment_id="seg", obs_key=f"{pano_id}:{heading}:{pitch}:{fov}",
        kind=ObservationKind.streetview, source=ObservationSource.google_streetview, sha256=name,
        file_path=f"imagery/{name}.jpg", media_type="image/jpeg", width_px=32, height_px=32, bytes=path.stat().st_size,
        lat=lat, lon=lon, heading_deg=heading, pitch_deg=pitch, fov_deg=fov, pano_id=pano_id, station_s=station_s,
        created_at=datetime(2026, 4, 1, tzinfo=UTC))


def test_build_writes_one_sphere_per_panorama_on_the_street_and_a_manifest(tmp_path: Path):
    ref = _ref()
    obs = [_obs(tmp_path, "A", h, station_s=20.0) for h in (0, 90, 180, 270)]
    obs += [_obs(tmp_path, "B", h, lon=77.6731) for h in (0, 180)]                       # s from lat/lon (~65 m)
    obs += [_obs(tmp_path, "FAR", 0, lat=12.9240)]                                        # > 20 m off the line
    obs += [_obs(tmp_path, "PAST", 0, station_s=150.0)]                                   # far past the end
    manifest = pm.build_panoramas("my-street", 100.0, obs, tmp_path, ref, width=128)
    assert manifest["schema"] == "hero-panoramas/1" and manifest["camera_height_m"] == 2.5
    assert abs(manifest["bearing0_deg"] - 90.0) < 1.0
    ids = [p["pano_id"] for p in manifest["panoramas"]]
    assert ids == ["A", "B"]                                                              # sorted by s, off-line dropped
    a, b = manifest["panoramas"]
    assert a["s"] == 20.0 and a["frames"] == 4 and a["width"] == 128 and a["height"] == 64
    assert 55 < b["s"] < 75 and b["frames"] == 2
    for p in (a, b):
        assert p["url"].startswith("/media/panoramas/my-street/") and (tmp_path / "panoramas" / "my-street" / Path(p["url"]).name).is_file()
    stored = json.loads((tmp_path / "panoramas" / "my-street" / pm.MANIFEST_NAME).read_text())
    assert stored == manifest
    assert pm.load_panoramas("my-street", tmp_path) == manifest
    assert pm.load_panoramas("other-street", tmp_path) is None


def test_build_is_content_addressed_and_rewrites_nothing_for_equal_input(tmp_path: Path):
    ref = _ref()
    obs = [_obs(tmp_path, "A", h, station_s=20.0) for h in (0, 90)]
    first = pm.build_panoramas("s", 100.0, obs, tmp_path, ref, width=64)
    img = tmp_path / "panoramas" / "s" / Path(first["panoramas"][0]["url"]).name
    stamp = img.stat().st_mtime_ns
    again = pm.build_panoramas("s", 100.0, obs, tmp_path, ref, width=64)
    assert again == first and img.stat().st_mtime_ns == stamp
    # a new frame of the same panorama changes the key and the image name
    more = obs + [_obs(tmp_path, "A", 180, station_s=20.0)]
    third = pm.build_panoramas("s", 100.0, more, tmp_path, ref, width=64)
    assert third["key"] != first["key"] and third["panoramas"][0]["url"] != first["panoramas"][0]["url"]
    assert pm.panorama_key(more) == third["key"]


def test_build_with_no_street_view_gives_an_empty_manifest(tmp_path: Path):
    manifest = pm.build_panoramas("s", 100.0, [], tmp_path, _ref())
    assert manifest["panoramas"] == [] and (tmp_path / "panoramas" / "s" / pm.MANIFEST_NAME).is_file()
