"""World pickups: dropped weapons (physics) and range racks/crates (static).

Pickups use their own collision group so they bounce off the level but do
not block bullets or movement. The player picks up what the crosshair points
at within reach (``use`` key) or walks over a weapon whose slot is empty.
"""
from __future__ import annotations

import math

from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode
from panda3d.core import BitMask32, NodePath, Point3, TextNode, Vec3

from engine.physics import GROUP_DEBRIS
from weapons.models import build_weapon_model

GROUP_PICKUP = BitMask32.bit(6)
REACH = 2.3


class Pickup:
    def __init__(self, mgr, kind: str, item, pos: Point3, heading: float = 0.0, vel: Vec3 | None = None,
                 static: bool = False, respawn: float = 0.0, label: str = ""):
        self.mgr = mgr
        game = mgr.game
        self.kind = kind            # weapon | ammo | armor | grenades
        self.item = item            # weapon key / WeaponState / grenade key
        self.static = static
        self.respawn = respawn
        self.spawn_pos = Point3(pos)
        self.spawn_heading = heading
        self.hidden_timer = 0.0
        self.label = label
        self.visual = NodePath("pickup_vis")
        model_key = None
        if kind == "weapon":
            wkey = item if isinstance(item, str) else item.d.key
            model_key = game.weapon_db.weapons[wkey].model
        elif kind == "grenade":
            model_key = game.weapon_db.grenades[item].model
        if model_key:
            m = build_weapon_model(game.materials, model_key, self.visual)
            lo, hi = m.root.getTightBounds()
            ext = (hi - lo) * 0.5
            center = (hi + lo) * 0.5
            m.root.setPos(-center)
        else:
            from engine.geometry import MeshBuilder
            mb = MeshBuilder()
            size = (0.6, 0.4, 0.3) if kind != "armor" else (0.5, 0.35, 0.25)
            mb.add_chamfer_box((0, 0, 0), size, bevel=0.02)
            box = self.visual.attachNewNode(mb.build(f"crate:{kind}"))
            game.materials.get({"ammo": "metal_olive", "armor": "canvas", "grenades": "metal_tan"}.get(kind, "metal_olive")).apply(box)
            ext = Vec3(*size) * 0.5
        self.extent = Vec3(max(ext.x, 0.02), max(ext.y, 0.02), max(ext.z, 0.02))
        node = BulletRigidBodyNode(f"pickup:{kind}")
        node.addShape(BulletBoxShape(self.extent))
        node.setIntoCollideMask(GROUP_DEBRIS | GROUP_PICKUP)
        node.setPythonTag("pickup", self)
        if not static:
            node.setMass(3.0)
            node.setFriction(0.9)
            node.setRestitution(0.15)
            node.setAngularDamping(0.5)
            node.setCcdMotionThreshold(0.02)
            node.setCcdSweptSphereRadius(0.02)
        self.node = node
        self.np = game.physics.root.attachNewNode(node)
        self.np.setPos(pos)
        self.np.setH(heading)
        if static:
            self.np.setR(90 if kind in ("weapon",) else 0)
        game.physics.world.attachRigidBody(node)
        if vel is not None and not static:
            node.setLinearVelocity(vel)
            node.setAngularVelocity(Vec3(0, 0, 3))
        self.visual.reparentTo(game.render)
        if label:
            tn = TextNode("pickup_label")
            tn.setText(label)
            tn.setAlign(TextNode.ACenter)
            tn.setTextColor(1, 1, 0.9, 1)
            tn.setShadow(0.05, 0.05)
            lab = game.render.attachNewNode(tn)
            lab.setPos(pos + Vec3(0, 0, 0.35))
            lab.setScale(0.09)
            lab.setBillboardPointEye()
            lab.setShaderOff(10)
            lab.setLightOff(10)
            from render.renderer import SHADOW_CAMERA_MASK
            lab.hide(SHADOW_CAMERA_MASK)
            self.label_np = lab
        else:
            self.label_np = None
        self.sync()

    def sync(self) -> None:
        if not self.np.isEmpty():
            self.visual.setTransform(self.np.getTransform(self.mgr.game.render))

    def available(self) -> bool:
        return self.hidden_timer <= 0.0

    def taken(self) -> None:
        if self.static and self.respawn > 0:
            self.hidden_timer = self.respawn
            self.visual.hide()
            self.mgr.game.physics.world.removeRigidBody(self.node)
        else:
            self.destroy()

    def update(self, dt: float) -> None:
        if self.hidden_timer > 0:
            self.hidden_timer -= dt
            if self.hidden_timer <= 0:
                self.visual.show()
                self.mgr.game.physics.world.attachRigidBody(self.node)
        elif not self.static:
            self.sync()

    def destroy(self) -> None:
        game = self.mgr.game
        if not self.np.isEmpty():
            game.physics.world.removeRigidBody(self.node)
            self.np.removeNode()
        self.visual.removeNode()
        if self.label_np is not None:
            self.label_np.removeNode()
        if self in self.mgr.items:
            self.mgr.items.remove(self)


class PickupManager:
    MAX_DROPPED = 24

    def __init__(self, game):
        self.game = game
        self.items: list[Pickup] = []

    def spawn(self, kind: str, item, pos, heading=0.0, vel=None, static=False, respawn=0.0, label="") -> Pickup:
        p = Pickup(self, kind, item, Point3(*pos), heading, vel, static, respawn, label)
        self.items.append(p)
        dropped = [i for i in self.items if not i.static]
        if len(dropped) > self.MAX_DROPPED:
            dropped[0].destroy()
        return p

    def update(self, dt: float) -> None:
        for p in list(self.items):
            p.update(dt)

    def look_at(self, eye: Point3, direction: Vec3) -> Pickup | None:
        res = self.game.physics.world.rayTestClosest(eye, eye + direction * REACH, GROUP_PICKUP)
        if res.hasHit():
            p = res.getNode().getPythonTag("pickup")
            if p is not None and p.available():
                return p
        # forgiving fallback: nearest pickup within a narrow cone
        best, best_d = None, 1e9
        for p in self.items:
            if not p.available():
                continue
            c = p.np.getPos(self.game.render)
            to = c - eye
            d = to.length()
            if d > REACH or d < 1e-3:
                continue
            if to.normalized().dot(direction) > math.cos(math.radians(12)) and d < best_d:
                best, best_d = p, d
        return best

    def near_feet(self, feet: Point3, radius: float = 0.9) -> list[Pickup]:
        out = []
        for p in self.items:
            if p.static or not p.available():
                continue
            if (p.np.getPos(self.game.render) - feet).length() < radius:
                out.append(p)
        return out
