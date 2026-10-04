"""Navmesh generation, path finding and path following on small synthetic levels
and on the compound."""
import math
import random
import unittest

from panda3d.core import NodePath

from ai.navmesh import NavConfig, NavMesh, string_pull
from ai.steering import PathFollower
from engine.physics import PhysicsWorld
from gameplay.character import KinematicCharacter, MoveInput, load_movement_config

CFG = load_movement_config()


def box(x0, y0, z0, x1, y1, z1, hpr=(0, 0, 0)):
    return ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2), (x1 - x0, y1 - y0, z1 - z0), hpr, "concrete"


def room_level():
    """20x20 m floor split by a wall with a 1 m door, a 1.5 m platform with
    stairs, a low beam (crouch only) and a tunnel under a raised deck."""
    c = [box(-10, -10, -0.3, 10, 10, 0.0)]
    # wall at y = 0 with a 1.0 m door at x in [2, 3]
    c.append(box(-10, -0.1, 0, 2, 0.1, 3))
    c.append(box(3, -0.1, 0, 10, 0.1, 3))
    # platform 1.5 m high at x in [-9, -5], y in [3, 7], stairs climbing +x towards it
    c.append(box(-9, 3, 0, -5, 7, 1.5))
    for k in range(10):
        c.append(box(-5 + k * 0.3, 4, 0, -4.7 + k * 0.3, 6, 1.5 - k * 0.15))
    # low beam: passage under it is only 1.3 m high
    c.append(box(1, 5, 1.3, 10, 6, 1.6))
    return c


def nav_for(colliders, seeds=((0, -5, 0),)):
    return NavMesh.build(colliders, [[-10, -10, -1], [10, 10, 5]], seeds, NavConfig.from_movement(CFG))


def path_is_walkable(nav, path):
    return all(nav.walkable_line(a, b) for a, b in zip(path, path[1:]))


class NavmeshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nav = nav_for(room_level())

    def test_wall_blocks_and_door_passes(self):
        nav = self.nav
        # nothing walkable inside or right next to the wall
        self.assertLess(nav.locate((-5, 0, 0), search=0), 0)
        self.assertLess(nav.locate((-5, 0.3, 0), search=0), 0)
        # the eroded mesh keeps the hull radius off the wall
        self.assertGreaterEqual(nav.locate((-5, 0.6, 0), search=0), 0)
        path = nav.find_path((-6, -5, 0), (0, 5, 0))
        self.assertIsNotNone(path)
        # the path goes through the door
        crossing = [a[0] + (b[0] - a[0]) * (0 - a[1]) / (b[1] - a[1])
                    for a, b in zip(path, path[1:]) if (a[1] < 0) != (b[1] < 0)]
        self.assertEqual(len(crossing), 1, path)
        self.assertTrue(2.25 < crossing[0] < 2.75, path)
        self.assertTrue(path_is_walkable(nav, path))

    def test_shortest_path_hugs_the_door_corner(self):
        path = self.nav.find_path((-6, -3, 0), (-6, 3, 0))
        length = self.nav.path_length(path)
        # the hull (0.3 m) must clear the wall ends: best line passes x = 2.375, y = -0.5 .. 0.5
        ideal = math.dist((-6, -3), (2.375, -0.5)) + 1.0 + math.dist((2.375, 0.5), (-6, 3))
        self.assertLess(length, ideal + 0.3)

    def test_stairs_reach_the_platform(self):
        nav = self.nav
        top = nav.locate((-7, 5, 1.5), search=0)
        self.assertGreaterEqual(top, 0)
        self.assertAlmostEqual(nav.node_pos(top)[2], 1.5, places=3)
        path = nav.find_path((0, 5, 0), (-7, 5, 1.5))
        self.assertIsNotNone(path)
        self.assertAlmostEqual(path[-1][2], 1.5, places=3)

    def test_platform_edge_is_not_a_shortcut(self):
        # walking off the 1.5 m edge is not a link: a straight line is not walkable
        self.assertFalse(self.nav.walkable_line((-7, 5, 1.5), (-7, 9, 0)))

    def test_low_beam_needs_crouch(self):
        self.assertTrue(self.nav.is_crouch((6, 5.5, 0)))
        self.assertFalse(self.nav.is_crouch((6, 8.5, 0)))

    def test_unreachable_regions_are_pruned(self):
        # a closed box room is not reachable from the seed
        c = room_level() + [box(5, -8, 0, 9, -7.8, 3), box(5, -4.2, 0, 9, -4, 3),
                            box(5, -8, 0, 5.2, -4, 3), box(8.8, -8, 0, 9, -4, 3)]
        nav = nav_for(c)
        self.assertLess(nav.locate((7, -6, 0), search=0), 0)
        self.assertGreaterEqual(nav.locate((0, -6, 0), search=0), 0)

    def test_tunnel_under_a_deck_has_two_levels(self):
        c = [box(-10, -10, -0.3, 10, 10, 0.0),
             box(-3, -10, 2.5, 3, 10, 2.8),                 # deck above the floor (2.5 m of headroom)
             box(-3.2, -10, 0, -3, 10, 2.5), box(3, -10, 0, 3.2, 10, 2.5)]
        nav = NavMesh.build(c, [[-10, -10, -1], [10, 10, 5]], [(0, 0, 0), (0, 0, 2.8)],
                            NavConfig.from_movement(CFG))
        low = nav.locate((0, 0, 0), search=0)
        high = nav.locate((0, 0, 2.8), search=0)
        self.assertGreaterEqual(low, 0)
        self.assertGreaterEqual(high, 0)
        self.assertNotEqual(low, high)
        self.assertAlmostEqual(nav.node_pos(low)[2], 0.0, places=3)
        self.assertAlmostEqual(nav.node_pos(high)[2], 2.8, places=3)

    def test_cache_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nav.npz"
            self.nav.save(path)
            again = NavMesh.load(path)
        self.assertEqual(again.node_count, self.nav.node_count)
        self.assertEqual(again.find_path((-6, -3, 0), (-6, 3, 0)), self.nav.find_path((-6, -3, 0), (-6, 3, 0)))

    def test_string_pull_straight_corridor(self):
        portals = [((1, 1), (1, -1)), ((2, 1), (2, -1)), ((3, 1), (3, -1))]
        self.assertEqual(string_pull((0, 0), (4, 0), portals), [(0, 0), (4, 0)])

    def test_string_pull_turns_at_inner_corner(self):
        # corridor east then north: the path bends at the inner corner (1, 1)
        portals = [((1, 1), (1, -1)), ((1, 1), (3, 1))]
        pts = string_pull((0, 0), (2, 4), portals)
        self.assertEqual(pts, [(0, 0), (1, 1), (2, 4)])


