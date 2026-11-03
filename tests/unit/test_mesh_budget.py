"""Budgets of spec/portability.md section 3 (100 m fixtures; x2 for 200 m except primitives)."""

from __future__ import annotations

import pytest

from hero.export.glb import write_glb
from hero.export.mesh_types import (
    MAX_GLB_BYTES, MAX_GLB_BYTES_200M, MAX_MATERIALS, MAX_PRIMITIVES, MAX_TEXTURES, MAX_TRIANGLES,
)

import mesh_support as ms

PATHS = [str(p) for p in ms.plan_paths()]


def _check(m, glb_limit, tri_limit):
    assert len(m.nodes) <= MAX_PRIMITIVES, [n.name for n in m.nodes]
    assert m.triangles <= tri_limit, m.stats()
    assert len({n.material for n in m.nodes}) <= MAX_MATERIALS
    assert len(m.materials) <= MAX_MATERIALS
    assert len(m.textures) <= MAX_TEXTURES
    data = write_glb(m)
    assert len(data) <= glb_limit, len(data)
    st = m.stats()
    assert st["primitives"] == len(m.nodes) and st["triangles"] == m.triangles


@pytest.mark.parametrize("path", PATHS, ids=ms.plan_ids())
def test_fixture_budgets(path):
    plan = ms.load_plan(path)
    big = plan.length_m > 110
    _check(ms.build(path), MAX_GLB_BYTES_200M if big else MAX_GLB_BYTES, MAX_TRIANGLES * (2 if big else 1))


@pytest.mark.parametrize("side", ["left", "right"])
def test_synthetic_100m_budget(side):
    _check(ms.build_plan(ms.synthetic_plan(100.0, side)), MAX_GLB_BYTES, MAX_TRIANGLES)


def test_synthetic_200m_budget():
    _check(ms.build_plan(ms.synthetic_plan(200.0)), MAX_GLB_BYTES_200M, MAX_TRIANGLES * 2)


def test_triangle_count_scales_linearly_not_quadratically():
    a = ms.build_plan(ms.synthetic_plan(100.0)).triangles
    b = ms.build_plan(ms.synthetic_plan(200.0)).triangles
    assert b < a * 2.4
