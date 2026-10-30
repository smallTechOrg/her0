"""Deterministic builders for street-model tests (owned by S1a; used by every slice's unit tests).

Import as ``from tests.fixtures.street_fixtures import ...`` (``tests/conftest.py`` puts the repo root on
``sys.path``). Everything here is pure and seeded: no clock, no network, no randomness.
"""

from __future__ import annotations

import io
import json
import math
from datetime import UTC, datetime

from pathlib import Path

from hero.model.refline import ReferenceLine
from hero.model.types import (
    CanonicalModel,
    Condition,
    Entity,
    EntityState,
    EntityType,
    Evidence,
    EvidenceRole,
    Extent,
    FidelityBin,
    FidelityMap,
    FidelityOverall,
    FootpathState,
    Material,
    ModelKind,
    ModelStats,
    Observation,
    ObservationKind,
    ObservationSource,
    Position,
    ProfileInterval,
    Strip,
    StripKind,
    ViewKind,
)
from hero.scene.geo import METERS_PER_DEG_LAT

FIXED_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
DEMO_ORIGIN = (12.9706, 77.6413)  # 100 Feet Road, Indiranagar (southern end of the demo stretch)
DEMO_SLUG = "fixture-curved-street"
SHA = "0" * 64


# ------------------------------------------------------------------ reference lines


