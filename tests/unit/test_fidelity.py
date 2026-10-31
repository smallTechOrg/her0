"""hero.model.fidelity: bins, L0-L3 rules, monotonicity."""

from __future__ import annotations

import math

import pytest

from hero.model.fidelity import BIN_M, compute_fidelity, count_stations, level_hint
from hero.model.types import ObservationKind, ViewKind
from tests.fixtures.street_fixtures import make_model, make_observation, straight_reference_line


def build(length=100.0, specs=()):
    ref = straight_reference_line(length)
    obs = {}
    for i, (s, view, kind) in enumerate(specs):
        lat, lon = ref.point_at(s, 0.0)
        o = make_observation(obs_id=f"o{i:03d}", kind=kind, station_s=s, view=view, lat=lat, lon=lon)
        obs[o.id] = o
    model = make_model(ref, fused_observation_ids=sorted(obs))
    return model, obs


SV = ObservationKind.streetview
UP = ObservationKind.user_photo
F, B, L, R = ViewKind.forward, ViewKind.back, ViewKind.left, ViewKind.right


def levels(fm):
    return [b.level for b in fm.bins]


@pytest.mark.parametrize("length,expected", [(100.0, 20), (100.2, 21), (97.0, 20), (5.0, 1), (4.0, 1), (12.6, 3)])
def test_bin_count_is_ceil_length_over_five(length, expected):
    model, obs = build(length)
    fm = compute_fidelity(model, obs)
    assert len(fm.bins) == expected == math.ceil(length / BIN_M)
    assert fm.bins[0].s_from == 0.0 and fm.bins[-1].s_to == pytest.approx(length)
    assert all(a.s_to == pytest.approx(b.s_from) for a, b in zip(fm.bins, fm.bins[1:]))


def test_no_observations_is_all_l0():
    model, obs = build(100.0)
    fm = compute_fidelity(model, obs)
    assert set(levels(fm)) == {0} and fm.overall.level == 0 and fm.overall.score == 0.0
    assert fm.overall.histogram_m["L0"] == pytest.approx(100.0)
    assert "forward or back" in fm.bins[0].hint


def test_one_forward_frame_gives_l1_over_its_cell_only():
    model, obs = build(100.0, [(10.0, F, SV)])
    fm = compute_fidelity(model, obs)
    assert levels(fm)[:5] == [0, 0, 1, 1, 1] and set(levels(fm)[5:]) == {0}
    assert fm.bins[2].views == 1 and fm.bins[2].stations == 1
    assert fm.overall.histogram_m["L1"] == pytest.approx(15.0)
    assert fm.overall.score == pytest.approx(0.15) and fm.overall.level == 0


def test_cell_must_overlap_a_bin_by_2_5_m():
    # forward from 12.6 covers 12.6-27.6: bin 10-15 overlaps by 2.4 (not covered), bin 25-30 by 2.6 (covered)
    model, obs = build(100.0, [(12.6, F, SV)])
    lv = levels(compute_fidelity(model, obs))
    assert lv[2] == 0 and lv[3] == 1 and lv[4] == 1 and lv[5] == 1 and lv[6] == 0


def test_short_final_bin_is_covered_whole_or_not_at_all():
    model, obs = build(100.2, [(80.0, F, SV)])  # covers 80-95: the 0.2 m tail bin is untouched
    fm = compute_fidelity(model, obs)
    assert fm.bins[-1].s_to == pytest.approx(100.2) and fm.bins[-1].level == 0
    model, obs = build(100.2, [(97.0, F, SV)])
    # cell clips at 100.2; the 0.2 m bin overlaps 0.2 m == the whole bin
    assert compute_fidelity(model, obs).bins[-1].level == 1


def test_l2_needs_two_stations_and_both_direct_sides():
    # left at 20 and right at 28: stations 8 m apart, both sides -> L2 where both cover a bin
    model, obs = build(100.0, [(20.0, L, SV), (28.0, R, SV)])
    fm = compute_fidelity(model, obs)
    assert fm.bins[4].level == 2 and fm.bins[4].sides == {"left": 1, "right": 1} and fm.bins[4].stations == 2
    assert fm.bins[3].level == 1  # 15-20: only the left cell (14-26) reaches it
    assert fm.bins[3].hint.startswith("Add a Street View frame facing left or right")


def test_two_close_stations_stay_l1():
    model, obs = build(100.0, [(20.0, L, SV), (24.0, R, SV)])  # 4 m apart < 6 m
    fm = compute_fidelity(model, obs)
    assert fm.bins[4].level == 1 and fm.bins[4].stations == 1
    assert "second viewpoint" in fm.bins[4].hint


def test_station_gap_boundary_is_six_metres():
    model, obs = build(100.0, [(20.0, L, SV), (26.0, R, SV)])
    assert compute_fidelity(model, obs).bins[4].level == 2


