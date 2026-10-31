"""Model top-down render + critique step (ReplayLLM)."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageChops

from hero.fusion.critique import (
    critique_prompt,
    drop_unknown_entity_ops,
    image_to_llm,
    model_summary,
    run_critique,
)
from hero.fusion.render import render_model_topdown
from hero.fusion.schemas import CritiqueResult
from hero.llm.client import LLMImage
from hero.model.types import EntityState, EntityType, FootpathState
from hero.scene.geo import TileGeoref
from tests.fixtures.replay_llm import ReplayLLM, load_extraction
from tests.fixtures.street_fixtures import (
    curved_reference_line,
    make_entity,
    make_model,
    straight_reference_line,
)


def _model(**kw):
    ents = kw.pop("entities", [make_entity(entity_id="ent-0000000001", type=EntityType.pole, s=10.0, t=5.5),
                               make_entity(entity_id="ent-0000000002", type=EntityType.tree, s=60.0, t=6.0, confidence=0.3)])
    return make_model(entities=ents, states=[FootpathState.intact, FootpathState.missing], **kw)


def _same(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


# ---------------------------------------------------------------- render

def test_plain_render_is_an_rgb_image_of_sensible_size_and_deterministic():
    m = _model()
    a, b = render_model_topdown(m), render_model_topdown(m)
    assert a.mode == "RGB" and 64 <= min(a.size) and max(a.size) <= 1280
    assert _same(a, b)


def test_render_changes_when_the_model_changes_and_ignores_removed_entities():
    base = render_model_topdown(_model(entities=[]))
    with_ent = render_model_topdown(_model())
    assert not _same(base, with_ent)
    removed = [make_entity(entity_id="ent-0000000001", state=EntityState.removed)]
    marker_diff = ImageChops.difference(base, render_model_topdown(_model(entities=removed))).getbbox()
    assert marker_diff is None


def test_footpath_state_colours_the_render():
    intact = make_model(states=[FootpathState.intact])
    broken = make_model(states=[FootpathState.broken])
    assert not _same(render_model_topdown(intact), render_model_topdown(broken))


def test_render_draws_over_a_satellite_tile_and_crops_to_the_corridor():
    ref = straight_reference_line(100.0)
    lat, lon = ref.point_at(50.0, 0.0)
    georef = TileGeoref(center_lat=lat, center_lon=lon, zoom=19, scale=2, size_px=(1280, 1280))
    tile = Image.new("RGB", (1280, 1280), (90, 80, 70))
    out = render_model_topdown(make_model(ref), tile=tile, georef=georef)
    assert out.mode == "RGB" and out.size[0] < 1280 and out.size[1] <= 1280
    assert out.getpixel((out.width // 2, out.height // 2)) != (90, 80, 70)
    assert out.size[1] > out.size[0], "a north-south street is taller than wide"


def test_curved_street_render_uses_a_square_ish_canvas():
    img = render_model_topdown(make_model(curved_reference_line()))
    assert 0.4 < img.width / img.height < 2.5


def test_labels_can_be_turned_off():
    m = _model()
    assert not _same(render_model_topdown(m), render_model_topdown(m, labels=False))


# ---------------------------------------------------------------- critique

def test_model_summary_lists_ids_types_positions_and_footpath_runs():
    text = model_summary(_model())
    assert "ent-0000000001: pole at s=10.0, t=+5.5" in text
    assert "conf 0.30" in text
    assert "Footpath state on the left side by s" in text and "intact" in text and "missing" in text


def test_model_summary_handles_no_entities_and_caps_long_lists():
    assert "(no entities)" in model_summary(make_model())
    many = [make_entity(entity_id=f"ent-{i:010x}", s=float(i % 90), t=5.5) for i in range(80)]
    text = model_summary(make_model(entities=many))
    assert "(+20 more not listed)" in text


def test_critique_prompt_has_tag_limits_and_every_placeholder_filled():
    p = critique_prompt(_model())
    assert p.startswith("<node:critique>") and "{{" not in p
    assert "at most 8 ops" in p and "2 entities" in p and "left side" in p


def test_run_critique_sends_one_call_with_render_first_and_capped_extras():
    llm = ReplayLLM()
    m = _model()
    extra = [LLMImage(b"x", "image/jpeg", f"c{i}", 10, 10) for i in range(5)]
    res, usage = run_critique(llm, run_id="run-1", model=m, render=render_model_topdown(m), crops=extra[:4],
                              street_frames=extra[:3], model_slug="claude-x", structured_mode="json_text")
    assert isinstance(res, CritiqueResult) and res.accepted and res.ops == []
    assert llm.call_count == 1
    rec = llm.calls[0]
    assert rec.node == "critique" and rec.step == "critique_1" and rec.model == "claude-x"
    assert rec.n_images == 1 + 3 + 2 and rec.captions[0].startswith("Image 1 | top-down render of model v0")
    assert rec.structured_mode == "json_text" and rec.prompt_name == "critique_model" and usage.calls == 1


def test_run_critique_returns_ops_and_unknown_entity_ops_are_filtered():
    m = _model()
    llm = ReplayLLM(critique=load_extraction("critique_ops"))
    res, _ = run_critique(llm, run_id="r", model=m, render=render_model_topdown(m), model_slug="x")
    assert not res.accepted and [o.op for o in res.ops] == ["set_footpath_state", "nudge_entity"]
    cleaned, skipped = drop_unknown_entity_ops(m, res)       # ent-0000000001 exists in _model
    assert len(cleaned.ops) == 2 and skipped == []
    ghost = res.model_copy(update={"ops": res.ops + [type(res.ops[1])(entity_id="ent-ffff", ds_m=1, dt_m=0)]})
    cleaned, skipped = drop_unknown_entity_ops(m, ghost)
    assert len(cleaned.ops) == 2 and "ent-ffff" in skipped[0]


def test_run_critique_does_not_pass_structured_mode_when_unset_and_propagates_failures():
    class Strict:
        def vision_json(self, *, run_id, step, images, prompt, schema, model, prompt_name, system=None, max_tokens=4096):
            raise RuntimeError("upstream down")
    m = _model()
    with pytest.raises(RuntimeError, match="upstream down"):
        run_critique(Strict(), run_id="r", model=m, render=render_model_topdown(m), model_slug="x")


def test_image_to_llm_round_trips_png_and_jpeg():
    img = Image.new("RGB", (30, 20), (1, 2, 3))
    p = image_to_llm(img, "cap")
    assert p.media_type == "image/png" and (p.width_px, p.height_px) == (30, 20) and p.caption == "cap"
    assert Image.open(io.BytesIO(p.data)).size == (30, 20)
    assert image_to_llm(img, "c", media_type="image/jpeg").data[:2] == b"\xff\xd8"
