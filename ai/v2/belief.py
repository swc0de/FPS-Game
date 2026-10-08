"""Where unseen enemies could be by now: the possibility field (v2 bots).

Each team keeps one picture of the enemy team over the tactical points
(ai/v2/tactical_map.py). For every point, ``ready[p]`` is the earliest time
an enemy could be there, given everything the team knows:

* **Round start.** Nobody can be anywhere before running there from their
  spawn: ``ready = start + eta[enemy spawn]`` (attackers leave their spawn
  when the round goes live; defenders already move during preparation).
* **Facts** (a sighting, a sound, a callout, a ping) say an enemy was within
  ``radius`` of a position at time t: those points get ``ready <= t``.
* **Spreading.** Enemies move: ``ready[q] <= ready[p] + dist(p, q) / speed``
  along the point graph (vectorised relaxation, sorted by destination so one
  ``minimum.reduceat`` does every edge).
* **Clearing.** A point a teammate looks at right now (in its view cone, in
  the precomputed line of sight, not behind smoke) holds nobody now. It
  becomes a barrier: an enemy can only arrive there after the moment it was
  seen empty (``ready >= last_seen``), coming from somewhere unseen. So a
  corner that was just checked fills in again only from the areas next to
  it, at walking or running speed.

Enemies that were seen or heard recently get their own *track*: the same
field, seeded only from their latest fact. A track stays sharp for a while
(``track_time``) and then dissolves into the general field. With every
living enemy tracked, the rest of the map is known to be empty except where
a track can reach - "all five are at B" means A is clear for now.

``danger`` turns this into a weight per point: tracked enemies spread over
the points they could have reached, untracked ones over everything still
possible, weighted by priors (defenders like holding spots in sites). The
brains use it to pre-aim, clear corners in order, decide where it is safe
to walk, and rotate.

Updates are time-sliced: ``step`` advances one field by one hop per call
(the team strategy calls it every fourth tick), so no single tick does all
the work.
"""
from __future__ import annotations

import math

import numpy as np

INF = 1e9


class Field:
    """Earliest-arrival field over the tactical points."""

    def __init__(self, tm, speed: float):
        self.tm = tm
        self.speed = speed
        self.ready = np.full(tm.n, INF)
        self.seen = np.full(tm.n, -INF)          # last time each point was seen empty

    def seed(self, idx, t: float) -> None:
        self.ready[idx] = np.minimum(self.ready[idx], t)

    def relax(self, graph, iters: int = 2) -> None:
        src, dst, dst_u, starts, length = graph[:5]
        if len(src) == 0:
            return
        travel, half, a, b = _scratch(graph, self.speed)
        for _ in range(iters):
            # an enemy possible at src walks to dst; it cannot be in dst before dst was
            # last seen empty, and needs part of the way after that (points are ~3 m regions)
            # (the same arithmetic as max(ready[src] + travel, seen[dst] + 0.5 travel), into
            # preallocated buffers: this runs every few ticks over every edge)
            np.take(self.ready, src, out=a)
            a += travel
            np.take(self.seen, dst, out=b)
            b += half
            np.maximum(a, b, out=a)
            best = np.minimum.reduceat(a, starts)
            np.minimum(self.ready[dst_u], best, out=best)
            self.ready[dst_u] = best

    def clear(self, mask: np.ndarray, now: float) -> None:
        self.seen[mask] = now
        self.ready[mask] = INF

    def possible(self, now: float) -> np.ndarray:
        return self.ready <= now


class Track:
    def __init__(self, tm, enemy, fact, speed: float):
        self.enemy = enemy
        self.fact = fact
        self.field = Field(tm, speed)
        self.born = fact.time
        self.updated = fact.time
        idx = tm.points_near(fact.pos, max(fact.radius, 1.6), max_dz=2.2)
        if len(idx) == 0:
            i = tm.nearest(fact.pos)
            idx = np.array([i] if i >= 0 else [], np.int64)
        self.field.seed(idx, fact.time)


