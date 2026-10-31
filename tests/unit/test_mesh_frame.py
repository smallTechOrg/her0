"""spec/portability.md section 2: the street frame, and the triangle builder's contract."""

from __future__ import annotations

import math

import pytest

from hero.export.frame import Builder, Frame, dot, cross, sub

import mesh_support as ms


def test_straight_north_frame_matches_spec():
    f = Frame([0.0, 2.5, 5.0], [0.0, 0.0, 0.0])
    assert f.world(0.0, 0.0) == (0.0, 0.0, 0.0)
    # travel is toward -Z; a LEFT offset (t > 0) is at negative X (+X is the right-hand side)
    x, y, z = f.world(5.0, 2.0, 0.3)
    assert (round(x, 9), y, round(z, 9)) == (-2.0, 0.3, -5.0)
    assert f.world(0.0, -1.5)[0] == pytest.approx(1.5)


def test_turning_right_integrates_midpoint_heading():
    # bearing grows clockwise -> phi grows; forward f = (sin phi, -cos phi)
    f = Frame([0.0, 2.5, 5.0], [0.0, 10.0, 20.0])
    phi1, phi2 = math.radians(10), math.radians(20)
    mid = 0.5 * (phi1 + phi2)
    assert f.px[2] == pytest.approx(math.sin(0.5 * phi1) * 2.5 + math.sin(mid) * 2.5)
    assert f.pz[2] == pytest.approx(-math.cos(0.5 * phi1) * 2.5 - math.cos(mid) * 2.5)
    assert f.end_pose()["yaw_rad"] == pytest.approx(phi2, abs=1e-6)


def test_wrap_across_north():
    f = Frame([0.0, 2.5, 5.0], [359.0, 1.0, 3.0])  # crosses north
    assert abs(f.phi[1]) < math.radians(3) and f.phi[1] > 0


def test_inverse_roundtrip_on_curved_fixture():
    f = Frame.from_plan(ms.load_plan(str(ms.CURVED)))
    for s in (0.0, 1.3, 17.5, 33.33, 59.99):
        for t in (-12.0, -4.0, 0.0, 6.7, 14.0):
            x, _, z = f.world(s, t, 0.4)
            s2, t2 = f.inverse(x, z)
            assert s2 == pytest.approx(s, abs=1e-6) and t2 == pytest.approx(t, abs=1e-6)


def test_right_handed_no_mirroring():
    f = Frame([0.0, 2.5], [0.0, 0.0])
    fwd = (0.0, 0.0, -1.0)
    right = (1.0, 0.0, 0.0)
    assert cross(right, (0.0, 1.0, 0.0))[2] == 1.0  # Right x Up = +Z (spec 2)
    assert f.t_dir(0.0) == (-1.0, -0.0) or f.t_dir(0.0) == (-1.0, 0.0)
    assert fwd[2] < 0


def test_builder_winding_dedupe_and_degenerate():
    b = Builder("Road", colour=True)
    up = (0.0, 1.0, 0.0)
    pts = [(0, 0, 0), (1, 0, 0), (1, 0, -1), (0, 0, -1)]
    b.quad(*pts, want=up, uvs=[(0, 0), (1, 0), (1, 1), (0, 1)], col=(1, 2, 3, 255))
    b.quad(pts[1], (2, 0, 0), (2, 0, -1), pts[2], want=up, uvs=[(1, 0), (2, 0), (2, 1), (1, 1)], col=(1, 2, 3, 255))
    assert len(b.idx) == 12 and len(b.pos) == 6  # shared edge vertices de-duplicated
    for k in range(0, len(b.idx), 3):
        a, c, d = (b.pos[b.idx[k + j]] for j in range(3))
        assert dot(cross(sub(c, a), sub(d, a)), up) > 0  # CCW seen from above
    n = len(b.idx)
    b.tri((0, 0, 0), (1, 0, 0), (2, 0, 0), want=up, uvs=[(0, 0)] * 3, col=(1, 2, 3, 255))  # collinear
    assert len(b.idx) == n


def test_positions_are_millimetre_rounded():
    m = ms.build(str(ms.CURVED))
    for nd in m.nodes:
        for p in nd.positions[::7]:
            assert all(abs(c * 1000 - round(c * 1000)) < 1e-6 for c in p)
