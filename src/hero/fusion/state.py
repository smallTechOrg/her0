"""FusionState: everything one fusion run knows (spec/agent.md -> ## State).

A plain dataclass, mutated by the step functions in ``hero.fusion.steps``. The JSON-serialisable pieces are
persisted on the run row after every step by ``hero.fusion.runner.persist`` so the admin UI can poll.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from hero.db.model_repo import ExtractionRecord, ModelVersionRecord
from hero.db.repo import PipelineRunRecord, SegmentRecord, StepRecord
from hero.fusion.schemas import OverheadExtraction, StreetExtraction
from hero.fusion.views import OverheadWindow, StreetBatch
from hero.imagery.acquire import AcquireResult
from hero.imagery.osm import OsmGeometry
from hero.imagery.plan import ImageryPlan, PlanInfo, PlannedImage
from hero.model.batch import ProjectedBatch
from hero.model.merge import ChangeRecord
from hero.model.priors import OsmContext
from hero.model.refline import ReferenceLine
from hero.model.types import (
    CanonicalModel,
    FidelityMap,
    ModelDiff,
    Observation,
    ReferenceLineModel,
    RunReport,
)
from hero.model.scene_v2 import SceneV2


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
class CritiqueIteration:
    n: int
    accepted: bool
    critique: str
    ops: list[dict[str, Any]] = field(default_factory=list)
    applied: int = 0
    skipped: list[str] = field(default_factory=list)
    preview_path: str = ""
    preview_url: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"n": self.n, "preview_path": self.preview_path, "preview_url": self.preview_url,
                "accepted": self.accepted, "critique": self.critique, "patch_ops": self.ops,
                "applied": self.applied > 0, "applied_count": self.applied, "skipped": self.skipped,
                "detail": self.detail}


@dataclass
class FusionState:
    run: PipelineRunRecord
    segment: SegmentRecord
    mode: str = "generate"                                   # "generate" | "acquire"
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    plan_overrides: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    previous_segment_status: str = "draft"

    # ---- model + geometry
    head_version: int | None = None
    head_record: ModelVersionRecord | None = None
    fresh_lineage: bool = False                              # the head is a legacy v0: nothing is merged from it
    legacy_imported: bool = False
    base: CanonicalModel | None = None
    ref: ReferenceLine | None = None
    ref_model: ReferenceLineModel | None = None
    ref_source: str | None = None
    osm: OsmGeometry | None = None
    osm_error: str | None = None
    osm_context: OsmContext | None = None

    # ---- imagery
    plan: ImageryPlan | None = None
    planned: list[PlannedImage] = field(default_factory=list)
    plan_info: PlanInfo | None = None
    acquired: AcquireResult | None = None
    new_observations: list[Observation] = field(default_factory=list)      # first ingested AND paid by this run
    cached_observations: list[Observation] = field(default_factory=list)  # reused from the cache / earlier runs
    observations: dict[str, Observation] = field(default_factory=dict)
    unfused: list[Observation] = field(default_factory=list)
    ineligible: list[Observation] = field(default_factory=list)
    noop: bool = False

    # ---- perception
    replay_street: list[ExtractionRecord] = field(default_factory=list)
    replay_overhead: ExtractionRecord | None = None
    windows: list[OverheadWindow] = field(default_factory=list)
    street_batches: list[StreetBatch] = field(default_factory=list)
    est_calls_usd: float = 0.0
    budget_trimmed: int = 0
    overhead_result: OverheadExtraction | None = None
    street_results: list[tuple[StreetBatch, StreetExtraction]] = field(default_factory=list)
    calls_attempted: int = 0
    calls_succeeded: int = 0
    skipped_batches: list[str] = field(default_factory=list)
    extractions_saved: int = 0
    projected: list[ProjectedBatch] = field(default_factory=list)
    dropped_detections: int = 0
    range_disagreements: int = 0

    # ---- merge / critique / publish
    candidate: CanonicalModel | None = None
    merge_log: list[ChangeRecord] = field(default_factory=list)
    fidelity_before: FidelityMap | None = None
    fidelity_after: FidelityMap | None = None
    diff: ModelDiff | None = None
    critique_iterations: list[CritiqueIteration] = field(default_factory=list)
    published: CanonicalModel | None = None
    scene: SceneV2 | None = None
    version: int | None = None
    version_created: bool = False
    thumbnail_path: str | None = None
    report: RunReport | None = None

    # ---- bookkeeping
    steps: list[StepRecord] = field(default_factory=list)
    cost: CostLedger = field(default_factory=CostLedger)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    error: PipelineError | None = None

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def has_overhead(self) -> bool:
        return bool(self.windows)

    @property
    def has_street(self) -> bool:
        return bool(self.street_batches or self.replay_street)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def imagery_json(self) -> dict[str, Any]:
        used = [o.id for o in self.new_observations + self.cached_observations]
        return {
            "observation_ids": used,
            "new_observation_ids": [o.id for o in self.new_observations],
            "osm_path": self.osm.cache_path if self.osm else None,
            "osm_available": self.osm is not None,
            "osm_error": self.osm_error,
            "reference_line_source": self.ref_source,
            "planned": len(self.planned),
            "plan_trimmed": bool(self.plan_info.trimmed) if self.plan_info else False,
            "warnings": list(self.warnings),
        }