def test_two_stations_without_both_sides_stay_l1():
    model, obs = build(100.0, [(20.0, F, SV), (30.0, F, SV), (22.0, L, SV)])
    fm = compute_fidelity(model, obs)
    assert max(levels(fm)) == 1
    assert "right-facing" in fm.bins[4].hint


def test_forward_and_back_only_never_reach_l2():
    specs = [(s, F, SV) for s in range(0, 100, 10)] + [(s, B, SV) for s in range(10, 101, 10)]
    fm = compute_fidelity(*build(100.0, specs))
    assert set(levels(fm)) == {1} and fm.overall.level == 1 and fm.overall.score == 1.0


def test_l3_needs_a_user_closeup():
    base = [(20.0, L, SV), (28.0, R, SV)]
    assert compute_fidelity(*build(100.0, base)).bins[4].level == 2
    model, obs = build(100.0, base + [(22.0, F, UP)])
    fm = compute_fidelity(model, obs)
    assert fm.bins[4].level == 3 and fm.overall.level == 0
    model, obs = build(100.0, base + [(22.0, F, SV)])  # a plain Street View frame is not a close-up
    assert compute_fidelity(model, obs).bins[4].level == 2


def test_hires_overhead_is_reported_but_does_not_raise_the_level():
    ref = straight_reference_line(100.0)
    lat, lon = ref.point_at(50.0, 0.0)
    crop = make_observation(obs_id="c1", kind=ObservationKind.satellite_crop, zoom=20, station_s=None, view=None,
                            lat=lat, lon=lon)
    model = make_model(ref, fused_observation_ids=["c1"])
    fm = compute_fidelity(model, {"c1": crop})
    assert set(levels(fm)) == {0} and all(b.overhead_hires for b in fm.bins[1:-1])


def test_only_fused_observations_count_and_unknown_ids_are_ignored():
    model, obs = build(100.0, [(10.0, F, SV)])
    unfused = model.model_copy(update={"fused_observation_ids": []})
    assert set(levels(compute_fidelity(unfused, obs))) == {0}
    ghost = model.model_copy(update={"fused_observation_ids": ["o000", "nope"]})
    assert levels(compute_fidelity(ghost, obs)) == levels(compute_fidelity(model, obs))


def test_overall_score_is_length_weighted_and_level_is_its_floor():
    specs = [(s, v, SV) for s in range(0, 100, 8) for v in (F, L, R)]
    fm = compute_fidelity(*build(100.0, specs))
    h = fm.overall.histogram_m
    assert sum(h.values()) == pytest.approx(100.0)
    assert fm.overall.score == pytest.approx(round((h["L1"] + 2 * h["L2"] + 3 * h["L3"]) / 100.0, 2))
    assert fm.overall.level == math.floor(fm.overall.score + 1e-9)


def test_monotone_adding_evidence_never_lowers_a_level():
    specs = [(s, v, SV) for s in range(0, 101, 7) for v in (F, B, L, R)]
    specs += [(31.0, F, UP), (64.0, L, UP)]
    # deterministic shuffle: stride permutation
    order = [specs[(i * 11) % len(specs)] for i in range(len(specs))]
    assert sorted(order) == sorted(specs)
    prev = None
    for k in range(len(order) + 1):
        model, obs = build(100.0, order[:k])
        fm = compute_fidelity(model, obs)
        if prev is not None:
            assert all(b >= a for a, b in zip(levels(prev), levels(fm)))
            assert fm.overall.score >= prev.overall.score
        prev = fm


def test_order_of_ids_does_not_matter():
    specs = [(10.0, F, SV), (20.0, L, SV), (27.0, R, SV)]
    model, obs = build(100.0, specs)
    rev = model.model_copy(update={"fused_observation_ids": list(reversed(model.fused_observation_ids))})
    assert compute_fidelity(model, obs) == compute_fidelity(rev, obs)


def test_custom_bin_and_validation():
    model, obs = build(100.0)
    assert len(compute_fidelity(model, obs, bin_m=10.0).bins) == 10
    with pytest.raises(ValueError):
        compute_fidelity(model, obs, bin_m=0)


def test_count_stations_is_greedy_with_a_six_metre_gap():
    assert count_stations([]) == 0 and count_stations([5.0]) == 1
    assert count_stations([0, 3, 6, 9, 12]) == 3
    assert count_stations([0, 5.9]) == 1 and count_stations([0, 6.0]) == 2


def test_hints_are_deterministic_and_empty_at_l3():
    assert level_hint(3, stations=2, left=1, right=1) == ""
    assert "close-up" in level_hint(2, stations=2, left=1, right=1)
    assert level_hint(1, stations=1, left=0, right=1) == level_hint(1, stations=1, left=0, right=1)
