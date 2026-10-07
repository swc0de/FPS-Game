"""Path queries for the v2 bots: resumable A* under a per-tick budget, cached.

``NavMesh.find_path`` (ai/navmesh.py) runs A* to completion, twice, inside
the caller's tick; on long lanes that took up to 16 ms (docs/baseline).
The v2 bots ask a ``PathService`` instead:

* A request returns the path at once when the portal chain between the two
  navmesh polygons is cached (any start and goal inside those polygons
  share it; only the funnel runs per request), else ``PENDING``.
* Pending searches are ``AStarJob``s: the same A* over the same polygons
  and portals (including the off-mesh links through blown walls), but it can
  stop after a number of node expansions and continue on the next tick.
  ``update`` spends at most ``budget`` expansions per tick, so the cost is
  bounded and - counted in expansions, not milliseconds - deterministic.
* Cost biases are named by a small integer key (route variety, avoiding
  known danger), so bots with the same key share cached results.
* The cache is dropped when the navmesh's dynamic links change (a wall
  opened, a new round).
"""
from __future__ import annotations

import heapq
import math
from collections import OrderedDict, deque

from ai.navmesh import string_pull
from ai.steering import PathFollower

PENDING = "pending"


class AStarJob:
    """The navmesh's A* (``NavMesh.find_path``) in resumable steps: both of its portal
    scorings (closest point, midpoint) one after the other, keeping the shorter route."""

    MODES = ("closest", "mid")

    def __init__(self, nav, sr: int, gr: int, sxy, gxy, cost_bias=None):
        self.nav = nav
        self.sr, self.gr = sr, gr
        self.sxy, self.gxy = sxy, gxy
        self.bias = cost_bias
        self.done = False
        self.chain = None
        self.expanded = 0
        self._mode = 0
        self._found: list = []
        self._start()

    def _start(self) -> None:
        sr, sxy = self.sr, self.sxy
        self.best = {sr: 0.0}
        self.at = {sr: sxy}
        self.came: dict[int, tuple[int, int]] = {}
        self.heap = [(math.dist(sxy, self.gxy), 0, sr)]
        self.tie = 1

    def step(self, budget: int) -> int:
        """Expand up to ``budget`` polygons (over both scorings); returns how many were used."""
        used = 0
        while not self.done and used < budget:
            used += self._expand(budget - used)
        return used

    def _expand(self, budget: int) -> int:
        nav = self.nav
        P = nav._portal_list
        crouch = nav._rect_crouch
        adj = nav.adj
        gxy = self.gxy
        used = 0
        heap, best, at, came = self.heap, self.best, self.at, self.came
        mid = self.MODES[self._mode] == "mid"
        while heap and used < budget:
            f, _, r = heapq.heappop(heap)
            if r == self.gr:
                self._finish()
                return used
            g = best[r]
            if f - math.dist(at[r], gxy) > g + 1e-6:
                continue
            used += 1
            self.expanded += 1
            px, py = at[r]
            for k in adj[r]:
                p = P[k]
                s = int(p[1])
                ax, ay, bx, by = p[2], p[3], p[4], p[5]
                ex, ey = bx - ax, by - ay
                if mid:
                    t = 0.5
                else:
                    L2 = ex * ex + ey * ey
                    t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * ex + (py - ay) * ey) / L2))
                qx, qy = ax + ex * t, ay + ey * t
                cost = math.hypot(qx - px, qy - py) + 0.05
                mult = 2.6 if crouch[s] else 1.0
                if self.bias is not None:
                    mult *= self.bias(s)
                ng = g + cost * mult
                if ng < best.get(s, 1e18):
                    best[s] = ng
                    at[s] = (qx, qy)
                    came[s] = (r, k)
                    heapq.heappush(heap, (ng + math.hypot(gxy[0] - qx, gxy[1] - qy), self.tie, s))
                    self.tie += 1
        if not heap:
            self._finish()
        return used

    def _finish(self) -> None:
        """One scoring done: start the next, or keep the shorter of the two routes."""
        if self.gr not in self.came:
            self.done = True                     # unreachable either way
            self.chain = None
            return
        chain = []
        r = self.gr
        while r != self.sr:
            pr, k = self.came[r]
            chain.append(k)
            r = pr
        chain.reverse()
        self._found.append(chain)
        self._mode += 1
        if self._mode < len(self.MODES):
            self._start()
            return
        self.done = True
        nav = self.nav

        def length(ch):
            pts = string_pull(self.sxy, self.gxy, nav._oriented_portals(ch))
            return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        best = None
        for ch in self._found:
            if best is None or length(ch) < length(best) - 1e-6:
                best = ch
        self.chain = best


