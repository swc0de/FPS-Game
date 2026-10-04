"""Ballistics tests: hitboxes, hit groups, falloff, penetration (headless Bullet)."""
import unittest

from panda3d.core import NodePath, Point3, TransformState

from engine.physics import PhysicsWorld
from gameplay.damage import Damageable
from gameplay.hitboxes import HitboxRig
from weapons.ballistics import Ballistics
from weapons.defs import WeaponDatabase

DB = WeaponDatabase()


class Target:
    def __init__(self, phys, pos, armor=0, helmet=False):
        self.damageable = Damageable(armor=armor, helmet=helmet)
        self.rig = HitboxRig(phys, self)
        self.rig.update(TransformState.makePos(Point3(*pos)))
        phys.step(1 / 64)


def world():
    phys = PhysicsWorld(NodePath("root"))
    phys.add_static_box((0, 0, -0.5), (50, 50, 0.5), surface="dirt")
    return phys


class BallisticsTests(unittest.TestCase):
    def test_headshot_and_hitgroups(self):
        phys = world()
        Target(phys, (0, 10, 0))
        bal = Ballistics(phys, DB)
        w = DB.weapons["p9"]
        res = bal.fire((0, 0, 1.635), (0, 1, 0), w)
        self.assertEqual(res.damage[0].hitgroup, "head")
        self.assertTrue(res.damage[0].killed)
        Target(phys, (3, 10, 0))
        res = bal.fire((3.1, 0, 0.6), (0, 1, 0), w)
        self.assertEqual(res.damage[0].hitgroup, "leg")
        expected = 30 * 0.91 ** ((10 - 0.085) / 10) * 0.75
        self.assertAlmostEqual(res.damage[0].health, expected, delta=0.5)

    def test_ignores_own_hitboxes(self):
        phys = world()
        me = Target(phys, (0, 0, 0))
        bal = Ballistics(phys, DB)
        res = bal.fire((0, 0, 1.3), (0, 1, 0), DB.weapons["r7"], attacker=me)
        self.assertEqual(res.damage, [])

    def test_penetrates_plaster_but_not_concrete(self):
        phys = world()
        phys.add_static_box((0, 5, 1.5), (2, 0.1, 1.5), surface="plaster")    # 20 cm plaster
        phys.add_static_box((6, 5, 1.5), (2, 0.15, 1.5), surface="concrete")  # 30 cm concrete
        Target(phys, (0, 10, 0))
        Target(phys, (6, 10, 0))
        bal = Ballistics(phys, DB)
        rifle = DB.weapons["r7"]
        res = bal.fire((0, 0, 1.3), (0, 1, 0), rifle)
        self.assertEqual(res.penetrations, 1)
        self.assertEqual(len(res.damage), 1)
        self.assertLess(res.damage[0].health, rifle.damage)        # lost damage in the wall
        self.assertTrue(any(i.exit for i in res.impacts))
        res = bal.fire((6, 0, 1.3), (0, 1, 0), rifle)
        self.assertEqual(res.damage, [])
        self.assertAlmostEqual(res.end.y, 4.85, delta=0.02)
        # pistol cannot get through 20 cm of plaster (cost 12 >= budget 12)
        res = bal.fire((0, 0, 1.3), (0, 1, 0), DB.weapons["p9"])
        self.assertEqual(res.damage, [])

    def test_overlapping_boxes_merge(self):
        phys = world()
        phys.add_static_box((0, 5, 1.5), (2, 0.05, 1.5), surface="wood")
        phys.add_static_box((0, 5.08, 1.5), (2, 0.05, 1.5), surface="wood")  # overlaps 2 cm
        Target(phys, (0, 10, 0))
        bal = Ballistics(phys, DB)
        res = bal.fire((0, 0, 1.3), (0, 1, 0), DB.weapons["r7"])
        self.assertEqual(res.penetrations, 1)
        exits = [i for i in res.impacts if i.exit]
        self.assertEqual(len(exits), 1)
        self.assertAlmostEqual(exits[0].pos.y, 5.13, delta=0.01)

    def test_armor(self):
        phys = world()
        t = Target(phys, (0, 5, 0), armor=100)
        bal = Ballistics(phys, DB)
        res = bal.fire((0, 0, 1.3), (0, 1, 0), DB.weapons["r7"])
        self.assertLess(res.damage[0].health, 36 * 0.776)
        self.assertLess(t.damageable.armor, 100)


if __name__ == "__main__":
    unittest.main()
