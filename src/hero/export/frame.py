"""Street frame + deterministic triangle builder (spec/portability.md section 2).

``Frame`` maps ``(s, t, y)`` to world ``(X, Y, Z)`` exactly as the spec formula (identical to the manifest and the xodr
writer): ``phi(s) = wrap_pi(radians(bearing(s) - bearing(0)))``, centreline ``P(s+ds) = P(s) + f(s) * ds`` integrated on
the reference-line grid with the midpoint heading, ``world = (P.x - t cos phi, y, P.z - t sin phi)``. Travel is -Z at
s = 0 and +X is the RIGHT-hand side; ``t`` is positive to the LEFT. Nothing is ever mirrored.

``Builder`` is the one place triangles are emitted: mm-rounded positions, CCW winding chosen from a wanted normal,
vertex de-duplication through an insertion-ordered dict (no hash-order dependence), sorted/explicit iteration only.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any, Sequence

from hero.export.mesh_types import NODE_HAS_UV, NODE_MATERIAL, MeshNode

Vec3 = tuple[float, float, float]


def r3(x: float) -> float:
    return round(x + 0.0, 3) + 0.0


def hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def shade(rgb: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * k))) for c in rgb)  # type: ignore[return-value]


def wrap_pi(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Frame:
    """The street frame, built from the reference-line grid (``s`` list + compass bearings in degrees)."""

    def __init__(self, s: Sequence[float], bearing_deg: Sequence[float]):
        if len(s) < 2 or len(s) != len(bearing_deg):
            raise ValueError("Frame needs >= 2 aligned (s, bearing) samples")
        self.s = [float(v) for v in s]
        self.phi = [wrap_pi(math.radians(b - bearing_deg[0])) for b in bearing_deg]
        self.px, self.pz = [0.0], [0.0]
        for i in range(len(self.s) - 1):
            ds = self.s[i + 1] - self.s[i]
            mid = 0.5 * (self.phi[i] + self.phi[i + 1])
            self.px.append(self.px[-1] + math.sin(mid) * ds)
            self.pz.append(self.pz[-1] - math.cos(mid) * ds)
        self.length = self.s[-1]

    @classmethod
    def from_plan(cls, plan: Any) -> "Frame":
        pts = plan.reference_line.points
        return cls([p.s for p in pts], [p.bearing_deg for p in pts])

    # ------------------------------------------------------------------ evaluation
    def _seg(self, s: float) -> int:
        return min(max(bisect_right(self.s, s + 1e-9) - 1, 0), len(self.s) - 2)

    def phi_at(self, s: float) -> float:
        i = self._seg(s)
        u = (s - self.s[i]) / (self.s[i + 1] - self.s[i])
        return self.phi[i] + (self.phi[i + 1] - self.phi[i]) * u

    def p_at(self, s: float) -> tuple[float, float]:
        i = self._seg(s)
        d = s - self.s[i]
        mid = 0.5 * (self.phi[i] + self.phi[i + 1])
        return (self.px[i] + math.sin(mid) * d, self.pz[i] - math.cos(mid) * d)

    def world(self, s: float, t: float, y: float = 0.0) -> Vec3:
        px, pz = self.p_at(s)
        ph = self.phi_at(s)
        return (px - t * math.cos(ph), y, pz - t * math.sin(ph))

    def world_i(self, i: int, t: float, y: float = 0.0) -> Vec3:
        return (self.px[i] - t * math.cos(self.phi[i]), y, self.pz[i] - t * math.sin(self.phi[i]))

    def t_dir(self, s: float) -> tuple[float, float]:
        """World (X, Z) direction of increasing ``t`` (to the left of travel)."""
        ph = self.phi_at(s)
        return (-math.cos(ph), -math.sin(ph))

    def forward(self, s: float) -> tuple[float, float]:
        ph = self.phi_at(s)
        return (math.sin(ph), -math.cos(ph))

    def end_pose(self) -> dict[str, float]:
        return {"x": r3(self.px[-1]), "z": r3(self.pz[-1]), "yaw_rad": round(self.phi[-1], 6)}

    def inverse(self, x: float, z: float) -> tuple[float, float]:
        """World (X, Z) -> ``(s, t)`` (nearest reference-line point; exact for points produced by ``world``)."""
        best: tuple[float, float, float] | None = None
        for i in range(len(self.s) - 1):
            s0, s1 = self.s[i], self.s[i + 1]
            s = s0
            for _ in range(40):
                px, pz = self.p_at(s)
                ph = self.phi_at(s)
                dx, dz = x - px, z - pz
                s_new = min(max(s + dx * math.sin(ph) - dz * math.cos(ph), s0), s1)
                done = abs(s_new - s) < 1e-10
                s = s_new
                if done:
                    break
            px, pz = self.p_at(s)
            ph = self.phi_at(s)
            dx, dz = x - px, z - pz
            along = abs(dx * math.sin(ph) - dz * math.cos(ph))
            t = -(dx * math.cos(ph) + dz * math.sin(ph))
            if best is None or along < best[0] - 1e-12:
                best = (along, s, t)
        assert best is not None
        return best[1], best[2]


# ---------------------------------------------------------------------- vectors


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def mm(p: Vec3) -> Vec3:
    return (round(p[0], 3) + 0.0, round(p[1], 3) + 0.0, round(p[2], 3) + 0.0)


UP: Vec3 = (0.0, 1.0, 0.0)
Colour = tuple[int, int, int, int]


class Builder:
    """Indexed triangle list for one glTF node (one primitive)."""

    def __init__(self, name: str, *, colour: bool, uv: bool | None = None):
        self.name = name
        self.has_uv = NODE_HAS_UV[name] if uv is None else uv
        self.has_colour = colour
        self.pos: list[Vec3] = []
        self.nrm: list[Vec3] = []
        self.uvs: list[tuple[float, float]] = []
        self.cols: list[Colour] = []
        self.idx: list[int] = []
        self._map: dict[tuple, int] = {}

    def _vertex(self, p: Vec3, n: Vec3, uv: tuple[float, float] | None, col: Colour | None) -> int:
        key = (p, n, uv, col)
        found = self._map.get(key)
        if found is not None:
            return found
        k = len(self.pos)
        self._map[key] = k
        self.pos.append(p)
        self.nrm.append(n)
        if self.has_uv:
            self.uvs.append(uv)  # type: ignore[arg-type]
        if self.has_colour:
            self.cols.append(col)  # type: ignore[arg-type]
        return k

    def tri(self, p0: Vec3, p1: Vec3, p2: Vec3, *, want: Vec3 | None = None, uvs: Sequence | None = None,
            col: Colour | None = None, outward: Vec3 | None = None) -> None:
        """One triangle; flipped so the CCW normal points along ``want`` (or away from the point ``outward``)."""
        p0, p1, p2 = mm(p0), mm(p1), mm(p2)
        n = cross(sub(p1, p0), sub(p2, p0))
        if dot(n, n) < 1e-12:
            return
        if want is None and outward is not None:
            c = ((p0[0] + p1[0] + p2[0]) / 3, (p0[1] + p1[1] + p2[1]) / 3, (p0[2] + p1[2] + p2[2]) / 3)
            want = sub(c, outward)
        order = (0, 2, 1) if (want is not None and dot(n, want) < 0) else (0, 1, 2)
        pts = (p0, p1, p2)
        a, b, c2 = (pts[k] for k in order)
        nn = cross(sub(b, a), sub(c2, a))
        ln = math.sqrt(dot(nn, nn))
        nn = (round(nn[0] / ln, 4) + 0.0, round(nn[1] / ln, 4) + 0.0, round(nn[2] / ln, 4) + 0.0)
        for k in order:
            uv = None
            if self.has_uv and uvs is not None:
                uv = (round(uvs[k][0], 5) + 0.0, round(uvs[k][1], 5) + 0.0)
            self.idx.append(self._vertex(pts[k], nn, uv, col))

    def quad(self, a: Vec3, b: Vec3, c: Vec3, d: Vec3, *, want: Vec3 | None = None, uvs: Sequence | None = None,
             col: Colour | None = None, outward: Vec3 | None = None) -> None:
        u = uvs or [None] * 4
        self.tri(a, b, c, want=want, uvs=[u[0], u[1], u[2]], col=col, outward=outward)
        self.tri(a, c, d, want=want, uvs=[u[0], u[2], u[3]], col=col, outward=outward)

    def box(self, cx: float, cz: float, axf: tuple[float, float], axn: tuple[float, float],
            w: float, h: float, d: float, y0: float, col: Colour) -> None:
        """Box: ``d`` along ``axf``, ``w`` along ``axn``, from ``y0`` to ``y0 + h`` (centre ``cx, cz``)."""
        pts = {}
        for a in (-1, 1):
            for b in (-1, 1):
                for yy in (0, 1):
                    pts[(a, b, yy)] = (cx + axf[0] * a * d / 2 + axn[0] * b * w / 2, y0 + yy * h,
                                       cz + axf[1] * a * d / 2 + axn[1] * b * w / 2)
        c3 = (cx, y0 + h / 2, cz)
        faces = (((-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)), ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)),
                 ((-1, -1, 0), (1, -1, 0), (1, -1, 1), (-1, -1, 1)), ((-1, 1, 0), (1, 1, 0), (1, 1, 1), (-1, 1, 1)),
                 ((-1, -1, 0), (-1, 1, 0), (-1, 1, 1), (-1, -1, 1)), ((1, -1, 0), (1, 1, 0), (1, 1, 1), (1, -1, 1)))
        for f in faces:
            self.quad(*[pts[k] for k in f], col=col, outward=c3)

    def frustum(self, cx: float, cz: float, r0: float, r1: float, h: float, y0: float, col: Colour, sides: int = 6,
                phase: float = 0.0) -> None:
        ring0 = [(cx + r0 * math.cos(2 * math.pi * k / sides + phase), y0, cz + r0 * math.sin(2 * math.pi * k / sides + phase)) for k in range(sides)]
        ring1 = [(cx + r1 * math.cos(2 * math.pi * k / sides + phase), y0 + h, cz + r1 * math.sin(2 * math.pi * k / sides + phase)) for k in range(sides)]
        c3 = (cx, y0 + h / 2, cz)
        for k in range(sides):
            k2 = (k + 1) % sides
            self.quad(ring0[k], ring0[k2], ring1[k2], ring1[k], col=col, outward=c3)
            self.tri((cx, y0 + h, cz), ring1[k], ring1[k2], want=UP, col=col)

    def blob(self, cx: float, cy: float, cz: float, r: float, col: Colour, y_scale: float = 0.8, sides: int = 6) -> None:
        """Low-poly canopy: top cap, one equator-ish band (two rings), bottom cap."""
        ring_hi = [(cx + 0.8 * r * math.cos(2 * math.pi * k / sides), cy + 0.45 * r * y_scale, cz + 0.8 * r * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
        ring_eq = [(cx + r * math.cos(2 * math.pi * (k + 0.5) / sides), cy - 0.1 * r * y_scale, cz + r * math.sin(2 * math.pi * (k + 0.5) / sides)) for k in range(sides)]
        ring_lo = [(cx + 0.6 * r * math.cos(2 * math.pi * k / sides), cy - 0.6 * r * y_scale, cz + 0.6 * r * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
        top, bot, c = (cx, cy + r * y_scale, cz), (cx, cy - 0.85 * r * y_scale, cz), (cx, cy, cz)
        for k in range(sides):
            k2 = (k + 1) % sides
            self.tri(top, ring_hi[k], ring_hi[k2], col=col, outward=c)
            self.tri(ring_hi[k], ring_eq[k], ring_hi[k2], col=col, outward=c)
            self.tri(ring_eq[k], ring_eq[(k - 1) % sides], ring_hi[k], col=col, outward=c)
            self.tri(ring_eq[k], ring_lo[k], ring_eq[(k - 1) % sides], col=col, outward=c)
            self.tri(ring_lo[k], ring_lo[k2], ring_eq[k], col=col, outward=c)
            self.tri(bot, ring_lo[k2], ring_lo[k], col=col, outward=c)

    def limb(self, p0: Vec3, p1: Vec3, r0: float, r1: float, col: Colour, sides: int = 5) -> None:
        """Tapered prism from ``p0`` (radius r0) to ``p1`` (radius r1), any direction; capped at ``p1``."""
        ax = sub(p1, p0)
        ln = math.sqrt(dot(ax, ax))
        if ln < 1e-6:
            return
        a = (ax[0] / ln, ax[1] / ln, ax[2] / ln)
        ref = (1.0, 0.0, 0.0) if abs(a[1]) > 0.9 else (0.0, 1.0, 0.0)
        u = cross(a, ref)
        lu = math.sqrt(dot(u, u))
        u = (u[0] / lu, u[1] / lu, u[2] / lu)
        v = cross(a, u)
        def ring(c: Vec3, r: float) -> list[Vec3]:
            return [(c[0] + r * (u[0] * math.cos(2 * math.pi * k / sides) + v[0] * math.sin(2 * math.pi * k / sides)),
                     c[1] + r * (u[1] * math.cos(2 * math.pi * k / sides) + v[1] * math.sin(2 * math.pi * k / sides)),
                     c[2] + r * (u[2] * math.cos(2 * math.pi * k / sides) + v[2] * math.sin(2 * math.pi * k / sides)))
                    for k in range(sides)]
        r_0, r_1 = ring(p0, r0), ring(p1, r1)
        for k in range(sides):
            k2 = (k + 1) % sides
            mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2, (p0[2] + p1[2]) / 2)
            ctr = mid if r0 > 0 else p0
            self.quad(r_0[k], r_0[k2], r_1[k2], r_1[k], col=col, outward=ctr)
            self.tri(p1, r_1[k], r_1[k2], want=a, col=col)

    def node(self, extras: dict[str, Any]) -> MeshNode | None:
        if not self.idx:
            return None
        return MeshNode(self.name, NODE_MATERIAL[self.name], self.pos, self.nrm, self.idx,
                        self.uvs if self.has_uv else None, self.cols if self.has_colour else None, extras)
