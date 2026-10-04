"""Leaning (Siege-style peeking): Q/E shift the eye sideways and roll the view.

The same controller drives the player and the bots. The eye offset is
limited by geometry (a ray from the head to the side), so you cannot lean
your camera through a wall, and the character's upper body rolls with the
lean so the head hit box really is where the camera is.
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

from engine.physics import MASK_SIGHT

OFFSET = 0.36         # metres the eye moves sideways at full lean
ROLL = 12.0           # degrees the view rolls at full lean
TIME = 0.16           # seconds from upright to full lean
MARGIN = 0.14         # closest the eye gets to a wall
HEAD_ABOVE_PIVOT = 0.72   # head height above the spine pivot (body lean angle)


class Lean:
    def __init__(self):
        self.amount = 0.0           # -1 full left .. +1 full right
        self.prev = 0.0

    def update(self, dt: float, target: float, free_left: float = 1.0, free_right: float = 1.0) -> None:
        """Move towards target (-1, 0, +1); free_* (0..1) is how far geometry lets the eye go."""
        self.prev = self.amount
        step = dt / TIME
        self.amount += max(-step, min(step, target - self.amount))
        self.amount = max(-free_left, min(free_right, self.amount))

    def reset(self) -> None:
        self.amount = self.prev = 0.0

    def value(self, alpha: float = 1.0) -> float:
        return self.prev + (self.amount - self.prev) * alpha

    def offset(self, alpha: float = 1.0) -> float:
        """Sideways eye offset in metres (positive = right)."""
        return self.value(alpha) * OFFSET

    def roll(self, alpha: float = 1.0) -> float:
        return self.value(alpha) * ROLL

    @staticmethod
    def body_roll(amount: float) -> float:
        """Spine roll (degrees) that puts the head over the leaned eye."""
        return math.degrees(math.asin(max(-1.0, min(1.0, amount * OFFSET / HEAD_ABOVE_PIVOT))))


def right_of(yaw: float) -> Vec3:
    h = math.radians(yaw)
    return Vec3(math.cos(h), math.sin(h), 0)


def clearance(physics, eye: Point3, yaw: float) -> tuple[float, float]:
    """(free_left, free_right): fraction of a full lean the geometry allows."""
    r = right_of(yaw)
    out = []
    for sign in (-1.0, 1.0):
        end = eye + r * (sign * (OFFSET + MARGIN))
        hit = physics.ray_cast(eye, end, MASK_SIGHT)
        if hit is None:
            out.append(1.0)
        else:
            d = (hit.pos - eye).length()
            out.append(max(0.0, min(1.0, (d - MARGIN) / OFFSET)))
    return out[0], out[1]
