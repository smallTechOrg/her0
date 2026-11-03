"""PipelineState and the typed references it carries (spec/agent.md -> ## State)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from hero.db.repo import PipelineRunRecord, SegmentRecord, StepRecord
from hero.imagery.osm import OsmGeometry
from hero.scene.geo import LocalFrame, TileGeoref
from hero.scene.schema import LayoutGraph, ObstacleGraph, Scene


@dataclass
class TileRef:
    path: str
    url: str
    center_lat: float
    center_lon: float
    zoom: int
    scale: int
    size_px: tuple[int, int]
    meters_per_px: float
    cache_hit: bool
    annotated_path: str | None = None
    annotated_url: str | None = None

    def georef(self) -> TileGeoref:
        return TileGeoref(center_lat=self.center_lat, center_lon=self.center_lon, zoom=self.zoom,
                          scale=self.scale, size_px=self.size_px)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PanoRef:
    index: int
    path: str
    url: str
    lat: float
    lon: float
    heading_deg: float
    x_m: float
    cache_hit: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewIteration:
    n: int
    preview_path: str
    preview_url: str
    accepted: bool
    critique: str
    patch_ops: list[dict[str, Any]]
    applied: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostLedger:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0

    def add(self, input_tokens: int, output_tokens: int, cost_usd: float, calls: int = 1) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd = round(self.cost_usd + cost_usd, 6)
        self.llm_calls += calls


@dataclass
class PipelineError:
    code: str
    message: str
    step: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineState:
    run: PipelineRunRecord
    segment: SegmentRecord
    settings_snapshot: dict[str, Any]
    frame: LocalFrame
    osm: OsmGeometry | None = None
    osm_error: str | None = None
    satellite: TileRef | None = None
    streetview: list[PanoRef] = field(default_factory=list)
    streetview_available: bool | None = None  # None until verify_streetview has run
    streetview_status: str | None = None
    layout: LayoutGraph | None = None
    obstacles: ObstacleGraph | None = None
    obstacles_failed: bool = False
    scene: Scene | None = None
    iterations: list[ReviewIteration] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    cost: CostLedger = field(default_factory=CostLedger)
    warnings: list[str] = field(default_factory=list)
    error: PipelineError | None = None
    started_at: datetime | None = None
    previous_segment_status: str = "draft"

    @property
    def run_id(self) -> str:
        return self.run.id

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def imagery_json(self) -> dict[str, Any]:
        return {
            "satellite": self.satellite.to_json() if self.satellite else None,
            "streetview": [p.to_json() for p in self.streetview],
            "osm_path": self.osm.cache_path if self.osm else None,
            "osm_available": self.osm is not None,
            "osm_error": self.osm_error,
            "streetview_available": self.streetview_available,
            "streetview_status": self.streetview_status,
            "warnings": list(self.warnings),
        }
