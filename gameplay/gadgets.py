"""Specialist gadgets and wall charges (Milestone 6).

Every gadget is plain data in data/specialists.json ("gadgets"); this module
holds the world objects they create. Placed gadgets ("deployables") share a
small base class: a model from data/weapon_models.json, an optional hit box
(GROUP_HITBOX, so bullets destroy it like they hurt a character) and the EMP
rule (electronics stop working for a while).

Attack                                      Defence
  wall charge   cuts a door-sized opening     razor wire     slows and rattles
  thermal lance same, through reinforcement   motion sensor  pings runners
  pulse scanner reveals enemies within 15 m   shield         bullet-proof cover
  hammer        smashes soft walls            jammer         stops charges/drones
  EMP grenade   disables enemy electronics
"""
from __future__ import annotations

import json
import math
import random

from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode
from panda3d.core import LVecBase4f, Point3, Vec3

from engine import paths
from engine.physics import GROUP_HITBOX, GROUP_WORLD, MASK_SIGHT, WORLD_COLLIDE
from gameplay.damage import Damageable, DamageInfo

_CFG = None


def load_specialists() -> dict:
    global _CFG
    if _CFG is None:
        with open(paths.DATA_DIR / "specialists.json", "r", encoding="utf-8") as f:
            _CFG = json.load(f)
    return _CFG


def gadget_cfg(key: str) -> dict:
    return load_specialists()["gadgets"].get(key, {})


def side_of(obj) -> str | None:
    """Team side of a participant, the player controller or a bot (None for the world)."""
    if obj is None:
        return None
    s = getattr(obj, "side", None)
    if s is None:
        s = getattr(getattr(obj, "agent", None), "side", None)
    return s


def hpr_facing(normal: Vec3) -> tuple[float, float, float]:
    """Heading/pitch that turn a model's +Y towards ``normal``."""
    n = Vec3(normal)
    n.normalize()
    h = math.degrees(math.atan2(-n.x, n.y))
    p = math.degrees(math.asin(max(-1.0, min(1.0, n.z))))
    return h, p, 0.0


LED_RED = LVecBase4f(12.0, 0.6, 0.3, 0)
LED_GREEN = LVecBase4f(0.4, 9.0, 1.0, 0)
LED_BLUE = LVecBase4f(1.0, 3.0, 14.0, 0)
LED_OFF = LVecBase4f(0.0, 0.0, 0.0, 0)


class Deployable:
    kind = ""
    model = ""
    electronic = False          # EMP disables it
    blocks_explosions = 0.0     # destroyed by explosions within this radius (0 = only by its hit box)

    def __init__(self, tac, owner, side: str, pos, hpr=(0.0, 0.0, 0.0)):
        from weapons.models import shared_weapon_model
        self.tac = tac
        self.game = tac.game
        self.owner = owner
        self.side = side
        self.pos = Point3(*pos)
        self.hpr = tuple(hpr)
        self.cfg = gadget_cfg(self.kind)
        self.alive = True
        self.controller = None          # who drives/watches it (drones, cameras)
        self.disabled_until = -1.0
        self.root = self.game.render.attachNewNode(f"gadget:{self.kind}")
        self.root.setPos(self.pos)
        self.root.setHpr(*self.hpr)
        # one draw per material for the body; the LED stays a separate node
        model = shared_weapon_model(self.game.materials, self.model, self.root, self.kind, flatten="body")
        self.led = model.groups.get("led")
        self.damageable: Damageable | None = None
        self.bodies = []

    # ------------------------------------------------------------ helpers
    @property
    def now(self) -> float:
        return self.game.loop.time

    @property
    def disabled(self) -> bool:
        return self.now < self.disabled_until

    def set_led(self, color) -> None:
        if self.led is not None:
            self.led.setShaderInput("u_emission", color)

    def add_hitbox(self, size, offset=(0, 0, 0), health: float = 1.0) -> None:
        """Bullets (any enemy weapon) destroy the gadget."""
        node = BulletRigidBodyNode(f"gadget:{self.kind}")
        node.addShape(BulletBoxShape(Vec3(*size) * 0.5))
        node.setKinematic(True)
        node.setIntoCollideMask(GROUP_HITBOX)
        node.setTag("hitgroup", "chest")
        node.setTag("surface", "metal")
        node.setPythonTag("owner", self)
        node.setPythonTag("deployable", self)
        np_ = self.root.attachNewNode(node)
        np_.setPos(*offset)
        self.game.physics.world.attachRigidBody(node)
        self.bodies.append(np_)
        self.damageable = Damageable(health=health, max_health=health, name=self.kind, team=self.side)
        self.damageable.damage_filter = lambda d, info: side_of(info.attacker) != self.side
        self.damageable.on_damage.append(self._damaged)

    def add_solid(self, size, offset=(0, 0, 0), surface: str = "reinforced") -> None:
        """Solid cover (blocks movement and bullets)."""
        node = BulletRigidBodyNode(f"gadget:{self.kind}")
        node.addShape(BulletBoxShape(Vec3(*size) * 0.5))
        node.setIntoCollideMask(GROUP_WORLD | WORLD_COLLIDE)
        node.setTag("surface", surface)
        node.setPythonTag("deployable", self)
        np_ = self.root.attachNewNode(node)
        np_.setPos(*offset)
        self.game.physics.world.attachRigidBody(node)
        self.bodies.append(np_)

    def _damaged(self, res) -> None:
        if res.killed and self.alive:
            self.destroy()

    def emp(self, until: float) -> None:
        if self.electronic:
            self.disabled_until = max(self.disabled_until, until)
            self.set_led(LED_OFF)

    def center(self) -> Point3:
        return self.root.getPos(self.game.render) + Vec3(0, 0, 0.1)

    # ------------------------------------------------------------- life
    def update(self, dt: float, now: float) -> None:
        pass

    def destroy(self, effect: bool = True) -> None:
        if not self.alive:
            return
        self.alive = False
        if effect:
            self.tac.log(f"destroyed_{self.kind}", f"{self.kind} ({self.side}) destroyed")
        if effect:
            c = self.center()
            self.game.effects.sparks(c, (0, 0, 1), 14, light=60.0)
            self.game.audio.play_at("impact_metal", c, 0.8)
        for np_ in self.bodies:
            self.game.physics.world.removeRigidBody(np_.node())
        self.bodies = []
        self.root.removeNode()


