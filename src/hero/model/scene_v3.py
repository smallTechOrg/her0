"""Scene JSON v3 (r2) - the lightweight "web scene" (spec/data.md -> "Scene JSON v3 (r2)", ``schema_version: 3``).

Carries only what the UI *around* the game needs: segment info, walkability, About, generation info, a
count/summary of ``dynamic_context``, the overview satellite (results map) and ``assets`` (URLs + hashes of the
export bundle). **No geometry, lanes, colliders, tokens or spawn hints** - those live in the manifest
(``spec/portability.md`` section 4) and the glb. ``extra="forbid"`` everywhere. v1/v2 pieces are re-used.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hero.model.gap_types import Interpolation
from hero.model.scene_v2 import About, CrossSectionV2, GroundV2
from hero.scene.schema import Generation, SegmentInfo, Walkability

SCHEMA_VERSION = 3
COMPILED_BY = "hero.model.compile/2"
MAX_PAYLOAD_BYTES = 40_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class AboutV3(About):
    """v2 About + the gap/dynamic summaries."""

    interpolated: list[Interpolation] = Field(default_factory=list)
    gap_summary: str = ""
    dynamic_summary: str = ""


class DynamicTypeStats(_Strict):
    count: int = Field(ge=0)
    per_100m: float = Field(ge=0)
    subtypes: dict[str, int] = Field(default_factory=dict)
    lane_share: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mean_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DynamicContext(_Strict):
    excluded_count: int = Field(ge=0)
    entity_ids: list[str] = Field(default_factory=list)
    by_type: dict[str, DynamicTypeStats] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts(self) -> "DynamicContext":
        if len(self.entity_ids) != self.excluded_count:
            raise ValueError(f"excluded_count {self.excluded_count} != len(entity_ids) {len(self.entity_ids)}")
        return self


class ModelRefV3(_Strict):
    version: int = Field(ge=0)
    model_hash: str
    compiled_by: str = COMPILED_BY


class Assets(_Strict):
    mesh_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sha8: str = Field(pattern=r"^[0-9a-f]{8}$")
    glb_url: str
    manifest_url: str
    xodr_url: str
    gameplay_url: str = "/api/gameplay.json"
    glb_bytes: int = Field(ge=0)
    triangles: int = Field(ge=0)
    primitives: int = Field(ge=0)


class SceneV3(_Strict):
    schema_version: Literal[3]
    segment: SegmentInfo
    ground: GroundV2
    cross_section: CrossSectionV2
    walkability: Walkability
    about: AboutV3
    dynamic_context: DynamicContext
    generation: Generation
    model: ModelRefV3
    assets: Assets | None = None  # null until exported; a scene without assets is not served as v3

    @model_validator(mode="after")
    def _cross_field(self) -> "SceneV3":
        length = self.segment.length_m
        if abs(self.cross_section.footpath_coverage[-1].to_m - length) > 1e-3:
            raise ValueError(f"footpath_coverage must end at segment length_m ({length})")
        if self.about.model_version != self.model.version:
            raise ValueError("about.model_version must equal model.version")
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    def payload_bytes(self) -> int:
        return len(self.model_dump_json().encode("utf-8"))


def json_schema() -> dict:
    return SceneV3.model_json_schema()


def dumps_schema() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=False) + "\n"
