"""hero.model.calibrate: calibration curve, logit/sigmoid, range weight, sigma."""

from __future__ import annotations

import math

import pytest

from hero.model.calibrate import (
    PARAMS,
    calibrate_confidence,
    confidence_from_logit,
    logit,
    range_weight,
    sigma_for_range,
    sigmoid,
)


def test_calibrate_known_values():
    assert calibrate_confidence(0.0) == pytest.approx(0.05)
    assert calibrate_confidence(1.0) == pytest.approx(0.9)  # 0.05 + 0.85 = 0.9, clipped at the cap
    assert calibrate_confidence(0.5) == pytest.approx(0.05 + 0.85 * 0.5 ** 1.3)


def test_calibrate_is_monotone_bounded_and_shrinks():
    xs = [i / 100 for i in range(101)]
    ys = [calibrate_confidence(x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:]))
    assert all(0.05 <= y <= 0.9 for y in ys)
    assert all(y <= x + 0.05 + 1e-9 for x, y in zip(xs, ys))  # never more confident than the model said (+ floor)


def test_calibrate_clamps_out_of_range_input():
    assert calibrate_confidence(-3) == calibrate_confidence(0)
    assert calibrate_confidence(7) == calibrate_confidence(1)


def test_logit_sigmoid_roundtrip_and_extremes():
    for p in (0.02, 0.1, 0.5, 0.9, 0.98):
        assert sigmoid(logit(p)) == pytest.approx(p)
    assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))
    assert sigmoid(-1000) == 0.0 or sigmoid(-1000) < 1e-300
    assert sigmoid(1000) == 1.0


def test_confidence_from_logit_clip():
    assert confidence_from_logit(50) == PARAMS["conf_max"]
    assert confidence_from_logit(-50) == PARAMS["conf_min"]
    assert confidence_from_logit(0) == pytest.approx(0.5)


@pytest.mark.parametrize("r,w", [(0, 1.0), (10, 1.0), (15, 0.8), (20, 0.6), (25, 0.45), (30, 0.3), (30.1, 0.0), (80, 0.0)])
def test_range_weight_piecewise(r, w):
    assert range_weight(r) == pytest.approx(w)


def test_range_weight_monotone_non_increasing():
    vals = [range_weight(r / 2) for r in range(0, 80)]
    assert all(b <= a + 1e-12 for a, b in zip(vals, vals[1:]))


def test_sigma_for_range():
    assert sigma_for_range(10, False) == pytest.approx(1.4)
    assert sigma_for_range(10, True) == pytest.approx(2.4)
    assert sigma_for_range(-5, False) == pytest.approx(0.4)