# --------------------------------------------------------------- attackers
class _Breacher(Deployable):
    """Wall charge / thermal lance: stuck to a destructible panel, opens a
    door-sized hole when its timer runs out (paused while jammed)."""
    timer = 3.0
    cut_kind = "charge"

    def __init__(self, tac, owner, side, panel, pos, normal):
        self.panel = panel
        self.normal = Vec3(normal)
        super().__init__(tac, owner, side, pos, hpr_facing(normal))
        self.left = float(self.cfg.get("fuse", self.cfg.get("burn_time", self.timer)))
        self.jammed = False
        self._tick = 0.0

    def update(self, dt: float, now: float) -> None:
        self.jammed = self.tac.jammed(self.pos, self.side)
        if self.jammed:
            self.set_led(LED_BLUE if (now * 6.0) % 1.0 < 0.5 else LED_OFF)
            return
        self.left -= dt
        self._effects(dt, now)
        if self.left <= 0.0:
            self.fire()

    def _effects(self, dt: float, now: float) -> None:
        pass

    def cut(self) -> int:
        """Remove a door-sized area: walls are opened from the floor up."""
        p = self.panel
        dm = self.game.destruction
        w = float(self.cfg.get("width", 1.1))
        h = float(self.cfg.get("height", 2.0))
        if p.spec.kind == "floor":
            return dm.cut_at(p, self.pos, w, w / 2, w / 2, self.cut_kind)
        lp = p.to_local(self.pos)
        a, b = p.ax
        vert = b if abs(p.R[b][2]) > 0.9 else a
        horiz = a if vert == b else b
        up = 1.0 if p.R[vert][2] > 0 else -1.0
        bottom = -p.size[vert] / 2 if up > 0 else p.size[vert] / 2
        lo, hi = sorted((bottom, bottom + up * h))
        u0, u1 = lp[horiz] - w / 2, lp[horiz] + w / 2
        if horiz == a:
            return p.cut(u0, u1, lo, hi, self.pos, self.cut_kind, self.normal)
        return p.cut(lo, hi, u0, u1, self.pos, self.cut_kind, self.normal)

    def fire(self) -> None:
        pass


