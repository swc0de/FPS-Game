"""Bot aiming: human-like view control (pure Python, unit tested).

* The view turns towards a target with an exponential "snap" (fast first,
  slowing near the target) limited by a maximum turn speed - flicks
  overshoot nothing and small corrections stay smooth.
* When a bot acquires a target its aim carries an *error* offset whose size
  depends on difficulty, distance and how fast the target and the bot are
  moving. The error shrinks while the bot keeps tracking the same target
  (``error_decay`` per second) down to ``min_error``: first shots at a
  surprised bot miss more, a bot that has been tracking you is accurate.
* While spraying, the bot pulls against the weapon's recoil pattern by
  ``spray_control`` of the offset (expert bots control sprays, easy bots
  spray into the sky).
* ``on_target`` tells the brain when the crosshair is close enough to the
  target (scaled by its angular size) to pull the trigger.
"""
from __future__ import annotations

import math
import random


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def angles_to(dx: float, dy: float, dz: float) -> tuple[float, float]:
    """Panda3D heading/pitch that look along (dx, dy, dz)."""
    yaw = math.degrees(math.atan2(-dx, dy))
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return yaw, pitch


class AimController:
    def __init__(self, profile: dict, rng: random.Random | None = None):
        self.p = profile
        self.rng = rng or random.Random()
        self.yaw = 0.0
        self.pitch = 0.0
        self.err_x = 0.0
        self.err_y = 0.0
        self.err_scale = 0.0
        self._target_id = None
        self._jitter_t = 0.0

    # --------------------------------------------------------------- view
    def turn_towards(self, yaw: float, pitch: float, dt: float, speed_scale: float = 1.0) -> float:
        """Rotate the view towards (yaw, pitch); returns the remaining angle."""
        dy = wrap180(yaw - self.yaw)
        dp = pitch - self.pitch
        dist = math.hypot(dy, dp)
        if dist < 1e-4:
            return 0.0
        k = 1.0 - math.exp(-float(self.p.get("snap", 10.0)) * dt)
        step = dist * k
        max_step = float(self.p.get("turn_speed", 400.0)) * speed_scale * dt
        step = min(step, max_step)
        # never stall on tiny remainders
        step = max(step, min(dist, 4.0 * dt))
        f = step / dist
        self.yaw = (self.yaw + dy * f) % 360.0
        self.pitch = max(-89.0, min(89.0, self.pitch + dp * f))
        return dist - step

    def look_at(self, eye, point, dt: float, speed_scale: float = 1.0) -> float:
        yaw, pitch = angles_to(point[0] - eye[0], point[1] - eye[1], point[2] - eye[2])
        return self.turn_towards(yaw, pitch, dt, speed_scale)

    # ------------------------------------------------------------- errors
    def acquire(self, target_id, distance: float, target_speed: float, own_speed: float) -> None:
        """New target: roll a fresh aim error."""
        if target_id == self._target_id:
            return
        self._target_id = target_id
        base = float(self.p.get("aim_error", 2.0))
        scale = base * (1.0 + target_speed / 6.0) * (1.0 + own_speed / 4.0)
        scale *= 0.7 + 0.3 * min(distance / 25.0, 1.5)
        a = self.rng.uniform(0, 2 * math.pi)
        r = scale * (0.5 + 0.5 * self.rng.random())
        self.err_x = math.cos(a) * r
        self.err_y = math.sin(a) * r * 0.7
        self.err_scale = scale

    def release(self) -> None:
        self._target_id = None

    def update_error(self, dt: float, target_speed: float = 0.0, distance: float = 0.0) -> None:
        decay = math.exp(-float(self.p.get("error_decay", 1.5)) * dt)
        # steadiness has limits: the residual error grows with distance, so
        # long fights take longer and are less one-sided
        floor = float(self.p.get("min_error", 0.3)) * (1.0 + target_speed / 5.0) * (1.0 + distance / 30.0)
        mag = math.hypot(self.err_x, self.err_y)
        if mag > floor:
            self.err_x *= decay
            self.err_y *= decay
        # a little hand wobble, re-rolled a few times per second
        self._jitter_t -= dt
        if self._jitter_t <= 0:
            self._jitter_t = self.rng.uniform(0.15, 0.35)
            j = floor * 0.5
            self.err_x += self.rng.uniform(-j, j)
            self.err_y += self.rng.uniform(-j, j) * 0.6
            mag = math.hypot(self.err_x, self.err_y)
            cap = max(self.err_scale, floor) * 1.2
            if mag > cap:
                self.err_x *= cap / mag
                self.err_y *= cap / mag

    # ------------------------------------------------------------- tracking
    def track(self, eye, point, dt: float, recoil=(0.0, 0.0), speed_scale: float = 1.0) -> float:
        """Aim at point (+ current error), pulling against the recoil offset.
        Returns the angular distance (deg) between the bullet line and the point."""
        yaw, pitch = angles_to(point[0] - eye[0], point[1] - eye[1], point[2] - eye[2])
        c = float(self.p.get("spray_control", 0.5))
        rx, ry = recoil
        # bullets land at view + recoil offset (x right, y up): aim the other way
        aim_yaw = yaw - self.err_x + rx * c
        aim_pitch = pitch + self.err_y - ry * c
        self.turn_towards(aim_yaw, aim_pitch, dt, speed_scale)
        # where the bullets go relative to the target
        by = wrap180(self.yaw - rx - yaw)
        bp = self.pitch + ry - pitch
        return math.hypot(by, bp)

    def fire_tolerance(self, distance: float, radius: float = 0.2) -> float:
        """Angle within which the bot pulls the trigger."""
        ang = math.degrees(math.atan2(radius, max(distance, 0.5)))
        return max(ang, 0.35) * float(self.p.get("fire_tolerance", 1.5))
