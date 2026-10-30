"""Generate the recorded fixture street (slice S1d): ``tests/e2e/fixtures/model_fixture/**`` + ``scene_curved_v2.json``.

    uv run python -m tests.fixtures.make_model_fixtures            # write the committed fixture
    uv run python -m tests.fixtures.make_model_fixtures --out DIR  # write somewhere else

The fixture is a *simulation of the real pipeline without Claude*: a ground-truth street is defined in code, synthetic
cameras "photograph" it (small JPEGs drawn with PIL), the detections a vision model would return are produced from the
camera geometry (pixel boxes, recorded in ``extractions.json``), and everything downstream is the REAL engine:
``hero.model.projection`` places the boxes, ``hero.model.coverage`` gives the visible regions, ``hero.model.merge``
fuses the batches, ``compute_fidelity`` / ``diff_models`` / ``compile_scene`` / ``build_run_report`` produce the
stored documents. Nothing in the output directory is hand-edited; every value is deterministic (no clock, no RNG).

Ground truth (``TRUTH_*``): a 100 m circular arc of radius 80 m (a right turn of ~71.6 degrees); a 1.8 m paver
footpath on the left that narrows to 1.1 m (30-45 m), shifts 0.6 m outward (45-60 m), is broken (60-72 m), missing
(72-84 m) and returns 1.5 m wide (84 m on); 10 objects of which 3 are only visible from the side views.

Runs: run 1 = forward + back frames (26 images incl. overview and 3 hi-res crops) -> v1 (L1 everywhere);
run 2 = + left/right frames (48 images) -> v2 (L2); run 3 = nothing new -> no-op (stays v2).

Directory contract: see ``hero.tools.load_fixture_street``. Extra file: ``report_run3.json`` (the no-op run);
the loader treats any number of ``report_run*.json`` files as consecutive runs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from hero.config import PROJECT_ROOT
from hero.imagery.cache import ImageryCache
from hero.model.batch import FacadeDetection, ProjectedBatch, ProjectedDetection, StateSpan, StripSample, VisibleRegion
from hero.model.compile import compile_scene
from hero.model.coverage import visible_regions
from hero.model.describe import version_summary
from hero.model.diff import diff_models
from hero.model.fidelity import compute_fidelity
from hero.model.merge import merge_batches
from hero.model.priors import OsmBuilding, OsmContext, build_prior_model
from hero.model.projection import (
    CAMERA_HEIGHT_M,
    footpath_runs_to_states,
    project_street_detection,
    strip_samples_from_traces,
)
from hero.model.refline import ReferenceLine, RectGrid
from hero.model.report import build_run_report
from hero.model.types import (
    CanonicalModel,
    Condition,
    FidelityMap,
    FootpathState,
    Material,
    ModelDiff,
    Observation,
    ObservationKind,
    ObservationSource,
    RunReport,
    StripKind,
    ViewKind,
)
from hero.scene.geo import TileGeoref, latlon_to_tile_px, meters_per_px
from tests.fixtures.street_fixtures import DEMO_ORIGIN, curved_reference_line

FIXTURE_SLUG = "fixture-curved-street"
FIXTURE_NAME = "Fixture curved street"
CITY = "Bengaluru"
OUT_DIR = PROJECT_ROOT / "tests" / "e2e" / "fixtures" / "model_fixture"
SCENE_OUT = PROJECT_ROOT / "tests" / "e2e" / "fixtures" / "scene_curved_v2.json"

NOWS = [datetime(2026, 10, 30, h, 0, 0, tzinfo=UTC) for h in (9, 10, 11, 12)]  # v0, run 1, run 2, run 3
RUN_IDS = ("fx-run-1", "fx-run-2", "fx-run-3")
SEGMENT_ID = "fixture"

RADIUS_M, ARC_M = 80.0, 100.0
SIDE = "left"
CAM_T = -1.8                      # the Street View car drives in the right-hand lane (t is left-positive)
SV_SIZE = 480
SV_FOV = 90.0
SV_STATIONS = [float(s) for s in range(0, 100, 10)] + [None]   # None -> the end of the line
CROP_CENTRES_S = (17.0, 50.0, 83.0)
CROP_ZOOM, CROP_SIZE = 20, 640
OVERVIEW_ZOOM, OVERVIEW_SIZE = 19, 1280
RECT_MPP = 0.1
RECT_HALF_T = 12.0
MAX_DETECT_RANGE_M = 25.0
OBS_PER_BATCH = 8

# ------------------------------------------------------------------ ground truth


def truth_footpath(s: float) -> dict[str, Any] | None:
    """Footpath on the left: ``{lo, hi, state, condition}`` at ``s`` (None where it is missing)."""
    if s < 30:
        lo, hi, st, cond = 4.6, 6.4, "intact", "good"
    elif s < 45:
        f = (s - 30) / 15
        lo, hi, st, cond = 4.6, 6.4 - 0.7 * f, "intact", "fair"          # narrows 1.8 -> 1.1
    elif s < 60:
        f = (s - 45) / 15
        lo, hi, st, cond = 4.6 + 0.6 * f, 5.7 + 0.6 * f, "intact", "fair"  # shifts 0.6 m outward
    elif s < 72:
        lo, hi, st, cond = 5.2, 6.3, "broken", "broken"
    elif s < 84:
        return None                                                      # missing
    else:
        lo, hi, st, cond = 4.7, 6.2, "intact", "good"
    return {"lo": lo, "hi": hi, "state": st, "condition": cond}


STATE_RUNS = [(0.0, 60.0, "intact"), (60.0, 72.0, "broken"), (72.0, 84.0, "missing"), (84.0, math.inf, "intact")]


@dataclass(frozen=True)
class TruthEntity:
    key: str
    type: str
    subtype: str | None
    s: float
    t: float
    w: float
    h: float
    d: float
    desc: str
    attrs: dict[str, Any]
    conf: float
    side_only: bool = False    # only recognisable in the left/right frames (small, or hidden from the road)


TRUTH_ENTITIES = [
    TruthEntity("e01", "parked_vehicle", "auto-rickshaw", 12.0, 5.5, 1.4, 1.7, 2.6,
                "Yellow auto-rickshaw parked half on the footpath.", {"colour": "yellow", "blocks_footpath": True}, 0.82),
    TruthEntity("e02", "pole", "utility", 26.0, 6.9, 0.3, 7.0, 0.3,
                "Concrete utility pole at the outer edge of the footpath.", {}, 0.8),
    TruthEntity("e03", "tree", None, 38.0, 6.0, 0.8, 5.0, 0.8,
                "Tree with low branches hanging over the footpath.", {"canopy_low": True}, 0.78),
    TruthEntity("e04", "pothole", None, 47.0, 1.2, 1.0, 0.1, 1.0,
                "Pothole in the carriageway near the centre line.", {}, 0.7),
    TruthEntity("e05", "signboard", None, 58.0, 6.2, 0.8, 2.0, 0.2,
                "Shop signboard standing on the footpath.", {"colour": "blue"}, 0.76),
    TruthEntity("e06", "drain", None, 66.0, 5.7, 0.8, 0.1, 1.0,
                "Open drain where the footpath slab is broken.", {"covered": False}, 0.72),
    TruthEntity("e07", "debris", None, 78.0, 5.5, 1.0, 0.4, 1.0,
                "Rubble heap where the footpath is missing.", {}, 0.74),
    TruthEntity("e08", "bollard", None, 88.0, 4.4, 0.2, 0.8, 0.2,
                "White bollard at the kerb.", {"colour": "white"}, 0.72, side_only=True),
    TruthEntity("e09", "bin", None, 20.0, 6.8, 0.6, 1.0, 0.6,
                "Green waste bin beside the footpath.", {"colour": "green"}, 0.7, side_only=True),
    TruthEntity("e10", "vendor_stall", "fruit stall", 93.0, 7.0, 1.5, 2.0, 1.5,
                "Roadside fruit stall at the back of the footpath.", {"colour": "red"}, 0.74, side_only=True),
]

OSM_BUILDINGS = [
    OsmBuilding(osm_way_id=3001, footprint=[(0.0, 10.0), (32.0, 10.0), (32.0, 22.0), (0.0, 22.0)], height_m=9.6, storeys=3),
    OsmBuilding(osm_way_id=3002, footprint=[(40.0, 10.0), (70.0, 10.0), (70.0, 21.0), (40.0, 21.0)], height_m=7.0, storeys=2),
    OsmBuilding(osm_way_id=3003, footprint=[(10.0, -10.0), (45.0, -10.0), (45.0, -20.0), (10.0, -20.0)], height_m=12.0, storeys=4),
]


# ------------------------------------------------------------------ small deterministic helpers


def _frac(key: str) -> float:
    return int(hashlib.sha1(key.encode()).hexdigest()[:6], 16) / 0xFFFFFF


def _wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def _state_pieces(a: float, b: float) -> list[tuple[float, float, str]]:
    """Ground-truth footpath states clipped to [a, b]."""
    return [(max(a, lo), min(b, hi), st) for lo, hi, st in STATE_RUNS if min(b, hi) - max(a, lo) > 1e-6]


# ------------------------------------------------------------------ cameras / drawing


@dataclass(frozen=True)
class Cam:
    ref: ReferenceLine
    obs: Observation

    @property
    def f(self) -> float:
        return (self.obs.width_px / 2.0) / math.tan(math.radians(self.obs.fov_deg / 2.0))

    def to_cam(self, s: float, t: float) -> tuple[float, float]:
        """Ground point -> (forward, right) metres in the camera frame."""
        cx, cy = self.ref.latlon_to_xy(self.obs.lat, self.obs.lon)
        x, y = self.ref.latlon_to_xy(*self.ref.point_at(s, t))
        dx, dy = x - cx, y - cy
        h = math.radians(self.obs.heading_deg)
        return dx * math.sin(h) + dy * math.cos(h), dx * math.cos(h) - dy * math.sin(h)

    def pixel(self, s: float, t: float, z: float = 0.0) -> tuple[float, float] | None:
        fwd, right = self.to_cam(s, t)
        if fwd < 0.4:
            return None
        w, h = self.obs.width_px, self.obs.height_px
        return w / 2.0 + self.f * right / fwd, h / 2.0 + self.f * (CAMERA_HEIGHT_M - z) / fwd


def render_street(cam: Cam, ents: list[TruthEntity]) -> bytes:
    w, h = cam.obs.width_px, cam.obs.height_px
    img = Image.new("RGB", (w, h), (176, 205, 232))
    d = ImageDraw.Draw(img)
    d.rectangle([0, h // 2, w, h], fill=(120, 132, 110))
    s0 = cam.obs.station_s or 0.0
    L = cam.ref.length_s
    ss = [min(L, max(0.0, s0 - 16 + i * 1.0)) for i in range(0, 50)]
    ss = sorted(set(round(x, 3) for x in ss))

    def band(lo_fn, hi_fn, colour, outline=None):
        left, right = [], []
        for s in ss:
            lo, hi = lo_fn(s), hi_fn(s)
            if lo is None:
                continue
            a, b = cam.pixel(s, lo), cam.pixel(s, hi)
            if a and b:
                left.append(a)
                right.append(b)
        if len(left) >= 2:
            d.polygon(left + right[::-1], fill=colour, outline=outline)

    band(lambda s: -4.25, lambda s: 4.25, (84, 86, 92))
    band(lambda s: (truth_footpath(s) or {}).get("lo") if truth_footpath(s) else None,
         lambda s: (truth_footpath(s) or {}).get("hi"), (196, 150, 128))
    band(lambda s: -5.9, lambda s: -4.4, (150, 150, 140))
    for e in ents:
        box = entity_box(cam, e)
        if box is not None:
            x, y, bw, bh = box
            d.rectangle([x, y, x + bw, y + bh], fill=(220, 190, 60) if e.type == "parked_vehicle" else (90, 120, 70),
                        outline=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=62)
    return buf.getvalue()


def entity_box(cam: Cam, e: TruthEntity) -> tuple[int, int, int, int] | None:
    """Pixel box [x, y, w, h] of the entity in this camera, or None when it is not (well) visible."""
    fwd, right = cam.to_cam(e.s, e.t)
    rng = math.hypot(fwd, right)
    if fwd < 2.5 or rng > MAX_DETECT_RANGE_M:
        return None
    W, H = cam.obs.width_px, cam.obs.height_px
    foot = cam.pixel(e.s, e.t)
    if foot is None:
        return None
    u, v = foot
    if not (0.06 * W <= u <= 0.94 * W and H / 2 + 6 <= v <= H - 3):
        return None
    wpx, hpx = max(4.0, cam.f * e.w / fwd), max(4.0, cam.f * e.h / fwd)
    x, y = round(u - wpx / 2), round(v - hpx)
    bw, bh = round(wpx), round(hpx)
    x, y = max(0, x), max(0, y)
    bw, bh = min(bw, W - x), min(bh, round(v) - y)
    return (x, y, max(bw, 3), max(bh, 3)) if bw >= 3 and bh >= 3 else None


def render_overhead(georef: TileGeoref, ref: ReferenceLine, size: int) -> bytes:
    img = Image.new("RGB", (size, size), (118, 134, 104))
    d = ImageDraw.Draw(img)
    L = ref.length_s
    ss = [i * 1.0 for i in range(int(L) + 1)] + [L]

    def px(s: float, t: float) -> tuple[float, float]:
        return latlon_to_tile_px(*ref.point_at(s, t), georef)

    def poly(lo_fn, hi_fn, colour):
        a, b = [], []
        for s in ss:
            lo = lo_fn(s)
            if lo is None:
                if len(a) >= 2:
                    d.polygon(a + b[::-1], fill=colour)
                a, b = [], []
                continue
            a.append(px(s, lo))
            b.append(px(s, hi_fn(s)))
        if len(a) >= 2:
            d.polygon(a + b[::-1], fill=colour)

    for bld in OSM_BUILDINGS:
        d.polygon([px(s, t) for s, t in bld.footprint], fill=(190, 170, 140))
    poly(lambda s: -4.25, lambda s: 4.25, (82, 84, 90))
    poly(lambda s: -5.9, lambda s: -4.4, (150, 148, 140))
    poly(lambda s: truth_footpath(s)["lo"] if truth_footpath(s) else None,
         lambda s: truth_footpath(s)["hi"], (198, 152, 130))
    for e in TRUTH_ENTITIES:
        x, y = px(e.s, e.t)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(230, 200, 60))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


# ------------------------------------------------------------------ bundle


@dataclass
class Bundle:
    ref: ReferenceLine
    observations: list[Observation]
    images: dict[str, bytes]
    models: list[CanonicalModel]
    fidelities: list[FidelityMap]
    diffs: list[ModelDiff | None]
    scenes: list[dict]
    summaries: list[str]
    reports: list[RunReport]
    extractions: list[dict]
    batches: dict[int, list[ProjectedBatch]] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)

    @property
    def observation_map(self) -> dict[str, Observation]:
        return {o.id: o for o in self.observations}


def _make_obs(oid: str, *, kind: ObservationKind, lat: float, lon: float, size: int, data: bytes, key: str,
              heading: float | None = None, zoom: int | None = None, scale: int | None = None,
              station: float | None = None, offset_t: float | None = None, view: ViewKind | None = None,
              pano: str | None = None, date: str | None = None, request: dict | None = None) -> Observation:
    sat = kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop)
    mpp = meters_per_px(lat, zoom, scale) if sat else None
    georef: dict[str, Any] = {"size_px": [size, size]}
    if sat:
        georef.update({"center_lat": lat, "center_lon": lon, "zoom": zoom, "scale": scale, "meters_per_px": round(mpp, 5)})
    else:
        georef["request"] = request or {}
    return Observation(
        id=oid, segment_id=SEGMENT_ID, obs_key=key, kind=kind,
        source=ObservationSource.google_static_maps if sat else ObservationSource.google_streetview,
        sha256=hashlib.sha256(data).hexdigest(), file_path=f"imagery/{oid}.jpg", media_type="image/jpeg",
        width_px=size, height_px=size, bytes=len(data), lat=round(lat, 8), lon=round(lon, 8),
        heading_deg=None if sat else round(heading, 3), pitch_deg=None if sat else 0.0,
        fov_deg=None if sat else SV_FOV, zoom=zoom, scale=scale, meters_per_px=None if mpp is None else round(mpp, 5),
        pano_id=pano, capture_date=None if sat else date, station_s=None if station is None else round(station, 3),
        offset_t=offset_t, view=view, georef=georef, cost_usd=0.002 if sat else 0.007, acquired_run_id=None, note=None,
        created_at=NOWS[0])


def _build_observations(ref: ReferenceLine) -> tuple[list[Observation], dict[str, bytes], dict[str, list[str]]]:
    """-> (observations, image bytes by basename, ids by group: overhead / run1 / run2)."""
    L = round(ref.length_s, 3)
    obs: list[Observation] = []
    images: dict[str, bytes] = {}
    groups: dict[str, list[str]] = {"overhead": [], "run1": [], "run2": []}
    n = 0

    def nid() -> str:
        nonlocal n
        n += 1
        return f"obs-fx-{n:03d}"

    # overview + hi-res crops
    clat, clon = ref.point_at(L / 2.0, 0.0)
    ov_geo = TileGeoref(clat, clon, OVERVIEW_ZOOM, 2, (OVERVIEW_SIZE, OVERVIEW_SIZE))
    data = render_overhead(ov_geo, ref, OVERVIEW_SIZE)
    oid = nid()
    obs.append(_make_obs(oid, kind=ObservationKind.satellite_overview, lat=clat, lon=clon, size=OVERVIEW_SIZE, data=data,
                         key=ImageryCache.key("satellite", {"c": [round(clat, 6), round(clon, 6)], "z": OVERVIEW_ZOOM}),
                         zoom=OVERVIEW_ZOOM, scale=2))
    images[f"{oid}.jpg"] = data
    groups["overhead"].append(oid)
    for c in CROP_CENTRES_S:
        lat, lon = ref.point_at(c, 0.0)
        geo = TileGeoref(lat, lon, CROP_ZOOM, 2, (CROP_SIZE, CROP_SIZE))
        data = render_overhead(geo, ref, CROP_SIZE)
        oid = nid()
        obs.append(_make_obs(oid, kind=ObservationKind.satellite_crop, lat=lat, lon=lon, size=CROP_SIZE, data=data,
                             key=ImageryCache.key("satellite", {"c": [round(lat, 6), round(lon, 6)], "z": CROP_ZOOM}),
                             zoom=CROP_ZOOM, scale=2))
        images[f"{oid}.jpg"] = data
        groups["overhead"].append(oid)

    # street view frames: run 1 = forward/back, run 2 = left/right
    stations = [L if s is None else s for s in SV_STATIONS]
    for run, views in (("run1", (("forward", 0.0), ("back", 180.0))), ("run2", (("left", -90.0), ("right", 90.0)))):
        for idx, s in enumerate(stations):
            for view, delta in views:
                lat, lon = ref.point_at(s, CAM_T)
                heading = (ref.bearing_deg(min(s, L)) + delta) % 360.0
                oid = nid()
                date = "2023-04" if s < 50.0 else "2024-02"
                pano = f"fxpano{idx:02d}"
                tmp = _make_obs(oid, kind=ObservationKind.streetview, lat=lat, lon=lon, size=SV_SIZE, data=b"x",
                                key="tmp", heading=heading, station=s, offset_t=CAM_T, view=ViewKind(view), pano=pano,
                                date=date)
                visible = [e for e in TRUTH_ENTITIES if not (e.side_only and view in ("forward", "back"))]
                data = render_street(Cam(ref, tmp), visible)
                req = {"pano": pano, "heading": round(heading, 1), "pitch": 0, "fov": int(SV_FOV), "size": SV_SIZE}
                obs.append(_make_obs(oid, kind=ObservationKind.streetview, lat=lat, lon=lon, size=SV_SIZE, data=data,
                                     key=ImageryCache.key("streetview", req), heading=heading, station=s,
                                     offset_t=CAM_T, view=ViewKind(view), pano=pano, date=date, request=req))
                images[f"{oid}.jpg"] = data
                groups[run].append(oid)
    return obs, images, groups


# ------------------------------------------------------------------ simulated perception -> ProjectedBatch


def _detections_for(obs: Observation, ref: ReferenceLine) -> tuple[list[dict], list[ProjectedDetection]]:
    """Raw detection dicts (the recorded extraction rows) + their projections for one street frame."""
    cam = Cam(ref, obs)
    raw, proj = [], []
    for e in TRUTH_ENTITIES:
        if e.side_only and obs.view in (ViewKind.forward, ViewKind.back):
            continue
        box = entity_box(cam, e)
        if box is None:
            continue
        fwd, right = cam.to_cam(e.s, e.t)
        rng = math.hypot(fwd, right)
        k = len(raw) + 1
        jit = _frac(f"{obs.id}:{e.key}")
        conf = round(min(0.95, max(0.3, e.conf - 0.06 + 0.12 * jit)), 2)
        est = round(rng * (1.0 + 0.1 * (jit - 0.5)), 1)
        row = {"local_id": f"d{k}", "image_index": 0, "type": e.type, "subtype": e.subtype, "box_px": list(box),
               "touches_ground": True, "est_distance_m": est, "size_m": {"w": e.w, "h": max(e.h, 0.05), "d": e.d},
               "orientation": "parallel", "attributes": dict(e.attrs), "description": e.desc, "confidence": conf}
        pp = project_street_detection(obs, ref, box, touches_ground=True, est_distance_m=est)
        if pp is None:
            continue
        raw.append(row)
        proj.append(ProjectedDetection(
            key=f"{obs.id}:d{k}", observation_id=obs.id, type=e.type, subtype=e.subtype,  # type: ignore[arg-type]
            s=round(pp.s, 3), t=round(pp.t, 3), sigma_m=round(pp.sigma_m, 3), extent_s=e.d, extent_t=e.w,
            height_m=e.h, orientation_deg=0.0, attributes=dict(e.attrs), description=e.desc, raw_confidence=conf,
            crop_px=box, weight=round(pp.weight, 4), flags=pp.flags))
    return raw, proj


FRAME_STATE_SPAN = {"forward": (0.0, 12.0), "back": (-12.0, 0.0), "left": (-6.0, 6.0), "right": (-6.0, 6.0)}
FRAME_STATE_WEIGHT = {"forward": 0.5, "back": 0.5, "left": 0.7, "right": 0.7}


def _frame_states(obs: Observation, ref: ReferenceLine) -> list[StateSpan]:
    a_off, b_off = FRAME_STATE_SPAN[obs.view.value]
    a, b = max(0.0, obs.station_s + a_off), min(ref.length_s, obs.station_s + b_off)
    if b - a < 0.5:
        return []
    return [StateSpan(observation_id=obs.id, side=SIDE, s_from=round(x, 3), s_to=round(y, 3),  # type: ignore[arg-type]
                      state=FootpathState(st), weight=FRAME_STATE_WEIGHT[obs.view.value])
            for x, y, st in _state_pieces(a, b)]


def _frame_visible(obs: Observation, ref: ReferenceLine) -> list[VisibleRegion]:
    """Coverage regions, minus what the camera cannot see: a forward/back frame sees a footpath-side object 7 m to
    the side only beyond 7.7 m / tan(fov/2) ahead, so absence closer than that is not evidence."""
    out = []
    lateral = abs(7.0 - CAM_T)
    min_ahead = lateral / math.tan(math.radians(SV_FOV / 2.0))
    for r in visible_regions(obs, ref):
        if obs.view in (ViewKind.forward, ViewKind.back):
            mid = (r.s_from + r.s_to) / 2.0
            if abs(mid - obs.station_s) < min_ahead + 1.0:
                continue
        out.append(r)
    return out


def _facades(obs: Observation) -> list[FacadeDetection]:
    if obs.view is not ViewKind.left or round(obs.station_s) not in (20, 50, 80):
        return []
    return [FacadeDetection(observation_id=obs.id, side="left", s_from=obs.station_s - 5.0, s_to=obs.station_s + 5.0,
                            colour_hex="#c9b79c", storeys=3, description="Cream three-storey shopfront block.",
                            crop_px=(0, 120, 480, 200), weight=0.6)]


class _Run:
    def __init__(self, x: dict):
        self.__dict__.update(x)


def _overhead_batch(obs_list: list[Observation], ref: ReferenceLine) -> tuple[ProjectedBatch, dict]:
    """Rectified windows for the three crops -> strips + states (via the real S1b functions) and the recorded extraction."""
    L = ref.length_s
    crops = [o for o in obs_list if o.kind is ObservationKind.satellite_crop]
    strips: list[StripSample] = []
    states: list[StateSpan] = []
    windows = []
    for i, crop in enumerate(crops, 1):
        c = (crop.station_s if crop.station_s is not None else ref.to_st(crop.lat, crop.lon).s)
        top, bottom = min(L, c + 22.0), max(0.0, c - 22.0)
        grid = RectGrid(s_top=round(top, 3), s_bottom=round(bottom, 3), t_left=RECT_HALF_T, t_right=-RECT_HALF_T,
                        m_per_px=RECT_MPP)

        def trace(fn, s_lo: float, s_hi: float) -> list[tuple[float, float]]:
            pts = []
            n = max(2, int((s_hi - s_lo) / 2.5) + 1)
            for k in range(n):
                s = s_hi - (s_hi - s_lo) * k / (n - 1)             # top (large s) first => y non-decreasing
                t = fn(s) + 0.06 * math.sin(1.3 * s + i)             # per-crop deterministic wobble
                x, y = grid.st_to_px(s, t)
                pts.append((round(x, 1), round(y, 1)))
            return pts

        w_strips = []
        for lo_s, hi_s in _footpath_runs(bottom, top):
            ea = trace(lambda s: truth_footpath(min(max(s, lo_s + 1e-3), hi_s - 1e-3))["lo"], lo_s, hi_s)
            eb = trace(lambda s: truth_footpath(min(max(s, lo_s + 1e-3), hi_s - 1e-3))["hi"], lo_s, hi_s)
            mid = (lo_s + hi_s) / 2.0
            cond = truth_footpath(mid)["condition"]
            strips += strip_samples_from_traces(grid, ea, eb, kind=StripKind.footpath, material=Material.paver,
                                                condition=Condition(cond), side=SIDE, weight=0.8,
                                                observation_id=crop.id)
            w_strips.append({"kind": "footpath", "side": SIDE, "material": "paver", "condition": cond,
                             "edge_a_px": [list(p) for p in ea], "edge_b_px": [list(p) for p in eb],
                             "confidence": 0.8, "description": f"Paver footpath, {cond}, from {lo_s:.0f} to {hi_s:.0f} m."})
        ca, cb = trace(lambda s: -4.25, bottom, top), trace(lambda s: 4.25, bottom, top)
        strips += strip_samples_from_traces(grid, ca, cb, kind=StripKind.carriageway, material=Material.asphalt,
                                            condition=Condition.fair, side="centre", weight=0.8, observation_id=crop.id)
        w_strips.append({"kind": "carriageway", "side": "centre", "material": "asphalt", "condition": "fair",
                         "edge_a_px": [list(p) for p in ca], "edge_b_px": [list(p) for p in cb], "confidence": 0.8,
                         "description": "Two-way asphalt carriageway."})
        runs = []
        for lo_s, hi_s, st in _state_pieces(bottom, top):
            runs.append({"side": SIDE, "y_from_px": round((grid.s_top - hi_s) / RECT_MPP, 1),
                         "y_to_px": round((grid.s_top - lo_s) / RECT_MPP, 1), "state": st, "confidence": 0.8,
                         "note": f"{st} footpath"})
        states += footpath_runs_to_states(grid, [_R(**r) for r in runs], observation_id=crop.id)
        windows.append({"image_index": i, "strips": w_strips, "footpath_runs": runs, "objects": [],
                        "notes": f"Rectified window s {grid.s_bottom:.0f}-{grid.s_top:.0f} m."})
    ids = tuple(sorted(o.id for o in obs_list))
    return (ProjectedBatch(batch_id="fx-overhead", observation_ids=ids, strips=tuple(strips), states=tuple(states)),
            {"windows": windows})


@dataclass
class _R:
    side: str
    y_from_px: float
    y_to_px: float
    state: str
    confidence: float
    note: str = ""


def _footpath_runs(a: float, b: float) -> list[tuple[float, float]]:
    """Maximal s-ranges inside [a, b] where the true footpath exists."""
    out, cur = [], None
    n = int(round((b - a) / 0.5))
    for k in range(n + 1):
        s = a + (b - a) * k / n
        has = truth_footpath(min(s, 99.999)) is not None
        if has and cur is None:
            cur = s
        if not has and cur is not None:
            out.append((cur, s - 0.5))
            cur = None
    if cur is not None:
        out.append((cur, b))
    return [(x, y) for x, y in out if y - x > 2.0]


def _street_batches(obs_list: list[Observation], ref: ReferenceLine, run: int) -> tuple[list[ProjectedBatch], list[dict]]:
    ordered = sorted(obs_list, key=lambda o: (o.station_s, o.view.value))
    batches, extractions = [], []
    for k in range(0, len(ordered), OBS_PER_BATCH):
        chunk = ordered[k:k + OBS_PER_BATCH]
        dets: list[ProjectedDetection] = []
        states: list[StateSpan] = []
        vis: list[VisibleRegion] = []
        facs: list[FacadeDetection] = []
        frames, raw_dets, raw_facs = [], [], []
        for idx, o in enumerate(chunk, 1):
            raw, proj = _detections_for(o, ref)
            for r, p in zip(raw, proj):
                r["image_index"] = idx
                raw_dets.append(r)
            dets += proj
            states += _frame_states(o, ref)
            vis += _frame_visible(o, ref)
            for f in _facades(o):
                facs.append(f)
                raw_facs.append({"image_index": idx, "box_px": list(f.crop_px), "side": f.side, "colour_hex": f.colour_hex,
                                 "storeys": f.storeys, "use": "shops", "description": f.description, "confidence": 0.7})
            mid = o.station_s + (FRAME_STATE_SPAN[o.view.value][0] + FRAME_STATE_SPAN[o.view.value][1]) / 2.0
            tf = truth_footpath(min(max(mid, 0.0), 99.999))
            frames.append({"image_index": idx, "visibility_ok": True, "footpath_visible": True, "footpath_side": SIDE,
                           "footpath_state": tf["state"] if tf else "missing", "notes": ""})
        bid = f"fx-street-r{run}-{k // OBS_PER_BATCH}"
        batches.append(ProjectedBatch(batch_id=bid, observation_ids=tuple(sorted(o.id for o in chunk)),
                                      detections=tuple(dets), states=tuple(states), facades=tuple(facs),
                                      visible=tuple(vis)))
        extractions.append({"run": run, "batch_index": len(extractions) + (1 if run else 0), "kind": "streetlevel",
                            "observation_ids": [o.id for o in chunk], "model": "replay",
                            "prompt_name": "perceive_streetlevel",
                            "extraction": {"frames": frames, "detections": raw_dets, "facades": raw_facs, "notes": ""}})
    return batches, extractions


# ------------------------------------------------------------------ assembling everything


def build() -> Bundle:
    ref = curved_reference_line(RADIUS_M, ARC_M, turn="right")
    ref_model = ref.to_model(source="osm_nominatim", osm_way_id=25173540, name="100 Feet Road")
    obs_list, images, groups = _build_observations(ref)
    omap = {o.id: o for o in obs_list}

    osm = OsmContext(highway_tags={"highway": "secondary", "lanes": "2", "width": "8.5", "name": "100 Feet Road"},
                     footway_polylines=[], buildings=OSM_BUILDINGS)
    v0 = build_prior_model(segment_slug=FIXTURE_SLUG, footpath_side=SIDE, ref=ref, ref_model=ref_model, osm=osm,
                           now=NOWS[0])

    over_batch, over_extraction = _overhead_batch([omap[i] for i in groups["overhead"]], ref)
    s1, ex1 = _street_batches([omap[i] for i in groups["run1"]], ref, 1)
    s2, ex2 = _street_batches([omap[i] for i in groups["run2"]], ref, 2)
    for j, e in enumerate(ex1):
        e["batch_index"] = j + 1
    for j, e in enumerate(ex2):
        e["batch_index"] = j
    extractions = [{"run": 1, "batch_index": 0, "kind": "overhead", "observation_ids": sorted(groups["overhead"]),
                    "model": "replay", "prompt_name": "perceive_overhead", "extraction": over_extraction}, *ex1, *ex2]

    r1 = merge_batches(v0, [over_batch, *s1], new_version=1, run_id=RUN_IDS[0], now=NOWS[1])
    r2 = merge_batches(r1.model, s2, new_version=2, run_id=RUN_IDS[1], now=NOWS[2])
    r3 = merge_batches(r2.model, [over_batch, *s1, *s2], new_version=3, run_id=RUN_IDS[2], now=NOWS[3])
    assert not r3.changes and len(r3.ignored_batches) == 1 + len(s1) + len(s2), "run 3 must be a no-op"

    models, fids = [], []
    for m in (v0, r1.model, r2.model):
        fid = compute_fidelity(m, omap)
        models.append(m.model_copy(update={"fidelity": fid, "stats": m.computed_stats()}))
        fids.append(fid)
    diffs: list[ModelDiff | None] = [None, diff_models(models[0], models[1], fids[0], fids[1]),
                                     diff_models(models[1], models[2], fids[1], fids[2])]
    tile = omap[groups["overhead"][0]]
    scenes, summaries = [], []
    for m, fid, df in zip(models, fids, diffs):
        sc = compile_scene(m, observations=omap, assets={}, tile=tile, fidelity=fid, slug_name=FIXTURE_NAME, city=CITY)
        scenes.append(json.loads(sc.model_dump_json()))
        summaries.append(version_summary(m, df, fid))

    # ---- run reports (run 1, run 2, run 3 = no-op)
    new1 = [omap[i] for i in groups["overhead"] + groups["run1"]]
    new2 = [omap[i] for i in groups["run2"]]
    cached2 = new1
    t = lambda h, mi: datetime(2026, 10, 30, h, mi, 0, tzinfo=UTC)  # noqa: E731
    rep1 = build_run_report(
        run=_Run({"id": RUN_IDS[0], "started_at": t(10, 0), "finished_at": t(10, 2)}), mode="generate", base=models[0],
        result=models[1], diff=diffs[1], fid_before=fids[0], fid_after=fids[1], new_observations=new1, cached_count=0,
        costs={"anthropic_usd": 0.31, "anthropic_calls": 5, "input_tokens": 61200, "output_tokens": 8100,
               "global_budget_usd": 50.0, "budget_remaining_usd": 49.69}, warnings=[], now=NOWS[1])
    rep2 = build_run_report(
        run=_Run({"id": RUN_IDS[1], "started_at": t(11, 0), "finished_at": t(11, 3)}), mode="generate", base=models[1],
        result=models[2], diff=diffs[2], fid_before=fids[1], fid_after=fids[2], new_observations=new2,
        cached_count=len(new1), cached_observations=cached2,
        costs={"anthropic_usd": 0.27, "anthropic_calls": 4, "input_tokens": 51200, "output_tokens": 7400,
               "global_budget_usd": 50.0, "budget_remaining_usd": 49.42}, warnings=[], now=NOWS[2])
    rep3 = build_run_report(
        run=_Run({"id": RUN_IDS[2], "started_at": t(12, 0), "finished_at": t(12, 0)}), mode="generate", base=models[2],
        result=models[2], diff=None, fid_before=fids[2], fid_after=fids[2], new_observations=[],
        cached_count=len(obs_list), cached_observations=obs_list,
        costs={"anthropic_usd": 0.0, "anthropic_calls": 0, "input_tokens": 0, "output_tokens": 0,
               "google_new_requests": 0, "google_est_usd": 0.0, "global_budget_usd": 50.0, "budget_remaining_usd": 49.42},
        warnings=[], now=NOWS[3])

    first, last = models[0].reference_line.points[0], models[0].reference_line.points[-1]
    manifest = {
        "slug": FIXTURE_SLUG, "name": FIXTURE_NAME, "city": CITY, "footpath_side": SIDE,
        "start": {"lat": first.lat, "lon": first.lon}, "end": {"lat": last.lat, "lon": last.lon},
        "description": "Generated test fixture: a 100 m arc (R=80 m, ~72 degrees) with a footpath that narrows, shifts, "
                       "breaks and goes missing. Photos are synthetic drawings. Regenerate with "
                       "`uv run python -m tests.fixtures.make_model_fixtures`.",
        "versions": [0, 1, 2], "runs": [{"run": 1, "result_version": 1}, {"run": 2, "result_version": 2},
                                        {"run": 3, "result_version": 2, "no_op": True}],
        "observations": len(obs_list), "origin": list(DEMO_ORIGIN),
    }
    bundle = Bundle(ref=ref, observations=obs_list, images=images, models=models, fidelities=fids, diffs=diffs,
                    scenes=scenes, summaries=summaries, reports=[rep1, rep2, rep3], extractions=extractions,
                    batches={1: [over_batch, *s1], 2: s2}, manifest=manifest)
    _validate_extractions(extractions)
    return bundle


def _validate_extractions(extractions: list[dict]) -> None:
    try:
        from hero.fusion.schemas import OverheadExtraction, StreetExtraction
    except ImportError:  # sibling slice (S3b) not present: the shapes follow spec/data.md by construction
        return
    for e in extractions:
        (OverheadExtraction if e["kind"] == "overhead" else StreetExtraction).model_validate(e["extraction"])


# ------------------------------------------------------------------ writing


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def _pyd(m: Any) -> Any:
    return json.loads(m.model_dump_json())


def write(bundle: Bundle, out_dir: Path = OUT_DIR, scene_out: Path | None = SCENE_OUT) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "images").mkdir(parents=True)
    for name, data in sorted(bundle.images.items()):
        (out_dir / "images" / name).write_bytes(data)
    (out_dir / "manifest.json").write_text(_dump(bundle.manifest), encoding="utf-8")
    (out_dir / "observations.json").write_text(_dump([_pyd(o) for o in bundle.observations]), encoding="utf-8")
    for v, (m, f, sc, summ) in enumerate(zip(bundle.models, bundle.fidelities, bundle.scenes, bundle.summaries)):
        (out_dir / f"model_v{v}.json").write_text(_dump(_pyd(m)), encoding="utf-8")
        (out_dir / f"scene_v{v}.json").write_text(_dump(sc), encoding="utf-8")
        (out_dir / f"fidelity_v{v}.json").write_text(_dump(_pyd(f)), encoding="utf-8")
        (out_dir / f"summary_v{v}.txt").write_text(summ + "\n", encoding="utf-8")
        if bundle.diffs[v] is not None:
            (out_dir / f"diff_v{v}.json").write_text(_dump(_pyd(bundle.diffs[v])), encoding="utf-8")
    for n, rep in enumerate(bundle.reports, 1):
        (out_dir / f"report_run{n}.json").write_text(_dump(_pyd(rep)), encoding="utf-8")
    (out_dir / "extractions.json").write_text(_dump(bundle.extractions), encoding="utf-8")
    if scene_out is not None:
        scene_out.write_text(_dump(bundle.scenes[2]), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the model fixture (tests/e2e/fixtures/model_fixture).")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    bundle = build()
    write(bundle, args.out, SCENE_OUT if args.out == OUT_DIR else None)
    m = bundle.models[2]
    print(f"wrote {args.out}: {len(bundle.observations)} observations, versions 0..2, "
          f"v2 has {m.computed_stats().entity_count} entities, fidelity L{bundle.fidelities[2].overall.level}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
