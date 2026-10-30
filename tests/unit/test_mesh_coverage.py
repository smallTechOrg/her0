"""No gaps: frontage contiguous in s (no gap > 1 mm per 5 m bin and side); ground tiles t wall-to-wall at every s."""

from __future__ import annotations

import pytest

from hero.export.frame import Frame
from hero.export.mesh_types import GROUND_NODES

import mesh_support as ms

PATHS = [str(p) for p in ms.plan_paths()]
IDS = ms.plan_ids()
GROUND = list(GROUND_NODES)


def _plans():
    for path in PATHS:
        yield ms.load_plan(path), ms.build(path)
    for side in ("left", "right"):
        p = ms.synthetic_plan(100.0, side)
        yield p, ms.build_plan(p)


def _cases():
    return [pytest.param(p, m, id=f"{p.slug}-{p.footpath_side}") for p, m in _plans()]


def _frontage_intervals(m, frame):
    out = {}
    for node in ("Frontage_L", "Frontage_R"):
        iv = []
        for tri in ms.tri_st(m, node, frame):
            lo, hi = min(p[0] for p in tri), max(p[0] for p in tri)
            if hi - lo > 1e-9:
                iv.append((lo, hi))
        out[node] = iv
    return out


@pytest.mark.parametrize("plan,mesh", _cases())
def test_mesh_coverage_frontage_every_5m_bin_both_sides(plan, mesh):
    frame = Frame.from_plan(plan)
    L = plan.length_m
    for node, iv in _frontage_intervals(mesh, frame).items():
        assert iv, node
        merged = ms.union(iv, tol=1e-3)  # gaps <= 1 mm are not gaps
        assert len(merged) == 1, (node, merged)
        for k in range(int(L // 5) + (1 if L % 5 else 0)):
            a, b = k * 5.0, min(L, (k + 1) * 5.0)
            assert merged[0][0] <= a + 1e-3 and merged[0][1] >= b - 1e-3, (node, k)


def _wall_t(mesh, frame, node, s):
    """Foot of the wall of ``node`` at ``s`` read from the frontage triangles themselves."""
    ts = []
    for tri in ms.tri_st(mesh, node, frame):
        iv = ms.t_interval_at(tri, s)
        if iv and not (max(p[0] for p in tri) - min(p[0] for p in tri) < 1e-9):
            ts += list(iv)
    return (min(ts) + max(ts)) / 2 if ts and max(ts) - min(ts) < 2e-3 else (min(ts), max(ts))


def _sample_s(plan):
    L = plan.length_m
    step = plan.corridor_step_m
    mids = [i * step + step / 2 for i in range(int(L // step)) if i * step + step / 2 < L]
    extra = [0.37 + 1.0 * k for k in range(int(L))]
    return sorted(set([round(s, 6) for s in mids + extra if 0.0 < s < L]))


@pytest.mark.parametrize("plan,mesh", _cases())
def test_mesh_coverage_ground_tiles_wall_to_wall(plan, mesh):
    frame = Frame.from_plan(plan)
    sg = 1.0 if plan.footpath_side == "left" else -1.0
    assert plan.footpath_side in ("left", "right")
    checked = 0
    for s in _sample_s(plan):
        left_wall = _wall_t(mesh, frame, "Frontage_L", s)
        right_wall = _wall_t(mesh, frame, "Frontage_R", s)
        if isinstance(left_wall, tuple) or isinstance(right_wall, tuple):
            continue  # sample landed exactly on a perpendicular closing face
        iv = []
        for name in GROUND:
            for tri in ms.tri_st(mesh, name, frame):
                r = ms.t_interval_at(tri, s)
                if r and r[1] - r[0] > 1e-9:
                    iv.append(r)
        merged = ms.union(iv, tol=1e-3)
        assert len(merged) == 1, (s, merged)
        assert merged[0][0] <= right_wall + 1.5e-3 and merged[0][1] >= left_wall - 1.5e-3, (s, merged, right_wall, left_wall)
        # and it does not poke through the wall by more than a mm
        assert merged[0][0] >= right_wall - 2e-3 and merged[0][1] <= left_wall + 2e-3, (s, merged, right_wall, left_wall)
        checked += 1
    assert checked >= len(_sample_s(plan)) * 0.8, "too many samples skipped"
    del sg


@pytest.mark.parametrize("plan,mesh", _cases())
def test_ground_walls_match_plan_walls_outside_the_seam(plan, mesh):
    frame = Frame.from_plan(plan)
    sides = {sd.side: sd for sd in plan.frontage.sides}
    sign = 1.0 if plan.footpath_side == "left" else -1.0
    L = plan.length_m
    checked = 0
    for s in _sample_s(plan):
        if s > L - 6.5:
            continue
        for which, node in (("modelled", "Frontage_L" if plan.footpath_side == "left" else "Frontage_R"),
                            ("opposite", "Frontage_R" if plan.footpath_side == "left" else "Frontage_L")):
            seg = next(g for g in sides[which].segments if g.s_from < s < g.s_to)
            u = (s - seg.s_from) / (seg.s_to - seg.s_from)
            expect = sign * (seg.lat_from + (seg.lat_to - seg.lat_from) * u)
            got = _wall_t(mesh, frame, node, s)
            if isinstance(got, tuple):
                continue
            # effective wall may only move AWAY from the street (clamp), never toward it; 1 cm = chord-vs-arc sagitta
            assert abs(got) >= abs(expect) - 1e-2, (s, which, got, expect)
            checked += 1
    assert checked > 20
