"""README documents the Phase 2.1 operator flows (and does not document engine-specific import steps)."""

from __future__ import annotations

import re
from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()


def _section(title: str) -> str:
    m = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#{{2,3}} |\Z)", README, re.S | re.M)
    assert m, f"README section {title!r} missing"
    return m.group(1)


def test_add_to_existing_model_steps():
    body = _section("Add to an existing model")
    for needle in ("How to improve this model", "Fill gaps", "Add more imagery",
                   "Upload photos placed on the coverage map", "Regenerate with what we have ($0)", "Generate"):
        assert needle in body, needle
    assert "claude-sonnet-5-5" in body and "claude-opus-5-5" in body


def test_max_quality_paragraph():
    assert "**Max quality (admin).**" in README
    for needle in ("every ~12 m with four headings", "zoom 21", "three independent extraction passes",
                   "up to four review-and-fix rounds", "$1.74 with Opus 5.5", "$1.00 with Sonnet 5.5",
                   "HERO_CLAUDE_BUDGET_USD hard stop", "there is no per-run cap"):
        assert needle in README, needle


def test_recompile_and_game_commands():
    assert "uv run python -m hero.tools.recompile --all" in README
    assert "uv run python -m hero.tools.vendor_three" in README
    assert "frontend/vendor/" in README and "?seed=" in README and "run number" in README


def test_no_engine_specific_import_docs():
    assert not re.search(r"^#+ .*(Unity|Unreal|Godot)", README, re.M)
    for forbidden in ("glTFast", "Package Manager", "Interchange", "Download for Unity"):
        assert forbidden not in README, forbidden
    assert "no engine export UI" in README
    assert "street.xodr" in README


def test_portability_spec_is_short_and_keeps_references():
    spec = (Path(__file__).resolve().parents[2] / "spec" / "portability.md").read_text()
    assert len(spec.splitlines()) <= 130
    assert "SPAWNER_ALGORITHM.md" in spec and "[R8]" in spec
    for h in ("## 2. Frame", "## 3. `street.glb`", "## 4. `street.manifest.json`", "## 5. `gameplay.json`", "## 7. The deterministic spawner"):
        assert h in spec, h
