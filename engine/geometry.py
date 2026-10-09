"""Procedural mesh construction with numpy.

``MeshBuilder`` accumulates triangles with full tangent frames (normal,
tangent, binormal) so normal mapping works, then writes them into a single
Panda3D ``GeomVertexData`` in one memcpy. Levels batch all pieces sharing a
material into one builder, which keeps draw calls low.

UV conventions
--------------
Box faces use *world-space* planar UVs by default: ``u = dot(P, T) / scale``
and ``v = dot(P, B) / scale`` where T/B are the face tangent/binormal and
``scale`` is metres per texture repeat. Adjacent coplanar pieces therefore
line up seamlessly and texel density is uniform across the whole map.
"""
from __future__ import annotations

import math

import numpy as np
from panda3d.core import (
    Geom,
    GeomNode,
    GeomTriangles,
    GeomVertexArrayFormat,
    GeomVertexData,
    GeomVertexFormat,
    InternalName,
    TransformState,
)

_FORMAT = None
FLOATS_PER_VERTEX = 14  # pos3 normal3 tangent3 binormal3 uv2


def vertex_format() -> GeomVertexFormat:
    global _FORMAT
    if _FORMAT is None:
        arr = GeomVertexArrayFormat()
        arr.addColumn(InternalName.getVertex(), 3, Geom.NTFloat32, Geom.CPoint)
        arr.addColumn(InternalName.getNormal(), 3, Geom.NTFloat32, Geom.CNormal)
        arr.addColumn(InternalName.getTangent(), 3, Geom.NTFloat32, Geom.CVector)
        arr.addColumn(InternalName.getBinormal(), 3, Geom.NTFloat32, Geom.CVector)
        arr.addColumn(InternalName.getTexcoord(), 2, Geom.NTFloat32, Geom.CTexcoord)
        _FORMAT = GeomVertexFormat.registerFormat(GeomVertexFormat(arr))
    return _FORMAT


_SKIN_FORMAT = None
SKIN_FLOATS = FLOATS_PER_VERTEX + 8     # + 4 joint indices + 4 weights


def skinned_vertex_format() -> GeomVertexFormat:
    """The standard format plus four bone indices and four weights per vertex
    (render/shaders/skinning.glsl). Indices are stored as floats: GLSL 3.30 reads
    them as a plain vec4 on every driver."""
    global _SKIN_FORMAT
    if _SKIN_FORMAT is None:
        arr = GeomVertexArrayFormat()
        arr.addColumn(InternalName.getVertex(), 3, Geom.NTFloat32, Geom.CPoint)
        arr.addColumn(InternalName.getNormal(), 3, Geom.NTFloat32, Geom.CNormal)
        arr.addColumn(InternalName.getTangent(), 3, Geom.NTFloat32, Geom.CVector)
        arr.addColumn(InternalName.getBinormal(), 3, Geom.NTFloat32, Geom.CVector)
        arr.addColumn(InternalName.getTexcoord(), 2, Geom.NTFloat32, Geom.CTexcoord)
        arr.addColumn(InternalName.make("skin_joints"), 4, Geom.NTFloat32, Geom.COther)
        arr.addColumn(InternalName.make("skin_weights"), 4, Geom.NTFloat32, Geom.COther)
        _SKIN_FORMAT = GeomVertexFormat.registerFormat(GeomVertexFormat(arr))
    return _SKIN_FORMAT


def transform_vertices(v: np.ndarray, mat) -> np.ndarray:
    """Move (n, 14) vertices by a row-vector 4x4 matrix: positions, and the
    normal, tangent and binormal re-normalised."""
    v = np.array(v, np.float64)
    m = np.asarray(mat, np.float64)
    v[:, 0:3] = v[:, 0:3] @ m[:3, :3] + m[3, :3]
    for a in (3, 6, 9):
        d = v[:, a:a + 3] @ m[:3, :3]
        v[:, a:a + 3] = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    return v


