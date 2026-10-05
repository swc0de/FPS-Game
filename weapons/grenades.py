"""Thrown grenades: Bullet rigid bodies with fuses and detonation effects.

* Frag  - explodes after its fuse; damage falls off with distance
          (max * (1 - d/r)^p) and requires line of sight to the target's
          centre of mass; armour absorbs explosion damage.
* Smoke - pops when it has come to rest (or after its fuse) and spawns a
          long-lived smoke cloud that also blocks AI vision.
* Flash - detonates after its fuse; anyone with line of sight is blinded
          for up to ``max_blind`` seconds, scaled by distance and by how
          directly they were looking at it.
"""
from __future__ import annotations

import math

from panda3d.bullet import BulletRigidBodyNode, BulletSphereShape
from panda3d.core import Point3, Vec3

from engine.physics import GROUP_DEBRIS, MASK_SIGHT
from gameplay.damage import DamageInfo
from weapons.models import build_weapon_model, model_defs


def blast_damage(distance: float, radius: float, max_damage: float, power: float) -> float:
    """Explosion damage at a distance: max * (1 - d/r)^power, zero beyond the radius."""
    if distance >= radius:
        return 0.0
    return max_damage * (1.0 - distance / radius) ** power


def flash_duration(distance: float, radius: float, cos_angle: float, max_blind: float) -> float:
    """Blind time: scaled by distance and by how directly the victim looked at the flash."""
    if distance >= radius:
        return 0.0
    facing = 1.0 if cos_angle > 0.6 else (0.55 if cos_angle > 0.0 else 0.25)
    return max_blind * facing * (1.0 - distance / radius) ** 0.6


