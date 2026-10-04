"""Match participants in the world.

* ``PlayerAgent`` - the human: wraps the player controller and its weapons.
* ``StandInAgent`` - Milestone 4 placeholder for the other team: a uniformed
  mannequin with hitboxes that holds a position and can be killed (it does
  not move or shoot). Milestone 5 replaces stand-ins with AI bots that use
  the same ``Participant`` interface.
"""
from __future__ import annotations

from panda3d.core import LVecBase4f, Point3, Vec3

from gameplay.damage import Damageable, DamageResult
from gameplay.hitboxes import HitboxRig, build_mannequin, pose_mannequin
from gameplay.match import Participant


class PlayerAgent(Participant):
    def __init__(self, game, name: str = "You"):
        super().__init__(name, is_human=True)
        self.game = game
        self.has_kit = False
        game.player.agent = self

    @property
    def damageable(self) -> Damageable:
        return self.game.player.damageable

    @property
    def alive(self) -> bool:
        return self.game.player.damageable.alive

    def position(self) -> Point3:
        return Point3(self.game.player.char.pos)

    def center_of_mass(self) -> Point3:
        return self.game.player.center_of_mass()


class StandInAgent(Participant):
    def __init__(self, game, name: str, side: str, uniform: str, weapon: str):
        super().__init__(name)
        self.game = game
        self.weapon_key = weapon
        self.has_kit = False
        self.crouch = 0.0
        self.root = game.render.attachNewNode(f"standin:{name}")
        self.body = self.root.attachNewNode("body")
        self.nodes = build_mannequin(game.materials, self.body, body_mat=uniform, helmet=True, vest=True)
        self.gun = None
        self._uniform = uniform
        self.damageable = Damageable(armor=100, helmet=True, name=name, team=side)
        self.rig = HitboxRig(game.physics, self, surface="flesh")
        self.flash = 0.0
        self.fall = 0.0
        self.set_weapon(weapon)
        self.root.hide()

    # ------------------------------------------------------------ setup
    def set_weapon(self, key: str) -> None:
        from weapons.models import build_weapon_model
        if self.gun is not None:
            self.gun.removeNode()
        self.weapon_key = key
        model = self.game.weapon_db.weapons[key].model
        self.gun = build_weapon_model(self.game.materials, model, self.body).root
        self._place_gun()

    def _place_gun(self) -> None:
        if self.crouch > 0.5:
            self.gun.setPosHpr(0.17, 0.42, 0.66, 0, 4, 0)
        else:
            self.gun.setPosHpr(0.17, 0.36, 1.19, 0, 2, 0)

    def place(self, pos, heading: float, crouch: bool = False) -> None:
        self.crouch = 1.0 if crouch else 0.0
        self.root.setPos(Point3(*pos))
        self.root.setH(heading)
        self.body.setHpr(0, 0, 0)
        self.body.setPos(0, 0, 0)
        pose_mannequin(self.nodes, self.crouch)
        self._place_gun()
        self.root.show()
        self.rig.set_enabled(True)
        self.rig.update(self.root.getTransform(self.game.render), self.crouch)

    def reset_for_round(self, keep_armor: bool) -> None:
        self.damageable.reset(armor=100, helmet=True)
        self.alive_flag = True
        self.fall = 0.0
        self.flash = 0.0
        self.gun.show()

    def remove(self) -> None:
        self.root.hide()
        self.rig.set_enabled(False)

    def destroy(self) -> None:
        self.rig.destroy()
        self.root.removeNode()

    # --------------------------------------------------------- interface
    @property
    def alive(self) -> bool:
        return self.damageable.alive

    def position(self) -> Point3:
        return self.root.getPos(self.game.render)

    def center_of_mass(self) -> Point3:
        return self.position() + Vec3(0, 0, 0.7 if self.crouch > 0.5 else 1.1)

    def forward(self) -> Vec3:
        return self.game.render.getRelativeVector(self.root, Vec3(0, 1, 0))

    def on_hit(self, res: DamageResult, pos, direction) -> None:
        self.flash = 0.12
        if res.killed:
            self.rig.set_enabled(False)
            self.fall = 1e-3
            self.gun.hide()
            self.game.drop_weapon_at(self.weapon_key, self.position() + Vec3(0, 0, 0.1), self.root.getH())

    # ------------------------------------------------------------- frame
    def frame_update(self, dt: float) -> None:
        if self.flash > 0:
            self.flash -= dt
            k = max(self.flash, 0.0) / 0.12
            self.body.setShaderInput("u_emission", LVecBase4f(0.5 * k, 0.04 * k, 0.02 * k, 0))
        if self.fall > 0 and self.fall < 1.0:
            self.fall = min(self.fall + dt * 2.6, 1.0)
            t = self.fall * self.fall
            self.body.setP(-88.0 * t)
            self.body.setZ(0.12 * t)