def skinned_node(verts: np.ndarray, joints: np.ndarray, weights: np.ndarray, indices: np.ndarray,
                 name: str = "skinned") -> GeomNode:
    """A GeomNode from bind-pose vertices (n, 14), up to four bones per vertex
    (n, 4 indices and n, 4 weights; rows are normalised to sum to 1) and triangles."""
    w = np.asarray(weights, np.float64)
    w = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-9)
    out = np.zeros((len(verts), SKIN_FLOATS), np.float32)
    out[:, :FLOATS_PER_VERTEX] = verts
    out[:, FLOATS_PER_VERTEX:FLOATS_PER_VERTEX + 4] = joints
    out[:, FLOATS_PER_VERTEX + 4:] = w
    alli = np.asarray(indices, np.uint32)
    vdata = GeomVertexData(name, skinned_vertex_format(), Geom.UHStatic)
    vdata.uncleanSetNumRows(len(out))
    memoryview(vdata.modifyArray(0)).cast("B")[:] = out.tobytes()
    prim = GeomTriangles(Geom.UHStatic)
    prim.setIndexType(Geom.NTUint32)
    handle = prim.modifyVertices()
    handle.uncleanSetNumRows(len(alli))
    memoryview(handle).cast("B")[:] = alli.tobytes()
    geom = Geom(vdata)
    geom.addPrimitive(prim)
    node = GeomNode(name)
    node.addGeom(geom)
    return node


def build_skinned(pieces, name: str = "skinned") -> GeomNode | None:
    """One rigidly skinned mesh from several builders, each on one bone.

    pieces: [(MeshBuilder, bone index, 4x4 matrix or None)], the matrix
    (row-vector convention) places the builder's vertices in the bind pose."""
    verts, joints, indices, base = [], [], [], 0
    for mb, bone, mat in pieces:
        if not mb._verts:
            continue
        v = np.concatenate(mb._verts).astype(np.float64)
        if mat is not None:
            v = transform_vertices(v, mat)
        verts.append(v)
        joints.append(np.full(len(v), bone, np.float32))
        indices.append(np.concatenate(mb._indices).astype(np.uint32) + np.uint32(base))
        base += len(v)
    if not verts:
        return None
    j = np.zeros((base, 4), np.float32)
    j[:, 0] = np.concatenate(joints)
    w = np.zeros((base, 4), np.float32)
    w[:, 0] = 1.0
    return skinned_node(np.concatenate(verts), j, w, np.concatenate(indices), name)


def hpr_matrix(hpr) -> np.ndarray:
    """3x3 rotation (row-vector convention, like Panda) for heading/pitch/roll."""
    m = TransformState.makeHpr(tuple(hpr)).getMat()
    return np.array([[m.getCell(r, c) for c in range(3)] for r in range(3)], dtype=np.float64)


# Local box faces: (normal, tangent, binormal) with T = B x N so the frame is
# right-handed and textures are never mirrored when seen from outside.
_BOX_FACES = (
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    ((-1, 0, 0), (0, -1, 0), (0, 0, 1)),
    ((0, 1, 0), (-1, 0, 0), (0, 0, 1)),
    ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
    ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
)
FACE_NAMES = ("+x", "-x", "+y", "-y", "+z", "-z")