class PossibilityField:
    def __init__(self, tm, enemy_side: str, run_speed: float = 5.4, walk_speed: float = 2.75,
                 track_time: float = 20.0, max_tracks: int = 5):
        self.tm = tm
        self.enemy_side = enemy_side
        self.run_speed = run_speed
        self.walk_speed = walk_speed
        self.track_time = track_time
        self.max_tracks = max_tracks
        self.general = Field(tm, run_speed)
        self.tracks: dict[int, Track] = {}
        self.enemies_alive = 5
        self.prior = np.ones(tm.n)
        self._graph = _sorted_graph(tm)
        self._turn = 0
        self.now = 0.0

    # --------------------------------------------------------------- round
    def reset(self, start: float, enemies_alive: int, prior: np.ndarray | None = None) -> None:
        """New round: enemies leave their spawn at ``start``."""
        self.general = Field(self.tm, self.run_speed)
        self.general.ready = start + self.tm.eta[self.enemy_side]
        self.tracks = {}
        self.enemies_alive = enemies_alive
        if prior is not None:
            self.prior = prior
        self.now = start

    # --------------------------------------------------------------- input
    def add_fact(self, fact) -> None:
        """An enemy (``fact.enemy``: an id, or None if unknown) was near fact.pos at fact.time."""
        key = fact.enemy if fact.enemy is not None else -len(self.tracks) - 1000
        old = self.tracks.get(key)
        if old is not None and old.fact.time >= fact.time:
            return
        speed = self.walk_speed if fact.source == "sight" and fact.speed < 3.0 else self.run_speed
        tr = Track(self.tm, fact.enemy, fact, speed)
        # catch up with the time that has passed since the fact
        tr.field.seen = self.general.seen.copy()
        tr.field.relax(self._graph, iters=3)
        self.tracks[key] = tr
        if len(self.tracks) > self.max_tracks:
            oldest = min(self.tracks, key=lambda k: self.tracks[k].updated)
            self._dissolve(oldest)
        # an enemy was here, so the general field must allow it as well
        self.general.seed(np.flatnonzero(tr.field.ready <= fact.time + 1e-6), fact.time)

    def enemy_died(self, enemy) -> None:
        self.tracks.pop(enemy, None)
        self.enemies_alive = max(self.enemies_alive - 1, 0)

    def observe(self, mask: np.ndarray, now: float) -> None:
        """Points the team sees right now (empty of enemies, or the enemies there are facts)."""
        self.now = now
        self.general.clear(mask, now)
        for tr in self.tracks.values():
            tr.field.clear(mask, now)

    # ---------------------------------------------------------------- tick
    def step(self, now: float) -> None:
        """Advance one field (time-sliced)."""
        self.now = now
        fields = [self.general] + [t.field for t in self.tracks.values()]
        self._turn = (self._turn + 1) % len(fields)
        fields[self._turn].relax(self._graph, iters=1)
        if self._turn == 0:
            for key in [k for k, t in self.tracks.items() if now - t.fact.time > self.track_time]:
                self._dissolve(key)

    def _dissolve(self, key) -> None:
        tr = self.tracks.pop(key, None)
        if tr is not None:
            np.minimum(self.general.ready, tr.field.ready, out=self.general.ready)

    # -------------------------------------------------------------- queries
    def possible(self, now: float | None = None) -> np.ndarray:
        now = self.now if now is None else now
        m = self.general.possible(now)
        for tr in self.tracks.values():
            m |= tr.field.possible(now)
        return m

    def danger(self, now: float | None = None) -> np.ndarray:
        """Expected number of enemies per point (sums to about the number of living enemies)."""
        now = self.now if now is None else now
        out = np.zeros(self.tm.n)
        tracked = 0
        for tr in self.tracks.values():
            m = tr.field.possible(now)
            k = int(m.sum())
            if k:
                # the longer ago, the more it has spread; nearer points stay likelier
                age = max(now - tr.fact.time, 0.0)
                w = np.where(m, 1.0 / (1.0 + (tr.field.ready - tr.fact.time).clip(0) / (2.0 + age)), 0.0)
                out += w / max(w.sum(), 1e-9)
                tracked += 1
        free = max(self.enemies_alive - tracked, 0)
        if free:
            m = self.general.possible(now)
            w = np.where(m, self.prior, 0.0)
            s = w.sum()
            if s > 0:
                out += free * w / s
        return out

    def tracked(self) -> int:
        return len(self.tracks)

    def age(self, now: float | None = None) -> np.ndarray:
        """Seconds since each point was last seen (large = never)."""
        now = self.now if now is None else now
        return now - self.general.seen


def _scratch(graph, speed: float):
    """Per graph and speed: the travel time of every edge, half of it, and two work buffers."""
    cache = graph[5]
    hit = cache.get(speed)
    if hit is None:
        travel = graph[4] / speed
        hit = (travel, 0.5 * travel, np.empty_like(travel), np.empty_like(travel))
        cache[speed] = hit
    return hit


def _sorted_graph(tm):
    """Edges sorted by destination, with the start index of every destination group."""
    src = tm.edge_src
    dst = tm.edge_dst
    length = tm.edge_len
    if len(src) == 0:
        z = np.zeros(0, np.int64)
        return z, z, z, z, np.zeros(0), {}
    order = np.argsort(dst, kind="stable")
    src, dst, length = src[order], dst[order], length[order]
    first = np.r_[True, dst[1:] != dst[:-1]]
    starts = np.flatnonzero(first)
    return src, dst, dst[starts], starts, length, {}


def view_mask(tm, eye, yaw: float, fov: float, max_range: float = 60.0, smokes=()) -> np.ndarray:
    """Tactical points a bot at ``eye`` looking along ``yaw`` sees now: inside its
    view cone, in the precomputed line of sight of its nearest point, and not
    behind a smoke cloud."""
    i = tm.nearest((eye[0], eye[1], eye[2] - 1.6))
    if i < 0:
        return np.zeros(tm.n, bool)
    m = tm.seen_in_view(i, eye, yaw, fov, max_range)
    m[i] = True
    for c, r in smokes:
        idx = np.flatnonzero(m)
        dx = tm._xy[idx, 0] - eye[0]
        dy = tm._xy[idx, 1] - eye[1]
        dist = np.hypot(dx, dy)
        cx, cy = c[0] - eye[0], c[1] - eye[1]
        cd = math.hypot(cx, cy)
        if cd < 1e-3:
            return np.zeros(tm.n, bool)
        # angular half-width of the cloud seen from the eye
        half = math.asin(min(r / cd, 1.0))
        cosang = (dx * cx + dy * cy) / np.maximum(dist * cd, 1e-6)
        behind = (cosang > math.cos(half)) & (dist > cd - r)
        m[idx[behind]] = False
    return m
