"""Ballistics tests: hitboxes, hit groups, falloff, penetration (headless Bullet)."""
import math
import unittest

from panda3d.core import Mat4, NodePath, Point3, TransformState, Vec3

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


class ExactCapsuleTests(unittest.TestCase):
    """Bullet tests rays against capsules with a tolerance (hits up to ~6 mm outside, entries up
    to 5 mm early); the soldiers' hit capsules are re-tested exactly (engine/physics.py)."""

    def test_segment_capsule_matches_sampling(self):
        import random
        from engine.physics import segment_capsule
        rng = random.Random(3)

        def inside(p, a, b, r):
            ab, ap = b - a, p - a
            t = min(max(ap.dot(ab) / ab.dot(ab), 0.0), 1.0)
            return (ap - ab * t).length() <= r
        n = 400
        for _ in range(150):
            a = Point3(*(rng.uniform(-0.3, 0.3) for _ in range(3)))
            b = a + Vec3(*(rng.uniform(-0.3, 0.3) for _ in range(3)))
            r = rng.uniform(0.03, 0.2)
            s = Point3(*(rng.uniform(-1, 1) for _ in range(3))) * 1.5
            e = a + (b - a) * rng.uniform(0, 1) + Vec3(*(rng.uniform(-0.25, 0.25) for _ in range(3)))
            e = s + (e - s) * 2.0                                   # through or near the capsule
            if inside(s, a, b, r):
                continue
            first = next((k / n for k in range(n + 1) if inside(s + (e - s) * (k / n), a, b, r)), None)
            got = segment_capsule(s, e, a, b, r)
            if first is None:
                self.assertTrue(got is None or got[1] - got[0] < 2.0 / n)  # a graze between samples
                continue
            self.assertIsNotNone(got)
            self.assertLessEqual(got[0], first + 1e-9)
            self.assertGreater(got[0], first - 1.0 / n - 1e-9)
            p = got[2]
            self.assertAlmostEqual((p - a - (b - a) * min(max((p - a).dot(b - a) / (b - a).dot(b - a), 0), 1)).length(),
                                   r, places=5)

    def _body(self):
        from tests.test_skeleton import make_body
        game, body = make_body()
        for _ in range(8):
            body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        game.physics.step(1 / 64)
        return game, body

    def _probe(self, game, body, name, offset):
        """A ray across the middle of capsule ``name`` at ``offset`` from its axis, away from
        the body: (start, end, its Bullet node)."""
        from gameplay.hitboxes import CAPSULES, capsule_length
        c = next(c for c in CAPSULES if c.name == name)
        m = body.hit_mounts[name].getMat(game.render)
        mid = m.xformPoint(Point3(0, 0, 0))
        axis = m.xformVec(Vec3(0, 0, 1))
        axis.normalize()
        out = mid - Point3(0, 0, mid.z)                              # away from the body's centre line
        out = Vec3(out - axis * out.dot(axis))
        out.normalize()
        d = axis.cross(out)
        node = next(np_.node() for part, np_ in body.rig.parts if part.name == name)
        start = mid + out * (c.radius + offset) - d * 1.0
        self.assertLess(capsule_length(c), 1.0)
        return start, start + d * 2.0, node

    def test_grazing_rays_miss_and_entries_are_on_the_surface(self):
        from engine.physics import GROUP_HITBOX
        game, body = self._body()
        phys = game.physics
        for name in ("upper_arm_l", "thigh_r", "forearm_r"):
            s, e, node = self._probe(game, body, name, 0.003)
            raw = [h.getNode() for h in phys.world.rayTestAll(s, e, GROUP_HITBOX).getHits()]
            self.assertIn(node, raw, name)                        # Bullet: a hit 3 mm outside
            self.assertNotIn(node, [h.node for h in phys.ray_cast_all(s, e, GROUP_HITBOX)], name)
            s, e, node = self._probe(game, body, name, -0.003)
            hit = next(h for h in phys.ray_cast_all(s, e, GROUP_HITBOX) if h.node == node)
            from gameplay.hitboxes import CAPSULES
            c = next(c for c in CAPSULES if c.name == name)
            inv = Mat4(body.hit_mounts[name].getMat(game.render))
            inv.invertInPlace()
            local = inv.xformPoint(hit.pos)
            self.assertAlmostEqual(math.hypot(local.x, local.y), c.radius, places=5)

    def test_shots_test_the_pose_of_the_last_step(self):
        """Between steps Bullet keeps the capsules where it synced them; the exact test agrees."""
        from engine.physics import GROUP_HITBOX
        game, body = self._body()
        s, e, node = self._probe(game, body, "thigh_l", -0.02)
        body.root.setPos(0.5, 0, 0)                                # moved, not stepped
        self.assertIn(node, [h.node for h in game.physics.ray_cast_all(s, e, GROUP_HITBOX)])
        game.physics.step(1 / 64)
        self.assertNotIn(node, [h.node for h in game.physics.ray_cast_all(s, e, GROUP_HITBOX)])
        # a pose set but not yet copied to the nodes is not what Bullet synced either
        body.root.setPos(0, 0, 0)
        game.physics.step(1 / 64)
        from gameplay import skeleton as sk
        body.pose.set_hpr(sk.INDEX["thigh_l"], 0.0, 70.0, 0.0)
        game.physics.step(1 / 64)
        self.assertIn(node, [h.node for h in game.physics.ray_cast_all(s, e, GROUP_HITBOX)])


if __name__ == "__main__":
    unittest.main()
