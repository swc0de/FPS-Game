"""Destructible panels: chunk damage, collision through holes, reinforcement,
round reset, decal removal and navmesh links through holes."""
import math
import unittest

import numpy as np
from panda3d.core import NodePath, Point3, Vec3

from ai.navlinks import NavLinks
from ai.navmesh import NavConfig, NavMesh
from engine.physics import MASK_BULLETS, PhysicsWorld
from gameplay.character import load_movement_config
from gameplay.destruction import DestructionManager, PanelSpec, _decals_in_holes, merge_rects

CFG = load_movement_config()


class _Mat:
    uv_scale = 1.0

    def apply(self, np_):
        pass


class _Materials:
    def get(self, name):
        return _Mat()


class _Decals:
    def __init__(self):
        self.calls = 0

    def remove_where(self, pred):
        self.calls += 1
        return 0


class _Effects:
    def __init__(self):
        self.decals = _Decals()
        self.bursts = 0

    def debris_burst(self, *a, **k):
        self.bursts += 1


class _Audio:
    def play_at(self, *a, **k):
        pass


class FakeGame:
    def __init__(self):
        self.render = NodePath("render")
        self.physics = PhysicsWorld(self.render)
        self.materials = _Materials()
        self.effects = _Effects()
        self.audio = _Audio()
        self.noises = 0

    def notify_noise(self, *a, **k):
        self.noises += 1


def wall_spec(name="w", reinforce=False, x0=-3.0, x1=3.0, y=0.0, h=3.0, t=0.15, surface="plaster"):
    return PanelSpec(((x0 + x1) / 2, y, h / 2), (x1 - x0, t, h), (0, 0, 0), "plaster", "plaster", surface,
                     reinforceable=reinforce, name=name)


def ray_blocked(game, a, b) -> bool:
    return game.physics.world.rayTestClosest(Point3(*a), Point3(*b), MASK_BULLETS).hasHit()


class MergeRectsTests(unittest.TestCase):
    def test_cover_is_exact_and_disjoint(self):
        rng = np.random.default_rng(3)
        for _ in range(20):
            alive = rng.random((9, 7)) > 0.35
            cover = np.zeros(alive.shape, int)
            for i0, i1, j0, j1 in merge_rects(alive):
                cover[i0:i1 + 1, j0:j1 + 1] += 1
            self.assertTrue(np.array_equal(cover, alive.astype(int)))

    def test_full_panel_is_one_box(self):
        self.assertEqual(merge_rects(np.ones((24, 12), bool)), [(0, 23, 0, 11)])


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.g = FakeGame()
        self.mgr = DestructionManager(self.g, [wall_spec()])
        self.p = self.mgr.panels[0]

    def test_grid(self):
        p = self.p
        self.assertEqual(p.thin, 1)
        self.assertEqual((p.nu, p.nv), (24, 12))
        self.assertAlmostEqual(p.cu, 0.25)
        self.assertEqual(p.body_np.node().getNumShapes(), 1)

    def test_bullets_open_a_hole_and_rays_pass(self):
        g, p = self.g, self.p
        a, b = (0.1, -2, 1.4), (0.1, 2, 1.4)
        self.assertTrue(ray_blocked(g, a, b))
        node = p.body_np.node()
        for _ in range(3):
            self.mgr.bullet_hit(node, Point3(0.1, -0.075, 1.4), Vec3(0, 1, 0), 30.0)
        self.assertGreater(p.destroyed_fraction(), 0)
        self.assertTrue(p.dirty)
        self.mgr.flush()
        self.assertFalse(p.dirty)
        self.assertFalse(ray_blocked(g, a, b))
        # still solid a metre away
        self.assertTrue(ray_blocked(g, (1.2, -2, 1.4), (1.2, 2, 1.4)))
        self.assertGreater(p.body_np.node().getNumShapes(), 1)
        self.assertGreater(g.effects.bursts, 0)
        self.assertGreater(g.effects.decals.calls, 0)

    def test_single_bullet_only_chips(self):
        p = self.p
        self.mgr.bullet_hit(p.body_np.node(), Point3(0.1, -0.075, 1.4), Vec3(0, 1, 0), 30.0)
        self.assertEqual(p.destroyed_fraction(), 0.0)
        self.assertLess(p.hp.min(), p.max_hp)

    def test_explosion_radius(self):
        p = self.p
        n = self.mgr.explosion((0, -0.3, 1.5), 260.0, 1.0)
        self.assertGreater(n, 20)
        # chunks far from the blast survive
        self.assertTrue(p.alive[0, 0] and p.alive[-1, -1])
        dead = ~p.alive
        ii, jj = np.nonzero(dead)
        u = p.cu_pos[ii, jj]
        v = p.cv_pos[ii, jj] + 1.5
        self.assertLessEqual(np.max(np.hypot(u, v - 1.5)), 1.0 + 0.2)

    def test_cut_opens_a_door(self):
        p = self.p
        n = self.mgr.cut_at(p, (0, -0.08, 0.9), 1.0, 1.0, 1.1, "charge")
        self.assertGreater(n, 0)
        self.mgr.flush()
        self.assertFalse(ray_blocked(self.g, (0, -2, 0.3), (0, 2, 0.3)))
        self.assertFalse(ray_blocked(self.g, (0, -2, 1.9), (0, 2, 1.9)))
        self.assertTrue(ray_blocked(self.g, (0, -2, 2.6), (0, 2, 2.6)))

    def test_intact_panels_are_drawn_from_the_shared_batch(self):
        mgr, p = self.mgr, self.p
        self.assertTrue(p.intact)
        self.assertIsNone(p.mesh_np)
        self.assertEqual(mgr.batch_np.getNumChildren(), 1)       # one material
        mgr.explosion((0, -0.3, 1.5), 400.0, 1.2)
        mgr.flush()
        self.assertFalse(p.intact)
        self.assertIsNotNone(p.mesh_np)
        self.assertEqual(mgr.batch_np.getNumChildren(), 0)
        mgr.reset()
        self.assertTrue(p.intact)
        self.assertIsNone(p.mesh_np)
        self.assertEqual(mgr.batch_np.getNumChildren(), 1)

    def test_reset_restores(self):
        p = self.p
        self.mgr.explosion((0, -0.3, 1.5), 400.0, 1.2)
        self.mgr.flush()
        self.assertGreater(p.destroyed_fraction(), 0.05)
        seen = []
        self.mgr.listeners.append(seen.append)
        self.mgr.reset()
        self.assertEqual(p.destroyed_fraction(), 0.0)
        self.assertTrue(ray_blocked(self.g, (0, -2, 1.5), (0, 2, 1.5)))
        self.assertEqual(seen, [None])
        self.assertEqual(len(self.g.physics.world.getRigidBodies()), 1)


