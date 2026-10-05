"""Drones (attackers) and security cameras (defenders).

* A drone is a tiny kinematic character (gameplay/character.py with a
  15 cm hull): it drives, hops up steps, falls through hatches and can be
  shot (one bullet). Attackers throw them during the prep phase to scout.
* Cameras are fixed map pieces (``{"type": "camera"}`` in the map JSON) the
  defenders look through; they pan within a limit and can be shot out.
* Both are "views" the human can take over (gameplay/tactical.py) and both
  spot enemies for their team: what a view sees can be pinged.
"""
from __future__ import annotations

import copy
import math

from panda3d.core import Point3, Vec3

from engine.physics import MASK_SIGHT
from gameplay.character import KinematicCharacter, MoveInput, load_movement_config
from gameplay.gadgets import LED_BLUE, LED_GREEN, LED_OFF, LED_RED, Deployable, gadget_cfg

DRONE_EYE = 0.11


def drone_movement(cfg: dict) -> dict:
    m = copy.deepcopy(load_movement_config())
    m.update({"radius": 0.13, "stand_height": 0.16, "crouch_height": 0.16, "step_height": 0.09,
              "run_speed": float(cfg.get("speed", 3.4)), "walk_speed": float(cfg.get("speed", 3.4)) * 0.5,
              "crouch_speed": float(cfg.get("speed", 3.4)) * 0.5, "jump_speed": float(cfg.get("jump", 4.2)),
              "eye_offset": 0.05, "ground_accel": 9.0, "ground_friction": 7.0})
    return m


def view_dir(yaw: float, pitch: float) -> Vec3:
    h, p = math.radians(yaw), math.radians(pitch)
    return Vec3(-math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p))


class Drone(Deployable):
    kind = "drone"
    model = "gadget_drone"
    electronic = True

    def __init__(self, tac, owner, side, pos, yaw: float, vel=None, label: str = ""):
        super().__init__(tac, owner, side, pos, (yaw, 0.0, 0.0))
        self.char = KinematicCharacter(self.game.physics, drone_movement(self.cfg))
        self.char.depen_interval = 4
        self.char.teleport(pos)
        if vel is not None:
            self.char.vel = Vec3(vel)
        self.yaw = yaw
        self.pitch = 0.0
        self.label = label
        self.add_hitbox((0.28, 0.22, 0.14), (0, 0, 0.08), float(self.cfg.get("health", 1)))
        self.wish = Vec3(0, 0, 0)
        self.jump = False
        self.controller = None            # "human" | bot | None
        self._buzz = 0.0
        self.set_led(LED_GREEN)

    @property
    def jammed(self) -> bool:
        return self.disabled or self.tac.jammed(self.char.pos, self.side)

    def eye(self) -> Point3:
        c = self.char.pos
        return Point3(c.x, c.y, c.z + DRONE_EYE)

    def center(self) -> Point3:
        return self.eye()

    def position(self) -> Point3:
        return Point3(self.char.pos)

    def drive(self, wish: Vec3, jump: bool = False) -> None:
        self.wish = Vec3(wish)
        self.jump = jump

    def update(self, dt: float, now: float) -> None:
        if self.jammed:
            self.wish = Vec3(0, 0, 0)
            self.jump = False
            self.set_led(LED_BLUE if (now * 4) % 1.0 < 0.5 else LED_OFF)
        else:
            self.set_led(LED_RED if self.controller is not None else LED_GREEN)
        self.char.step(dt, MoveInput(wish_dir=self.wish, jump=self.jump))
        self.jump = False
        if self.char.horizontal_speed > 0.5:
            self._buzz -= dt
            if self._buzz <= 0.0:
                self._buzz = 0.6
                self.game.audio.play_at("drone_motor", self.char.pos, 0.35)
                self.game.notify_noise(self.char.pos, 0.25, 7.0, source=self.owner)
        if self.char.pos.z < -60.0:
            self.destroy()

    def frame_update(self, alpha: float) -> None:
        if self.alive:
            self.root.setPos(self.char.interpolated_pos(alpha))
            self.root.setH(self.yaw)



class MapCamera(Deployable):
    kind = "camera"
    model = "gadget_camera"
    electronic = True
    PAN = 55.0           # degrees left/right of the mount direction
    TILT = (-55.0, 20.0)

    def __init__(self, tac, spec: dict, index: int):
        self.spec = spec
        self.base_yaw = float(spec.get("heading", 0.0))
        self.base_pitch = float(spec.get("pitch", -15.0))
        super().__init__(tac, None, "defend", spec["pos"], (self.base_yaw, 0.0, 0.0))
        self.label = spec.get("name") or f"Camera {index + 1}"
        self.index = index
        self.yaw = self.base_yaw
        self.pitch = self.base_pitch
        self.add_hitbox((0.12, 0.3, 0.14), (0, 0.06, 0.0), float(self.cfg.get("health", 1)))
        self.set_led(LED_GREEN)

    def eye(self) -> Point3:
        return self.root.getPos(self.game.render) + view_dir(self.yaw, self.pitch) * 0.2

    @property
    def jammed(self) -> bool:
        return self.disabled

    def look(self, dyaw: float, dpitch: float) -> None:
        off = (self.yaw + dyaw - self.base_yaw + 180.0) % 360.0 - 180.0
        self.yaw = self.base_yaw + max(-self.PAN, min(self.PAN, off))
        self.pitch = max(self.TILT[0], min(self.TILT[1], self.pitch + dpitch))

    def update(self, dt: float, now: float) -> None:
        if self.disabled:
            self.set_led(LED_OFF)
        else:
            self.set_led(LED_RED if (now * 1.0) % 1.0 < 0.15 else LED_GREEN)


def can_see(physics, eye: Point3, target: Point3, max_range: float, forward: Vec3 | None = None,
            cos_half: float = -1.0) -> bool:
    d = target - eye
    dist = d.length()
    if dist > max_range or dist < 1e-3:
        return False
    if forward is not None and d.dot(forward) / dist < cos_half:
        return False
    return physics.ray_cast(eye, target, MASK_SIGHT) is None


def view_cfg(kind: str) -> dict:
    return gadget_cfg("drone" if kind == "drone" else "camera")
