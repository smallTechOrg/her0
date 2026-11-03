"""The ``live`` gate in tests/conftest.py: live tests are skipped unless explicitly requested."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hero.config import PROJECT_ROOT
from tests.conftest import live_requested


def cfg(markexpr="", args=(), cwd=PROJECT_ROOT):
    return SimpleNamespace(getoption=lambda name, default=None: markexpr,
                           invocation_params=SimpleNamespace(args=list(args), dir=Path(cwd)))


@pytest.mark.parametrize("markexpr,args,expected", [
    ("", [], False),
    ("", ["tests"], False),
    ("", ["tests/unit", "tests/integration", "-q"], False),
    ("", ["tests/e2e/test_play_live.py"], False),
    ("not live", ["tests"], False),
    ("live", ["tests"], True),
    ("live and not slow", [], True),
    ("", ["tests/live", "-q", "-s"], True),
    ("", ["tests/live/test_phase2_ui.py::test_x"], True),
    ("", [str(PROJECT_ROOT / "tests" / "live")], True),
])
def test_live_requested(markexpr, args, expected):
    assert live_requested(cfg(markexpr, args)) is expected


def test_live_marked_items_in_this_session_are_skipped_by_default(request):
    live = [i for i in request.session.items if i.get_closest_marker("live")]
    if live_requested(request.config):
        pytest.skip("this session explicitly requested live tests")
    assert all(i.get_closest_marker("skip") for i in live)