class WallCharge(_Breacher):
    kind = "wall_charge"
    model = "gadget_charge"
    electronic = True

    def _effects(self, dt: float, now: float) -> None:
        self._tick -= dt
        if self._tick <= 0.0:
            self._tick = 0.5 if self.left > 1.0 else 0.18
            self.game.audio.play_at("charge_beep", self.pos, 0.55)
            self.set_led(LED_RED)
        elif self._tick < 0.08:
            self.set_led(LED_OFF)

    def emp(self, until: float) -> None:
        # a charge is armed and dumb: an EMP only delays it
        self.left = max(self.left, 1.0)

    def fire(self) -> None:
        g = self.game
        pos = Point3(self.pos)
        n = self.normal
        cut = self.cut()
        self.tac.log("charge_fired", f"wall charge fired on {self.panel.spec.name or self.panel.index}: {cut} chunks")
        g.destruction.explosion(pos - n * 0.1, 160.0, 0.7)
        g.effects.explosion(pos + n * 0.2, 3.0, scale=0.7, decal=False)
        g.audio.play_at("explosion", pos, 0.9)
        g.notify_noise(pos, 1.0, 50.0, source=self.owner)
        self.tac.on_explosion(pos, 2.2, source=self)
        # the blast goes through: whoever stands right behind the wall is hurt
        far = pos - n * 0.4
        r = float(self.cfg.get("blast_radius", 3.0))
        dmg_max = float(self.cfg.get("blast_damage", 120))
        for t in g.damageables():
            if not t.damageable.alive:
                continue
            c = t.center_of_mass()
            d = (c - far).length()
            if d >= r:
                continue
            if g.physics.ray_cast(far, c, MASK_SIGHT) is not None:
                continue
            dmg = dmg_max * (1.0 - d / r) ** 1.2
            dirv = (c - far).normalized()
            info = DamageInfo(dmg, 0.6, "chest", "explosion", self.owner, "wall_charge", tuple(c), tuple(dirv))
            res = t.damageable.take_damage(info)
            if res is not None and hasattr(t, "on_hit"):
                t.on_hit(res, c, dirv)
        self.destroy(effect=False)


class ThermalLance(_Breacher):
    kind = "thermal_lance"
    model = "gadget_lance"
    cut_kind = "thermal"
    electronic = False

    def _effects(self, dt: float, now: float) -> None:
        self._tick -= dt
        if self._tick <= 0.0:
            self._tick = 0.9
            self.game.audio.play_at("thermal_burn", self.pos, 0.7)
        p = self.panel
        a, b = p.ax
        # sparks run around the outline of the future opening
        w = float(self.cfg.get("width", 1.1))
        h = float(self.cfg.get("height", 2.1))
        t = random.random() * 2 * (w + h)
        if t < w:
            off = Vec3(t - w / 2, 0, -h / 2)
        elif t < w + h:
            off = Vec3(w / 2, 0, t - w - h / 2)
        elif t < 2 * w + h:
            off = Vec3(w / 2 - (t - w - h), 0, h / 2)
        else:
            off = Vec3(-w / 2, 0, h / 2 - (t - 2 * w - h))
        q = self.root.getQuat(self.game.render)
        world = self.pos + q.xform(Vec3(off.x, 0.08, off.z * 0.5))
        self.game.effects.sparks(world, self.normal, 3, light=25.0 if random.random() < 0.2 else 0.0)
        self.set_led(LED_RED if (now * 3.0) % 1.0 < 0.5 else LED_OFF)

    def fire(self) -> None:
        g = self.game
        cut = self.cut()
        self.tac.log("lance_fired", f"thermal lance burned through {self.panel.spec.name or self.panel.index}: "
                                    f"{cut} chunks")
        g.effects.sparks(self.pos, self.normal, 30, speed=(2.0, 8.0), light=200.0)
        g.audio.play_at("wall_break", self.pos, 1.0)
        g.notify_noise(self.pos, 1.0, 40.0, source=self.owner)
        self.tac.on_explosion(self.pos, 1.4, source=self)
        self.destroy(effect=False)


# ---------------------------------------------------------------- defenders
class RazorWire(Deployable):
    kind = "razor_wire"
    model = "gadget_wire"
    blocks_explosions = 2.2

    def __init__(self, tac, owner, side, pos, heading: float):
        super().__init__(tac, owner, side, pos, (heading, 0.0, 0.0))
        self.size = self.cfg.get("size", [1.8, 0.7])
        self._noise: dict[int, float] = {}
        self.hits = 0

    def contains(self, p) -> bool:
        h = math.radians(self.hpr[0])
        dx, dy = p[0] - self.pos.x, p[1] - self.pos.y
        lx = dx * math.cos(h) + dy * math.sin(h)
        ly = -dx * math.sin(h) + dy * math.cos(h)
        return abs(lx) <= self.size[0] / 2 + 0.25 and abs(ly) <= self.size[1] / 2 + 0.25 and abs(p[2] - self.pos.z) < 0.7

    def update(self, dt: float, now: float) -> None:
        slow = float(self.cfg.get("slow", 0.32))
        every = float(self.cfg.get("noise_every", 0.45))
        for agent, char in self.tac.characters():
            if not self.contains(char.pos):
                continue
            self.tac.slow(agent, slow)
            if char.horizontal_speed > 0.4 and now >= self._noise.get(id(agent), 0.0):
                self._noise[id(agent)] = now + every
                self.game.audio.play_at("wire_rattle", char.pos, 0.8)
                self.game.notify_noise(char.pos, 0.9, 24.0, source=agent)

    def hammer(self) -> None:
        self.destroy()