class MeshBuilder:
    def __init__(self):
        self._verts: list[np.ndarray] = []
        self._indices: list[np.ndarray] = []
        self._count = 0

    @property
    def vertex_count(self) -> int:
        return self._count

    def add(self, verts: np.ndarray, indices: np.ndarray) -> None:
        """Append raw vertices (N x 14 float array) and local triangle indices."""
        self._verts.append(np.asarray(verts, dtype=np.float32))
        self._indices.append(np.asarray(indices, dtype=np.uint32) + self._count)
        self._count += len(verts)

    def add_quad(self, corners, normal, tangent, binormal, uvs) -> None:
        v = np.zeros((4, FLOATS_PER_VERTEX), np.float32)
        v[:, 0:3] = corners
        v[:, 3:6] = normal
        v[:, 6:9] = tangent
        v[:, 9:12] = binormal
        v[:, 12:14] = uvs
        self.add(v, np.array([0, 1, 2, 0, 2, 3]))

    # ------------------------------------------------------------- boxes
    def add_box(self, center, size, hpr=(0, 0, 0), uv_scale: float = 1.0,
                skip_faces=(), uv_mode: str = "world", uv_offset=(0.0, 0.0)) -> None:
        """Axis box of ``size`` (full extents) centred at ``center``.

        uv_mode "world": world-planar UVs (seamless between pieces).
        uv_mode "fit":   each face maps to [0, repeats] where repeats = face
                         size / uv_scale rounded to >= 1 (good for crates).
        """
        c = np.asarray(center, np.float64)
        half = np.asarray(size, np.float64) * 0.5
        rot = hpr_matrix(hpr)
        signs = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], np.float64)
        for i, (n, t, b) in enumerate(_BOX_FACES):
            if FACE_NAMES[i] in skip_faces:
                continue
            n = np.array(n, np.float64)
            t = np.array(t, np.float64)
            b = np.array(b, np.float64)
            hn = abs(np.dot(n, half))
            ht = abs(np.dot(t, half))
            hb = abs(np.dot(b, half))
            local = n * hn + signs[:, :1] * t * ht + signs[:, 1:] * b * hb
            world = local @ rot + c
            nw, tw, bw = n @ rot, t @ rot, b @ rot
            if uv_mode == "fit":
                ru = max(round(2 * ht / uv_scale), 1)
                rv = max(round(2 * hb / uv_scale), 1)
                uvs = (signs * 0.5 + 0.5) * np.array([ru, rv])
            else:
                uvs = np.stack([world @ tw, world @ bw], axis=1) / uv_scale
            uvs = uvs + np.asarray(uv_offset)
            self.add_quad(world, nw, tw, bw, uvs)

    # ------------------------------------------------- chamfered boxes
    def add_chamfer_box(self, center, size, bevel: float = 0.01, hpr=(0, 0, 0), uv_scale: float = 1.0,
                        uv_space: str = "local") -> None:
        """Box with 45-degree bevelled edges and corners (26 faces).

        Bevels catch specular highlights, which makes hard-surface props
        (weapons, mannequins, crates) read far better than sharp boxes.
        uv_space "local" maps UVs from the box's own coordinates (for moving
        objects), "world" from world coordinates (for level pieces)."""
        h = np.asarray(size, np.float64) * 0.5
        b = min(bevel, *(h * 0.49))
        rot = hpr_matrix(hpr)
        c = np.asarray(center, np.float64)
        # 24 vertices: each corner x 3 axes (full along that axis, inset on the other two)
        verts = {}
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    s_ = np.array([sx, sy, sz], np.float64)
                    for axis in range(3):
                        p = s_ * (h - b)
                        p[axis] = s_[axis] * h[axis]
                        verts[(sx, sy, sz, axis)] = p
        for nx in (-1, 0, 1):
            for ny in (-1, 0, 1):
                for nz in (-1, 0, 1):
                    n = (nx, ny, nz)
                    k = sum(1 for v in n if v)
                    if k == 0:
                        continue
                    nz_axes = [a for a in range(3) if n[a] != 0]
                    pts = []
                    for (sx, sy, sz, axis), p in verts.items():
                        sv = (sx, sy, sz)
                        if axis not in nz_axes:
                            continue
                        if any(sv[a] != n[a] for a in nz_axes):
                            continue
                        pts.append(p)
                    # dedupe (corners share nothing, but keep safe)
                    uniq = []
                    for p in pts:
                        if not any(np.allclose(p, q) for q in uniq):
                            uniq.append(p)
                    nn = np.array(n, np.float64)
                    nn /= np.linalg.norm(nn)
                    up = np.array([0, 0, 1.0]) if abs(nn[2]) < 0.9 else np.array([0, 1.0, 0])
                    t = np.cross(up, nn)
                    t /= np.linalg.norm(t)
                    bt = np.cross(nn, t)
                    cen = np.mean(uniq, axis=0)
                    ang = [np.arctan2(np.dot(p - cen, bt), np.dot(p - cen, t)) for p in uniq]
                    ordered = [uniq[i] for i in np.argsort(ang)]
                    local = np.array(ordered)
                    world = local @ rot + c
                    tw, bw, nw = t @ rot, bt @ rot, nn @ rot
                    src = world if uv_space == "world" else local
                    v = np.zeros((len(world), FLOATS_PER_VERTEX), np.float32)
                    v[:, 0:3] = world
                    v[:, 3:6] = nw
                    v[:, 6:9] = tw
                    v[:, 9:12] = bw
                    v[:, 12] = src @ (tw if uv_space == "world" else t) / uv_scale
                    v[:, 13] = src @ (bw if uv_space == "world" else bt) / uv_scale
                    idx = []
                    for i in range(1, len(world) - 1):
                        idx += [0, i, i + 1]
                    self.add(v, np.array(idx))

    # ------------------------------------------------------------ polygons
    def add_polygon(self, points, normal, tangent, binormal, uv_scale: float = 1.0) -> None:
        """Convex planar polygon (CCW seen from the normal side), world UVs."""
        pts = np.asarray(points, np.float64)
        n = len(pts)
        v = np.zeros((n, FLOATS_PER_VERTEX), np.float32)
        v[:, 0:3] = pts
        v[:, 3:6] = normal
        v[:, 6:9] = tangent
        v[:, 9:12] = binormal
        v[:, 12] = pts @ np.asarray(tangent) / uv_scale
        v[:, 13] = pts @ np.asarray(binormal) / uv_scale
        idx = []
        for i in range(1, n - 1):
            idx += [0, i, i + 1]
        self.add(v, np.array(idx))

    def add_wedge(self, base, width: float, length: float, height: float, heading: float = 0.0,
                  uv_scale: float = 1.0) -> None:
        """Solid ramp: bottom-front-centre at ``base``, rising ``height`` over
        ``length`` along local +Y (rotated by ``heading``)."""
        rot = hpr_matrix((heading, 0, 0))
        b = np.asarray(base, np.float64)
        w2, L, H = width / 2, length, height

        def xf(p):
            return np.asarray(p, np.float64) @ rot + b

        def d(v):
            v = np.asarray(v, np.float64) @ rot
            return v / np.linalg.norm(v)
        slope_n = d((0, -H, L))
        slope_b = d((0, L, H))
        self.add_polygon([xf((-w2, 0, 0)), xf((w2, 0, 0)), xf((w2, L, H)), xf((-w2, L, H))],
                         slope_n, d((1, 0, 0)), slope_b, uv_scale)
        self.add_polygon([xf((w2, L, 0)), xf((-w2, L, 0)), xf((-w2, L, H)), xf((w2, L, H))],
                         d((0, 1, 0)), d((-1, 0, 0)), d((0, 0, 1)), uv_scale)
        self.add_polygon([xf((w2, 0, 0)), xf((-w2, 0, 0)), xf((-w2, L, 0)), xf((w2, L, 0))],
                         d((0, 0, -1)), d((-1, 0, 0)), d((0, 1, 0)), uv_scale)
        self.add_polygon([xf((w2, 0, 0)), xf((w2, L, 0)), xf((w2, L, H))],
                         d((1, 0, 0)), d((0, 1, 0)), d((0, 0, 1)), uv_scale)
        self.add_polygon([xf((-w2, L, 0)), xf((-w2, 0, 0)), xf((-w2, L, H))],
                         d((-1, 0, 0)), d((0, -1, 0)), d((0, 0, 1)), uv_scale)

    # --------------------------------------------------------- cylinders
    def add_cylinder(self, center, radius: float, height: float, segments: int = 24,
                     uv_scale: float = 1.0, caps: bool = True, hpr=(0, 0, 0)) -> None:
        """Vertical cylinder; ``center`` is the middle of the axis."""
        c = np.asarray(center, np.float64)
        rot = hpr_matrix(hpr)
        ang = np.linspace(0, 2 * math.pi, segments + 1)
        cos, sin = np.cos(ang), np.sin(ang)
        n = np.stack([cos, sin, np.zeros_like(cos)], 1)
        t = np.stack([-sin, cos, np.zeros_like(cos)], 1)
        b = np.tile([0.0, 0.0, 1.0], (segments + 1, 1))
        circumference = 2 * math.pi * radius
        u = ang / (2 * math.pi) * max(round(circumference / uv_scale), 1)
        verts = []
        for z, vv in ((-height / 2, 0.0), (height / 2, height / uv_scale)):
            p = np.stack([cos * radius, sin * radius, np.full_like(cos, z)], 1)
            row = np.zeros((segments + 1, FLOATS_PER_VERTEX), np.float32)
            row[:, 0:3] = p @ rot + c
            row[:, 3:6] = n @ rot
            row[:, 6:9] = t @ rot
            row[:, 9:12] = b @ rot
            row[:, 12] = u
            row[:, 13] = vv
            verts.append(row)
        verts = np.concatenate(verts)
        s = segments + 1
        idx = []
        for i in range(segments):
            a, b2, c2, d = i, i + 1, s + i + 1, s + i
            idx += [a, b2, c2, a, c2, d]
        self.add(verts, np.array(idx))
        if caps:
            for top in (False, True):
                z = height / 2 if top else -height / 2
                nz = 1.0 if top else -1.0
                p = np.stack([cos[:-1] * radius, sin[:-1] * radius, np.full(segments, z)], 1)
                p = np.vstack([[0, 0, z], p])
                cap = np.zeros((segments + 1, FLOATS_PER_VERTEX), np.float32)
                cap[:, 0:3] = p @ rot + c
                cap[:, 3:6] = np.array([0, 0, nz]) @ rot
                tx = np.array([nz, 0, 0])
                cap[:, 6:9] = tx @ rot
                cap[:, 9:12] = np.array([0, 1, 0]) @ rot
                cap[:, 12] = p[:, 0] * nz / uv_scale
                cap[:, 13] = p[:, 1] / uv_scale
                tri = []
                for i in range(segments):
                    j = (i + 1) % segments
                    if top:
                        tri += [0, 1 + i, 1 + j]
                    else:
                        tri += [0, 1 + j, 1 + i]
                self.add(cap, np.array(tri))

    # ----------------------------------------------------------- spheres
    def add_sphere(self, center, radius: float, rings: int = 24, segments: int = 48,
                   uv_scale: float = 1.0) -> None:
        c = np.asarray(center, np.float64)
        theta = np.linspace(0, math.pi, rings + 1)        # polar angle from +Z
        phi = np.linspace(0, 2 * math.pi, segments + 1)
        th, ph = np.meshgrid(theta, phi, indexing="ij")
        n = np.stack([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)], -1)
        t = np.stack([-np.sin(ph), np.cos(ph), np.zeros_like(ph)], -1)
        b = np.cross(n, t)
        v = np.zeros((rings + 1, segments + 1, FLOATS_PER_VERTEX), np.float32)
        v[..., 0:3] = n * radius + c
        v[..., 3:6] = n
        v[..., 6:9] = t
        v[..., 9:12] = b
        v[..., 12] = ph / (2 * math.pi) * max(round(2 * math.pi * radius / uv_scale), 1)
        v[..., 13] = (1 - th / math.pi) * max(round(math.pi * radius / uv_scale), 1)
        v = v.reshape(-1, FLOATS_PER_VERTEX)
        s = segments + 1
        idx = []
        for r in range(rings):
            for k in range(segments):
                a = r * s + k
                b2 = a + 1
                c2 = a + s + 1
                d = a + s
                idx += [a, d, c2, a, c2, b2]
        self.add(v, np.array(idx))

    # ------------------------------------------------------------- build
    def build(self, name: str = "mesh") -> GeomNode | None:
        if not self._verts:
            return None
        verts = np.concatenate(self._verts).astype(np.float32)
        indices = np.concatenate(self._indices).astype(np.uint32)
        vdata = GeomVertexData(name, vertex_format(), Geom.UHStatic)
        vdata.uncleanSetNumRows(len(verts))
        memoryview(vdata.modifyArray(0)).cast("B")[:] = verts.tobytes()
        prim = GeomTriangles(Geom.UHStatic)
        prim.setIndexType(Geom.NTUint32)
        handle = prim.modifyVertices()
        handle.uncleanSetNumRows(len(indices))
        memoryview(handle).cast("B")[:] = indices.tobytes()
        geom = Geom(vdata)
        geom.addPrimitive(prim)
        node = GeomNode(name)
        node.addGeom(geom)
        return node


def fullscreen_quad(name: str = "quad"):
    """A unit quad in the XZ plane (-1..1), for post-processing passes."""
    mb = MeshBuilder()
    mb.add_quad(np.array([[-1, 0, -1], [1, 0, -1], [1, 0, 1], [-1, 0, 1]]),
                (0, -1, 0), (1, 0, 0), (0, 0, 1), np.array([[0, 0], [1, 0], [1, 1], [0, 1]]))
    return mb.build(name)
