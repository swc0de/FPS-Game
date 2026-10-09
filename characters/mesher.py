"""Meshing signed distance fields: sparse, narrow-band surface nets.

Surface nets (Gibson 1998, "naive" variant) put one vertex in every grid cell
the surface passes through, at the average of the points where the cell's
edges cross zero, and connect the four cells around every crossing edge with
a quad. The result is a closed, evenly spaced quad mesh with no holes or
cracks, which is what the soldiers need from a union of limbs.

Only a thin band around the surface matters, so the grid is sparse:

1. the bounds are split into blocks of ``block`` cells, and the distance at
   each block's centre says whether the surface can pass through it (a
   distance is never more than the true distance, so a block whose centre
   is farther than its half-diagonal is empty);
2. the fine grid's corners are evaluated only inside the active blocks;
3. vertices and quads are built with vectorised numpy over the crossing
   cells, then
4. each vertex is pulled onto the surface with a few Newton steps along the
   distance gradient, and its normal is that gradient.

A 1.8 m body at 1 cm evaluates about 0.3 M points instead of 8 M.
"""
from __future__ import annotations

import numpy as np

# corner offsets of a cell, and its 12 edges as corner index pairs
_CORNERS = np.array([[i, j, k] for k in (0, 1) for j in (0, 1) for i in (0, 1)], np.int64)
_EDGES = np.array([[0, 1], [2, 3], [4, 5], [6, 7],          # along x
                   [0, 2], [1, 3], [4, 6], [5, 7],          # along y
                   [0, 4], [1, 5], [2, 6], [3, 7]], np.int64)  # along z


def _lin(ijk: np.ndarray, dims: np.ndarray) -> np.ndarray:
    return (ijk[..., 0] * dims[1] + ijk[..., 1]) * dims[2] + ijk[..., 2]


def gradient(sdf, p: np.ndarray, eps: float) -> np.ndarray:
    """Central-difference gradient of the distance at points p (n, 3)."""
    n = len(p)
    offs = np.vstack([np.eye(3), -np.eye(3)]) * eps
    q = (p[None, :, :] + offs[:, None, :]).reshape(-1, 3)
    d = sdf(q).reshape(6, n)
    return np.stack([d[0] - d[3], d[1] - d[4], d[2] - d[5]], axis=1) / (2.0 * eps)


