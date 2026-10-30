"""Confidence calibration and range weighting (spec/data.md -> "Evidence, confidence and state rules").

All tunable parameters live in ``PARAMS`` (one-file recalibration). Pure functions, no I/O.

> Assumed: parameter values, until labelled data exists.
"""

from __future__ import annotations

import math

PARAMS: dict[str, float] = {
    # calibrate_confidence: clip(A + B * raw**GAMMA, LO, HI)
    "cal_a": 0.05,
    "cal_b": 0.85,
    "cal_gamma": 1.3,
    "cal_lo": 0.05,
    "cal_hi": 0.9,
    # range weight: 1.0 up to R1, linear to W2 at R2, linear to W3 at R3, 0 beyond (not projected)
    "r1": 10.0,
    "r2": 20.0,
    "r3": 30.0,
    "w2": 0.6,
    "w3": 0.3,
    # position sigma = base + per_m * r (+ estimated_extra when the range was model-estimated)
    "sigma_base": 0.4,
    "sigma_per_m": 0.1,
    "sigma_estimated_extra": 1.0,
    # entity confidence = clip(sigmoid(logit), CONF_MIN, CONF_MAX); logit itself is clamped to +-LOGIT_CLAMP
    "conf_min": 0.02,
    "conf_max": 0.98,
    "logit_clamp": 5.0,
}

_EPS = 1e-6


def logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def calibrate_confidence(raw: float) -> float:
    """``clip(0.05 + 0.85 * raw**1.3, 0.05, 0.9)`` - a monotone shrink of the model's over-confident scores."""
    raw = min(max(raw, 0.0), 1.0)
    v = PARAMS["cal_a"] + PARAMS["cal_b"] * raw ** PARAMS["cal_gamma"]
    return min(max(v, PARAMS["cal_lo"]), PARAMS["cal_hi"])


def clamp_logit(x: float) -> float:
    c = PARAMS["logit_clamp"]
    return min(max(x, -c), c)


def confidence_from_logit(x: float) -> float:
    return min(max(sigmoid(x), PARAMS["conf_min"]), PARAMS["conf_max"])


def range_weight(range_m: float) -> float:
    """1.0 for r <= 10 m, linear to 0.6 at 20 m, to 0.3 at 30 m; 0.0 beyond 30 m (not projected)."""
    r = max(range_m, 0.0)
    p = PARAMS
    if r <= p["r1"]:
        return 1.0
    if r <= p["r2"]:
        return 1.0 + (p["w2"] - 1.0) * (r - p["r1"]) / (p["r2"] - p["r1"])
    if r <= p["r3"]:
        return p["w2"] + (p["w3"] - p["w2"]) * (r - p["r2"]) / (p["r3"] - p["r2"])
    return 0.0


def sigma_for_range(range_m: float, estimated: bool) -> float:
    """Position sigma ``0.4 + 0.1 * r`` m, +1.0 m when the range came from the model's own estimate."""
    p = PARAMS
    return p["sigma_base"] + p["sigma_per_m"] * max(range_m, 0.0) + (p["sigma_estimated_extra"] if estimated else 0.0)
