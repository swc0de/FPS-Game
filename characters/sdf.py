"""Signed distance functions for the procedural soldiers (Milestone 9).

A shape is described as a union of simple primitives - round cones along
bones, ellipsoids for muscles and the skull, rounded boxes for hands and
pouches - blended with a smooth minimum, plus smooth subtractions for eye
sockets and the like. Everything works on (n, 3) numpy point arrays, so a
whole narrow band of a mesh is evaluated in a handful of vectorised calls
(characters/mesher.py).

Every primitive carries a ``tag`` (a body region such as "arm_l" or
"head"). ``Shape.evaluate`` returns, with the distance, the tag of the
primitive closest to each point. The skin weights use it to keep a thigh's
vertices off the other thigh's bones and an arm's off the torso
(characters/weights.py), and the clothing uses it to tell sleeves from the
torso.

Smooth minimum (Inigo Quilez, polynomial): for distances a, b and blend
radius k, ``h = clamp(0.5 + 0.5 (b - a) / k, 0, 1)``,
``smin = mix(b, a, h) - k h (1 - h)``. It equals min(a, b) away from the
seam and rounds the crease where two shapes meet, which is what turns a
union of limbs into a continuous body.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# ------------------------------------------------------------------ primitives
def round_cone(p: np.ndarray, a, b, ra: float, rb: float) -> np.ndarray:
    """Distance to a cone with rounded ends from a (radius ra) to b (radius rb)."""
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    ba = b - a
    l2 = float(ba @ ba)
    rr = ra - rb
    a2 = l2 - rr * rr
    il2 = 1.0 / l2
    pa = p - a
    y = pa @ ba
    z = y - l2
    xv = pa * l2 - y[:, None] * ba
    x2 = np.einsum("ij,ij->i", xv, xv)
    y2 = y * y * l2
    z2 = z * z * l2
    k = np.sign(rr) * rr * rr * x2
    d = np.empty(len(p))
    m1 = np.sign(z) * a2 * z2 > k
    m2 = ~m1 & (np.sign(y) * a2 * y2 < k)
    m3 = ~(m1 | m2)
    d[m1] = np.sqrt(x2[m1] + z2[m1]) * il2 - rb
    d[m2] = np.sqrt(x2[m2] + y2[m2]) * il2 - ra
    d[m3] = (np.sqrt(x2[m3] * a2 * il2) + y[m3] * rr) * il2 - ra
    return d


def ellipsoid(p: np.ndarray, center, radii, rot=None) -> np.ndarray:
    """Approximate distance to an ellipsoid (the bound of Quilez's ``sdEllipsoid``).
    ``rot`` (3x3, row vectors) turns world offsets into the ellipsoid's frame."""
    q = p - np.asarray(center, np.float64)
    if rot is not None:
        q = q @ np.asarray(rot, np.float64).T
    r = np.asarray(radii, np.float64)
    k0 = np.linalg.norm(q / r, axis=1)
    k1 = np.linalg.norm(q / (r * r), axis=1)
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-12)


def rounded_box(p: np.ndarray, center, half, radius: float, rot=None) -> np.ndarray:
    q = p - np.asarray(center, np.float64)
    if rot is not None:
        q = q @ np.asarray(rot, np.float64).T
    d = np.abs(q) - (np.asarray(half, np.float64) - radius)
    outside = np.linalg.norm(np.maximum(d, 0.0), axis=1)
    inside = np.minimum(np.max(d, axis=1), 0.0)
    return outside + inside - radius


def smin(a: np.ndarray, b: np.ndarray, k: float) -> np.ndarray:
    if k <= 0.0:
        return np.minimum(a, b)
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b + (a - b) * h - k * h * (1.0 - h)


def smax(a: np.ndarray, b: np.ndarray, k: float) -> np.ndarray:
    return -smin(-a, -b, k)


