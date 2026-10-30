"""Run-detail imagery view: ``streetview_available`` is null until verify_streetview has run."""

from __future__ import annotations

from hero.api.admin import _imagery


def test_streetview_available_is_null_before_the_check_ran():
    view = _imagery({"satellite": None, "streetview": []})
    assert view["streetview_available"] is None and view["streetview_status"] is None


def test_streetview_available_stays_false_once_checked_and_unavailable():
    view = _imagery({"satellite": None, "streetview": [], "streetview_available": False, "streetview_status": "ZERO_RESULTS"})
    assert view["streetview_available"] is False and view["streetview_status"] == "ZERO_RESULTS"


def test_streetview_available_true_when_checked_ok():
    assert _imagery({"streetview_available": True})["streetview_available"] is True