class Grenade:
    def __init__(self, game, gdef, pos: Point3, vel: Vec3, thrower):
        self.game = game
        self.d = gdef
        self.thrower = thrower
        self.age = 0.0
        self.still_time = 0.0
        self.detonated = False
        self.done = False
        self.cloud = None
        radius = model_defs()[gdef.model].get("radius", 0.035)
        node = BulletRigidBodyNode(f"grenade:{gdef.key}")
        node.addShape(BulletSphereShape(radius))
        node.setMass(0.4)
        node.setRestitution(gdef.restitution)
        node.setFriction(gdef.friction)
        node.setAngularDamping(0.4)
        node.setLinearDamping(0.05)
        node.setCcdMotionThreshold(radius * 0.5)
        node.setCcdSweptSphereRadius(radius * 0.9)
        node.setIntoCollideMask(GROUP_DEBRIS)
        node.setDeactivationEnabled(False)
        self.np = game.physics.root.attachNewNode(node)
        self.np.setPos(pos)
        node.setLinearVelocity(vel)
        node.setAngularVelocity(Vec3(5, 3, 8))
        game.physics.world.attachRigidBody(node)
        self.visual_root = game.render.attachNewNode("grenade_vis")
        model = build_weapon_model(game.materials, gdef.model, self.visual_root)
        model.root.setPos(0, 0, 0)
        self.node = node
        self.last_speed = vel.length()

    @property
    def pos(self) -> Point3:
        return self.np.getPos(self.game.render)

    def frame_update(self) -> None:
        if self.visual_root is not None and not self.np.isEmpty():
            self.visual_root.setTransform(self.np.getTransform(self.game.render))

    def fixed_update(self, dt: float) -> None:
        if self.done:
            return
        self.age += dt
        speed = self.node.getLinearVelocity().length()
        if self.last_speed - speed > 2.0 and self.age > 0.05:
            self.game.audio.play_at("grenade_bounce", self.pos, volume=min((self.last_speed - speed) / 8, 1.0))
        self.last_speed = speed
        if not self.detonated:
            kind = self.d.key
            if kind == "smoke":
                self.still_time = self.still_time + dt if speed < 0.25 else 0.0
                if self.still_time >= float(self.d.get("settle_time", 0.35)) or self.age >= self.d.fuse:
                    self.detonate()
            elif self.age >= self.d.fuse:
                self.detonate()
        elif self.d.key == "smoke":
            if self.cloud is None or self.cloud.age >= self.cloud.duration:
                self.destroy()

    def detonate(self) -> None:
        self.detonated = True
        p = self.pos
        kind = self.d.key
        if kind == "frag":
            self.game.effects.explosion(p, float(self.d.get("radius", 9.0)))
            self.game.audio.play_at("explosion", p, volume=1.0)
            self.game.destruction.explosion(p, float(self.d.get("wall_damage", 260)),
                                            float(self.d.get("wall_radius", 1.1)))
            if self.game.tactical is not None:
                self.game.tactical.on_explosion(p, 2.5)
            self._apply_blast(p)
            self.game.notify_noise(p, 1.0, 60.0, source=self.thrower)
            self.destroy()
        elif kind == "flash":
            self.game.effects.flashbang(p)
            self.game.audio.play_at("flashbang", p, volume=1.0)
            self._apply_flash(p)
            self.game.notify_noise(p, 1.0, 45.0, source=self.thrower)
            self.destroy()
        elif kind == "emp":
            r = float(self.d.get("radius", 7.0))
            self.game.effects.emp_burst(p, r)
            self.game.audio.play_at("emp", p, volume=1.0)
            tac = self.game.tactical
            if tac is not None:
                side = getattr(self.thrower, "side", None) or getattr(getattr(self.thrower, "agent", None), "side", "")
                tac.emp(p, r, float(self.d.get("duration", 12.0)), side)
            self.game.notify_noise(p, 0.7, 30.0, source=self.thrower)
            self.destroy()
        elif kind == "smoke":
            self.cloud = self.game.effects.smoke_grenade(p, float(self.d.get("radius", 3.7)),
                                                         float(self.d.get("duration", 18.0)))
            self.game.audio.play_at("smoke_pop", p, volume=0.8)
            self.node.setLinearVelocity(Vec3(0, 0, 0))

    def _apply_blast(self, p: Point3) -> None:
        r = float(self.d.get("radius", 9.0))
        dmg_max = float(self.d.get("damage", 98))
        power = float(self.d.get("falloff_power", 1.4))
        for target in self.game.damageables():
            center = target.center_of_mass()
            dist = (center - p).length()
            if dist >= r or not target.damageable.alive:
                continue
            hit = self.game.physics.ray_cast(p + Vec3(0, 0, 0.15), center, MASK_SIGHT)
            if hit is not None and (hit.pos - center).length() > 0.3:
                continue
            dmg = blast_damage(dist, r, dmg_max, power)
            info = DamageInfo(dmg, float(self.d.get("armor_penetration", 0.5)), "chest", "explosion",
                              self.thrower, "frag", tuple(center), tuple((center - p).normalized()))
            res = target.damageable.take_damage(info)
            if res is not None and hasattr(target, "on_hit"):
                target.on_hit(res, center, (center - p).normalized())
            if res is not None and self.thrower is self.game.player and target is not self.game.player:
                self.game.hud.hit_marker(res.killed)

    def _apply_flash(self, p: Point3) -> None:
        director = getattr(self.game, "director", None)
        if director is not None:
            director.apply_flash(p, float(self.d.get("radius", 28.0)), float(self.d.get("max_blind", 4.6)))
        player = self.game.player
        if not player.damageable.alive:
            return
        eye = player.camera_pos
        to = p - eye
        dist = to.length()
        r = float(self.d.get("radius", 28.0))
        if dist >= r:
            return
        hit = self.game.physics.ray_cast(eye, p + (eye - p).normalized() * 0.1, MASK_SIGHT)
        if hit is not None:
            return
        if self.game.effects.smoke_between(eye, p) > 0.8:
            return
        fwd = self.game.camera.getQuat(self.game.render).getForward()
        cos_a = fwd.dot(to / max(dist, 1e-3))
        max_blind = float(self.d.get("max_blind", 4.6))
        duration = flash_duration(dist, r, cos_a, max_blind)
        strength = duration / max_blind
        if duration > 0.15:
            self.game.hud.blind(duration, strength)

    def destroy(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.np.isEmpty():
            self.game.physics.world.removeRigidBody(self.node)
            self.np.removeNode()
        if self.visual_root is not None:
            self.visual_root.removeNode()
            self.visual_root = None


def throw_velocity(yaw: float, pitch: float, speed: float, inherit: Vec3) -> Vec3:
    """Grenades leave slightly above the crosshair (like CS) plus the thrower's velocity."""
    p = math.radians(min(pitch + 8.0, 89.0))
    h = math.radians(yaw)
    d = Vec3(-math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p))
    return d * speed + inherit * 1.0