class ReinforceTests(unittest.TestCase):
    def setUp(self):
        self.g = FakeGame()
        self.mgr = DestructionManager(self.g, [wall_spec(reinforce=True), wall_spec("soft", x0=4, x1=6)])
        self.p, self.q = self.mgr.panels

    def test_only_marked_walls(self):
        self.assertTrue(self.p.can_reinforce())
        self.assertFalse(self.q.can_reinforce())

    def test_heavily_damaged_wall_cannot_be_reinforced(self):
        self.mgr.explosion((0, -0.3, 1.5), 600.0, 1.4)
        self.assertFalse(self.p.can_reinforce())

    def test_reinforced_ignores_bullets_and_explosions(self):
        p = self.p
        p.reinforce()
        self.mgr.flush()
        node = p.body_np.node()
        self.assertEqual(node.getTag("surface"), "reinforced")
        for _ in range(40):
            self.mgr.bullet_hit(node, Point3(0.1, -0.075, 1.4), Vec3(0, 1, 0), 40.0)
        self.assertEqual(self.mgr.explosion((0, -0.3, 1.5), 900.0, 2.0), 0)
        self.assertEqual(p.destroyed_fraction(), 0.0)
        self.assertFalse(p.can_reinforce())

    def test_thermal_cuts_through(self):
        p = self.p
        p.reinforce()
        self.mgr.flush()
        n = self.mgr.cut_at(p, (0, -0.08, 0.9), 0.9, 0.9, 1.0, "thermal")
        self.assertGreater(n, 0)
        self.assertFalse(p.reinforced)
        self.mgr.flush()
        self.assertFalse(ray_blocked(self.g, (0, -2, 1.0), (0, 2, 1.0)))

    def test_reset_removes_plates(self):
        self.p.reinforce()
        self.mgr.flush()
        self.mgr.reset()
        self.assertFalse(self.p.reinforced)
        self.assertIsNone(self.p.plates)


class DecalMaskTests(unittest.TestCase):
    def test_only_decals_over_holes(self):
        g = FakeGame()
        mgr = DestructionManager(g, [wall_spec()])
        p = mgr.panels[0]
        mgr.cut_at(p, (0, -0.08, 1.5), 0.6, 0.3, 0.3, "charge")
        pts = np.array([[0.0, -0.08, 1.5], [2.0, -0.08, 1.5], [0.0, -3.0, 1.5], [0.0, 0.08, 1.45]])
        self.assertEqual(_decals_in_holes(p, pts).tolist(), [True, False, False, True])


