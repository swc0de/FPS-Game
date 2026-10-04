"""Shooting-range target dummies (hitboxes + mannequin + damage readout)."""
from __future__ import annotations

import math

from panda3d.core import LVecBase4f, NodePath, Point3, TextNode, Vec3

from gameplay.damage import Damageable, DamageResult
from gameplay.hitboxes import HitboxRig, build_mannequin


class TargetDummy:
    def __init__(self, game, spec: dict):
        self.game = game
        self.spec = spec
        self.base_pos = Point3(*spec["pos"])
        self.heading = float(spec.get("heading", 0.0))
        self.armor0 = float(spec.get("armor", 0))
        self.helmet0 = bool(spec.get("helmet", False))
        self.respawn_delay = float(spec.get("respawn", 2.5))
        move = spec.get("move")
        self.move_axis = Vec3(*move["axis"]).normalized() if move else None
        self.move_dist = float(move.get("distance", 4.0)) if move else 0.0
        self.move_speed = float(move.get("speed", 2.5)) if move else 0.0
        self.t = 0.0
        self.root = game.render.attachNewNode("dummy")
        self.root.setPos(self.base_pos)
        self.root.setH(self.heading)
        self.body = self.root.attachNewNode("body")
        self.nodes = build_mannequin(game.materials, self.body, helmet=self.helmet0, vest=self.armor0 > 0)
        self.damageable = Damageable(armor=self.armor0, helmet=self.helmet0, name=spec.get("name", "dummy"),
                                     team="target")
        self.rig = HitboxRig(game.physics, self, surface="dummy")
        self.dead_timer = 0.0
        self.flash = 0.0
        self.label_np = self._make_label()
        self.label_timer = 0.0
        self.burst_damage = 0.0
        self.burst_hits = 0
        self._last_hit_time = -10.0
        self.update_rig()

    def _make_label(self) -> NodePath:
        tn = TextNode("dummy_label")
        tn.setAlign(TextNode.ACenter)
        tn.setTextColor(1, 0.95, 0.8, 1)
        tn.setShadow(0.05, 0.05)
        tn.setShadowColor(0, 0, 0, 1)
        np_ = self.root.attachNewNode(tn)
        np_.setPos(0, 0, 2.05)
        np_.setScale(0.16)
        np_.setBillboardPointEye()
        np_.setShaderOff(10)
        np_.setLightOff(10)
        np_.setDepthOffset(1)
        from render.renderer import SHADOW_CAMERA_MASK
        np_.hide(SHADOW_CAMERA_MASK)
        return np_

    # ------------------------------------------------------------- damage
    def on_hit(self, res: DamageResult, pos, direction) -> None:
        now = self.game.loop.time
        if now - self._last_hit_time > 1.0:
            self.burst_damage = 0.0
            self.burst_hits = 0
        self._last_hit_time = now
        self.burst_damage += res.health
        self.burst_hits += 1
        self.flash = 0.12
        tag = res.hitgroup.upper()
        extra = f"  (armor -{res.armor:.0f})" if res.armor > 0.5 else ""
        pen = f"  [{res.info.penetrated} wall]" if res.info.penetrated else ""
        text = f"-{res.health:.0f} {tag}{extra}{pen}\n{self.burst_hits} hits  {self.burst_damage:.0f} total"
        if res.killed:
            text = "KILLED\n" + text
        self.label_np.node().setText(text)
        self.label_timer = 2.5
        if res.killed:
            self.dead_timer = self.respawn_delay
            self.rig.set_enabled(False)

    # ------------------------------------------------------------- update
    def update_rig(self) -> None:
        self.rig.update(self.root.getTransform(self.game.render))

    def fixed_update(self, dt: float) -> None:
        self.t += dt
        if self.move_axis is not None and self.damageable.alive:
            phase = math.sin(self.t * self.move_speed / max(self.move_dist, 0.1) * math.pi)
            self.root.setPos(self.base_pos + self.move_axis * (phase * self.move_dist * 0.5))
        if not self.damageable.alive:
            self.dead_timer -= dt
            if self.dead_timer <= 0:
                self.damageable.reset(armor=self.armor0, helmet=self.helmet0)
                self.body.setP(0)
                self.body.setZ(0)
                self.rig.set_enabled(True)
        if self.damageable.alive:
            self.update_rig()

    def frame_update(self, dt: float) -> None:
        if self.flash > 0:
            self.flash -= dt
            k = max(self.flash, 0) / 0.12
            self.body.setShaderInput("u_emission", LVecBase4f(0.6 * k, 0.05 * k, 0.02 * k, 0))
        if not self.damageable.alive:
            # topple over backwards
            p = self.body.getP()
            self.body.setP(max(p - dt * 260.0, -88.0))
        if self.label_timer > 0:
            self.label_timer -= dt
            self.label_np.setAlphaScale(min(self.label_timer / 0.5, 1.0))
            if self.label_timer <= 0:
                self.label_np.node().setText("")

    def destroy(self) -> None:
        self.rig.destroy()
        self.root.removeNode()