class MotionSensor(Deployable):
    kind = "motion_sensor"
    model = "gadget_sensor"
    electronic = True

    def __init__(self, tac, owner, side, pos, normal):
        self.normal = Vec3(normal)
        self.normal.normalize()
        super().__init__(tac, owner, side, pos, hpr_facing(normal))
        self.add_hitbox((0.14, 0.08, 0.17), (0, 0.03, 0), float(self.cfg.get("health", 1)))
        self._scan = 0.0
        self._blink = 0.0
        self.set_led(LED_GREEN)

    def update(self, dt: float, now: float) -> None:
        if self.disabled:
            return
        self._blink -= dt
        if self._blink <= 0:
            self.set_led(LED_GREEN)
        self._scan -= dt
        if self._scan > 0:
            return
        self._scan = 0.2
        rng = float(self.cfg.get("range", 8.0))
        cos_half = math.cos(math.radians(float(self.cfg.get("fov", 120.0)) / 2))
        min_speed = float(self.cfg.get("min_speed", 2.2))
        eye = self.pos + self.normal * 0.06
        for enemy in self.tac.enemies_of(self.side):
            c = enemy.center_of_mass()
            d = c - eye
            dist = d.length()
            if dist > rng or dist < 1e-3:
                continue
            if d.dot(self.normal) / dist < cos_half:
                continue
            v = enemy.velocity() if hasattr(enemy, "velocity") else Vec3(0, 0, 0)
            if math.hypot(v.x, v.y) < min_speed:
                continue
            if self.game.physics.ray_cast(eye, c, MASK_SIGHT) is not None:
                continue
            self.tac.ping(self.side, enemy.position(), "sensor", float(self.cfg.get("ping_time", 2.0)), agent=enemy,
                          by=self.owner)
            self.set_led(LED_RED)
            self._blink = 0.4


class DeployShield(Deployable):
    kind = "deploy_shield"
    model = "gadget_shield"
    blocks_explosions = 2.0

    def __init__(self, tac, owner, side, pos, heading: float):
        super().__init__(tac, owner, side, pos, (heading, 0.0, 0.0))
        sx, sy, sz = self.cfg.get("size", [1.05, 0.08, 1.1])
        self.add_solid((sx, sy, sz), (0, 0, 0.07 + sz / 2))

    def center(self) -> Point3:
        return self.root.getPos(self.game.render) + Vec3(0, 0, 0.6)

    def hammer(self) -> None:
        self.destroy()


class SignalJammer(Deployable):
    kind = "signal_jammer"
    model = "gadget_jammer"
    electronic = True

    def __init__(self, tac, owner, side, pos, heading: float):
        super().__init__(tac, owner, side, pos, (heading, 0.0, 0.0))
        self.add_hitbox((0.32, 0.22, 0.16), (0, 0, 0.08), float(self.cfg.get("health", 1)))
        self.radius = float(self.cfg.get("radius", 5.0))

    @property
    def active(self) -> bool:
        return self.alive and not self.disabled

    def update(self, dt: float, now: float) -> None:
        if self.active:
            self.set_led(LED_BLUE if (now * 1.5) % 1.0 < 0.5 else LED_OFF)


# ---------------------------------------------------------------- EMP grenade
def emp_grenade_def():
    from weapons.defs import GrenadeDef
    c = gadget_cfg("emp_grenade")
    return GrenadeDef(key="emp", name=c.get("name", "EMP grenade"), model=c.get("model", "grenade_emp"), price=0,
                      max_carry=int(c.get("count", 2)), fuse=float(c.get("fuse", 1.4)),
                      throw_speed=float(c.get("throw_speed", 15.0)), lob_speed=float(c.get("throw_speed", 15.0)) * 0.55,
                      restitution=float(c.get("restitution", 0.4)), friction=float(c.get("friction", 0.6)), speed=1.0,
                      raw=dict(c))


DEPLOYABLES = {"wall_charge": WallCharge, "thermal_lance": ThermalLance, "razor_wire": RazorWire,
               "motion_sensor": MotionSensor, "deploy_shield": DeployShield, "signal_jammer": SignalJammer}