class DoorSizedTests(unittest.TestCase):
    def test_only_full_height_wall_panels_and_hatches(self):
        from ai.gadget_ai import GadgetAI
        g = FakeGame()
        specs = [wall_spec("full", x0=0, x1=4, h=3.2),
                 PanelSpec((10, 0, 0.5), (1.4, 0.15, 1.0), (0, 0, 0), "plaster", "plaster", "plaster", name="sill"),
                 PanelSpec((20, 0, 2.7), (1.0, 0.15, 1.0), (0, 0, 0), "plaster", "plaster", "plaster", name="lintel"),
                 PanelSpec((30, 0, 2.85), (1.2, 1.2, 0.3), (0, 0, 0), "wood", "wood", "wood", kind="floor", name="hatch"),
                 PanelSpec((40, 0, 1.6), (4.0, 0.15, 3.2), (90, 0, 0), "plaster", "plaster", "plaster", name="turned")]
        mgr = DestructionManager(g, specs)
        self.assertEqual([GadgetAI.door_sized(p) for p in mgr.panels], [True, False, False, True, True])


def box(x0, y0, z0, x1, y1, z1):
    return ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2), (x1 - x0, y1 - y0, z1 - z0), (0, 0, 0), "concrete"


class NavLinkTests(unittest.TestCase):
    def test_hole_in_a_wall_links_both_sides(self):
        g = FakeGame()
        spec = PanelSpec((0, 0, 1.5), (20, 0.15, 3.0), (0, 0, 0), "plaster", "plaster", "plaster")
        mgr = DestructionManager(g, [spec])
        floor = box(-10, -10, -0.3, 10, 10, 0)
        colliders = [floor, (spec.center, spec.size, spec.hpr, "plaster")]
        nav = NavMesh.build(colliders, [[-10, -10, -1], [10, 10, 5]], [(0, -5, 0), (0, 5, 0)],
                            NavConfig.from_movement(CFG))
        links = NavLinks(nav, mgr)
        self.assertIsNone(nav.find_path((-3, -4, 0), (-3, 4, 0)))
        p = mgr.panels[0]
        # a murder hole at head height is not a passage
        mgr.cut_at(p, (4, -0.08, 1.5), 0.5, 0.25, 0.25, "charge")
        mgr.flush()
        self.assertIsNone(nav.find_path((-3, -4, 0), (-3, 4, 0)))
        # a door-sized breach is
        mgr.cut_at(p, (0, -0.08, 0.0), 1.0, 0.0, 2.0, "charge")
        mgr.flush()
        self.assertIn(0, links.links)
        path = nav.find_path((-3, -4, 0), (-3, 4, 0))
        self.assertIsNotNone(path)
        crossing = [a for a, b in zip(path, path[1:]) if (a[1] < 0) != (b[1] < 0)]
        self.assertEqual(len(crossing), 1)
        a = crossing[0]
        b = path[path.index(a) + 1]
        x = a[0] + (b[0] - a[0]) * (0 - a[1]) / (b[1] - a[1])
        self.assertLess(abs(x), 0.5)
        self.assertLess(nav.path_length(path), 8.0 + 2.0)
        # new round: the wall is whole again
        mgr.reset()
        self.assertIsNone(nav.find_path((-3, -4, 0), (-3, 4, 0)))

    def test_open_hatch_is_a_one_way_drop(self):
        g = FakeGame()
        # a 3 m high roof over a room; the roof is reached from a separate seed
        hatch = PanelSpec((0, 0, 2.85), (1.2, 1.2, 0.3), (0, 0, 0), "wood", "wood", "wood", kind="floor")
        mgr = DestructionManager(g, [hatch])
        colliders = [box(-10, -10, -0.3, 10, 10, 0),
                     box(-5, -5, 2.7, 5, -0.6, 3.0), box(-5, 0.6, 2.7, 5, 5, 3.0),
                     box(-5, -0.6, 2.7, -0.6, 0.6, 3.0), box(0.6, -0.6, 2.7, 5, 0.6, 3.0),
                     (hatch.center, hatch.size, hatch.hpr, "wood")]
        nav = NavMesh.build(colliders, [[-10, -10, -1], [10, 10, 5]], [(3, 3, 3.0), (-8, -8, 0)],
                            NavConfig.from_movement(CFG))
        NavLinks(nav, mgr)
        roof, below = (3, 3, 3.0), (-2, 2, 0.0)
        self.assertIsNone(nav.find_path(roof, below))
        mgr.explosion((0, 0, 3.1), 400.0, 1.1)
        mgr.flush()
        path = nav.find_path(roof, below)
        self.assertIsNotNone(path)
        drop = [pt for pt in path if math.hypot(pt[0], pt[1]) < 0.4]
        self.assertEqual(len(drop), 1)
        self.assertLess(drop[0][2], 0.5)            # the corner is on the floor below: walk on until you fall
        # one way: no climbing back up through the hatch
        self.assertIsNone(nav.find_path(below, roof))


if __name__ == "__main__":
    unittest.main()