def _latlon(origin: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
    lat0, lon0 = origin
    return lat0 + north_m / METERS_PER_DEG_LAT, lon0 + east_m / (METERS_PER_DEG_LAT * math.cos(math.radians(lat0)))


def straight_reference_line(length_m: float = 100.0, *, bearing_deg: float = 0.0,
                            origin: tuple[float, float] = DEMO_ORIGIN, n_points: int = 2) -> ReferenceLine:
    b = math.radians(bearing_deg)
    pts = [_latlon(origin, math.sin(b) * length_m * i / (n_points - 1), math.cos(b) * length_m * i / (n_points - 1))
           for i in range(n_points)]
    return ReferenceLine.from_latlons(pts)


def curved_reference_line(radius_m: float = 80.0, length_m: float = 100.0, *, start_bearing_deg: float = 0.0,
                          turn: str = "right", origin: tuple[float, float] = DEMO_ORIGIN,
                          step_m: float = 0.25) -> ReferenceLine:
    """A circular arc of ``radius_m`` and arc length ``length_m`` as a dense polyline (default 0.25 m chords).

    ``turn="right"`` bends clockwise (bearing increases). 100 m on R=80 m turns ~71.6 degrees.
    """
    if radius_m <= 0 or length_m <= 0 or step_m <= 0:
        raise ValueError("radius_m, length_m and step_m must be positive")
    k = (1.0 if turn == "right" else -1.0) / radius_m
    b0 = math.radians(start_bearing_deg)
    n = max(2, math.ceil(length_m / step_m) + 1)
    pts = []
    for i in range(n):
        s = length_m * i / (n - 1)
        east = (math.cos(b0) - math.cos(b0 + k * s)) / k
        north = (math.sin(b0 + k * s) - math.sin(b0)) / k
        pts.append(_latlon(origin, east, north))
    return ReferenceLine.from_latlons(pts)


# ------------------------------------------------------------------ observations


def make_observation(*, obs_id: str = "obs-0001", segment_id: str = "seg-0001",
                     kind: ObservationKind = ObservationKind.streetview, station_s: float | None = 0.0,
                     view: ViewKind | None = ViewKind.forward, offset_t: float | None = 0.0,
                     lat: float = DEMO_ORIGIN[0], lon: float = DEMO_ORIGIN[1], heading_deg: float | None = 0.0,
                     pitch_deg: float | None = 0.0, fov_deg: float | None = 90.0, zoom: int | None = None,
                     size_px: tuple[int, int] = (640, 640), capture_date: str | None = "2024-02",
                     pano_id: str | None = None, obs_key: str | None = None) -> Observation:
    source = {ObservationKind.streetview: ObservationSource.google_streetview,
              ObservationKind.user_photo: ObservationSource.upload}.get(kind, ObservationSource.google_static_maps)
    satellite = kind in (ObservationKind.satellite_overview, ObservationKind.satellite_crop)
    ext = "png" if satellite else "jpg"
    return Observation(
        id=obs_id, segment_id=segment_id, obs_key=obs_key or f"key-{obs_id}", kind=kind, source=source, sha256=SHA,
        file_path=f"imagery/{obs_id}.{ext}", media_type="image/png" if satellite else "image/jpeg",
        width_px=size_px[0], height_px=size_px[1], bytes=1000, lat=lat, lon=lon,
        heading_deg=None if satellite else heading_deg, pitch_deg=None if satellite else pitch_deg,
        fov_deg=None if satellite else fov_deg, zoom=zoom if satellite else None,
        scale=2 if satellite else None, meters_per_px=0.15 if satellite else None,
        pano_id=pano_id, capture_date=None if satellite else capture_date, station_s=station_s, offset_t=offset_t,
        view=None if satellite else view, georef={"size_px": list(size_px)}, cost_usd=0.0, acquired_run_id=None,
        note=None, created_at=FIXED_NOW,
    )


# ------------------------------------------------------------------ model pieces


def make_strips(side: str = "left", *, footpath: bool = True, weight: float = 0.15,
                footpath_t: tuple[float, float] = (5.0, 6.5), carriageway_t: tuple[float, float] = (-4.25, 4.25),
                tag: str = "") -> list[Strip]:
    """Carriageway (centre) plus, optionally, the modelled-side footpath; edges constant along the interval."""
    conf = round(min(0.95, 1 - math.exp(-weight)), 4)
    strips = [Strip(id=f"sp-carriageway-C{tag}", kind=StripKind.carriageway, side="centre", material=Material.asphalt,
                    condition=Condition.fair, height_m=0.0, t_lo=(carriageway_t[0],) * 2,
                    t_hi=(carriageway_t[1],) * 2, weight=weight, confidence=conf,
                    description="Two-way asphalt carriageway.", provenance=[])]
    if footpath:
        lo, hi = footpath_t if side == "left" else (-footpath_t[1], -footpath_t[0])
        strips.append(Strip(id=f"sp-footpath-{'L' if side == 'left' else 'R'}{tag}", kind=StripKind.footpath,
                            side=side, material=Material.paver, condition=Condition.fair, height_m=0.15,
                            t_lo=(lo, lo), t_hi=(hi, hi), weight=weight, confidence=conf,
                            description="Paver footpath.", provenance=[]))
    return sorted(strips, key=lambda s: s.t_lo[0])


def make_profile(length_s: float, *, side: str = "left", interval_m: float = 25.0,
                 states: list[FootpathState] | None = None) -> list[ProfileInterval]:
    """Tile ``[0, length_s]`` with intervals of ~``interval_m``; ``states`` cycles over them (default all unknown)."""
    n = max(1, round(length_s / interval_m))
    out: list[ProfileInterval] = []
    for i in range(n):
        s0, s1 = length_s * i / n, length_s * (i + 1) / n
        state = (states[i % len(states)] if states else FootpathState.unknown)
        out.append(ProfileInterval(
            id=f"pi-{i:04d}", s_from=round(s0, 3), s_to=round(s1, 3) if i < n - 1 else length_s,
            footpath_state=state, state_votes={state.value: 0.15},
            strips=make_strips(side, footpath=state is not FootpathState.missing, tag=f"-{i}"),
            description=f"{s0:.0f}-{s1:.0f} m: {state.value} footpath.", evidence_summary="Map prior only."))
    return out


def make_entity(*, entity_id: str = "ent-0000000001", type: EntityType = EntityType.pole, s: float = 10.0,
                t: float = 5.5, confidence: float = 0.7, state: EntityState = EntityState.present,
                observation_id: str = "obs-0001", version: int = 1, description: str = "A pole.") -> Entity:
    return Entity(
        id=entity_id, type=type, state=state, position=Position(s=s, t=t), position_sigma_m=0.8,
        extent=Extent(length_s=0.3, width_t=0.3), height_m=7.0, orientation_deg=0.0, attributes={},
        description=description, confidence=confidence, logit=math.log(confidence / (1 - confidence)),
        provenance=[Evidence(observation_id=observation_id, role=EvidenceRole.supports, detection_key=f"{observation_id}:d1",
                             crop_px=(10, 20, 30, 40), weight=0.9, added_in_version=version)],
        billboard=None, first_seen_version=version, last_changed_version=version)


def make_fidelity_map(length_s: float, level: int = 0, *, bin_m: float = 5.0) -> FidelityMap:
    n = max(1, math.ceil(length_s / bin_m - 1e-9))
    bins = [FidelityBin(index=i, s_from=round(i * bin_m, 3), s_to=round(min(length_s, (i + 1) * bin_m), 3),
                        level=level, views=0 if level == 0 else 2, stations=0 if level == 0 else 1,
                        sides={"left": 0, "right": 0}, overhead_hires=False, hint="") for i in range(n)]
    hist = {f"L{i}": 0.0 for i in range(4)}
    hist[f"L{level}"] = round(length_s, 3)
    return FidelityMap(bin_m=bin_m, length_s=length_s, bins=bins,
                       overall=FidelityOverall(level=level, score=float(level), histogram_m=hist))


def make_model(ref: ReferenceLine | None = None, *, slug: str = DEMO_SLUG, version: int = 0,
               kind: ModelKind = ModelKind.prior, side: str = "left", entities: list[Entity] | None = None,
               fused_observation_ids: list[str] | None = None, states: list[FootpathState] | None = None,
               source: str = "osm_nominatim", run_id: str | None = None, parent_version: int | None = None) -> CanonicalModel:
    """A small valid ``CanonicalModel`` over ``ref`` (default: the 100 m R=80 m curved fixture line)."""
    ref = ref or curved_reference_line()
    ents = entities or []
    fused = fused_observation_ids or []
    legacy = kind is ModelKind.legacy
    model = CanonicalModel(
        segment_slug=slug, version=version, parent_version=parent_version if parent_version is not None else (
            None if version == 0 else version - 1), kind=kind, run_id=run_id, created_at=FIXED_NOW, legacy=legacy,
        placement_verified=source.startswith("osm") and not legacy, footpath_side=side,
        reference_line=ref.to_model(source=source, osm_way_id=25173540, name="100 Feet Road"),
        profile=make_profile(round(ref.length_s, 3), side=side, states=states), entities=ents, buildings=[],
        contradictions=[], fused_observation_ids=fused)
    return model.model_copy(update={"stats": model.computed_stats()})


# ------------------------------------------------------------------ scene v2


def minimal_scene_v2_dict(length_m: float = 20.0, *, slug: str = DEMO_SLUG, with_object: bool = True) -> dict:
    """A valid straight-road Scene v2 as a plain dict (stations every 2.5 m; footpath lane at +5.9 m)."""
    s_vals = [i * 2.5 for i in range(int(length_m // 2.5) + 1)]
    if abs(s_vals[-1] - length_m) > 1e-9:
        s_vals.append(length_m)
    stations = [{
        "s": s, "lane_lat": [5.9, 4.1, 2.3],
        "footpath": {"lo": 5.1, "hi": 6.7, "state": "intact", "material": "paver", "height_m": 0.15, "confidence": 0.9},
        "strips": [{"kind": "carriageway", "lo": -4.25, "hi": 4.25, "material": "asphalt", "height_m": 0.0,
                    "confidence": 0.6}],
        "ground_uv": [[0.5, 0.4], [0.5, 0.6]]} for s in s_vals]
    samples = [{"s": s, "x": 0.0, "z": -s, "theta": 0.0, "tile_px": [640.0, 640.0 - s * 6.8]} for s in s_vals]
    objects = []
    if with_object:
        objects.append({
            "id": "ent-0000000001", "entity_id": "ent-0000000001", "type": "parked_vehicle",
            "label": "auto-rickshaw parked on footpath", "x_m": 5.0, "length_m": 2.6, "lanes": [0, 1], "action": "dodge",
            "lateral_m": 5.9, "yaw_deg": 4.0, "size_m": {"w": 1.4, "h": 1.7, "d": 2.6}, "confidence": 0.86,
            "scenery": False, "description": "Yellow auto-rickshaw parked half on the footpath.",
            "evidence": {"observation_count": 3, "sources": {"street_view": 3, "satellite": 0, "upload": 0},
                         "summary": "Seen in 3 Street View frames.", "observation_ids": ["obs-0001"], "state": "present"},
            "billboard_url": None, "source": None, "latlon": {"lat": 12.9707, "lon": 77.6414}, "tile_px": [640.0, 600.0]})
    return {
        "schema_version": 2,
        "segment": {"slug": slug, "name": "Fixture curved street", "city": "Bengaluru",
                    "start": {"lat": 12.9706, "lon": 77.6413},
                    "end": {"lat": 12.9706 + length_m / METERS_PER_DEG_LAT, "lon": 77.6413},
                    "length_m": length_m, "heading_deg": 0.0, "footpath_side": "left"},
        "ground": {"tile_url": None, "size_px": [1280, 1280], "zoom": 19, "scale": 2,
                   "center": {"lat": 12.9707, "lon": 77.6413}, "meters_per_px": 0.1455,
                   "frame_to_px": {"origin_px": [640.0, 640.0], "along_px_per_m": [0.0, -6.87],
                                   "across_px_per_m": [-6.87, 0.0]}},
        "path": {"length_m": length_m, "samples": samples},
        "corridor": {"step_m": 2.5, "ground_half_m": 30.0, "stations": stations},
        "cross_section": {"road_width_m": 8.5,
                          "lanes": [{"index": 0, "name": "footpath", "kind": "footpath", "lat_m": 5.9, "width_m": 1.5},
                                    {"index": 1, "name": "road_edge", "kind": "road_edge", "lat_m": 4.1, "width_m": 1.6},
                                    {"index": 2, "name": "road", "kind": "road", "lat_m": 2.3, "width_m": 2.0}],
                          "footpath_coverage": [{"from_m": 0.0, "to_m": length_m, "state": "intact", "confidence": 0.9}]},
        "buildings": [{"id": "bld-osm-1827364", "source": "osm",
                       "footprint": [[0.0, 9.0], [10.0, 9.0], [10.0, 21.0], [0.0, 21.0]], "height_m": 9.6,
                       "color_hex": "#c9b79c", "facade_url": None, "confidence": 0.9, "side": "modelled",
                       "description": "Three-storey cream shopfront block."}],
        "objects": objects,
        "tokens": [{"id": "t1", "x_m": 2.5, "lane": 0, "reason": "intact_footpath",
                    "latlon": {"lat": 12.9706, "lon": 77.6413}, "tile_px": [640.0, 620.0]}],
        "walkability": {"footpath_coverage_pct": 100.0, "hazard_count": len(objects), "crossing_present": False, "score": 78},
        "about": {"model_version": 1,
                  "fidelity": {"level": "L1", "score": 1.0, "label": "Single viewpoint",
                               "histogram_m": {"L0": 0.0, "L1": length_m, "L2": 0.0, "L3": 0.0}},
                  "photos": {"total": 4, "street_view": 3, "satellite": 1, "user": 0},
                  "last_updated": "2026-10-30", "capture_dates": {"oldest": "2023-04", "newest": "2024-02"},
                  "low_confidence_parts": [], "summary": "This street is modelled at fidelity L1 from 4 photos (model v1).",
                  "improve_hint": "Add left and right views.", "legacy": False, "placement_verified": True, "degraded": False},
        "generation": {"run_id": "run-0001", "quality": "full", "confidence_overall": 0.7, "review_iterations": 1,
                       "models": {"vision": "claude-sonnet-4-5", "review": "claude-sonnet-4-5"}, "warnings": []},
        "model": {"version": 1, "model_hash": "a" * 64, "compiled_by": "hero.model.compile/1"},
    }


# ------------------------------------------------------------------ a tiny fixture directory (loader tests)


def write_mini_fixture_dir(root: Path, *, slug: str = DEMO_SLUG, length_m: float = 100.0) -> Path:
    """Write a minimal directory following the documented ``model_fixture`` contract (3 versions, 2 runs).

    Observation ids are ``obs-fx-1`` ... ``obs-fx-4``; the v1 model has one entity, v2 two. Not the S1d fixture:
    just enough to exercise ``hero.tools.load_fixture_street`` deterministically.
    """
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    ref = straight_reference_line(length_m)
    obs_ids = [f"obs-fx-{i}" for i in range(1, 5)]
    observations = []
    for i, oid in enumerate(obs_ids):
        kind = ObservationKind.satellite_overview if i == 0 else ObservationKind.streetview
        o = make_observation(obs_id=oid, segment_id="fixture", kind=kind, station_s=None if i == 0 else 10.0 * i,
                             view=None if i == 0 else (ViewKind.forward if i < 3 else ViewKind.left), zoom=19,
                             obs_key=f"fixture-key-{i}")
        observations.append(json.loads(o.model_dump_json()))
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (40 * i, 80, 120)).save(buf, "JPEG")
        (root / "images" / Path(o.file_path).name).write_bytes(buf.getvalue())
    (root / "observations.json").write_text(json.dumps(observations), encoding="utf-8")

    e1 = make_entity(entity_id="ent-00000000a1", s=20.0, observation_id="obs-fx-2", confidence=0.6, version=1)
    e2 = make_entity(entity_id="ent-00000000a2", type=EntityType.bollard, s=40.0, observation_id="obs-fx-3", version=2)
    e1_v2 = e1.model_copy(update={"confidence": 0.8, "last_changed_version": 2})
    models = [
        make_model(ref, slug=slug, version=0, kind=ModelKind.prior),
        make_model(ref, slug=slug, version=1, kind=ModelKind.fused, entities=[e1], run_id="fx-run-1",
                   fused_observation_ids=obs_ids[:2]),
        make_model(ref, slug=slug, version=2, kind=ModelKind.fused, entities=[e1_v2, e2], run_id="fx-run-2",
                   fused_observation_ids=obs_ids),
    ]
    from hero.model.types import ModelDiff, RunReport

    for v, m in enumerate(models):
        (root / f"model_v{v}.json").write_text(m.model_dump_json(), encoding="utf-8")
        scene = minimal_scene_v2_dict(length_m, slug=slug)
        scene["about"]["model_version"] = v
        (root / f"scene_v{v}.json").write_text(json.dumps(scene), encoding="utf-8")
        (root / f"fidelity_v{v}.json").write_text(make_fidelity_map(ref.length_s, level=min(v, 2)).model_dump_json(),
                                                  encoding="utf-8")
        (root / f"summary_v{v}.txt").write_text(f"Summary of version {v}.\n", encoding="utf-8")
        if v >= 1:
            diff = ModelDiff(from_version=v - 1, to_version=v, is_empty=False,
                             summary_lines=[f"{len(m.entities)} entities in v{v}"])
            (root / f"diff_v{v}.json").write_text(diff.model_dump_json(), encoding="utf-8")
    for n in (1, 2):
        rep = RunReport(run_id=f"fx-run-{n}", segment_slug=slug, mode="generate", base_version=n - 1, result_version=n,
                        version_created=True, summary=f"Run {n} fixed.")
        (root / f"report_run{n}.json").write_text(rep.model_dump_json(), encoding="utf-8")
    extractions = [
        {"run": 1, "batch_index": 0, "kind": "streetlevel", "observation_ids": obs_ids[1:3], "model": "replay",
         "prompt_name": "perceive_streetlevel", "extraction": {"frames": [], "detections": []}},
        {"run": 2, "batch_index": 0, "kind": "streetlevel", "observation_ids": obs_ids[3:], "model": "replay",
         "prompt_name": "perceive_streetlevel", "extraction": {"frames": [], "detections": []}},
    ]
    (root / "extractions.json").write_text(json.dumps(extractions), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({"slug": slug}), encoding="utf-8")
    return root