def assemble(nav, start, goal, chain) -> list:
    """Corners of the shortest path through a portal chain, with floor heights."""
    sn = nav.locate(start)
    gn = nav.locate(goal)
    sxy = (float(start[0]), float(start[1]))
    gxy = (float(goal[0]), float(goal[1]))
    pts2 = string_pull(sxy, gxy, nav._oriented_portals(chain)) if chain else [sxy, gxy]
    z = nav._z[sn]
    out = [(sxy[0], sxy[1], z)]
    j = 0
    for x, y in pts2[1:-1]:
        rects = None
        while j < len(chain):
            p = nav._portal_list[chain[j]]
            j += 1
            if (p[2] == x and p[3] == y) or (p[4] == x and p[5] == y):
                rects = (int(p[0]), int(p[1]))
                break
        zz = nav._z_in_rects(x, y, rects) if rects else None
        if zz is None:
            m = nav.locate((x, y, z + 0.3), search=2)
            zz = nav._z[m] if m >= 0 else z
        z = zz
        out.append((x, y, z))
    out.append((gxy[0], gxy[1], nav._z[gn]))
    return out


class PathService:
    def __init__(self, nav, budget: int = 260, cache_size: int = 600):
        self.nav = nav
        self.budget = budget
        self.cache: OrderedDict = OrderedDict()
        self.jobs: dict[tuple, AStarJob] = {}
        self.queue: deque = deque()
        self.biases: dict[int, object] = {}
        self.cache_size = cache_size
        self._links_sig = None
        self.stats = {"requests": 0, "hits": 0, "searches": 0, "expansions": 0}

    def set_bias(self, key: int, fn) -> None:
        """Register (or replace) a cost bias; results computed with the old one are dropped."""
        self.biases[key] = fn
        for k in [k for k in self.cache if k[2] == key]:
            del self.cache[k]

    def _sig(self):
        return len(self.nav._portal_list), sum(len(v) for v in self.nav._dyn.values()), len(self.nav._dyn)

    def request(self, start, goal, bias_key: int = 0):
        """A path (list of (x, y, z)), None if unreachable, or PENDING."""
        self.stats["requests"] += 1
        nav = self.nav
        sn = nav.locate(start)
        gn = nav.locate(goal)
        if sn < 0 or gn < 0:
            return None
        sr, gr = int(nav.rect_of[sn]), int(nav.rect_of[gn])
        if sr == gr:
            self.stats["hits"] += 1
            return assemble(nav, start, goal, [])
        key = (sr, gr, bias_key)
        if key in self.cache:
            self.stats["hits"] += 1
            self.cache.move_to_end(key)
            chain = self.cache[key]
            return None if chain is None else assemble(nav, start, goal, chain)
        if key not in self.jobs:
            sxy = (float(start[0]), float(start[1]))
            gxy = (float(goal[0]), float(goal[1]))
            self.jobs[key] = AStarJob(nav, sr, gr, sxy, gxy, self.biases.get(bias_key))
            self.queue.append(key)
            self.stats["searches"] += 1
        return PENDING

    def update(self) -> int:
        sig = self._sig()
        if sig != self._links_sig:
            self._links_sig = sig
            self.cache.clear()
            # running searches may use a removed link: start them again
            for key in list(self.jobs):
                j = self.jobs[key]
                self.jobs[key] = AStarJob(self.nav, j.sr, j.gr, j.sxy, j.gxy, j.bias)
        left = self.budget
        while self.queue and left > 0:
            key = self.queue[0]
            job = self.jobs[key]
            left -= max(job.step(left), 1)
            if job.done:
                self.queue.popleft()
                del self.jobs[key]
                self.cache[key] = job.chain
                self.stats["expansions"] += job.expanded
                if len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
        return self.budget - left


class FollowerV2(PathFollower):
    """PathFollower whose re-plan after getting stuck goes through the service."""

    def __init__(self, nav, service: PathService):
        super().__init__(nav)
        self.service = service
        self.bias_key = 0

    def _repath(self, pos) -> None:
        if self.goal is None:
            return
        self.repaths += 1
        path = self.service.request((pos.x, pos.y, pos.z), self.goal, self.bias_key)
        if path is None:
            self.failed = True
            self.path = []
        elif path != PENDING:
            self._set(path)
