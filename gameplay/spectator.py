"""Spectator camera for dead players (and ``--spectate``).

Follows a living bot over the shoulder (third person, looking where the
bot looks; the camera is pulled in when a wall is behind the bot).
Left / right mouse switch to the next / previous player, Space toggles a
free-flying camera. Teammates are followed first, like in CS.
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

from engine.physics import MASK_SIGHT
from weapons.weapon import angles_to_dir

BACK = 2.4
SIDE = 0.55
UP = 0.35


class Spectator:
    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.active = False
        self.free = False
        self.target = None
        self._cam = None

    def candidates(self) -> list:
        d = self.director
        side = d.player_agent.side
        bots = [b for b in d.bots if b.active and b.alive]
        mates = [b for b in bots if b.side == side]
        return mates or bots

    def start(self) -> None:
        self.active = True
        self.free = False
        self._cam = None
        c = self.candidates()
        self.target = c[0] if c else None
        if self.target is None:
            self._go_free()

    def stop(self) -> None:
        self.active = False
        self.free = False
        self.target = None
        self.game.player.noclip = False

    def cycle(self, step: int) -> None:
        c = self.candidates()
        if not c:
            self._go_free()
            return
        if self.free:
            self.free = False
            self.game.player.noclip = False
        i = c.index(self.target) if self.target in c else -1
        self.target = c[(i + step) % len(c)]
        self._cam = None

    def _go_free(self) -> None:
        self.free = True
        p = self.game.player
        if not p.noclip:
            p.noclip = True
            cam = self.game.camera.getPos(self.game.render)
            eye = p.char.stand_height - p.char.cfg["eye_offset"]
            p.char.pos = Point3(cam.x, cam.y, cam.z - eye)
            p.char.prev_pos = Point3(p.char.pos)
            hpr = self.game.camera.getHpr(self.game.render)
            p.yaw, p.pitch = hpr.x, hpr.y

    # ------------------------------------------------------------- frame
    def frame_update(self, dt: float) -> None:
        if not self.active:
            return
        inp = self.game.input
        if inp.consume("fire"):
            self.cycle(1)
        if inp.consume("aim"):
            self.cycle(-1)
        if inp.consume("jump"):
            if self.free:
                self.cycle(0)
            else:
                self._go_free()
        if self.free:
            return
        t = self.target
        if t is None or not t.alive or not t.active:
            self.cycle(1)
            t = self.target
            if t is None or self.free:
                return
        root = t.body.root.getPos(self.game.render)
        eye = Point3(root.x, root.y, root.z + t.char.eye_height)
        yaw, pitch = t.aim.yaw, t.aim.pitch
        fwd = Vec3(*angles_to_dir(yaw, pitch))
        h = math.radians(yaw)
        right = Vec3(math.cos(h), math.sin(h), 0)
        want = eye - fwd * BACK + right * SIDE + Vec3(0, 0, UP)
        hit = self.game.physics.ray_cast(eye, want, MASK_SIGHT)
        if hit is not None:
            want = eye + (hit.pos - eye) * 0.85
        if self._cam is None:
            self._cam = Point3(want)
        else:
            k = 1.0 - math.exp(-dt * 14.0)
            self._cam = self._cam + (want - self._cam) * k
        cam = self.game.camera
        cam.setPos(self._cam)
        cam.lookAt(eye + fwd * 12.0)

    def status(self) -> str:
        if not self.active:
            return ""
        keys = "[LMB / RMB] switch player   [SPACE] free camera"
        if self.free or self.target is None:
            return "Free camera   [SPACE] follow players"
        t = self.target
        ws = t.weapons.inv.current()
        w = ws.d.name if ws is not None else ("Breach charge" if t.weapons.inv.slot == "bomb" else "")
        return f"Spectating {t.name}  -  {t.damageable.health:.0f} HP  {w}\n{keys}"