class FollowTests(unittest.TestCase):
    """A character driven by the path follower reaches its goals through real Bullet collision."""

    @classmethod
    def setUpClass(cls):
        cls.colliders = room_level()
        cls.nav = nav_for(cls.colliders)
        cls.phys = PhysicsWorld(NodePath("root"))
        for c, size, hpr, surface in cls.colliders:
            cls.phys.add_static_box(c, [s * 0.5 for s in size], hpr, surface=surface)

    def walk(self, start, goal, limit=40.0):
        ch = KinematicCharacter(self.phys, CFG)
        ch.teleport(start)
        f = PathFollower(self.nav)
        self.assertTrue(f.go_to(start, goal))
        t, dt = 0.0, 1 / 64
        crouched = False
        while t < limit and f.active:
            wish, crouch, jump = f.update(dt, ch.pos, ch.horizontal_speed)
            crouched |= crouch
            ch.step(dt, MoveInput(wish_dir=wish, crouch=crouch, jump=jump))
            t += dt
        return f, ch, crouched

    def test_walk_through_the_door_and_up_the_stairs(self):
        f, ch, _ = self.walk((5, -6, 0), (-7, 5, 1.5))
        self.assertTrue(f.arrived)
        self.assertAlmostEqual(ch.pos.z, 1.5, delta=0.05)

    def test_crouch_under_the_beam(self):
        f, ch, crouched = self.walk((6, 2, 0), (6, 8.5, 0))
        self.assertTrue(f.arrived)
        self.assertTrue(crouched)

    def test_random_walks_arrive(self):
        rng = random.Random(4)
        for _ in range(12):
            a = self.nav.random_point(rng)
            b = self.nav.random_point(rng)
            f, ch, _ = self.walk(a, b, 60.0)
            self.assertTrue(f.arrived, (a, b, tuple(ch.pos)))


class CompoundNavTests(unittest.TestCase):
    def test_compound_lanes_are_connected(self):
        from tests.test_maps import build
        data, ctx = build("compound")
        nav = NavMesh.build(ctx.colliders, data["bounds"], [s["pos"] for s in ctx.spawns],
                            NavConfig.from_movement(CFG))
        att = next(s["pos"] for s in ctx.spawns if s["team"] == "attack")
        dfn = next(s["pos"] for s in ctx.spawns if s["team"] == "defend")
        for z in ctx.zones:
            if z["kind"] != "bombsite":
                continue
            c = ((z["min"][0] + z["max"][0]) / 2, (z["min"][1] + z["max"][1]) / 2, z["min"][2] + 0.6)
            for s in (att, dfn):
                self.assertIsNotNone(nav.find_path(s, c), f"{s} -> site {z['name']}")
        for r in data["test_routes"]:
            for w in r["waypoints"]:
                self.assertGreaterEqual(nav.locate((w[0], w[1], w[2] if len(w) > 2 else 0.0)), 0,
                                        f"{r['name']} waypoint {w}")
        for side, positions in data["practice_positions"].items():
            for p in positions:
                self.assertGreaterEqual(nav.locate(p[:3]), 0, f"{side} position {p}")


if __name__ == "__main__":
    unittest.main()
