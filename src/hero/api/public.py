"""Public JSON API (spec/api.md ## Public): health, gallery list, segment detail, scene (v1 | v2), game runs.

Phase 2 adds the additive ``model`` fidelity badge on gallery cards / detail (derived from the head
``model_versions`` row; ``null`` for a segment with no model version). The scene is served verbatim, v1 or v2,
as stored by ``publish_version`` (v2 carries ``about``). ``GET /segments/nearest`` and
``GET /segments/{slug}/runs`` are Phase 3 and deliberately absent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from hero import __version__
from hero.api.envelope import ApiError, ok
from hero.db import model_repo, repo
from hero.db.repo import CLIENT_MAX_CHARS, GameRunCreate, InvalidInputError, NotFoundError, SegmentRecord
from hero.db.session import get_session
from hero.model.scene_v2 import SceneV2
from hero.scene.schema import LatLon, Quality, Scene, SegmentStatus, Walkability

router = APIRouter(prefix="/api", tags=["public"])

NOT_GENERATED_MESSAGE = "This street has not been generated yet."


# ------------------------------------------------------------------ response models


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


FIDELITY_LABELS = {
    0: "Map-only sketch: satellite and map data, no street-level photos",
    1: "Single viewpoint: every part seen from at least one place",
    2: "Multi-view: every part seen from at least two places, both sides",
    3: "Close-up verified: multi-view plus a close-up photo",
}


class ModelBadge(_Out):
    """Additive Phase-2 fidelity badge shown on gallery cards."""

    version: int
    fidelity_level: str
    fidelity_label: str
    photos: int
    updated_at: str
    legacy: bool


class SegmentSummary(_Out):
    """One gallery card (``GET /api/segments``)."""

    slug: str
    name: str
    city: str
    quality: Quality | None
    length_m: float
    thumbnail_url: str | None
    best_score: int | None
    best_distance_m: float | None
    run_count: int
    walkability: Walkability | None
    model: ModelBadge | None = None


class SegmentDetail(SegmentSummary):
    """``GET /api/segments/{slug}``: the summary plus endpoints and lifecycle status."""

    start: LatLon
    end: LatLon
    status: SegmentStatus


class HazardOut(_Out):
    id: str
    type: str
    label: str
    confidence: float
    tile_px: tuple[float, float] | None
    latlon: LatLon
    action: str


class GameRunOut(_Out):
    """``POST /api/segments/{slug}/runs`` response."""

    run_id: str
    score: int
    best_score: int
    is_new_best: bool
    walkability: Walkability
    hazards: list[HazardOut]


# ------------------------------------------------------------------ mapping


def thumbnail_url(record: SegmentRecord) -> str | None:
    return f"/media/{record.thumbnail_path}" if record.thumbnail_path else None


def model_badge(record: SegmentRecord) -> ModelBadge | None:
    """Badge from the head model version; ``None`` when the segment has no version (never imported)."""
    versions = model_repo.list_versions(record.id)
    if not versions:
        return None
    head = versions[-1]
    level = min(3, max(0, head.fidelity_level))
    return ModelBadge(version=head.version, fidelity_level=f"L{level}", fidelity_label=FIDELITY_LABELS[level],
                      photos=head.observation_count, updated_at=head.created_at.isoformat(),
                      legacy=head.kind == "legacy")


def _summary_fields(record: SegmentRecord) -> dict[str, Any]:
    return {
        "slug": record.slug, "name": record.name, "city": record.city, "quality": record.quality,
        "length_m": record.length_m, "thumbnail_url": thumbnail_url(record),
        "best_score": record.best_score, "best_distance_m": record.best_distance_m,
        "run_count": repo.count_game_runs(record.id),
        "walkability": record.scene.walkability if record.scene else None,
        "model": model_badge(record),
    }


def to_summary(record: SegmentRecord) -> SegmentSummary:
    return SegmentSummary(**_summary_fields(record))


def to_detail(record: SegmentRecord) -> SegmentDetail:
    return SegmentDetail(**_summary_fields(record), start=record.start, end=record.end, status=record.status)


def _val(x: Any) -> str:
    return x.value if hasattr(x, "value") else str(x)  # v1 uses enums, v2 plain catalogue strings


def hazards_of(scene: Scene | SceneV2) -> list[HazardOut]:
    return [
        HazardOut(id=o.id, type=_val(o.type), label=o.label, confidence=o.confidence, tile_px=o.tile_px,
                  latlon=o.latlon, action=_val(o.action))
        for o in scene.hazards()
    ]


def _dump(model: BaseModel | list[BaseModel]) -> Any:
    if isinstance(model, list):
        return [m.model_dump(mode="json") for m in model]
    return model.model_dump(mode="json")


def _require(slug: str) -> SegmentRecord:
    try:
        return repo.require_segment(slug)
    except NotFoundError as exc:
        raise ApiError("not_found", str(exc)) from exc


def _require_ready_scene(record: SegmentRecord) -> Scene | SceneV2:
    if record.status is not SegmentStatus.ready or record.scene is None:
        raise ApiError("not_found", NOT_GENERATED_MESSAGE)
    return record.scene


# ------------------------------------------------------------------ routes


@router.get("/health")
def health():
    with get_session() as session:
        session.execute(text("SELECT 1"))
    return ok({"status": "ok", "version": __version__, "db": "ok"})


@router.get("/segments")
def list_segments():
    return ok(_dump([to_summary(s) for s in repo.list_ready_segments()]))


@router.get("/segments/{slug}")
def segment_detail(slug: str):
    return ok(_dump(to_detail(_require(slug))))


@router.get("/segments/{slug}/scene")
def segment_scene(slug: str):
    return ok(_dump(_require_ready_scene(_require(slug))))


@router.post("/segments/{slug}/runs")
def post_game_run(slug: str, body: GameRunCreate, request: Request):
    record = _require(slug)
    scene = _require_ready_scene(record)
    client = (request.headers.get("user-agent") or "")[:CLIENT_MAX_CHARS]
    try:
        result = repo.record_game_run(slug, body, client)
    except InvalidInputError as exc:
        raise ApiError("validation_error", str(exc)) from exc
    except NotFoundError as exc:
        raise ApiError("not_found", str(exc)) from exc
    return ok(_dump(GameRunOut(
        run_id=result.run.id, score=result.run.score, best_score=result.best_score, is_new_best=result.is_new_best,
        walkability=scene.walkability, hazards=hazards_of(scene),
    )))
