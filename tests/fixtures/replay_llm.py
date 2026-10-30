"""ReplayLLM: a zero-spend ``LLMClient`` for tests. Returns canned perception outputs and records the calls.

Branches on the node tag in the prompt's first line (``<node:overhead>``, ``<node:streetlevel>``,
``<node:critique>``), never on prose. Responses come from ``tests/fixtures/extractions/*.json`` unless overridden:

* overhead  - ``overhead_curved.json`` (2 windows, cycled/truncated to the number of images sent);
* street    - built per call from ``street_template.json``: each image gets the frame + detections + facades of its
              ``view`` (parsed from the caption), with ``image_index`` set to its caption position and unique
              ``local_id``s (``d1``, ``d2`` ... per call - ids deliberately repeat ACROSS calls);
* critique  - ``critique_accept.json``.

Override per node with ``overhead=`` / ``street=`` / ``critique=``: a dict / pydantic object (always returned), or a
list consumed one entry per call (exhausted -> ``AssertionError``). An entry may also be an ``Exception`` (raised),
or a callable ``(images, prompt) -> dict`` . A dict that does not validate raises ``SchemaValidationError``
(the real client re-asks once first).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ValidationError

from hero.llm.client import LLMImage, LLMUsage
from hero.llm.errors import SchemaValidationError

FIXTURE_DIR = Path(__file__).resolve().parent / "extractions"
NODE_TAG = re.compile(r"^\s*<node:([a-z_]+)>", re.MULTILINE)
VIEW_IN_CAPTION = re.compile(r"view:\s*([a-z]+)")
TOKENS_PER_IMAGE = 1600


@dataclass
class RecordedCall:
    node: str
    run_id: str
    step: str
    model: str
    prompt_name: str
    prompt: str
    captions: list[str]
    n_images: int
    schema_name: str
    structured_mode: str | None
    response: dict | None = None


def load_extraction(name: str, directory: Path = FIXTURE_DIR) -> dict:
    return json.loads((Path(directory) / (name if name.endswith(".json") else f"{name}.json")).read_text())


class ReplayLLM:
    def __init__(self, *, overhead: Any = None, street: Any = None, critique: Any = None,
                 usage_cost_usd: float = 0.0, latency_ms: int = 5, extractions_dir: Path = FIXTURE_DIR) -> None:
        self._dir = Path(extractions_dir)
        self._src = {"overhead": overhead, "streetlevel": street, "critique": critique}
        self._cursor = {"overhead": 0, "streetlevel": 0, "critique": 0}
        self.usage_cost_usd = usage_cost_usd
        self.latency_ms = latency_ms
        self.calls: list[RecordedCall] = []

    # ---------------------------------------------------------------- introspection

    def calls_for(self, node: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.node == node]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    # ---------------------------------------------------------------- LLMClient protocol

    def vision_json(self, *, run_id: str, step: str, images: Sequence[LLMImage], prompt: str, schema: type[BaseModel],
                    model: str, prompt_name: str, system: str | None = None, max_tokens: int = 4096,
                    structured_mode: str | None = None, **_: Any) -> tuple[BaseModel, LLMUsage]:
        match = NODE_TAG.search(prompt)
        if match is None or match.group(1) not in self._src:
            raise ValueError(f"ReplayLLM: prompt has no known <node:...> tag (prompt_name={prompt_name!r})")
        node = match.group(1)
        rec = RecordedCall(node=node, run_id=run_id, step=step, model=model, prompt_name=prompt_name, prompt=prompt,
                           captions=[i.caption for i in images], n_images=len(images), schema_name=schema.__name__,
                           structured_mode=structured_mode)
        self.calls.append(rec)  # recorded even when the canned answer is an error
        raw = self._next(node, images, prompt)
        if isinstance(raw, BaseException):
            raise raw
        data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
        rec.response = data
        try:
            obj = schema.model_validate(data)
        except ValidationError as exc:
            raise SchemaValidationError(f"ReplayLLM canned {node} answer failed {schema.__name__}: {exc}", raw=data) from exc
        tokens_in = TOKENS_PER_IMAGE * len(images) + len(prompt) // 4
        usage = LLMUsage(model=model, input_tokens=tokens_in, output_tokens=len(json.dumps(data)) // 4,
                         cost_usd=self.usage_cost_usd, latency_ms=self.latency_ms, calls=1)
        return obj, usage

    # ---------------------------------------------------------------- canned answers

    def _next(self, node: str, images: Sequence[LLMImage], prompt: str) -> Any:
        src = self._src[node]
        if src is None:
            return self._default(node, images)
        if isinstance(src, list):
            i = self._cursor[node]
            if i >= len(src):
                raise AssertionError(f"ReplayLLM: {node} call #{i + 1} but only {len(src)} canned responses")
            self._cursor[node] = i + 1
            src = src[i]
        if callable(src) and not isinstance(src, (BaseModel, BaseException)):
            return src(images, prompt)
        return copy.deepcopy(src)

    def _default(self, node: str, images: Sequence[LLMImage]) -> dict:
        if node == "overhead":
            tpl = load_extraction("overhead_curved", self._dir)["windows"]
            wins = []
            for i in range(1, len(images) + 1):
                w = copy.deepcopy(tpl[(i - 1) % len(tpl)])
                w["image_index"] = i
                wins.append(w)
            return {"windows": wins}
        if node == "critique":
            return load_extraction("critique_accept", self._dir)
        return self._street_from_template(images)

    def _street_from_template(self, images: Sequence[LLMImage]) -> dict:
        path = self._dir / "street_template.json"
        by_view = json.loads(path.read_text())["by_view"]
        frames: list[dict] = []
        dets: list[dict] = []
        facades: list[dict] = []
        n = 0
        for idx, img in enumerate(images, 1):
            m = VIEW_IN_CAPTION.search(img.caption)
            tpl = by_view.get(m.group(1) if m else "forward") or by_view["forward"]
            frames.append({"image_index": idx, **copy.deepcopy(tpl["frame"])})
            for d in tpl["detections"]:
                n += 1
                dets.append({**copy.deepcopy(d), "local_id": f"d{n}", "image_index": idx})
            facades += [{**copy.deepcopy(f), "image_index": idx} for f in tpl["facades"]]
        return {"frames": frames, "detections": dets, "facades": facades, "notes": "ReplayLLM canned street-level answer."}
