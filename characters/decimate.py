"""Mesh simplification for the character LODs: quadric error edge collapse.

Garland & Heckbert (1997): every vertex accumulates the planes of its
triangles as a 4x4 quadric Q = sum(p p^T); the cost of moving a vertex to v
is v^T Q v (the summed squared distance to those planes). Collapsing an edge
merges the two quadrics and puts the vertex where that cost is smallest
(here: the cheaper of the two ends and the midpoint, which keeps attributes
such as skin weights exact at the ends).

Collapsing one edge at a time in Python is far too slow, so this works in
rounds: all edges are costed at once, then the cheapest ones that share no
vertex (and none of whose neighbours are touched twice) collapse together.
A round removes up to about a fifth of the triangles; a dozen rounds take a
12k-triangle head to 4k.

A collapse is refused when it would flip a triangle (its normal turning by
more than about 60 degrees) or when it would join two vertices of different
``groups`` (open borders, material regions, the seams between body parts),
so silhouettes, borders and regions survive.

Per-vertex attributes (colours, joints and weights, surface parameters)
follow the surviving vertex; the collapsed position is one of the two ends
or the midpoint, chosen by the quadric.
"""
from __future__ import annotations

import numpy as np


def _face_planes(v: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    area2 = np.linalg.norm(n, axis=1)
    n = n / np.maximum(area2, 1e-20)[:, None]
    d = -(n * v[f[:, 0]]).sum(axis=1)
    return np.concatenate([n, d[:, None]], axis=1), area2


def _quadrics(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    planes, area2 = _face_planes(v, f)
    k = np.einsum("fi,fj->fij", planes, planes) * area2[:, None, None]
    q = np.zeros((len(v), 4, 4))
    for c in range(3):
        np.add.at(q, f[:, c], k)
    return q


def _boundary_vertices(f: np.ndarray, n: int) -> np.ndarray:
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    keys, cnt = np.unique(e[:, 0] * n + e[:, 1], return_counts=True)
    open_e = keys[cnt == 1]
    out = np.zeros(n, bool)
    out[open_e // n] = True
    out[open_e % n] = True
    return out


def decimate(verts: np.ndarray, faces: np.ndarray, target: int, groups: np.ndarray | None = None,
             attrs: dict | None = None, max_rounds: int = 60, flip_cos: float = 0.5):
    """Simplify to about ``target`` triangles.

    groups: (v,) int, collapses only join vertices of the same group; open
    borders are locked. attrs: {name: (v, ...) array} carried along.
    Returns (verts, faces, attrs)."""
    v = np.asarray(verts, np.float64).copy()
    f = np.asarray(faces, np.int64).copy()
    attrs = {k: np.asarray(a).copy() for k, a in (attrs or {}).items()}
    n = len(v)
    grp = np.zeros(n, np.int64) if groups is None else np.asarray(groups, np.int64).copy()
    locked = _boundary_vertices(f, n)
    q = _quadrics(v, f)
    for _ in range(max_rounds):
        if len(f) <= target:
            break
        # unique edges
        e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
        e = np.unique(e, axis=0)
        a, b = e[:, 0], e[:, 1]
        ok = (grp[a] == grp[b]) & ~locked[a] & ~locked[b]
        e, a, b = e[ok], a[ok], b[ok]
        if len(e) == 0:
            break
        qs = q[a] + q[b]
        cands = np.stack([v[a], v[b], 0.5 * (v[a] + v[b])], axis=1)          # (e, 3, 3)
        h = np.concatenate([cands, np.ones(cands.shape[:2] + (1,))], axis=2)
        cost = np.einsum("eki,eij,ekj->ek", h, qs, h)
        pick = np.argmin(cost, axis=1)
        best = cost[np.arange(len(e)), pick]
        newpos = cands[np.arange(len(e)), pick]
        # greedy independent set, cheapest first: an edge collapses only if its ends share exactly
        # two neighbours (the link condition: anything else pinches the surface), and nothing in
        # either end's one-ring collapses in the same round, so every flip check below is exact
        order = np.argsort(best, kind="stable")
        budget = max(1, (len(f) - target) // 2 + 1)
        nbrs = [set() for _ in range(n)]
        for x, y in np.unique(np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1),
                              axis=0).tolist():
            nbrs[x].add(y)
            nbrs[y].add(x)
        taken = np.zeros(n, bool)
        chosen = []
        al, bl = a.tolist(), b.tolist()
        for i in order.tolist():
            if len(chosen) >= budget:
                break
            x, y = al[i], bl[i]
            if taken[x] or taken[y]:
                continue
            if len(nbrs[x] & nbrs[y]) != 2:
                continue
            chosen.append(i)
            taken[x] = taken[y] = True
            for z in nbrs[x] | nbrs[y]:
                taken[z] = True
        if not chosen:
            break
        chosen = np.array(chosen)
        ka, kb, kp = a[chosen], b[chosen], newpos[chosen]
        # reject flips: faces around either end, with the end moved, must keep their orientation
        remap = np.arange(n)
        remap[kb] = ka
        moved = np.zeros(n, bool)
        moved[ka] = moved[kb] = True
        target_pos = v.copy()
        target_pos[ka] = kp
        target_pos[kb] = kp
        touched = moved[f].any(axis=1)
        tf = f[touched]
        old_n = np.cross(v[tf[:, 1]] - v[tf[:, 0]], v[tf[:, 2]] - v[tf[:, 0]])
        nf = remap[tf]
        degenerate = (nf[:, 0] == nf[:, 1]) | (nf[:, 1] == nf[:, 2]) | (nf[:, 2] == nf[:, 0])
        new_n = np.cross(target_pos[nf[:, 1]] - target_pos[nf[:, 0]], target_pos[nf[:, 2]] - target_pos[nf[:, 0]])
        on = np.linalg.norm(old_n, axis=1) * np.linalg.norm(new_n, axis=1)
        cosang = (old_n * new_n).sum(axis=1) / np.maximum(on, 1e-30)
        bad_face = ~degenerate & (cosang < flip_cos)
        if bad_face.any():
            # undo the collapses that own a bad face
            bad_verts = np.unique(tf[bad_face])
            bad = np.zeros(n, bool)
            bad[bad_verts] = True
            keep = ~(bad[ka] | bad[kb])
            ka, kb, kp = ka[keep], kb[keep], kp[keep]
            if len(ka) == 0:
                break
            remap = np.arange(n)
            remap[kb] = ka
        v[ka] = kp
        q[ka] = q[ka] + q[kb]
        f = remap[f]
        f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])]
    # compact
    used = np.unique(f)
    rm = np.full(n, -1, np.int64)
    rm[used] = np.arange(len(used))
    return v[used], rm[f], {k: a[used] for k, a in attrs.items()}
