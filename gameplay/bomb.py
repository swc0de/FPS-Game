"""The breach charge (bomb): carried by an attacker, planted at a bomb site,
defused by a defender or detonated when its timer runs out.

States: none -> carried <-> dropped -> planted -> (defused | exploded)

While planted it beeps with a steadily shrinking interval (1 s at plant time
down to 0.12 s at the end) and its LED blinks in sync, lighting the floor
around it. The detonation damages everyone in range with a Gaussian falloff
(no line-of-sight check: walls do not protect you from the bomb, as in CS).
"""
from __future__ import annotations

import math

from panda3d.core import LVecBase4f, Point3, Vec3

from engine.physics import MASK_SIGHT
from gameplay.damage import DamageInfo
from render.local_lights import LocalLight

LED_ON = LVecBase4f(30, 1.5, 0.8, 0)
LED_OFF = LVecBase4f(0.4, 0.02, 0.01, 0)


def bomb_damage(distance: float, max_damage: float, sigma: float, radius: float) -> float:
    if distance >= radius:
        return 0.0
    return max_damage * math.exp(-distance * distance / (2.0 * sigma * sigma))


def beep_interval(time_left: float, total: float, slow: float, fast: float) -> float:
    frac = max(min(time_left / max(total, 1e-3), 1.0), 0.0)
    return fast + (slow - fast) * frac ** 1.4


class Bomb:
    def __init__(self, game, rules: dict):
        from weapons.models import build_weapon_model
        self.game = game
        self.cfg = rules["bomb"]
        self.timers = rules["timers"]
        self.state = "none"
        self.carrier = None
        self.pos = Point3()
        self.site = ""
        model = build_weapon_model(game.materials, "bomb_charge", game.render, "bomb_world")
        self.np = model.root
        self.led = model.groups.get("led")
        self.np.setScale(1.6)          # the world model reads better slightly larger than the hand-held one
        self.np.hide()
        self.light: LocalLight | None = None
        self._beep_t = 0.0
        self._led_t = 0.0

    # ------------------------------------------------------------ state
    def reset(self) -> None:
        self.state = "none"
        self.carrier = None
        self.site = ""
        self.np.hide()
        self._remove_light()

    def give(self, agent) -> None:
        self.state = "carried"
        self.carrier = agent
        self.np.hide()

    def drop(self, pos: Point3) -> None:
        """Dropped on death or by the carrier (G with the bomb selected)."""
        ground = self._ground(pos)
        self.state = "dropped"
        self.carrier = None
        self.pos = ground
        self.np.setPos(ground + Vec3(0, 0, 0.07))
        self.np.setHpr(37, 0, 0)
        self.np.show()

    def try_pickup(self, agent, feet: Point3) -> bool:
        if self.state != "dropped" or agent.side != "attack" or not agent.alive:
            return False
        if (Point3(feet.x, feet.y, 0) - Point3(self.pos.x, self.pos.y, 0)).length() > 1.1 or \
                abs(feet.z - self.pos.z) > 1.2:
            return False
        self.give(agent)
        return True

    def plant(self, pos: Point3, heading: float, site: str) -> None:
        self.state = "planted"
        self.carrier = None
        self.site = site
        self.pos = self._ground(pos)
        self.np.setPos(self.pos + Vec3(0, 0, 0.07))
        self.np.setHpr(heading, 0, 0)
        self.np.show()
        self._beep_t = 0.0
        self._led_t = 0.0
        self._remove_light()
        self.light = LocalLight(pos=self.pos + Vec3(0, 0, 0.25), color=Vec3(0, 0, 0), range=3.0, shadows=False)
        self.game.renderer.lights.add(self.light)
        self.game.audio.play_at("bomb_planted", self.pos, volume=1.0)

    def defused(self) -> None:
        self.state = "defused"
        self._remove_light()
        if self.led is not None:
            self.led.setShaderInput("u_emission", LVecBase4f(0.02, 0.5, 0.1, 0))
        self.game.audio.play_at("bomb_defused", self.pos, volume=1.0)

    def explode(self) -> None:
        g = self.game
        self.state = "exploded"
        self.np.hide()
        self._remove_light()
        g.effects.big_explosion(self.pos + Vec3(0, 0, 0.3))
        g.audio.play_at("explosion", self.pos, volume=1.0)
        g.notify_noise(self.pos, 1.0, 120.0)
        center = self.pos + Vec3(0, 0, 0.5)
        g.destruction.explosion(center, float(self.cfg.get("wall_damage", 900)), float(self.cfg.get("wall_radius", 4.5)))
        for target in g.damageables():
            if not target.damageable.alive:
                continue
            tc = target.center_of_mass()
            dmg = bomb_damage((tc - center).length(), float(self.cfg["max_damage"]), float(self.cfg["sigma"]),
                              float(self.cfg["radius"]))
            if dmg <= 0.5:
                continue
            d = (tc - center)
            d = d / max(d.length(), 1e-3)
            info = DamageInfo(dmg, 0.6, "chest", "explosion", None, "bomb", tuple(tc), tuple(d))
            res = target.damageable.take_damage(info)
            if res is not None and hasattr(target, "on_hit"):
                target.on_hit(res, tc, d)

    # ----------------------------------------------------------- frame
    def update(self, dt: float, time_left: float) -> None:
        if self.state != "planted":
            return
        total = float(self.timers["bomb_timer"])
        self._beep_t -= dt
        if self._beep_t <= 0.0:
            self._beep_t = beep_interval(time_left, total, float(self.cfg["beep_slow"]), float(self.cfg["beep_fast"]))
            self._led_t = 0.09
            self.game.audio.play_at("bomb_beep", self.pos, volume=0.9)
        self._led_t -= dt
        on = self._led_t > 0.0
        if self.led is not None:
            self.led.setShaderInput("u_emission", LED_ON if on else LED_OFF)
        if self.light is not None:
            self.light.color = Vec3(3.0, 0.15, 0.05) if on else Vec3(0, 0, 0)

    # ---------------------------------------------------------- helpers
    def defuse_target(self, eye: Point3, direction: Vec3) -> bool:
        """Is the player close enough and looking at the planted bomb?"""
        if self.state != "planted":
            return False
        to = (self.pos + Vec3(0, 0, 0.1)) - eye
        dist = to.length()
        if dist > float(self.cfg["defuse_range"]) + 1.0:
            return False
        flat = Point3(eye.x, eye.y, 0) - Point3(self.pos.x, self.pos.y, 0)
        if flat.length() > float(self.cfg["defuse_range"]):
            return False
        return to.normalized().dot(direction) > math.cos(math.radians(40))

    def _ground(self, pos: Point3) -> Point3:
        p = Point3(*pos)
        hit = self.game.physics.ray_cast(p + Vec3(0, 0, 0.5), p - Vec3(0, 0, 3.0), MASK_SIGHT)
        if hit is not None:
            return Point3(hit.pos)
        return p

    def _remove_light(self) -> None:
        if self.light is not None:
            self.game.renderer.lights.remove(self.light)
            self.light = None
