"""Path following for bots: navmesh path -> per-tick movement intent.

The follower walks the funnel path corner to corner. It skips ahead when
a later corner can be reached in a straight line (smooths the small kinks
the grid leaves), crouches before crouch-only areas, and detects being
stuck (no progress for a while): first it tries a hop and a sidestep,
then it asks for a new path.
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

ARRIVE_RADIUS = 0.35
CORNER_RADIUS = 0.45
STUCK_TIME = 0.8


class PathFollower:
    def __init__(self, nav):
        self.nav = nav
        self.path: list[tuple[float, float, float]] = []
        self.index = 0
        self.goal: Point3 | None = None
        self.arrived = True
        self.failed = False
        self._stuck_t = 0.0
        self._best_dist = 1e9
        self._unstick = 0.0
        self._side = 1.0
        self._skip_check = 0
        self.repaths = 0
        self.stuck_events = 0

    # ---------------------------------------------------------------- paths
    def go_to(self, start, goal, cost_bias=None) -> bool:
        path = self.nav.find_path(start, goal, cost_bias=cost_bias)
        self.goal = Point3(*goal)
        self.repaths = 0
        if path is None:
            self.path = []
            self.arrived = False
            self.failed = True
            return False
        self._set(path)
        return True

    def follow(self, points) -> None:
        """Use a precomputed path (list of (x, y, z))."""
        self.goal = Point3(*points[-1])
        self._set(list(points))

    def _set(self, path) -> None:
        self.path = path
        self.index = 1 if len(path) > 1 else 0
        self.arrived = len(path) <= 1
        self.failed = False
        self._stuck_t = 0.0
        self._best_dist = 1e9

    def stop(self) -> None:
        self.path = []
        self.arrived = True
        self.goal = None

    @property
    def active(self) -> bool:
        return bool(self.path) and not self.arrived

    def remaining(self, pos) -> float:
        if not self.active:
            return 0.0
        d = math.dist((pos[0], pos[1]), self.path[self.index][:2])
        for k in range(self.index, len(self.path) - 1):
            d += math.dist(self.path[k][:2], self.path[k + 1][:2])
        return d

    def look_ahead(self, pos, distance: float = 3.0) -> Point3 | None:
        """Point ``distance`` metres further along the path (where a bot looks while moving)."""
        if not self.active:
            return None
        p = (pos[0], pos[1], pos[2])
        left = distance
        for k in range(self.index, len(self.path)):
            q = self.path[k]
            seg = math.dist(p[:2], q[:2])
            if seg >= left:
                t = left / max(seg, 1e-6)
                return Point3(p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t)
            left -= seg
            p = q
        return Point3(*self.path[-1])

    # ----------------------------------------------------------------- tick
    def update(self, dt: float, pos: Point3, speed: float):
        """Returns (wish_dir, crouch, jump) for this tick."""
        if not self.active:
            return Vec3(0, 0, 0), False, False
        # advance through reached corners
        while True:
            target = self.path[self.index]
            last = self.index == len(self.path) - 1
            d = math.hypot(target[0] - pos.x, target[1] - pos.y)
            r = ARRIVE_RADIUS if last else CORNER_RADIUS
            if d < r and abs(target[2] - pos.z) < 1.2:
                if last:
                    self.arrived = True
                    return Vec3(0, 0, 0), False, False
                self.index += 1
                self._best_dist = 1e9
                continue
            break
        # skip ahead if the corner after this one is in a straight walk
        self._skip_check -= 1
        if self._skip_check <= 0 and self.index < len(self.path) - 1:
            self._skip_check = 8
            nxt = self.path[self.index + 1]
            if self.nav.walkable_line((pos.x, pos.y, pos.z), nxt):
                self.index += 1
                self._best_dist = 1e9
                target = self.path[self.index]
                d = math.hypot(target[0] - pos.x, target[1] - pos.y)
        wish = Vec3(target[0] - pos.x, target[1] - pos.y, 0)
        if wish.lengthSquared() > 1e-8:
            wish.normalize()

        # stuck detection: the distance to the current corner must keep shrinking
        jump = False
        if d < self._best_dist - 0.05:
            self._best_dist = d
            self._stuck_t = 0.0
        else:
            self._stuck_t += dt
        if self._unstick > 0:
            self._unstick -= dt
            side = Vec3(-wish.y, wish.x, 0) * self._side
            wish = (wish * 0.4 + side).normalized()
        elif self._stuck_t > STUCK_TIME:
            self.stuck_events += 1
            self._stuck_t = 0.0
            self._best_dist = 1e9
            if self.stuck_events % 3 == 0:
                self._repath(pos)
            else:
                self._unstick = 0.35
                self._side = -self._side
                jump = True
        ahead = Point3(pos.x + wish.x * 0.55, pos.y + wish.y * 0.55, pos.z)
        crouch = self.nav.is_crouch(pos) or self.nav.is_crouch(ahead)
        return wish, crouch, jump

    def _repath(self, pos: Point3) -> None:
        if self.goal is None:
            return
        self.repaths += 1
        path = self.nav.find_path((pos.x, pos.y, pos.z), self.goal)
        if path is None:
            self.failed = True
            self.path = []
            return
        self._set(path)
