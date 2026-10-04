"""Local player: input -> character movement, first-person camera.

Movement runs in fixed ticks (see engine/fixed_loop.py); the camera is
updated every rendered frame from the interpolated character state plus
the latest mouse input, so aiming stays responsive at any frame rate.
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

from gameplay.character import KinematicCharacter, MoveInput, StepEvent
from gameplay.damage import Damageable, DamageResult

# Mouse sensitivity uses the same scale as CS: degrees = counts * 0.022 * sens
YAW_PER_COUNT = 0.022


class PlayerController:
    def __init__(self, app, physics, inp, settings):
        self.app = app
        self.input = inp
        self.settings = settings
        self.char = KinematicCharacter(physics)
        self.char.on_event = self._on_step_event
        self.yaw = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.noclip = False
        self._jump_buffer = 0.0
        self._bob_phase = 0.0
        self._bob_weight = 0.0
        self._land_offset = 0.0
        self._land_vel = 0.0
        self.step_events: list[StepEvent] = []   # consumed by audio/AI
        self.camera_pos = Point3()
        self.view_offset = (0.0, 0.0, 0.0)       # recoil/punch/shake (yaw, pitch, roll) set by weapons
        self.last_mouse = (0.0, 0.0)
        self.speed_scale = lambda: 1.0           # weapon weight / ADS (set by the weapon controller)
        self.damageable = Damageable(name="player", team="player")
        self.on_damaged = None                   # callback(DamageResult)
        self.on_step = None                      # callback(StepEvent) -> audio / AI noise
        self.sens_scale = 1.0                    # zoomed scopes slow the mouse down
        self.move_lock = False                   # freeze time, planting, defusing: look but don't move
        self.agent = None                        # match participant (gameplay/agents.py)

    # ------------------------------------------------------------- spawning
    def spawn(self, pos, heading: float = 0.0) -> None:
        self.char.teleport(pos)
        self.yaw = heading
        self.pitch = 0.0

    def set_pose(self, pos, hpr) -> None:
        """Debug: place the eye at pos with the given heading/pitch."""
        self.yaw, self.pitch = hpr[0], hpr[1]
        eye = self.char.stand_height - self.char.cfg["eye_offset"]
        self.char.teleport((pos[0], pos[1], pos[2] - eye))
        self.char.prev_pos = Point3(self.char.pos)

    # ------------------------------------------------------------ helpers
    def _basis(self):
        h = math.radians(self.yaw)
        forward = Vec3(-math.sin(h), math.cos(h), 0)
        right = Vec3(math.cos(h), math.sin(h), 0)
        return forward, right

    def _on_step_event(self, ev: StepEvent) -> None:
        self.step_events.append(ev)
        if self.on_step is not None and not self.noclip:
            self.on_step(ev)
        if len(self.step_events) > 64:
            del self.step_events[:-64]
        if ev.kind == "land":
            self._land_vel -= min(ev.speed, 9.0) * 0.12

    # ---------------------------------------------------------- fixed tick
    def fixed_update(self, dt: float) -> None:
        inp = self.input
        f = float(inp.is_down("forward")) - float(inp.is_down("back"))
        s = float(inp.is_down("right")) - float(inp.is_down("left"))
        forward, right = self._basis()
        wish = forward * f + right * s
        if wish.lengthSquared() > 1e-6:
            wish.normalize()
        if inp.consume("jump"):
            self._jump_buffer = 0.08
        else:
            self._jump_buffer = max(self._jump_buffer - dt, 0.0)
        if self.move_lock and not self.noclip:
            wish = Vec3(0, 0, 0)
            self._jump_buffer = 0.0

        if self.noclip:
            self._noclip_move(dt, wish)
            return
        move = MoveInput(wish_dir=wish, walk=inp.is_down("walk"), crouch=inp.is_down("crouch"),
                         jump=self._jump_buffer > 0.0, speed_scale=self.speed_scale())
        if not self.damageable.alive:
            move = MoveInput(crouch=True)
        was_ground = self.char.on_ground
        self.char.step(dt, move)
        if move.jump and was_ground and not self.char.on_ground:
            self._jump_buffer = 0.0

    def _noclip_move(self, dt: float, wish: Vec3) -> None:
        c = self.char
        c.prev_pos = Point3(c.pos)
        p = math.radians(self.pitch)
        forward, right = self._basis()
        f = float(self.input.is_down("forward")) - float(self.input.is_down("back"))
        s = float(self.input.is_down("right")) - float(self.input.is_down("left"))
        up = float(self.input.is_down("jump")) - float(self.input.is_down("crouch"))
        d = forward * math.cos(p) * f + Vec3(0, 0, math.sin(p) * f) + right * s + Vec3(0, 0, up)
        speed = 4.0 if self.input.is_down("walk") else 12.0
        c.pos += d * speed * dt
        c.vel = Vec3(0, 0, 0)

    # --------------------------------------------------------------- frame
    def frame_update(self, dt: float, alpha: float) -> None:
        sens = float(self.settings.input.get("sensitivity", 2.0)) * self.sens_scale
        dx, dy = self.input.mouse_delta()
        self.last_mouse = (dx, dy)
        invert = -1.0 if self.settings.input.get("invert_y", False) else 1.0
        self.yaw = (self.yaw - dx * YAW_PER_COUNT * sens) % 360.0
        self.pitch = max(-89.0, min(89.0, self.pitch - dy * YAW_PER_COUNT * sens * invert))

        c = self.char
        pos = c.interpolated_pos(alpha)
        eye = c.interpolated_height(alpha) - c.cfg["eye_offset"]

        # view bob: subtle, scaled by ground speed (toggle in settings)
        speed = c.horizontal_speed if c.on_ground and not self.noclip else 0.0
        target_w = min(speed / max(c.cfg["run_speed"], 1e-3), 1.0)
        self._bob_weight += (target_w - self._bob_weight) * min(dt * 8.0, 1.0)
        self._bob_phase += dt * speed * 2.1
        bob_z = bob_x = 0.0
        if self.settings.input.get("head_bob", True):
            amp = 0.022 * self._bob_weight
            bob_z = math.sin(self._bob_phase * 2.0) * amp
            bob_x = math.sin(self._bob_phase) * amp * 0.6
        # landing dip: critically damped spring
        k = 120.0
        damp = 2.0 * math.sqrt(k)
        self._land_vel += (-k * self._land_offset - damp * self._land_vel) * dt
        self._land_offset += self._land_vel * dt
        self._land_offset = max(min(self._land_offset, 0.05), -0.25)

        forward, right = self._basis()
        cam = Point3(pos.x, pos.y, pos.z + eye + bob_z + self._land_offset) + right * bob_x
        self.camera_pos = cam
        self.app.camera.setPos(cam)
        vy, vp, vr = self.view_offset
        self.app.camera.setHpr(self.yaw + vy, max(-89.9, min(89.9, self.pitch + vp)), self.roll + vr)

    # ----------------------------------------------------- shared interface
    @property
    def bob_phase(self) -> float:
        return self._bob_phase

    @property
    def bob_weight(self) -> float:
        return self._bob_weight if self.settings.input.get("head_bob", True) else self._bob_weight * 0.6

    @property
    def land_offset(self) -> float:
        return self._land_offset

    def center_of_mass(self) -> Point3:
        c = self.char
        return Point3(c.pos.x, c.pos.y, c.pos.z + c.height * 0.55)

    def forward(self) -> Vec3:
        return self._basis()[0]

    def on_hit(self, res: DamageResult, pos, direction) -> None:
        if self.on_damaged:
            self.on_damaged(res)

    @property
    def state_text(self) -> str:
        c = self.char
        if self.noclip:
            return "noclip"
        parts = ["ground" if c.on_ground else "air"]
        if c.crouched:
            parts.append("crouch")
        elif c.walking:
            parts.append("walk")
        else:
            parts.append("run")
        return "/".join(parts)