# ---------------------------------------------------------------------- shapes
@dataclass
class Prim:
    kind: str                  # "cone" | "ellipsoid" | "box"
    args: tuple
    tag: str = ""
    blend: float = 0.02        # smooth-union radius with what came before
    subtract: bool = False     # carve instead of add (eye sockets, mouth line)

    def distance(self, p: np.ndarray) -> np.ndarray:
        if self.kind == "cone":
            return round_cone(p, *self.args)
        if self.kind == "ellipsoid":
            return ellipsoid(p, *self.args)
        if self.kind == "box":
            return rounded_box(p, *self.args)
        raise ValueError(self.kind)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if self.kind == "cone":
            a, b, ra, rb = self.args
            r = max(ra, rb)
            lo = np.minimum(a, b) - r
            hi = np.maximum(a, b) + r
        elif self.kind == "ellipsoid":
            r = float(np.max(self.args[1]))
            lo = np.asarray(self.args[0]) - r
            hi = np.asarray(self.args[0]) + r
        else:
            r = float(np.linalg.norm(self.args[1]))
            lo = np.asarray(self.args[0]) - r
            hi = np.asarray(self.args[0]) + r
        return np.asarray(lo, np.float64), np.asarray(hi, np.float64)


def cone(a, b, ra, rb, tag="", blend=0.02) -> Prim:
    return Prim("cone", (np.asarray(a, np.float64), np.asarray(b, np.float64), float(ra), float(rb)), tag, blend)


def ell(center, radii, tag="", blend=0.02, rot=None, subtract=False) -> Prim:
    return Prim("ellipsoid", (np.asarray(center, np.float64), np.asarray(radii, np.float64), rot), tag, blend,
                subtract)


def box(center, half, radius, tag="", blend=0.02, rot=None, subtract=False) -> Prim:
    return Prim("box", (np.asarray(center, np.float64), np.asarray(half, np.float64), float(radius), rot), tag,
                blend, subtract)


@dataclass
class Shape:
    """Primitives combined in order: each one smooth-unions (or smooth-subtracts) with
    the result so far. ``offset`` grows the whole surface (clothing shells)."""
    prims: list = field(default_factory=list)
    offset: float = 0.0
    displace: object = None        # optional f(points, distance) -> extra offset

    def add(self, *prims: Prim) -> "Shape":
        self.prims.extend(prims)
        return self

    def tags(self) -> list[str]:
        return sorted({p.tag for p in self.prims if not p.subtract})

    def bounds(self, margin: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        los, his = zip(*(p.bounds() for p in self.prims if not p.subtract))
        m = margin + abs(self.offset)
        return np.min(los, axis=0) - m, np.max(his, axis=0) + m

    def evaluate(self, p: np.ndarray, with_tags: bool = False):
        """Distances (n,), and with ``with_tags`` the index into ``self.tags()`` of the
        nearest additive primitive for each point.

        Each primitive is evaluated only where it can matter: a point whose distance to
        the primitive's bounding box is at least the running distance plus the blend
        radius is untouched by a smooth union, and a carving primitive only reaches points
        closer to it than to the outside, so they keep their value. Most primitives are
        small, so this skips most of the work."""
        p = np.asarray(p, np.float64)
        n = len(p)
        d = np.full(n, np.inf)
        tags = self.tags()
        nearest = np.full(n, np.inf)
        tag_idx = np.zeros(n, np.int64)
        for prim in self.prims:
            lo, hi = prim.bounds()
            gap = np.linalg.norm(np.maximum(np.maximum(lo - p, p - hi), 0.0), axis=1)
            if prim.subtract:
                # carving changes d only where -distance > d - blend, i.e. distance < -d + blend
                sel = np.nonzero(gap < np.maximum(-d, 0.0) + prim.blend)[0]
                if len(sel):
                    d[sel] = smax(d[sel], -prim.distance(p[sel]), prim.blend)
                continue
            sel = np.nonzero((gap < d + prim.blend) | (with_tags & (gap < nearest)))[0]
            if len(sel) == 0:
                continue
            di = prim.distance(p[sel])
            if with_tags:
                closer = di < nearest[sel]
                nearest[sel[closer]] = di[closer]
                tag_idx[sel[closer]] = tags.index(prim.tag)
            cur = d[sel]
            d[sel] = np.where(np.isinf(cur), di, smin(np.where(np.isinf(cur), di, cur), di, prim.blend))
        d = d - self.offset
        if self.displace is not None:
            d = d - self.displace(p, d)
        return (d, tag_idx) if with_tags else d

    def __call__(self, p: np.ndarray) -> np.ndarray:
        return self.evaluate(p)
