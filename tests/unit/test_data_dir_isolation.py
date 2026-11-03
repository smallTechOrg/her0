"""The test session must never point at the real ``data/`` dir."""

from __future__ import annotations

from hero.config import PROJECT_ROOT, get_settings


def test_data_dir_is_a_session_tmp_dir_not_the_projects():
    d = get_settings().data_dir_abs
    assert d != PROJECT_ROOT / "data" and PROJECT_ROOT not in d.parents
