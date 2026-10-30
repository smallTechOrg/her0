"""One function per pipeline step (spec/agent.md -> ## Steps). Each takes (state, ctx), mutates the
state and returns a short detail string for the step record. Exceptions propagate to the runner,
which decides (per step) whether the run continues."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from hero.config import Settings
from hero.imagery.annotate import annotate_tile
from hero.imagery.cache import ImageryCache
from hero.imagery.google import GoogleImagery
from hero.imagery.osm import fetch_osm
from hero.imagery.preview import make_thumbnail, render_topdown
from hero.llm.client import LLMClient, LLMImage
from hero.llm.errors import SchemaValidationError, UpstreamError
from hero.pipeline.assemble import assemble_scene
from hero.pipeline.review import PatchError, ReviewOutput, apply_patch, scene_summary
from hero.pipeline.state import PanoRef, PipelineState, ReviewIteration, TileRef
from hero.prompts import render_prompt
from hero.scene.geo import meters_per_px
from hero.scene.schema import LayoutGraph, ObstacleGraph, Quality, Scene

SATELLITE_SEND_PX = 1024
PREVIEW_SEND_PX = 1024
PANO_SEND_PX = 640
JPEG_QUALITY = 85
LAYOUT_MAX_TOKENS = 4096
OBSTACLES_MAX_TOKENS = 6000
REVIEW_MAX_TOKENS = 2500
THUMB_PX = 480


@dataclass
class PipelineContext:
    settings: Settings
    run_id: str
    llm: LLMClient
    google: GoogleImagery
    osm_cache: ImageryCache
    data_dir: Path

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews" / self.run_id

    @property
    def crops_dir(self) -> Path:
        return self.data_dir / "crops" / self.run_id


StepFn = Callable[[PipelineState, PipelineContext], str]


# ------------------------------------------------------------------ helpers


def image_payload(image: Image.Image, caption: str, max_px: int) -> LLMImage:
    img = image.convert("RGB")
    if max(img.size) > max_px:
        img = img.copy()
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return LLMImage(data=buffer.getvalue(), media_type="image/jpeg", caption=caption, width_px=img.width,
                    height_px=img.height)


def _open(path: str) -> Image.Image:
    return Image.open(path).convert("RGB")


def _pano_payloads(state: PipelineState) -> list[LLMImage]:
    return [image_payload(_open(p.path), f"Street View frame pano_index={p.index}, taken at x_m={p.x_m:.1f} "
                          f"looking along heading {p.heading_deg:.0f} deg", PANO_SEND_PX) for p in state.streetview]


def _quality(state: PipelineState) -> Quality:
    if state.streetview_available and state.streetview and state.obstacles is not None:
        return Quality.full
    return Quality.satellite_only


def _models(settings: Settings) -> dict[str, str]:
    return {"vision": settings.vision_model, "review": settings.review_model}


def _save_preview(state: PipelineState, ctx: PipelineContext, name: str, *, crop: bool = True) -> tuple[Path, Image.Image]:
    assert state.scene is not None and state.satellite is not None
    image = render_topdown(state.scene, _open(state.satellite.path), crop=crop)
    ctx.previews_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.previews_dir / name
    image.save(path, format="PNG", optimize=True)
    return path, image


def _media_url(ctx: PipelineContext, path: Path) -> str:
    return "/media/" + path.relative_to(ctx.data_dir).as_posix()


# ------------------------------------------------------------------ steps


def resolve_geometry(state: PipelineState, ctx: PipelineContext) -> str:
    frame = state.frame
    detail = f"length {frame.length_m:.1f} m, heading {frame.heading_deg:.1f} deg"
    try:
        state.osm = fetch_osm(frame, ctx.osm_cache, ctx.settings.overpass_url)
        detail += f"; OSM {'cached' if state.osm.cache_hit else 'fetched'}: {state.osm.summary(frame.length_m)}"
    except (UpstreamError, ValueError, OSError) as exc:
        state.osm = None
        state.osm_error = str(exc)[:300]
        state.warn("Overpass (OSM) unavailable; buildings come from the satellite pass only.")
        detail += f"; OSM unavailable ({type(exc).__name__}: {str(exc)[:120]})"
    return detail


def fetch_satellite(state: PipelineState, ctx: PipelineContext) -> str:
    mid = state.frame.midpoint()
    zoom = ctx.settings.satellite_zoom
    fetched = ctx.google.satellite_tile(mid.lat, mid.lon, zoom)
    with Image.open(fetched.path) as img:
        size = img.size
    state.satellite = TileRef(
        path=fetched.path, url=fetched.url, center_lat=round(mid.lat, 6), center_lon=round(mid.lon, 6), zoom=zoom,
        scale=2, size_px=(size[0], size[1]), meters_per_px=meters_per_px(mid.lat, zoom, 2), cache_hit=fetched.cache_hit,
    )
    annotated = annotate_tile(_open(fetched.path), state.frame, state.satellite.georef(), state.osm)
    ctx.previews_dir.mkdir(parents=True, exist_ok=True)
    annotated_path = ctx.previews_dir / "annotated.png"
    annotated.save(annotated_path, format="PNG", optimize=True)
    state.satellite.annotated_path = str(annotated_path)
    state.satellite.annotated_url = _media_url(ctx, annotated_path)
    return (f"{size[0]}x{size[1]} px at zoom {zoom} ({state.satellite.meters_per_px:.4f} m/px), "
            f"{'cache hit' if fetched.cache_hit else 'fetched from Google'}")


def verify_streetview(state: PipelineState, ctx: PipelineContext) -> str:
    start = state.frame.to_latlon(0.0, 0.0)
    meta = ctx.google.streetview_metadata(start.lat, start.lon)
    state.streetview_status = meta.status
    state.streetview_available = meta.ok
    if not meta.ok:
        state.warn(f"Street View unavailable at the segment start (status {meta.status}); generating satellite-only.")
        return f"status {meta.status}: satellite-only generation ({'cached' if meta.cache_hit else 'checked'})"
    return f"status OK, pano {meta.pano_id} ({'cached' if meta.cache_hit else 'checked'})"


def fetch_streetview(state: PipelineState, ctx: PipelineContext) -> str:
    samples = max(1, ctx.settings.streetview_samples)
    length = state.frame.length_m
    positions = [0.0] if samples == 1 else [length * i / (samples - 1) for i in range(samples)]
    panos: list[PanoRef] = []
    failures: list[str] = []
    for index, x_m in enumerate(positions):
        point = state.frame.to_latlon(x_m, 0.0)
        try:
            fetched = ctx.google.streetview_image(point.lat, point.lon, state.frame.heading_deg)
        except UpstreamError as exc:
            failures.append(f"x_m={x_m:.0f}: {exc.message[:80]}")
            continue
        panos.append(PanoRef(index=len(panos), path=fetched.path, url=fetched.url, lat=round(point.lat, 6),
                             lon=round(point.lon, 6), heading_deg=round(state.frame.heading_deg, 1), x_m=round(x_m, 1),
                             cache_hit=fetched.cache_hit))
    state.streetview = panos
    if failures:
        state.warn(f"{len(failures)} Street View frame(s) could not be fetched: " + "; ".join(failures))
    if not panos:
        state.streetview_available = False
        state.warn("No Street View frame could be fetched; generating satellite-only.")
        raise UpstreamError("All Street View frames failed: " + "; ".join(failures), kind="streetview")
    hits = sum(1 for p in panos if p.cache_hit)
    return f"{len(panos)} frame(s) at x_m " + ", ".join(f"{p.x_m:.0f}" for p in panos) + f" ({hits} cached)"


def detect_layout(state: PipelineState, ctx: PipelineContext) -> str:
    assert state.satellite is not None and state.satellite.annotated_path
    tile = state.satellite
    frame = state.frame
    osm_line = (f"OSM context for the area: {state.osm.summary(frame.length_m)}; OSM ways are drawn as thin lines "
                "(blue roads, orange footways, yellow buildings)." if state.osm else
                "No OpenStreetMap data is available for this tile.")
    prompt = render_prompt(
        "detect_layout", length_m=f"{frame.length_m:.1f}", footpath_side=frame.footpath_side.value, osm_line=osm_line,
        meters_per_px=f"{tile.meters_per_px:.4f}", px_per_10m=f"{10.0 / tile.meters_per_px:.0f}",
    )
    images = [image_payload(_open(tile.path), "raw satellite tile", SATELLITE_SEND_PX),
              image_payload(_open(tile.annotated_path), "annotated satellite tile (segment, START/END, 10 m ticks, "
                            "footpath-side arrow)", SATELLITE_SEND_PX)]
    layout, usage = ctx.llm.vision_json(
        run_id=ctx.run_id, step="detect_layout", images=images, prompt=prompt, schema=LayoutGraph,
        model=ctx.settings.vision_model, prompt_name="detect_layout", max_tokens=LAYOUT_MAX_TOKENS,
    )
    state.layout = layout
    state.cost.add(usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.calls)
    return (f"road {layout.road_width_m:.1f} m, footpath {layout.footpath_width_m:.1f} m, "
            f"{len(layout.footpath_coverage)} coverage range(s), {len(layout.buildings)} building(s), "
            f"{len(layout.trees)} tree(s), {len(layout.crossings)} crossing(s); ${usage.cost_usd:.4f}")


def detect_obstacles(state: PipelineState, ctx: PipelineContext) -> str:
    frame = state.frame
    prompt = render_prompt(
        "detect_obstacles", pano_count=len(state.streetview), length_m=f"{frame.length_m:.1f}",
        heading_deg=f"{frame.heading_deg:.0f}", footpath_side=frame.footpath_side.value,
    )
    try:
        obstacles, usage = ctx.llm.vision_json(
            run_id=ctx.run_id, step="detect_obstacles", images=_pano_payloads(state), prompt=prompt,
            schema=ObstacleGraph, model=ctx.settings.vision_model, prompt_name="detect_obstacles",
            max_tokens=OBSTACLES_MAX_TOKENS,
        )
    except SchemaValidationError as exc:
        state.obstacles = None
        state.obstacles_failed = True
        state.warn("Obstacle pass returned an invalid graph twice; objects come from the satellite pass only.")
        raise UpstreamError(f"obstacle pass invalid twice: {exc.message[:200]}", kind="anthropic") from exc
    except UpstreamError:
        state.obstacles = None
        state.obstacles_failed = True
        state.warn("Obstacle pass failed upstream; objects come from the satellite pass only.")
        raise
    state.obstacles = obstacles
    state.cost.add(usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.calls)
    types = sorted({o.type.value for o in obstacles.objects})
    return (f"{len(obstacles.objects)} object(s) [{', '.join(types)}], {len(obstacles.facades)} facade(s); "
            f"${usage.cost_usd:.4f}")


def assemble(state: PipelineState, ctx: PipelineContext) -> str:
    assert state.layout is not None and state.satellite is not None
    quality = _quality(state)
    state.scene = assemble_scene(
        segment=state.segment, frame=state.frame, tile=state.satellite, osm=state.osm, layout=state.layout,
        obstacles=state.obstacles, panos=state.streetview, run_id=ctx.run_id, quality=quality,
        models=_models(ctx.settings), warnings=state.warnings, crops_dir=ctx.crops_dir,
    )
    scene = state.scene
    return (f"{len(scene.objects)} object(s) ({len(scene.hazards())} hazard(s)), {len(scene.buildings)} building(s), "
            f"{len(scene.tokens)} token(s), quality {quality.value}, walkability {scene.walkability.score}/100")


def review_once(n: int) -> StepFn:
    def _review(state: PipelineState, ctx: PipelineContext) -> str:
        assert state.scene is not None and state.satellite is not None
        preview_path, preview = _save_preview(state, ctx, f"iter{n}.png")
        frame = state.frame
        prompt = render_prompt(
            "review_scene", length_m=f"{frame.length_m:.1f}", pano_count=len(state.streetview),
            footpath_side=frame.footpath_side.value, scene_summary=scene_summary(state.scene),
        )
        images = [image_payload(preview, "top-down preview of the current scene drawn over the tile", PREVIEW_SEND_PX),
                  image_payload(_open(state.satellite.annotated_path or state.satellite.path),
                                "satellite tile with the segment annotated", SATELLITE_SEND_PX)]
        images += _pano_payloads(state)
        result, usage = ctx.llm.vision_json(
            run_id=ctx.run_id, step=f"review_iteration_{n}", images=images, prompt=prompt, schema=ReviewOutput,
            model=ctx.settings.review_model, prompt_name="review_scene", max_tokens=REVIEW_MAX_TOKENS,
        )
        state.cost.add(usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.calls)
        ops_json = [op.model_dump(mode="json", exclude_none=True) for op in result.patch_ops]
        applied = False
        detail = ""
        if result.patch_ops:
            try:
                patched = apply_patch(state.scene, result.patch_ops, frame, state.satellite.georef())
                state.scene = patched.scene
                applied = True
                detail = f"{patched.applied}/{len(result.patch_ops)} op(s) applied"
                if patched.skipped:
                    detail += "; skipped " + "; ".join(patched.skipped)[:300]
            except PatchError as exc:
                detail = f"patch rejected: {exc}"
                state.warn(f"Review iteration {n}: patch rejected ({str(exc)[:120]}).")
        else:
            detail = "no changes proposed"
        state.iterations.append(ReviewIteration(
            n=n, preview_path=str(preview_path), preview_url=_media_url(ctx, preview_path), accepted=result.accepted,
            critique=result.critique, patch_ops=ops_json, applied=applied, detail=detail,
        ))
        return f"{'accepted' if result.accepted else 'not accepted'}; {detail}; ${usage.cost_usd:.4f}"

    return _review


def finalize(state: PipelineState, ctx: PipelineContext) -> str:
    assert state.scene is not None and state.satellite is not None
    quality = _quality(state)
    scene_dict = state.scene.model_dump(mode="json")
    scene_dict["generation"].update({
        "quality": quality.value, "review_iterations": len(state.iterations), "warnings": list(state.warnings),
        "models": _models(ctx.settings), "run_id": ctx.run_id,
    })
    state.scene = Scene.model_validate(scene_dict)
    final_path, final_img = _save_preview(state, ctx, "final.png")
    thumb_path = ctx.previews_dir / "thumb.png"
    make_thumbnail(final_img, THUMB_PX).save(thumb_path, format="PNG", optimize=True)
    return (f"quality {quality.value}, confidence {state.scene.generation.confidence_overall:.2f}, "
            f"{len(state.scene.objects)} object(s) [{object_type_summary(state.scene)}], {len(state.scene.tokens)} token(s), "
            f"walkability {state.scene.walkability.score}/100, {len(state.iterations)} review iteration(s)")


def object_type_summary(scene: Scene) -> str:
    counts: dict[str, int] = {}
    for obj in scene.objects:
        counts[obj.type.value] = counts.get(obj.type.value, 0) + 1
    return ", ".join(f"{k} x{v}" for k, v in sorted(counts.items()))


def thumbnail_rel_path(ctx: PipelineContext) -> str:
    return f"previews/{ctx.run_id}/thumb.png"
