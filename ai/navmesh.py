"""Navigation mesh generated from the level's collision boxes.

A small numpy take on the Recast pipeline:

1. **Rasterise** every collider into vertical columns of a 2D grid
   (cell = 0.25 m). Upright boxes (walls, floors, crates) are rasterised
   *conservatively* - every cell their footprint touches - so a 10 cm wall
   can never fall between two cell centres. Tilted boxes (ramps) are
   sampled at the cell centre, which gives the exact surface height.
2. **Merge** the solid intervals of each column. The top of every merged
   interval with at least crouch height of free space above it is a
   walkable *node* (several per column: tunnel floor, ground, platform).
3. **Link** each node to its 8 neighbours when the height difference is a
   step the character controller climbs (``step_height``).
4. **Erode**: a node with any missing neighbour is dropped. With 0.25 m
   cells this keeps every remaining cell centre at least 0.375 m from any
   blocked cell, i.e. clear of the 0.30 m hull radius; walls and ledges
   lose one ring. Nodes next to low ceilings are flagged "crouch".
5. Keep only what is **reachable** from the spawns (roofs, wall tops and
   the terrain outside the perimeter are discarded).
6. **Polygons**: nodes are merged greedily into rectangles (convex, at
   most ``max_rect`` cells a side). Shared edges between rectangles become
   *portals*.

Queries: ``find_path`` runs A* over the rectangles and then the "simple
stupid funnel" string-pulling algorithm through the portal sequence, which
yields the shortest corner-to-corner path. Paths can be biased per bot
(``cost_bias``) so bots do not all take the same line.

The result is cached in assets/cache/nav keyed by the collider set.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

VERSION = 3

# neighbour order: E N W S NE NW SW SE
DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))
E, N, W, S = 0, 1, 2, 3


@dataclass
class NavConfig:
    cell: float = 0.25
    radius: float = 0.30
    stand_height: float = 1.80
    crouch_height: float = 1.20
    step: float = 0.42
    max_slope_deg: float = 46.0
    max_rect: int = 16

    @classmethod
    def from_movement(cls, cfg: dict) -> "NavConfig":
        return cls(radius=float(cfg["radius"]), stand_height=float(cfg["stand_height"]),
                   crouch_height=float(cfg["crouch_height"]), step=float(cfg["step_height"]),
                   max_slope_deg=float(cfg["max_slope_deg"]))


# ------------------------------------------------------------------ rasterising
_ROT_CACHE: dict[tuple, np.ndarray] = {}


def rotation_rows(hpr) -> np.ndarray:
    """3x3 matrix whose rows are the box's local axes in world space
    (Panda3D convention: world = local * M)."""
    key = tuple(round(float(v), 6) for v in hpr)
    m = _ROT_CACHE.get(key)
    if m is None:
        if key == (0.0, 0.0, 0.0):
            m = np.eye(3)
        else:
            from panda3d.core import Mat3, Vec3, composeMatrix
            pm = Mat3()
            composeMatrix(pm, Vec3(1, 1, 1), Vec3(*key))
            m = np.array([[pm.getCell(r, c) for c in range(3)] for r in range(3)])
        _ROT_CACHE[key] = m
    return m


def rasterize(colliders, x0: float, y0: float, nx: int, ny: int, cs: float):
    """Solid intervals per column -> (col, z0, z1, top_normal_z) arrays."""
    cols, z0s, z1s, nzs = [], [], [], []
    shrink = 0.01            # boxes that only touch a cell edge do not block it
    for c, size, hpr, _surface in colliders:
        c = np.asarray(c, dtype=float)
        h = np.asarray(size, dtype=float) * 0.5
        R = rotation_rows(hpr)
        ext = np.abs(R).T @ h                        # world AABB half extents
        i0 = max(int(math.floor((c[0] - ext[0] - x0) / cs)), 0)
        i1 = min(int(math.floor((c[0] + ext[0] - x0) / cs)), nx - 1)
        j0 = max(int(math.floor((c[1] - ext[1] - y0) / cs)), 0)
        j1 = min(int(math.floor((c[1] + ext[1] - y0) / cs)), ny - 1)
        if i1 < i0 or j1 < j0:
            continue
        ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="xy")
        ii = ii.ravel()
        jj = jj.ravel()
        qx = x0 + (ii + 0.5) * cs - c[0]
        qy = y0 + (jj + 0.5) * cs - c[1]
        if abs(R[2, 2]) > 0.9999:
            # upright box: conservative footprint (separating axis test of the
            # cell square against the rotated rectangle)
            half = cs * 0.5 - shrink
            keep = np.ones(len(ii), dtype=bool)
            for a in (0, 1):
                u = R[a, :2]
                reach = h[a] + half * (abs(u[0]) + abs(u[1]))
                keep &= np.abs(qx * u[0] + qy * u[1]) <= reach
            keep &= np.abs(qx) <= ext[0] + half
            keep &= np.abs(qy) <= ext[1] + half
            n = int(keep.sum())
            if n == 0:
                continue
            cols.append((jj[keep] * nx + ii[keep]).astype(np.int64))
            z0s.append(np.full(n, c[2] - h[2]))
            z1s.append(np.full(n, c[2] + h[2]))
            nzs.append(np.ones(n))
        else:
            # tilted box (ramp): exact intersection of the vertical line
            # through the cell centre with the oriented box
            lo = np.full(len(ii), -np.inf)
            hi = np.full(len(ii), np.inf)
            nz = np.ones(len(ii))
            keep = np.ones(len(ii), dtype=bool)
            for a in range(3):
                ax = R[a]
                av = qx * ax[0] + qy * ax[1] - c[2] * ax[2]
                b = ax[2]
                if abs(b) < 1e-9:
                    keep &= np.abs(av) <= h[a]
                    continue
                t1 = (-h[a] - av) / b
                t2 = (h[a] - av) / b
                lo = np.maximum(lo, np.minimum(t1, t2))
                top = np.maximum(t1, t2)
                better = top < hi
                hi = np.where(better, top, hi)
                nz = np.where(better, abs(b), nz)
            keep &= lo < hi
            n = int(keep.sum())
            if n == 0:
                continue
            cols.append((jj[keep] * nx + ii[keep]).astype(np.int64))
            z0s.append(lo[keep])
            z1s.append(hi[keep])
            nzs.append(nz[keep])
    if not cols:
        empty = np.zeros(0)
        return np.zeros(0, dtype=np.int64), empty, empty, empty
    return np.concatenate(cols), np.concatenate(z0s), np.concatenate(z1s), np.concatenate(nzs)


def merge_columns(col, z0, z1, nz):
    """Union of the solid intervals in every column (vectorised: intervals are
    sorted by column then bottom, and offset by column so one running maximum
    handles all columns at once)."""
    order = np.lexsort((z0, col))
    col, z0, z1, nz = col[order], z0[order], z1[order], nz[order]
    big = 1000.0
    k0 = col * big + z0
    k1 = col * big + z1
    run = np.maximum.accumulate(k1)
    new = np.ones(len(col), dtype=bool)
    new[1:] = k0[1:] > run[:-1] + 1e-4
    group = np.cumsum(new) - 1
    starts = np.flatnonzero(new)
    gcol = col[starts]
    gz0 = z0[starts]
    gz1 = np.maximum.reduceat(z1, starts)
    top = z1 >= gz1[group] - 1e-6
    gnz = np.zeros(len(starts))
    np.maximum.at(gnz, group[top], nz[top])
    return gcol, gz0, gz1, gnz


# ---------------------------------------------------------------------- mesh
class NavMesh:
    """Built with ``NavMesh.build`` or ``NavMesh.load``; see the module docstring."""

    def __init__(self):
        self.cfg = NavConfig()
        self.x0 = self.y0 = 0.0
        self.nx = self.ny = 0
        self.col_first = np.zeros(0, dtype=np.int32)
        self.col_count = np.zeros(0, dtype=np.int8)
        self.node_col = np.zeros(0, dtype=np.int64)
        self.node_z = np.zeros(0)
        self.crouch = np.zeros(0, dtype=bool)
        self.links = np.zeros((0, 8), dtype=np.int32)
        self.rect_of = np.zeros(0, dtype=np.int32)
        self.rects = np.zeros((0, 5), dtype=np.int32)         # i0, j0, w, h, crouch
        self.rect_z = np.zeros((0, 2))                        # min, max height
        self.portals = np.zeros((0, 6))                       # rect, other, ax, ay, bx, by
        self.adj: list[list[int]] = []
        self.build_info = ""

    # ------------------------------------------------------------- building
    @classmethod
    def build(cls, colliders, bounds, seeds, cfg: NavConfig | None = None, log=None) -> "NavMesh":
        t0 = time.time()
        self = cls()
        cfg = cfg or NavConfig()
        self.cfg = cfg
        cs = cfg.cell
        (bx0, by0, _), (bx1, by1, _) = bounds
        self.x0, self.y0 = float(bx0) - 1.0, float(by0) - 1.0
        self.nx = int(math.ceil((bx1 - bx0 + 2.0) / cs))
        self.ny = int(math.ceil((by1 - by0 + 2.0) / cs))
        ncols = self.nx * self.ny

        col, z0, z1, nz = rasterize(colliders, self.x0, self.y0, self.nx, self.ny, cs)
        gcol, gz0, gz1, gnz = merge_columns(col, z0, z1, nz)
        # free space above every interval top
        gap = np.full(len(gcol), np.inf)
        same = gcol[1:] == gcol[:-1]
        gap[:-1] = np.where(same, gz0[1:] - gz1[:-1], np.inf)
        min_nz = math.cos(math.radians(cfg.max_slope_deg))
        ok = (gap >= cfg.crouch_height) & (gnz >= min_nz - 1e-6)
        node_col = gcol[ok]
        node_z = gz1[ok]
        clear = gap[ok]
        n_raw = len(node_col)

        links = self._link(node_col, node_z, ncols)
        # erosion: one ring around anything that is not walkable at the same level
        keep = (links >= 0).all(axis=1)
        node_col, node_z, clear, links = _subset(keep, node_col, node_z, clear, links)
        n_eroded = len(node_col)
        crouch = clear < cfg.stand_height + 0.02
        # the hull is wider than a cell: crouch next to low ceilings too
        dil = crouch.copy()
        for d in range(8):
            nb = links[:, d]
            has = nb >= 0
            dil[has] |= crouch[nb[has]]
        crouch = dil

        # reachability from the seeds (spawn points)
        self.node_col, self.node_z, self.crouch, self.links = node_col, node_z, crouch, links
        self._index_columns(ncols)
        reach = np.zeros(len(node_col), dtype=bool)
        lists = [links[:, d].tolist() for d in range(4)]
        q = deque()
        for s in seeds:
            n = self.locate(s)
            if n >= 0 and not reach[n]:
                reach[n] = True
                q.append(n)
        while q:
            n = q.popleft()
            for d in range(4):
                m = lists[d][n]
                if m >= 0 and not reach[m]:
                    reach[m] = True
                    q.append(m)
        if reach.any():
            node_col, node_z, crouch, links = _subset(reach, node_col, node_z, crouch, links)
        self.node_col, self.node_z, self.crouch, self.links = node_col, node_z, crouch, links
        self._index_columns(ncols)
        self._build_rects()
        self._build_portals()
        self.build_info = (f"{n_raw} raw nodes, {n_eroded} after erosion, {len(node_col)} reachable, "
                           f"{len(self.rects)} polygons, {len(self.portals)} portals in {time.time() - t0:.2f}s")
        if log:
            log(f"[nav] {self.build_info}")
        return self

    def _link(self, node_col, node_z, ncols) -> np.ndarray:
        nx, ny = self.nx, self.ny
        col_first = np.full(ncols, -1, dtype=np.int64)
        col_count = np.bincount(node_col, minlength=ncols)
        first_idx = np.flatnonzero(np.r_[True, node_col[1:] != node_col[:-1]]) if len(node_col) else []
        col_first[node_col[first_idx]] = first_idx
        maxc = int(col_count.max()) if len(node_col) else 0
        ci = node_col % nx
        cj = node_col // nx
        links = np.full((len(node_col), 8), -1, dtype=np.int32)
        step = self.cfg.step
        for d, (dx, dy) in enumerate(DIRS):
            ni, nj = ci + dx, cj + dy
            valid = (ni >= 0) & (ni < nx) & (nj >= 0) & (nj < ny)
            ncol = np.where(valid, nj * nx + ni, 0)
            first = col_first[ncol]
            cnt = np.where(valid, col_count[ncol], 0)
            best = np.full(len(node_col), -1, dtype=np.int64)
            bestdz = np.full(len(node_col), np.inf)
            for k in range(maxc):
                has = k < cnt
                cand = np.where(has, first + k, 0)
                dz = np.abs(node_z[cand] - node_z)
                better = has & (dz <= step) & (dz < bestdz)
                best = np.where(better, cand, best)
                bestdz = np.where(better, dz, bestdz)
            links[:, d] = best
        return links

    def _index_columns(self, ncols: int) -> None:
        self.col_first = np.full(ncols, -1, dtype=np.int32)
        self.col_count = np.bincount(self.node_col, minlength=ncols).astype(np.int8)
        if len(self.node_col):
            first_idx = np.flatnonzero(np.r_[True, self.node_col[1:] != self.node_col[:-1]])
            self.col_first[self.node_col[first_idx]] = first_idx
        self._first = self.col_first.tolist()
        self._count = self.col_count.tolist()
        self._z = self.node_z.tolist()

    def _build_rects(self) -> None:
        n = len(self.node_col)
        lE = self.links[:, E].tolist()
        lN = self.links[:, N].tolist()
        crouch = self.crouch.tolist()
        rect_of = [-1] * n
        rects = []
        M = self.cfg.max_rect
        ci = (self.node_col % self.nx).tolist()
        cj = (self.node_col // self.nx).tolist()
        order = np.lexsort((self.node_z, self.node_col % self.nx, self.node_col // self.nx)).tolist()

        def free(m, c):
            return m >= 0 and rect_of[m] < 0 and crouch[m] == c

        def grow(start, c, fwd, side):
            """Grow a run along ``fwd`` from start, then add parallel runs along ``side``."""
            run = [start]
            while len(run) < M and free(fwd[run[-1]], c):
                run.append(fwd[run[-1]])
            runs = [run]
            while len(runs) < M:
                cand = []
                for k, m in enumerate(runs[-1]):
                    u = side[m]
                    if not free(u, c) or (k > 0 and fwd[cand[-1]] != u):
                        cand = None
                        break
                    cand.append(u)
                if cand is None:
                    break
                runs.append(cand)
            return runs

        for start in order:
            if rect_of[start] >= 0:
                continue
            c = crouch[start]
            a = grow(start, c, lE, lN)            # rows first
            b = grow(start, c, lN, lE)            # columns first
            na, nb = len(a) * len(a[0]), len(b) * len(b[0])
            if nb > na:
                # transpose the column runs into rows
                rows = [[b[k][r] for k in range(len(b))] for r in range(len(b[0]))]
            else:
                rows = a
            row = rows[0]
            rid = len(rects)
            for r in rows:
                for m in r:
                    rect_of[m] = rid
            rects.append((ci[start], cj[start], len(row), len(rows), int(c)))
        self.rect_of = np.array(rect_of, dtype=np.int32)
        self.rects = np.array(rects, dtype=np.int32).reshape(-1, 5)
        zmin = np.full(len(rects), np.inf)
        zmax = np.full(len(rects), -np.inf)
        np.minimum.at(zmin, self.rect_of, self.node_z)
        np.maximum.at(zmax, self.rect_of, self.node_z)
        self.rect_z = np.stack([zmin, zmax], axis=1) if len(rects) else np.zeros((0, 2))

    def _build_portals(self) -> None:
        """Runs of boundary cells whose neighbour lies in another rectangle."""
        cs, x0, y0 = self.cfg.cell, self.x0, self.y0
        links = self.links
        rect_of = self.rect_of
        ci = self.node_col % self.nx
        cj = self.node_col // self.nx
        out = []
        for d in (E, N, W, S):
            nb = links[:, d]
            has = nb >= 0
            src = np.flatnonzero(has)
            dst = nb[has]
            cross = rect_of[src] != rect_of[dst]
            src, dst = src[cross], dst[cross]
            if len(src) == 0:
                continue
            ra, rb = rect_of[src], rect_of[dst]
            # run key: (rect, other rect, boundary line), position along the line
            if d in (E, W):
                line = ci[src] + (1 if d == E else 0)
                along = cj[src]
            else:
                line = cj[src] + (1 if d == N else 0)
                along = ci[src]
            order = np.lexsort((along, line, rb, ra))
            ra, rb, line, along = ra[order], rb[order], line[order], along[order]
            brk = np.ones(len(ra), dtype=bool)
            brk[1:] = (ra[1:] != ra[:-1]) | (rb[1:] != rb[:-1]) | (line[1:] != line[:-1]) | \
                      (along[1:] != along[:-1] + 1)
            starts = np.flatnonzero(brk)
            ends = np.r_[starts[1:], len(ra)] - 1
            for s_, e_ in zip(starts.tolist(), ends.tolist()):
                a0 = (along[s_] + 0.5) * cs
                a1 = (along[e_] + 0.5) * cs
                ln = line[s_] * cs
                if d in (E, W):
                    p = (x0 + ln, y0 + a0, x0 + ln, y0 + a1)
                else:
                    p = (x0 + a0, y0 + ln, x0 + a1, y0 + ln)
                out.append((int(ra[s_]), int(rb[s_])) + p)
        self.portals = np.array(out, dtype=float).reshape(-1, 6)
        self._make_adjacency()

    def _make_adjacency(self) -> None:
        self.adj = [[] for _ in range(len(self.rects))]
        for k, p in enumerate(self.portals.tolist()):
            self.adj[int(p[0])].append(k)
        self._portal_list = self.portals.tolist()
        r = self.rects.astype(float)
        cs = self.cfg.cell
        self._rect_center = [(self.x0 + (a + w * 0.5) * cs, self.y0 + (b + h * 0.5) * cs)
                             for a, b, w, h in r[:, :4].tolist()]
        self._rect_crouch = self.rects[:, 4].astype(bool).tolist() if len(self.rects) else []
        self._rect_area = (self.rects[:, 2] * self.rects[:, 3]).astype(float) if len(self.rects) else np.zeros(0)
        self._base_portals = len(self._portal_list)
        self._dyn: dict = {}

    # ------------------------------------------------- dynamic links (destruction)
    def add_link(self, tag, ra: int, rb: int, a, b, two_way: bool = True) -> None:
        """Off-mesh connection between two rects through the segment a-b (2D):
        a hole blown through a wall (two-way) or an open hatch (one-way drop).
        It behaves like a portal for A* and the funnel."""
        ks = self._dyn.setdefault(tag, [])
        for r0, r1 in ((ra, rb), (rb, ra)) if two_way else ((ra, rb),):
            k = len(self._portal_list)
            self._portal_list.append([float(r0), float(r1), float(a[0]), float(a[1]), float(b[0]), float(b[1])])
            self.adj[r0].append(k)
            ks.append(k)

    def remove_links(self, tag) -> None:
        for k in self._dyn.pop(tag, []):
            r0 = int(self._portal_list[k][0])
            if k in self.adj[r0]:
                self.adj[r0].remove(k)

    def clear_links(self) -> None:
        if len(self._portal_list) != self._base_portals or self._dyn:
            self._make_adjacency()

    def rect_at(self, pos, search: int = 3, max_dz: float = 0.6) -> int:
        """Rect of the walkable node at pos (feet height within max_dz), or -1."""
        n = self.locate(pos, search)
        if n < 0 or abs(self._z[n] - float(pos[2])) > max_dz:
            return -1
        return int(self.rect_of[n])

    def rect_below(self, x: float, y: float, z: float, search: int = 2) -> int:
        """Rect of the highest walkable node below height z around (x, y), or -1."""
        cs = self.cfg.cell
        i0 = int((x - self.x0) // cs)
        j0 = int((y - self.y0) // cs)
        for r in range(search + 1):
            best, best_z = -1, -1e9
            for i in range(i0 - r, i0 + r + 1):
                for j in range(j0 - r, j0 + r + 1):
                    if not (0 <= i < self.nx and 0 <= j < self.ny):
                        continue
                    col = j * self.nx + i
                    first = self._first[col]
                    if first < 0:
                        continue
                    for n in range(first, first + self._count[col]):
                        if best_z < self._z[n] < z:
                            best, best_z = n, self._z[n]
            if best >= 0:
                return int(self.rect_of[best])
        return -1

    # --------------------------------------------------------------- cache
    def save(self, path) -> None:
        np.savez_compressed(path, meta=json.dumps({"version": VERSION, "cfg": asdict(self.cfg), "x0": self.x0,
                                                    "y0": self.y0, "nx": self.nx, "ny": self.ny,
                                                    "info": self.build_info}),
                            node_col=self.node_col, node_z=self.node_z, crouch=self.crouch, links=self.links,
                            rect_of=self.rect_of, rects=self.rects, rect_z=self.rect_z, portals=self.portals)

    @classmethod
    def load(cls, path) -> "NavMesh | None":
        try:
            data = np.load(path)
            meta = json.loads(str(data["meta"]))
        except (OSError, ValueError, KeyError):
            return None
        if meta.get("version") != VERSION:
            return None
        self = cls()
        self.cfg = NavConfig(**meta["cfg"])
        self.x0, self.y0, self.nx, self.ny = meta["x0"], meta["y0"], meta["nx"], meta["ny"]
        self.build_info = meta.get("info", "") + " (cached)"
        for k in ("node_col", "node_z", "crouch", "links", "rect_of", "rects", "rect_z", "portals"):
            setattr(self, k, data[k])
        self._index_columns(self.nx * self.ny)
        self._make_adjacency()
        return self

    @staticmethod
    def cache_key(colliders, bounds, seeds, cfg: NavConfig) -> str:
        h = hashlib.sha1()
        h.update(json.dumps([VERSION, asdict(cfg), bounds, [list(map(float, s)) for s in seeds]]).encode())
        for c, size, hpr, _s in colliders:
            h.update(np.asarray(list(c) + list(size) + list(hpr), dtype=np.float32).tobytes())
        return h.hexdigest()[:16]

    @classmethod
    def cached(cls, colliders, bounds, seeds, cache_dir, name: str, cfg: NavConfig | None = None,
               log=None) -> "NavMesh":
        cfg = cfg or NavConfig()
        key = cls.cache_key(colliders, bounds, seeds, cfg)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{name}_{key}.npz"
        if path.exists():
            nav = cls.load(path)
            if nav is not None:
                if log:
                    log(f"[nav] {name}: {nav.build_info}")
                return nav
        nav = cls.build(colliders, bounds, seeds, cfg, log=log)
        try:
            nav.save(path)
        except OSError:
            pass
        return nav

    # ------------------------------------------------------------- queries
    @property
    def node_count(self) -> int:
        return len(self.node_col)

    def node_pos(self, n: int) -> tuple[float, float, float]:
        c = int(self.node_col[n])
        cs = self.cfg.cell
        return (self.x0 + (c % self.nx + 0.5) * cs, self.y0 + (c // self.nx + 0.5) * cs, self._z[n])

    def _column_node(self, i: int, j: int, z: float, tol_up: float = 0.7, tol_down: float = 3.0) -> int:
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        col = j * self.nx + i
        first = self._first[col]
        if first < 0:
            return -1
        best, best_d = -1, 1e9
        for n in range(first, first + self._count[col]):
            dz = self._z[n] - z
            if -tol_down <= dz <= tol_up:
                d = abs(dz) if dz <= 0 else dz * 3.0          # prefer the floor under the point
                if d < best_d:
                    best, best_d = n, d
        return best

    def locate(self, pos, search: int = 6) -> int:
        """Node under ``pos`` (feet position); searches nearby cells if the point
        is just off the mesh (next to a wall). -1 if nothing is close."""
        cs = self.cfg.cell
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2]) if len(pos) > 2 else 0.0
        i = int((x - self.x0) // cs)
        j = int((y - self.y0) // cs)
        n = self._column_node(i, j, z)
        if n >= 0:
            return n
        best, best_d = -1, 1e9
        for r in range(1, search + 1):
            for di in range(-r, r + 1):
                for dj in (-r, r) if abs(di) != r else range(-r, r + 1):
                    m = self._column_node(i + di, j + dj, z, 1.2, 1.5)
                    if m >= 0:
                        d = di * di + dj * dj
                        if d < best_d:
                            best, best_d = m, d
            if best >= 0:
                return best
        return -1

    def snap(self, pos, search: int = 6):
        """Closest walkable point (cell centre) to pos, or None."""
        n = self.locate(pos, search)
        return None if n < 0 else self.node_pos(n)

    def height_at(self, x: float, y: float, z_hint: float) -> float | None:
        n = self.locate((x, y, z_hint), search=1)
        return None if n < 0 else self._z[n]

    def is_crouch(self, pos) -> bool:
        n = self.locate(pos, search=1)
        return n >= 0 and bool(self.crouch[n])

    def walkable_line(self, a, b, max_step: float | None = None) -> bool:
        """True if a straight walk from a to b stays on the mesh (no wall, no drop)."""
        cs = self.cfg.cell
        ax, ay, az = float(a[0]), float(a[1]), float(a[2])
        dx, dy = float(b[0]) - ax, float(b[1]) - ay
        dist = math.hypot(dx, dy)
        n = self.locate((ax, ay, az), search=1)
        if n < 0:
            return False
        z = self._z[n]
        steps = max(int(dist / (cs * 0.5)), 1)
        step = self.cfg.step if max_step is None else max_step
        for k in range(1, steps + 1):
            t = k / steps
            i = int((ax + dx * t - self.x0) // cs)
            j = int((ay + dy * t - self.y0) // cs)
            m = self._column_node(i, j, z, step + 0.01, step + 0.01)
            if m < 0:
                return False
            z = self._z[m]
        # same level at the end (not under the stairs that lead to b)
        return len(b) < 3 or abs(z - float(b[2])) <= step + 0.3

    def random_point(self, rng, near=None, radius: float = 0.0):
        """Random walkable point: anywhere (area weighted) or within radius of near."""
        if near is not None and radius > 0:
            for _ in range(24):
                a = rng.uniform(0, 2 * math.pi)
                r = radius * math.sqrt(rng.random())
                p = (near[0] + math.cos(a) * r, near[1] + math.sin(a) * r, near[2])
                n = self.locate(p, search=2)
                if n >= 0 and abs(self._z[n] - near[2]) < 2.5:
                    return self.node_pos(n)
            return None
        if len(self.rects) == 0:
            return None
        k = int(np.searchsorted(np.cumsum(self._rect_area), rng.random() * self._rect_area.sum()))
        k = min(k, len(self.rects) - 1)
        i0, j0, w, h, _ = self.rects[k].tolist()
        i = i0 + rng.randrange(w)
        j = j0 + rng.randrange(h)
        n = self._column_node(i, j, float(self.rect_z[k][1]), 0.1, 50.0)
        return None if n < 0 else self.node_pos(n)

    # ---------------------------------------------------------------- paths
    def find_path(self, start, goal, cost_bias=None, max_expand: int = 20000):
        """Shortest path from start to goal as a list of (x, y, z) corners
        (start and goal included), or None if unreachable.

        ``cost_bias(rect_index) -> multiplier`` lets a caller prefer or avoid
        areas (per-bot route variety, avoiding known danger)."""
        sn = self.locate(start)
        gn = self.locate(goal)
        if sn < 0 or gn < 0:
            return None
        sr, gr = int(self.rect_of[sn]), int(self.rect_of[gn])
        sxy = (float(start[0]), float(start[1]))
        gxy = (float(goal[0]), float(goal[1]))
        if sr == gr:
            pts2, chain = [sxy, gxy], []
        else:
            # A* only estimates corridor lengths; scoring portals at their
            # midpoints or at the point closest to the entry each wins on
            # different layouts, so run both and keep the shorter string.
            pts2 = None
            for mode in ("closest", "mid"):
                ch = self._astar(sr, gr, sxy, gxy, cost_bias, max_expand, mode)
                if ch is None:
                    return None
                cand = string_pull(sxy, gxy, self._oriented_portals(ch))
                if pts2 is None or _length2(cand) < _length2(pts2) - 1e-6:
                    pts2, chain = cand, ch
        # heights: every corner is a portal end point; take the floor of the
        # rects that portal joins (unambiguous on stairs over walkable ground)
        out = [(sxy[0], sxy[1], self._z[sn])]
        j = 0
        z = self._z[sn]
        for x, y in pts2[1:-1]:
            rects = None
            while j < len(chain):
                p = self._portal_list[chain[j]]
                j += 1
                if (p[2] == x and p[3] == y) or (p[4] == x and p[5] == y):
                    rects = (int(p[0]), int(p[1]))
                    break
            zz = self._z_in_rects(x, y, rects) if rects else None
            if zz is None:
                m = self.locate((x, y, z + 0.3), search=2)
                zz = self._z[m] if m >= 0 else z
            z = zz
            out.append((x, y, z))
        out.append((gxy[0], gxy[1], self._z[gn]))
        return out

    def _z_in_rects(self, x: float, y: float, rects) -> float | None:
        cs = self.cfg.cell
        rect_of = self.rect_of
        for ox, oy in ((0.01, 0.01), (-0.01, 0.01), (0.01, -0.01), (-0.01, -0.01)):
            i = int((x + ox - self.x0) // cs)
            j = int((y + oy - self.y0) // cs)
            if not (0 <= i < self.nx and 0 <= j < self.ny):
                continue
            col = j * self.nx + i
            first = self._first[col]
            if first < 0:
                continue
            for n in range(first, first + self._count[col]):
                if int(rect_of[n]) in rects:
                    return self._z[n]
        return None

    def _oriented_portals(self, chain):
        portals = []
        for k in chain:
            p = self._portal_list[k]
            a, b = (p[2], p[3]), (p[4], p[5])
            ca = self._rect_center[int(p[0])]
            cb = self._rect_center[int(p[1])]
            # left/right as seen travelling from rect a to rect b
            dx, dy = cb[0] - ca[0], cb[1] - ca[1]
            if dx * (a[1] - b[1]) - dy * (a[0] - b[0]) > 0:
                portals.append((a, b))
            else:
                portals.append((b, a))
        return portals

    def _astar(self, sr: int, gr: int, sxy, gxy, cost_bias, max_expand: int, mode: str = "closest"):
        P = self._portal_list
        crouch = self._rect_crouch
        best = {sr: 0.0}
        at = {sr: sxy}
        came: dict[int, tuple[int, int]] = {}
        heap = [(math.dist(sxy, gxy), 0, sr)]
        tie = 1
        expanded = 0
        while heap:
            f, _, r = heapq.heappop(heap)
            if r == gr:
                break
            g = best[r]
            if f - math.dist(at[r], gxy) > g + 1e-6:
                continue
            expanded += 1
            if expanded > max_expand:
                return None
            px, py = at[r]
            for k in self.adj[r]:
                p = P[k]
                s = int(p[1])
                ax, ay, bx, by = p[2], p[3], p[4], p[5]
                ex, ey = bx - ax, by - ay
                if mode == "mid":
                    t = 0.5
                else:                       # closest point to where this rect was entered
                    L2 = ex * ex + ey * ey
                    t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * ex + (py - ay) * ey) / L2))
                qx, qy = ax + ex * t, ay + ey * t
                cost = math.hypot(qx - px, qy - py) + 0.05
                mult = 2.6 if crouch[s] else 1.0
                if cost_bias is not None:
                    mult *= cost_bias(s)
                ng = g + cost * mult
                if ng < best.get(s, 1e18):
                    best[s] = ng
                    at[s] = (qx, qy)
                    came[s] = (r, k)
                    heapq.heappush(heap, (ng + math.hypot(gxy[0] - qx, gxy[1] - qy), tie, s))
                    tie += 1
        if gr not in came:
            return None
        chain = []
        r = gr
        while r != sr:
            pr, k = came[r]
            chain.append(k)
            r = pr
        chain.reverse()
        return chain

    def path_length(self, path) -> float:
        return sum(math.dist(path[i][:2], path[i + 1][:2]) for i in range(len(path) - 1)) if path else 0.0


def _length2(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _subset(keep, *arrays):
    """Keep a subset of nodes; neighbour links are remapped (links to dropped nodes -> -1)."""
    idx = np.cumsum(keep) - 1
    out = []
    for a in arrays:
        out.append(a[keep])
    links = out[-1]
    valid = links >= 0
    safe = np.where(valid, links, 0)
    links = np.where(valid & keep[safe], idx[safe], -1).astype(np.int32)
    out[-1] = links
    return out


# ------------------------------------------------------------- string pulling
def _tri2(a, b, c) -> float:
    """Twice the signed area of triangle abc (positive = c is clockwise of a->b)."""
    ax, ay = b[0] - a[0], b[1] - a[1]
    bx, by = c[0] - a[0], c[1] - a[1]
    return bx * ay - ax * by


def _same(a, b) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 < 1e-10


def string_pull(start, goal, portals):
    """Simple stupid funnel algorithm (M. Mononen): the shortest path through a
    sequence of portals given as (left, right) points seen in travel direction."""
    ports = [(start, start)] + list(portals) + [(goal, goal)]
    pts = [start]
    apex = left = right = start
    ai = li = ri = 0
    i = 1
    n = len(ports)
    guard = 0
    while i < n and guard < 10000:
        guard += 1
        pl, pr = ports[i]
        if _tri2(apex, right, pr) <= 0.0:
            if _same(apex, right) or _tri2(apex, left, pr) > 0.0:
                right, ri = pr, i
            else:
                if not _same(pts[-1], left):
                    pts.append(left)
                apex, ai = left, li
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        if _tri2(apex, left, pl) >= 0.0:
            if _same(apex, left) or _tri2(apex, right, pl) < 0.0:
                left, li = pl, i
            else:
                if not _same(pts[-1], right):
                    pts.append(right)
                apex, ai = right, ri
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        i += 1
    if not _same(pts[-1], goal):
        pts.append(goal)
    return pts