def surface_nets(sdf, lo, hi, cell: float, block: int = 8, project: int = 3):
    """Mesh the zero level of ``sdf`` (points (n, 3) -> distances (n,)) inside [lo, hi].

    Returns (vertices (v, 3), triangles (t, 3) int64, normals (v, 3)); the
    triangles wind counter-clockwise seen from outside (negative = inside)."""
    lo = np.asarray(lo, np.float64) - 2 * cell
    hi = np.asarray(hi, np.float64) + 2 * cell
    nblocks = np.maximum(np.ceil((hi - lo) / (cell * block)).astype(np.int64), 1)
    dims = nblocks * block + 1                                   # fine corners per axis
    # 1. active blocks
    bidx = np.stack(np.meshgrid(*[np.arange(n) for n in nblocks], indexing="ij"), -1).reshape(-1, 3)
    centres = lo + (bidx + 0.5) * cell * block
    reach = 0.5 * np.sqrt(3.0) * cell * block + 1.5 * cell
    active = bidx[np.abs(sdf(centres)) <= reach]
    if len(active) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3))
    # 2. corners of the fine grid inside active blocks
    local = np.stack(np.meshgrid(*[np.arange(block + 1)] * 3, indexing="ij"), -1).reshape(-1, 3)
    corner_ijk = (active[:, None, :] * block + local[None, :, :]).reshape(-1, 3)
    corner_ids = np.unique(_lin(corner_ijk, dims))
    cijk = np.stack(np.unravel_index(corner_ids, tuple(dims)), -1)
    values = sdf(lo + cijk * cell)
    values = np.where(values == 0.0, 1e-9, values)               # no exact zeros: signs decide

    def value_at(ijk: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ids = _lin(ijk, dims)
        pos = np.clip(np.searchsorted(corner_ids, ids), 0, len(corner_ids) - 1)
        found = corner_ids[pos] == ids
        return values[pos], found

    # 3. crossing cells (each active block's block^3 cells)
    clocal = np.stack(np.meshgrid(*[np.arange(block)] * 3, indexing="ij"), -1).reshape(-1, 3)
    cells = (active[:, None, :] * block + clocal[None, :, :]).reshape(-1, 3)
    cv, ok = value_at(cells[:, None, :] + _CORNERS[None, :, :])  # (c, 8)
    ok = ok.all(axis=1)
    inside = cv < 0.0
    crossing = ok & inside.any(axis=1) & ~inside.all(axis=1)
    cells, cv = cells[crossing], cv[crossing]
    # vertex = mean of the edge crossings
    a, b = cv[:, _EDGES[:, 0]], cv[:, _EDGES[:, 1]]
    cross = (a < 0.0) != (b < 0.0)
    t = np.where(cross, a / np.where(cross, a - b, 1.0), 0.0)
    pa = _CORNERS[_EDGES[:, 0]].astype(np.float64)
    pb = _CORNERS[_EDGES[:, 1]].astype(np.float64)
    pts = pa[None] + (pb - pa)[None] * t[..., None]               # (c, 12, 3) in cell units
    w = cross[..., None].astype(np.float64)
    offset = (pts * w).sum(axis=1) / np.maximum(w.sum(axis=1), 1.0)
    verts = lo + (cells + offset) * cell
    cell_ids = _lin(cells, dims)
    order = np.argsort(cell_ids)
    cell_ids, cells, verts, cv = cell_ids[order], cells[order], verts[order], cv[order]

    def vertex_of(ijk: np.ndarray) -> np.ndarray:
        ids = _lin(ijk, dims)
        pos = np.clip(np.searchsorted(cell_ids, ids), 0, len(cell_ids) - 1)
        return np.where(cell_ids[pos] == ids, pos, -1)

    # quads around the crossing edges each cell owns (the three leaving its first corner)
    quads = []
    for axis, (u, v), end in ((0, (1, 2), 1), (1, (2, 0), 2), (2, (0, 1), 4)):
        d0, d1 = cv[:, 0], cv[:, end]
        sel = (d0 < 0.0) != (d1 < 0.0)
        base = cells[sel]
        eu, ev = np.zeros(3, np.int64), np.zeros(3, np.int64)
        eu[u], ev[v] = 1, 1
        ring = np.stack([vertex_of(base - eu - ev), vertex_of(base - ev), vertex_of(base),
                         vertex_of(base - eu)], axis=1)
        flip = d0[sel] > 0.0                                      # outside -> inside: normal along -axis
        ring[flip] = ring[flip][:, ::-1]
        quads.append(ring[(ring >= 0).all(axis=1)])
    quads = np.concatenate(quads) if quads else np.zeros((0, 4), np.int64)
    # 4. onto the surface, normals from the gradient
    eps = cell * 0.25
    for _ in range(project):
        g = gradient(sdf, verts, eps)
        d = sdf(verts)
        g2 = np.maximum((g * g).sum(axis=1), 1e-12)
        step = (d / g2)[:, None] * g
        lim = 0.5 * cell
        n = np.linalg.norm(step, axis=1, keepdims=True)
        verts = verts - step * np.minimum(1.0, lim / np.maximum(n, 1e-12))
    g = gradient(sdf, verts, eps)
    normals = g / np.maximum(np.linalg.norm(g, axis=1, keepdims=True), 1e-12)
    # quads -> triangles along the shorter diagonal
    q = quads
    d02 = np.linalg.norm(verts[q[:, 0]] - verts[q[:, 2]], axis=1)
    d13 = np.linalg.norm(verts[q[:, 1]] - verts[q[:, 3]], axis=1)
    use02 = d02 <= d13
    tris = np.concatenate([
        np.stack([q[use02, 0], q[use02, 1], q[use02, 2]], 1), np.stack([q[use02, 0], q[use02, 2], q[use02, 3]], 1),
        np.stack([q[~use02, 0], q[~use02, 1], q[~use02, 3]], 1), np.stack([q[~use02, 1], q[~use02, 2], q[~use02, 3]], 1),
    ])
    used = np.unique(tris)
    remap = np.full(len(verts), -1, np.int64)
    remap[used] = np.arange(len(used))
    return verts[used], remap[tris], normals[used]


def taubin(verts: np.ndarray, tris: np.ndarray, iterations: int = 4, lam: float = 0.5, mu: float = -0.53,
           pinned: np.ndarray | None = None) -> np.ndarray:
    """Taubin smoothing (alternating shrink and inflate Laplacian steps): removes the grid's
    stair-stepping without shrinking the shape."""
    n = len(verts)
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e = np.concatenate([e, e[:, ::-1]])
    deg = np.bincount(e[:, 0], minlength=n).astype(np.float64)
    v = verts.copy()
    for k in range(iterations * 2):
        f = lam if k % 2 == 0 else mu
        acc = np.zeros_like(v)
        np.add.at(acc, e[:, 0], v[e[:, 1]])
        lap = acc / np.maximum(deg, 1.0)[:, None] - v
        if pinned is not None:
            lap[pinned] = 0.0
        v = v + f * lap
    return v


def vertex_normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals from the triangles."""
    fn = np.cross(verts[tris[:, 1]] - verts[tris[:, 0]], verts[tris[:, 2]] - verts[tris[:, 0]])
    n = np.zeros_like(verts)
    for k in range(3):
        np.add.at(n, tris[:, k], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
