"""Tactical map analysis for the v2 bots: precomputed once per map, cached.

Humans learn a map's angles; this module gives the bots the same knowledge
by analysing the level geometry once (a few seconds on first start, then a
cached ``assets/cache/nav/<map>_<hash>_tactical.npz`` next to the navmesh,
keyed by the collider set):

1. **Occupancy.** The level's collider boxes are rasterised into a 3D grid
   (0.25 m cells, conservative like the navmesh, so a thin wall is never
   missed). Destructible panels go into a second grid that stores which
   panel fills a cell.
2. **Tactical points.** Walkable sample points: one per 3 m on every floor
   of the navmesh, plus every doorway (a navmesh portal with walls at both
   ends), every hand-placed position of the map and every lane waypoint.
   That is ~1,500 points on the compound. Each knows its callout area,
   bomb site, navmesh polygon and floor height.
3. **Visibility.** Line of sight between every pair of points within
   ``max_range``, eye to eye, standing (1.55 m) and crouched (1.05 m). The
   rays are marched through the occupancy grid in numpy, thousands at a
   time (vectorised sampling every 0.2 m). Results are bitsets, one row per
   point. Pairs that only a destructible panel blocks remember the panel:
   when it is shot open (``on_panel``), those pairs are re-tested against
   the real geometry with Bullet rays, a few per tick (``update``), and a
   new round restores the baked bits.
4. **Cover.** For each point, 16 directions at crouch and standing height:
   is there something solid within 1.2 m?
5. **Point graph.** Neighbours within 4.5 m that can be walked to in a
   straight line, with the distance; the belief model spreads "an enemy
   could be here by now" along it. Arrival times from both spawns
   (running) bound where enemies can be early in a round.
6. **Spots**, scored from the above for each bomb site:
   * *entries*: doorways and lane points just outside the site;
   * *holds*: points in the site, or near it in the defenders' territory (they
     get there at least 8 s before attackers can), that are behind every entry
     they can see (attackers must pass the entry to reach them), watch an
     entry from 5-30 m, have cover, and are seen from few points on the
     attackers' side beyond the entries;
   * *forward spots*: covered points on the attackers' way in that defenders
     reach 2-8 s first, for early information plays;
   * *off-angles*: holds whose line to the entry is far (> 50 deg) from the
     direction attackers walk in, or deep in the site;
   * *crossfire pairs*: two holds that see the same entry with at least 60
     degrees between them.
   The hand-placed defender positions are ordinary candidates with a small
   bonus, no longer the only options.

Runtime queries (all array lookups): ``nearest``, ``sees``, ``visible_mask``,
``in_view``, ``cover_mask``, ``holds``.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import time
from dataclasses import asdict, dataclass

import numpy as np

from ai.navmesh import NavMesh, rasterize

VERSION = 1
N_DIRS = 16


@dataclass
class TacticalConfig:
    spacing: float = 3.0            # point grid
    max_range: float = 55.0         # visibility pairs
    eye_stand: float = 1.55
    eye_crouch: float = 1.05
    cell: float = 0.25              # occupancy grid
    dz: float = 0.25
    step: float = 0.2               # ray sampling
    link_range: float = 4.5         # point graph
    cover_range: float = 1.2
    run_speed: float = 5.4


# ------------------------------------------------------------------ occupancy
class Occupancy:
    """3D bool grid of the level (hard) and of the destructible panels (soft:
    panel index + 1 per cell, 0 = empty)."""

    def __init__(self, colliders, panels, bounds, cfg: TacticalConfig):
        (bx0, by0, bz0), (bx1, by1, bz1) = bounds
        cs, dz = cfg.cell, cfg.dz
        self.cs, self.dz = cs, dz
        self.x0, self.y0, self.z0 = float(bx0) - 1.0, float(by0) - 1.0, float(bz0) - 1.0
        self.nx = int(math.ceil((bx1 - bx0 + 2.0) / cs))
        self.ny = int(math.ceil((by1 - by0 + 2.0) / cs))
        self.nz = int(math.ceil((bz1 - bz0 + 6.0) / dz))
        self.hard = self._fill(colliders, None)
        self.soft = self._fill([(p.center, p.size, p.hpr, p.surface) for p in panels], np.arange(len(panels)))

    def _fill(self, colliders, ids):
        nx, ny, nz = self.nx, self.ny, self.nz
        ncols = nx * ny
        if ids is None:
            col, z0, z1, _ = rasterize(colliders, self.x0, self.y0, nx, ny, self.cs)
            k0 = np.clip(np.floor((z0 - self.z0) / self.dz), 0, nz).astype(np.int64)
            k1 = np.clip(np.ceil((z1 - self.z0) / self.dz), 0, nz).astype(np.int64)
            diff = np.zeros((ncols, nz + 1), np.int16)
            np.add.at(diff, (col, k0), 1)
            np.add.at(diff, (col, k1), -1)
            return (np.cumsum(diff[:, :nz], axis=1) > 0).reshape(ny, nx, nz)
        out = np.zeros((ny, nx, nz), np.int16)
        for idx, box in zip(ids, colliders):
            col, z0, z1, _ = rasterize([box], self.x0, self.y0, nx, ny, self.cs)
            if len(col) == 0:
                continue
            k0 = np.clip(np.floor((z0 - self.z0) / self.dz), 0, nz).astype(np.int64)
            k1 = np.clip(np.ceil((z1 - self.z0) / self.dz), 0, nz).astype(np.int64)
            for c, a, b in zip(col.tolist(), k0.tolist(), k1.tolist()):
                out[c // nx, c % nx, a:b] = idx + 1
        return out

    def trace(self, a: np.ndarray, b: np.ndarray, step: float, chunk: int = 6000):
        """Vectorised line tests between point arrays a and b (M x 3).
        Returns (hard_blocked, soft_panel) where soft_panel is the panel
        index + 1 that blocks the ray (0 = none), for rays not hard-blocked."""
        m = len(a)
        hard = np.zeros(m, bool)
        soft = np.zeros(m, np.int16)
        d = b - a
        length = np.linalg.norm(d, axis=1)
        n = np.maximum(np.ceil(length / step).astype(np.int64), 1)
        order = np.argsort(n)
        nx, ny, nz = self.nx, self.ny, self.nz
        hflat = self.hard.reshape(-1)
        sflat = self.soft.reshape(-1)
        for s in range(0, m, chunk):
            idx = order[s:s + chunk]
            ns = n[idx]
            kmax = int(ns.max())
            k = np.arange(kmax)[None, :]
            valid = k < ns[:, None]
            t = (k + 0.5) / ns[:, None]
            pa = a[idx]
            dd = d[idx]
            ix = np.floor((pa[:, 0:1] + dd[:, 0:1] * t - self.x0) / self.cs).astype(np.int64)
            iy = np.floor((pa[:, 1:2] + dd[:, 1:2] * t - self.y0) / self.cs).astype(np.int64)
            iz = np.floor((pa[:, 2:3] + dd[:, 2:3] * t - self.z0) / self.dz).astype(np.int64)
            inside = valid & (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0) & (iz < nz)
            flat = np.where(inside, (iy * nx + ix) * nz + iz, 0)
            hb = hflat[flat] & inside
            hard[idx] = hb.any(axis=1)
            sb = np.where(inside, sflat[flat], 0)
            soft[idx] = sb.max(axis=1)
        soft[hard] = 0
        return hard, soft


# --------------------------------------------------------------------- the map
class TacticalMap:
    def __init__(self):
        self.cfg = TacticalConfig()
        self.pos = np.zeros((0, 3), np.float32)
        self.kind = np.zeros(0, np.int8)            # flags: 1 doorway, 2 hand-placed position, 4 lane waypoint
        self.rect = np.zeros(0, np.int32)
        self.area_id = np.zeros(0, np.int16)
        self.areas: list[str] = []
        self.site_id = np.zeros(0, np.int8)         # -1 or index into self.sites
        self.sites: list[str] = []
        self.vis = np.zeros((0, 0), np.uint8)       # packed bits, standing eye to standing eye
        self.vis_low = np.zeros((0, 0), np.uint8)   # crouched eye to crouched eye
        self.cover = np.zeros(0, np.uint16)         # directions blocked at crouch height
        self.cover_high = np.zeros(0, np.uint16)    # directions blocked at standing height
        self.soft_pairs = np.zeros((0, 3), np.int32)
        self.edges = np.zeros((0, 3), np.float32)   # src, dst, distance (both directions)
        self.eta = {}                               # side -> running arrival time from its spawn
        self.spots: dict = {}
        self.build_info = ""
        self._base_vis = None
        self._grid = {}
        self._pending: list[int] = []

    # ------------------------------------------------------------ building
    @classmethod
    def build(cls, level, nav: NavMesh, cfg: TacticalConfig | None = None, log=None) -> "TacticalMap":
        t0 = time.time()
        self = cls()
        cfg = cfg or TacticalConfig()
        self.cfg = cfg
        occ = Occupancy(level.colliders, level.panel_specs, level.data["bounds"], cfg)
        t_occ = time.time() - t0
        pts, kinds = _sample_points(level, nav, cfg)
        self.pos = pts.astype(np.float32)
        self.kind = kinds.astype(np.int8)
        n = len(pts)
        self.rect = np.array([nav.rect_at(p, search=4, max_dz=0.8) for p in pts], np.int32)
        self.areas = sorted({level.callout_at(p[0], p[1]) for p in pts} | {""})
        amap = {a: i for i, a in enumerate(self.areas)}
        self.area_id = np.array([amap[level.callout_at(p[0], p[1])] for p in pts], np.int16)
        zones = [z for z in level.zones if z["kind"] == "bombsite"]
        self.sites = [z["name"] for z in zones]
        self.site_id = np.full(n, -1, np.int8)
        for k, z in enumerate(zones):
            inside = np.all((pts >= np.array(z["min"]) - [0, 0, 0.5]) & (pts <= np.array(z["max"])), axis=1)
            self.site_id[inside] = k
        # visibility
        t1 = time.time()
        ii, jj = _pairs(pts, cfg.max_range)
        w = (n + 7) // 8
        self.vis = np.zeros((n, w), np.uint8)
        self.vis_low = np.zeros((n, w), np.uint8)
        soft_rows = []
        for eye, out in ((cfg.eye_stand, self.vis), (cfg.eye_crouch, self.vis_low)):
            a = pts[ii] + [0, 0, eye]
            b = pts[jj] + [0, 0, eye]
            hard, soft = occ.trace(a, b, cfg.step)
            clear = ~hard & (soft == 0)
            m = np.zeros((n, n), bool)
            m[ii[clear], jj[clear]] = True
            m |= m.T
            np.fill_diagonal(m, True)
            out[:] = np.packbits(m, axis=1)
            if eye == cfg.eye_stand:
                sel = ~hard & (soft > 0)
                soft_rows = np.stack([ii[sel], jj[sel], soft[sel].astype(np.int64) - 1], axis=1)
        self.soft_pairs = np.asarray(soft_rows, np.int32).reshape(-1, 3)
        t_vis = time.time() - t1
        # cover
        self.cover, self.cover_high = _cover(occ, pts, cfg)
        # point graph and spawn arrival times
        self.edges = _edges(pts, nav, cfg)
        for side in ("attack", "defend"):
            seeds = [s["pos"] for s in level.spawns if s["team"] == side]
            self.eta[side] = self._dijkstra([self._nearest_raw(p) for p in seeds]) / cfg.run_speed
        self._index()
        self.spots = _spots(self, level)
        self.build_info = (f"{n} points ({int(((kinds & 1) != 0).sum())} doorways), {len(ii)} pairs within "
                           f"{cfg.max_range:.0f} m, {int(np.unpackbits(self.vis, axis=1)[:, :n].sum() // 2)} "
                           f"visible, {len(self.soft_pairs)} through breakable walls, {len(self.edges) // 2} links; "
                           f"occupancy {t_occ:.1f}s, visibility {t_vis:.1f}s, total {time.time() - t0:.1f}s")
        if log:
            log(f"[tactical] {self.build_info}")
        return self

    @staticmethod
    def cache_key(level, nav_key: str, cfg: TacticalConfig) -> str:
        h = hashlib.sha1()
        h.update(json.dumps([VERSION, asdict(cfg), nav_key, level.data.get("bounds"),
                             level.data.get("practice_positions"), level.data.get("test_routes"),
                             [s["pos"] for s in level.spawns]]).encode())
        for c, size, hpr, _s in list(level.colliders) + [(p.center, p.size, p.hpr, "") for p in level.panel_specs]:
            h.update(np.asarray(list(c) + list(size) + list(hpr), np.float32).tobytes())
        return h.hexdigest()[:16]

    @classmethod
    def cached(cls, level, nav: NavMesh, cache_dir, name: str, log=None) -> "TacticalMap":
        cfg = TacticalConfig()
        key = cls.cache_key(level, f"{nav.node_count}:{len(nav.rects)}:{len(nav.portals)}", cfg)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{name}_{key}_tactical.npz"
        if path.exists():
            tm = cls.load(path)
            if tm is not None:
                if log:
                    log(f"[tactical] {name}: {tm.build_info} (cached)")
                return tm
        tm = cls.build(level, nav, cfg, log=log)
        try:
            tm.save(path)
        except OSError:
            pass
        return tm

    def save(self, path) -> None:
        meta = {"version": VERSION, "cfg": asdict(self.cfg), "areas": self.areas, "sites": self.sites,
                "info": self.build_info, "spots": self.spots}
        np.savez_compressed(path, meta=json.dumps(meta), pos=self.pos, kind=self.kind, rect=self.rect,
                            area_id=self.area_id, site_id=self.site_id, vis=self.vis, vis_low=self.vis_low,
                            cover=self.cover, cover_high=self.cover_high, soft_pairs=self.soft_pairs,
                            edges=self.edges, eta_attack=self.eta["attack"], eta_defend=self.eta["defend"])

    @classmethod
    def load(cls, path) -> "TacticalMap | None":
        try:
            data = np.load(path)
            meta = json.loads(str(data["meta"]))
        except (OSError, ValueError, KeyError):
            return None
        if meta.get("version") != VERSION:
            return None
        self = cls()
        self.cfg = TacticalConfig(**meta["cfg"])
        self.areas, self.sites, self.build_info = meta["areas"], meta["sites"], meta["info"]
        self.spots = {k: {kk: (vv if not isinstance(vv, list) else vv) for kk, vv in v.items()}
                      for k, v in meta["spots"].items()}
        for k in ("pos", "kind", "rect", "area_id", "site_id", "vis", "vis_low", "cover", "cover_high",
                  "soft_pairs", "edges"):
            setattr(self, k, data[k])
        self.eta = {"attack": data["eta_attack"], "defend": data["eta_defend"]}
        self._index()
        return self

    def _index(self) -> None:
        """2D buckets for nearest-point lookups; neighbour lists for the graph."""
        self.n = len(self.pos)
        self._base_vis = self.vis.copy()
        self._grid = {}
        self._pl = self.pos.tolist()
        self._near_cache: dict = {}
        for i, p in enumerate(self._pl):
            self._grid.setdefault((int(p[0] // 4.0), int(p[1] // 4.0)), []).append(i)
        self.edge_src = self.edges[:, 0].astype(np.int64) if len(self.edges) else np.zeros(0, np.int64)
        self.edge_dst = self.edges[:, 1].astype(np.int64) if len(self.edges) else np.zeros(0, np.int64)
        self.edge_len = self.edges[:, 2].astype(np.float64) if len(self.edges) else np.zeros(0)
        self.area_of_name = {a: i for i, a in enumerate(self.areas)}
        self._xy = self.pos[:, :2].astype(np.float64)
        self._z = self.pos[:, 2].astype(np.float64)
        self._soft_by_panel: dict[int, np.ndarray] = {}
        if len(self.soft_pairs):
            for pid in np.unique(self.soft_pairs[:, 2]).tolist():
                self._soft_by_panel[pid] = np.flatnonzero(self.soft_pairs[:, 2] == pid)

    def _nearest_raw(self, p) -> int:
        d = np.hypot(self.pos[:, 0] - p[0], self.pos[:, 1] - p[1]) + np.abs(self.pos[:, 2] - p[2]) * 3.0
        return int(np.argmin(d))

    def _dijkstra(self, sources: list[int]) -> np.ndarray:
        n = len(self.pos)
        src = self.edges[:, 0].astype(np.int64)
        dst = self.edges[:, 1].astype(np.int64)
        w = self.edges[:, 2].astype(np.float64)
        adj: list[list] = [[] for _ in range(n)]
        for a, b, c in zip(src.tolist(), dst.tolist(), w.tolist()):
            adj[a].append((b, c))
        dist = np.full(n, np.inf)
        heap = []
        for s in sources:
            dist[s] = 0.0
            heap.append((0.0, s))
        heapq.heapify(heap)
        while heap:
            d0, u = heapq.heappop(heap)
            if d0 > dist[u]:
                continue
            for v, c in adj[u]:
                nd = d0 + c
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(heap, (nd, v))
        dist[~np.isfinite(dist)] = 1e4
        return dist

    # ------------------------------------------------------------- queries
    def nearest(self, p, max_dz: float = 1.6) -> int:
        """Index of the tactical point nearest to a feet position p (same floor), or -1.
        Answers are cached per 0.25 m cell (the queries come from moving bots, many per tick)."""
        px, py, pz = float(p[0]), float(p[1]), float(p[2])
        key = (int(px * 4.0), int(py * 4.0), int(pz * 2.0), max_dz)
        hit = self._near_cache.get(key)
        if hit is not None:
            return hit
        gx, gy = int(px // 4.0), int(py // 4.0)
        best, best_d = -1, 1e9
        grid, pl = self._grid, self._pl
        for r in range(0, 4):
            for x in range(gx - r, gx + r + 1):
                for y in range(gy - r, gy + r + 1):
                    if r and abs(x - gx) != r and abs(y - gy) != r:
                        continue
                    for i in grid.get((x, y), ()):
                        q = pl[i]
                        dz = abs(q[2] - pz)
                        if dz > max_dz:
                            continue
                        d = (q[0] - px) ** 2 + (q[1] - py) ** 2 + dz * dz * 9.0
                        if d < best_d:
                            best, best_d = i, d
            if best >= 0 and r >= 1:
                break
        if len(self._near_cache) > 60000:
            self._near_cache.clear()
        self._near_cache[key] = best
        return best

    def sees(self, i: int, j: int, low: bool = False) -> bool:
        row = (self.vis_low if low else self.vis)[i]
        return bool(row[j >> 3] & (0x80 >> (j & 7)))

    def visible_mask(self, i: int, low: bool = False) -> np.ndarray:
        return np.unpackbits((self.vis_low if low else self.vis)[i])[:self.n].astype(bool)

    def in_view(self, eye, yaw_deg: float, fov_deg: float, max_range: float = 60.0) -> np.ndarray:
        """Points inside a horizontal view cone from eye (no occlusion test)."""
        dx = self._xy[:, 0] - eye[0]
        dy = self._xy[:, 1] - eye[1]
        dist = np.hypot(dx, dy)
        h = math.radians(yaw_deg)
        fx, fy = -math.sin(h), math.cos(h)
        cosang = (dx * fx + dy * fy) / np.maximum(dist, 1e-3)
        return ((cosang >= math.cos(math.radians(fov_deg * 0.5))) | (dist < 2.0)) & (dist <= max_range)

    def cover_toward(self, i: int, dx: float, dy: float, high: bool = False) -> bool:
        """Is point i covered (within 1.2 m) in the direction (dx, dy)?"""
        k = int(round((math.degrees(math.atan2(dy, dx)) % 360.0) / (360.0 / N_DIRS))) % N_DIRS
        bits = int((self.cover_high if high else self.cover)[i])
        # the neighbouring directions count too: cover is wider than a ray
        return bool(bits & ((1 << k) | (1 << ((k + 1) % N_DIRS)) | (1 << ((k - 1) % N_DIRS))))

    def points_near(self, p, radius: float, max_dz: float = 2.0) -> np.ndarray:
        d = np.hypot(self._xy[:, 0] - p[0], self._xy[:, 1] - p[1])
        return np.flatnonzero((d <= radius) & (np.abs(self._z - p[2]) <= max_dz))

    def site_name(self, i: int) -> str:
        s = int(self.site_id[i])
        return self.sites[s] if s >= 0 else ""

    def area(self, i: int) -> str:
        return self.areas[int(self.area_id[i])]

    # --------------------------------------------------------- destruction
    def on_panel(self, panel) -> None:
        """A destructible panel changed (None: every panel was restored)."""
        if panel is None:
            self.vis[:] = self._base_vis
            self._pending = []
            return
        rows = self._soft_by_panel.get(panel.index)
        if rows is not None:
            self._pending.extend(rows.tolist())

    def update(self, physics, budget: int = 48) -> int:
        """Re-test pairs behind changed panels with real rays (time-sliced)."""
        if not self._pending:
            return 0
        from engine.physics import MASK_SIGHT
        from panda3d.core import Point3
        done = 0
        eye = self.cfg.eye_stand
        while self._pending and done < budget:
            r = self._pending.pop()
            i, j, _ = (int(v) for v in self.soft_pairs[r])
            a, b = self.pos[i], self.pos[j]
            hit = physics.ray_cast(Point3(float(a[0]), float(a[1]), float(a[2]) + eye),
                                   Point3(float(b[0]), float(b[1]), float(b[2]) + eye), MASK_SIGHT)
            self._set(i, j, hit is None)
            done += 1
        return done

    def _set(self, i: int, j: int, on: bool) -> None:
        for a, b in ((i, j), (j, i)):
            if on:
                self.vis[a, b >> 3] |= np.uint8(0x80 >> (b & 7))
            else:
                self.vis[a, b >> 3] &= np.uint8(~(0x80 >> (b & 7)) & 0xFF)

    # --------------------------------------------------------------- spots
    def holds(self, site: str) -> list[dict]:
        return self.spots.get(site, {}).get("holds", [])

    def entries(self, site: str) -> list[int]:
        return self.spots.get(site, {}).get("entries", [])


# ------------------------------------------------------------------ helpers
def _sample_points(level, nav: NavMesh, cfg: TacticalConfig):
    """Grid points on every floor, doorways, hand-placed spots and lane waypoints."""
    cs = nav.cfg.cell
    col = nav.node_col.astype(np.int64)
    x = nav.x0 + (col % nav.nx + 0.5) * cs
    y = nav.y0 + (col // nav.nx + 0.5) * cs
    z = nav.node_z.astype(np.float64)
    sp = cfg.spacing
    gx = np.floor(x / sp).astype(np.int64)
    gy = np.floor(y / sp).astype(np.int64)
    gz = np.round(z / 1.6).astype(np.int64)
    # distance to the bucket centre; keep the nearest node of every (x, y, floor) bucket
    dc = (x - (gx + 0.5) * sp) ** 2 + (y - (gy + 0.5) * sp) ** 2
    key = (gx - gx.min()) * 1_000_000 + (gy - gy.min()) * 1000 + (gz - gz.min())
    order = np.lexsort((dc, key))
    first = np.r_[True, key[order][1:] != key[order][:-1]]
    pick = order[first]
    pts = [np.stack([x[pick], y[pick], z[pick]], axis=1)]
    kinds = [np.zeros(len(pick), np.int8)]
    extra, ek = [], []
    # doorways: narrow portals whose line runs into walls just beyond both ends
    seen_doors = set()
    for p in nav.portals.tolist():
        ax, ay, bx, by = p[2], p[3], p[4], p[5]
        zr = float(nav.rect_z[int(p[0])][0])
        vertical = abs(ax - bx) < 1e-6                 # the portal line runs along y
        lo, hi = (min(ay, by), max(ay, by)) if vertical else (min(ax, bx), max(ax, bx))
        width = hi - lo + cs
        if width > 2.2:
            continue
        line = ax if vertical else ay
        k = int(round((line - (nav.x0 if vertical else nav.y0)) / cs))
        a0 = int(math.floor((lo - (nav.y0 if vertical else nav.x0)) / cs))
        a1 = int(math.floor((hi - (nav.y0 if vertical else nav.x0)) / cs))

        def blocked(c, a):
            i, j = (c, a) if vertical else (a, c)
            return nav._column_node(i, j, zr, 0.6, 0.6) < 0
        door = any(blocked(c, a0 - 1) and blocked(c, a0 - 2) and blocked(c, a1 + 1) and blocked(c, a1 + 2)
                   for c in (k - 1, k))
        if not door:
            continue
        mx, my = (ax + bx) * 0.5, (ay + by) * 0.5
        key = (round(mx, 1), round(my, 1), round(zr))
        if key in seen_doors:
            continue
        seen_doors.add(key)
        q = nav.snap((mx, my, zr), search=3)
        if q is not None:
            extra.append(q)
            ek.append(1)
    for side in ("defend", "attack"):
        for e in level.data.get("practice_positions", {}).get(side, []):
            q = nav.snap((e[0], e[1], e[2]), search=6)
            if q is not None:
                extra.append(q)
                ek.append(2)
    for r in level.data.get("test_routes", []):
        z0 = r["start"][2] if len(r["start"]) > 2 else 0.0
        for wpt in [r["start"][:3]] + r["waypoints"]:
            q = nav.snap((wpt[0], wpt[1], wpt[2] if len(wpt) > 2 else z0), search=6)
            if q is not None:
                extra.append(q)
                ek.append(4)
    if extra:
        pts.append(np.array(extra, np.float64))
        kinds.append(np.array(ek, np.int8))
    allp = np.concatenate(pts)
    allk = np.concatenate(kinds)
    # drop near duplicates, keeping the special points (doorways, hand-placed spots, lane
    # waypoints) and merging their flags into the point that stays
    order = np.argsort(-(allk > 0).astype(np.int8), kind="stable")
    keep: list[int] = []
    flags = allk.copy()
    grid: dict[tuple, list] = {}
    for i in order.tolist():
        p = allp[i]
        cell = (int(p[0] // 2), int(p[1] // 2))
        close = -1
        for cx in (cell[0] - 1, cell[0], cell[0] + 1):
            for cy in (cell[1] - 1, cell[1], cell[1] + 1):
                for j in grid.get((cx, cy), ()):
                    q = allp[j]
                    lim = 1.0 if allk[i] or allk[j] else sp * 0.6
                    if abs(q[2] - p[2]) < 1.0 and math.hypot(q[0] - p[0], q[1] - p[1]) < lim:
                        close = j
                        break
                if close >= 0:
                    break
            if close >= 0:
                break
        if close < 0:
            keep.append(i)
            grid.setdefault(cell, []).append(i)
        else:
            flags[close] |= allk[i]
    keep.sort()
    return allp[keep], flags[keep]


def _pairs(pts: np.ndarray, max_range: float):
    n = len(pts)
    ii, jj = [], []
    for i in range(n - 1):
        d = np.linalg.norm(pts[i + 1:] - pts[i], axis=1)
        j = np.flatnonzero(d <= max_range) + i + 1
        ii.append(np.full(len(j), i, np.int64))
        jj.append(j)
    if not ii:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return np.concatenate(ii), np.concatenate(jj)


def _cover(occ: Occupancy, pts: np.ndarray, cfg: TacticalConfig):
    out = []
    n = len(pts)
    for eye in (cfg.eye_crouch, cfg.eye_stand):
        bits = np.zeros(n, np.uint16)
        for k in range(N_DIRS):
            ang = 2 * math.pi * k / N_DIRS
            a = pts + [0, 0, eye]
            b = a + [math.cos(ang) * cfg.cover_range, math.sin(ang) * cfg.cover_range, 0]
            hard, soft = occ.trace(a, b, cfg.step * 0.5)
            blocked = hard | (soft > 0)
            bits |= (blocked.astype(np.uint16) << k)
        out.append(bits)
    return out[0], out[1]


def _edges(pts: np.ndarray, nav: NavMesh, cfg: TacticalConfig) -> np.ndarray:
    n = len(pts)
    rows = []
    grid: dict[tuple, list] = {}
    for i, p in enumerate(pts.tolist()):
        grid.setdefault((int(p[0] // cfg.link_range), int(p[1] // cfg.link_range)), []).append(i)
    for i, p in enumerate(pts.tolist()):
        cx, cy = int(p[0] // cfg.link_range), int(p[1] // cfg.link_range)
        for x in (cx - 1, cx, cx + 1):
            for y in (cy - 1, cy, cy + 1):
                for j in grid.get((x, y), ()):
                    if j <= i:
                        continue
                    q = pts[j]
                    d = math.dist(p, q)
                    if d > cfg.link_range or abs(q[2] - p[2]) > 2.2:
                        continue
                    if nav.walkable_line(p, q) or nav.walkable_line(q, p):
                        rows.append((i, j, d))
                        rows.append((j, i, d))
                    else:
                        # round a corner or through a doorway: a short navmesh detour counts too
                        path = nav.find_path(p, q, max_expand=80)
                        if path is not None:
                            length = nav.path_length(path)
                            if length <= min(1.6 * d, cfg.link_range * 1.5):
                                rows.append((i, j, length))
                                rows.append((j, i, length))
    # join stranded groups (around corners) to their nearest reachable neighbour
    e = np.array(rows, np.float64).reshape(-1, 3)
    comp = _components(n, e)
    main = np.bincount(comp).argmax() if n else 0
    for c in set(comp.tolist()) - {main}:
        members = np.flatnonzero(comp == c)
        others = np.flatnonzero(comp == main)
        best = None
        for i in members[:6].tolist():
            d = np.linalg.norm(pts[others] - pts[i], axis=1)
            for j in others[np.argsort(d)[:3]].tolist():
                path = nav.find_path(tuple(pts[i]), tuple(pts[j]), max_expand=400)
                if path is not None:
                    length = nav.path_length(path)
                    if length < 12.0 and (best is None or length < best[2]):
                        best = (i, j, length)
        if best is not None:
            rows.append(best)
            rows.append((best[1], best[0], best[2]))
    return np.array(rows, np.float32).reshape(-1, 3)


def _components(n: int, e: np.ndarray) -> np.ndarray:
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b, _ in e.tolist():
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return np.array([find(i) for i in range(n)], np.int64)


def _spots(tm: TacticalMap, level) -> dict:
    """Entries, holds, off-angles and crossfire pairs per bomb site."""
    out = {}
    pts = tm.pos.astype(np.float64)
    n = len(pts)
    vis = np.unpackbits(tm.vis, axis=1)[:, :n].astype(bool)
    low = np.unpackbits(tm.vis_low, axis=1)[:, :n].astype(bool)
    eta_a, eta_d = tm.eta["attack"], tm.eta["defend"]
    zones = {z["name"]: z for z in level.zones if z["kind"] == "bombsite"}
    for k, name in enumerate(tm.sites):
        z = zones[name]
        zmin, zmax = np.array(z["min"], float), np.array(z["max"], float)
        dx = np.maximum(np.maximum(zmin[0] - pts[:, 0], 0), pts[:, 0] - zmax[0])
        dy = np.maximum(np.maximum(zmin[1] - pts[:, 1], 0), pts[:, 1] - zmax[1])
        dzone = np.hypot(dx, dy)
        inside = tm.site_id == k
        centre = (zmin + zmax) * 0.5
        # entries: where the attackers' lanes (the map's routes into the site that do not
        # start at the defenders' spawn) cross into the site, plus doorways on the site's
        # edge that attackers reach before defenders
        cand = []
        for lane in _lanes(level, name, z):
            prev = None
            for q in lane:
                if zmin[0] <= q[0] <= zmax[0] and zmin[1] <= q[1] <= zmax[1]:
                    if prev is not None:
                        a, b = np.array(prev[:2]), np.array(q[:2])
                        for t in np.linspace(0.0, 1.0, 21):           # last point outside the zone
                            c = a + (b - a) * t
                            if zmin[0] <= c[0] <= zmax[0] and zmin[1] <= c[1] <= zmax[1]:
                                break
                            hit = c
                        zq = q[2] if len(q) > 2 else zmin[2] + 0.5
                        d = np.hypot(pts[:, 0] - hit[0], pts[:, 1] - hit[1]) + np.abs(pts[:, 2] - zq) * 3.0
                        cand.append(int(np.argmin(d)))
                    break
                prev = q
        linked_in = np.zeros(n, bool)
        src, dst = tm.edges[:, 0].astype(np.int64), tm.edges[:, 1].astype(np.int64)
        linked_in[src[inside[dst]]] = True
        doors = np.flatnonzero(((tm.kind & 1) != 0) & (dzone > 0.2) & (dzone < 3.0) & linked_in & (eta_a < eta_d))
        entries = _thin(pts, cand + doors.tolist(), 4.0)
        # the direction attackers walk through each entry: from the entry towards the site
        holds = []
        margin = eta_a - eta_d                  # how much earlier defenders get there
        region = np.flatnonzero((inside & (margin > 0.0)) | ((dzone < 14.0) & (margin >= 8.0)))
        region = region[~np.isin(region, entries)]
        beyond = np.flatnonzero(~inside & (dzone > 6.0) & (eta_a < eta_d))   # attackers' side
        for h in region.tolist():
            # an entry this spot can hold: in sight, and behind it from the attackers' side
            # (they must pass the entry before they could reach the spot)
            seen = [e for e in entries if vis[h, e]]
            if any(eta_a[h] < eta_a[e] + 0.5 for e in seen):
                continue                # in front of an entry it can see: the attackers' side
            if not seen:
                continue
            score = 0.0
            best_e, best_angle = -1, 0.0
            for e in seen:
                d = float(np.linalg.norm(pts[h] - pts[e]))
                if d < 4.0:
                    continue
                w = 1.0 if 6.0 <= d <= 25.0 else 0.5
                # cover towards the entry: crouched hidden but standing visible, or a cover direction
                v = pts[e] - pts[h]
                covered = tm.cover_toward(h, v[0], v[1]) or (vis[h, e] and not low[h, e])
                score += w * (1.4 if covered else 1.0)
                walk = centre - pts[e]
                ang = _angle(walk[:2], (pts[h] - pts[e])[:2])
                if ang > best_angle:
                    best_e, best_angle = e, ang
            if score <= 0.0:
                continue
            exposure = int(vis[h, beyond].sum()) if len(beyond) else 0
            score -= 0.02 * exposure
            score += 0.3 if tm.kind[h] & 2 else 0.0
            depth = float(np.linalg.norm(pts[h] - pts[best_e])) if best_e >= 0 else 0.0
            holds.append({"i": int(h), "score": round(float(score), 3), "exposure": exposure,
                          "sees": [int(e) for e in seen], "off_angle": bool(best_angle > 50.0 or depth > 18.0),
                          "crouch": bool(any(not low[h, e] for e in seen) and any(vis[h, e] for e in seen))})
        holds.sort(key=lambda d: -d["score"])
        picked = []
        for hd in holds:
            if all(np.linalg.norm(pts[hd["i"]] - pts[o["i"]]) > 3.0 for o in picked):
                picked.append(hd)
            if len(picked) >= 16:
                break
        cross = []
        for a_i, a in enumerate(picked):
            for b in picked[a_i + 1:]:
                for e in set(a["sees"]) & set(b["sees"]):
                    va = (pts[a["i"]] - pts[e])[:2]
                    vb = (pts[b["i"]] - pts[e])[:2]
                    if _angle(va, vb) >= 60.0:
                        cross.append([a["i"], b["i"], int(e)])
                        break
        # forward spots: early-round information plays on the attackers' way in (defenders get
        # there 2-8 s first), with cover, seeing a lane point
        forward = []
        lane_pts = np.flatnonzero((tm.kind & 4) != 0)
        for f in np.flatnonzero((margin > 2.0) & (margin < 8.0) & (dzone < 30.0) & ~inside).tolist():
            sees = [int(l) for l in lane_pts if vis[f, l] and 6.0 < np.linalg.norm(pts[f] - pts[l]) < 35.0]
            if sees and tm.cover[f]:
                forward.append({"i": int(f), "sees": sees, "margin": round(float(margin[f]), 1)})
        forward.sort(key=lambda d: (-len(d["sees"]), -d["margin"]))
        out[name] = {"entries": [int(e) for e in entries], "holds": picked, "crossfires": cross,
                     "forward": _thin_dicts(pts, forward, 4.0)[:8]}
    return out


def _lanes(level, site: str, zone: dict) -> list[list]:
    """Attack routes of the map ending in a site (as ai/tactics.build_lanes)."""
    dspawn = [s["pos"] for s in level.spawns if s["team"] == "defend"]
    cx = sum(p[0] for p in dspawn) / max(len(dspawn), 1)
    cy = sum(p[1] for p in dspawn) / max(len(dspawn), 1)
    out = []
    for r in level.data.get("test_routes", []):
        pts = [tuple(r["start"][:3])] + [tuple(w) for w in r["waypoints"]]
        if math.hypot(pts[0][0] - cx, pts[0][1] - cy) < 15.0:
            continue
        end = pts[-1]
        if zone["min"][0] - 0.5 <= end[0] <= zone["max"][0] + 0.5 and zone["min"][1] - 0.5 <= end[1] <= zone["max"][1] + 0.5:
            out.append(pts)
    return out


def _thin_dicts(pts: np.ndarray, items: list[dict], min_d: float) -> list[dict]:
    out: list[dict] = []
    for d in items:
        if all(np.linalg.norm(pts[d["i"]] - pts[o["i"]]) >= min_d for o in out):
            out.append(d)
    return out


def _thin(pts: np.ndarray, idx: list[int], min_d: float) -> list[int]:
    out: list[int] = []
    for i in idx:
        if all(np.linalg.norm(pts[i] - pts[j]) >= min_d for j in out):
            out.append(i)
    return out


def _angle(a, b) -> float:
    na = math.hypot(a[0], a[1])
    nb = math.hypot(b[0], b[1])
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    c = (a[0] * b[0] + a[1] * b[1]) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))
