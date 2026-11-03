"""Critique step: one bounded reflection over a render of the candidate model vs. the imagery.

``run_critique`` makes ONE vision call (render + crops + <= 2 street frames) and returns the validated
``CritiqueResult``; applying ops is ``hero.model.merge.apply_critique`` (S1c) and looping is the orchestrator's job.
"""

from __future__ import annotations

import io
from typing import Sequence

from PIL import Image

from hero.fusion.schemas import MAX_CRITIQUE_OPS, CritiqueResult
from hero.llm.client import LLMClient, LLMImage, LLMUsage
from hero.model.types import CanonicalModel, EntityState
from hero.prompts import render_prompt

MAX_STREET_FRAMES = 2
MAX_CROPS = 3
MAX_SUMMARY_ENTITIES = 60


def image_to_llm(img: Image.Image, caption: str, *, media_type: str = "image/png") -> LLMImage:
    buf = io.BytesIO()
    fmt = "PNG" if media_type == "image/png" else "JPEG"
    img.convert("RGB").save(buf, format=fmt)
    return LLMImage(data=buf.getvalue(), media_type=media_type, caption=caption, width_px=img.width, height_px=img.height)


def render_caption(model: CanonicalModel) -> str:
    return (f"Image 1 | top-down render of model v{model.version} | travel toward increasing s | s labels every 10 m | "
            f"footpath on the {model.footpath_side} | markers: green >= 0.7, amber 0.4-0.7, red < 0.4 confidence")


def model_summary(model: CanonicalModel) -> str:
    """Compact text with ids: entities (ranked by confidence, capped) and the modelled-side footpath state runs."""
    ents = sorted((e for e in model.entities if e.state is not EntityState.removed), key=lambda e: (e.position.s, e.id))
    lines = [f"- {e.id}: {e.type.value}{f' ({e.subtype})' if e.subtype else ''} at s={e.position.s:.1f}, t={e.position.t:+.1f}, "
             f"conf {e.confidence:.2f}, {e.state.value}" for e in ents[:MAX_SUMMARY_ENTITIES]]
    if len(ents) > MAX_SUMMARY_ENTITIES:
        lines.append(f"- (+{len(ents) - MAX_SUMMARY_ENTITIES} more not listed)")
    if not lines:
        lines.append("- (no entities)")
    runs: list[list] = []
    for iv in model.profile:
        if runs and runs[-1][2] == iv.footpath_state.value and abs(runs[-1][1] - iv.s_from) < 1e-6:
            runs[-1][1] = iv.s_to
        else:
            runs.append([iv.s_from, iv.s_to, iv.footpath_state.value])
    lines.append(f"Footpath state on the {model.footpath_side} side by s:")
    lines += [f"- {a:.1f}-{b:.1f} m: {state}" for a, b, state in runs]
    return "\n".join(lines)


def critique_prompt(model: CanonicalModel) -> str:
    n = sum(1 for e in model.entities if e.state is not EntityState.removed)
    return render_prompt("critique_model", length_m=f"{model.length_s:.0f}", footpath_side=model.footpath_side,
                         n_entities=n, model_summary=model_summary(model), max_ops=MAX_CRITIQUE_OPS)


def run_critique(llm: LLMClient, *, run_id: str, model: CanonicalModel, render: Image.Image,
                 crops: Sequence[LLMImage] = (), street_frames: Sequence[LLMImage] = (), model_slug: str,
                 step: str = "critique_1", structured_mode: str | None = None) -> tuple[CritiqueResult, LLMUsage]:
    """One reflection call. Images are sent in the order render, crops (<= 3), street frames (<= 2)."""
    images = [image_to_llm(render, render_caption(model))]
    images += list(crops)[:MAX_CROPS] + list(street_frames)[:MAX_STREET_FRAMES]
    extra = {"structured_mode": structured_mode} if structured_mode else {}
    return llm.vision_json(run_id=run_id, step=step, images=images, prompt=critique_prompt(model),
                           schema=CritiqueResult, model=model_slug, prompt_name="critique_model", **extra)


def known_entity_ids(model: CanonicalModel) -> set[str]:
    return {e.id for e in model.entities}


def drop_unknown_entity_ops(model: CanonicalModel, result: CritiqueResult) -> tuple[CritiqueResult, list[str]]:
    """Pre-filter ops naming an entity id the model does not have; returns (cleaned result, skipped reasons).
    (``apply_critique`` also skips them; this keeps the stored critique honest.)"""
    ids = known_entity_ids(model)
    kept, skipped = [], []
    for op in result.ops:
        eid = getattr(op, "entity_id", None)
        if eid is not None and eid not in ids:
            skipped.append(f"{op.op}: unknown entity id {eid}")
        else:
            kept.append(op)
    return result.model_copy(update={"ops": kept}), skipped
