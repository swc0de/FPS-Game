"""Tactical map analysis (ai/v2/tactical_map.py) on a small synthetic level:
a floor split by a wall with a doorway, a crate, and a breakable panel."""
import math
import random
import unittest
from types import SimpleNamespace

from panda3d.core import NodePath, Point3

from ai.navmesh import NavMesh
from ai.v2.tactical_map import TacticalMap
from engine.physics import MASK_SIGHT, PhysicsWorld

FLOOR = ((0, 0, -0.1), (26, 16, 0.2), (0, 0, 0), "concrete")
WALL_S = ((0, -4.3, 1.5), (0.2, 7.4, 3.0), (0, 0, 0), "concrete")      # wall at x = 0 with a 1.2 m door
WALL_N = ((0, 4.3, 1.5), (0.2, 7.4, 3.0), (0, 0, 0), "concrete")
CRATE = ((5.0, 3.0, 0.6), (1.0, 1.0, 1.2), (0, 0, 0), "wood")          # crouch cover, not standing cover
PANEL = SimpleNamespace(center=(-6.0, -4.0, 1.5), size=(0.1, 4.0, 3.0), hpr=(0, 0, 0), surface="plaster",
                        index=0)


def make_level(with_panel=False):
    colliders = [FLOOR, WALL_S, WALL_N, CRATE]
    lvl = SimpleNamespace(
        colliders=colliders,
        panel_specs=[PANEL] if with_panel else [],
        data={"bounds": [[-12, -7, -1], [12, 7, 4]], "practice_positions": {"defend": [[8, -3, 0, 90, 0]]},
              "test_routes": [{"name": "lane", "start": [-9, 0, 0.1, 90], "waypoints": [[0, 0], [8, 0]]}]},
        spawns=[{"pos": (-9.0, 0.0, 0.0), "team": "attack"}, {"pos": (9.0, 0.0, 0.0), "team": "defend"}],
        zones=[{"name": "A", "kind": "bombsite", "min": [4, -6, -0.5], "max": [11, 6, 3]}],
        callout_at=lambda x, y: "West" if x < 0 else "East")
    nav = NavMesh.build(lvl.colliders + [(p.center, p.size, p.hpr, p.surface) for p in lvl.panel_specs],
                        lvl.data["bounds"], [s["pos"] for s in lvl.spawns])
    return lvl, nav


class TacticalMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.level, cls.nav = make_level()
        cls.tm = TacticalMap.build(cls.level, cls.nav)

    def near(self, x, y):
        return self.tm.nearest((x, y, 0.0))

    def test_points_cover_both_rooms_and_find_the_doorway(self):
        tm = self.tm
        self.assertGreater(tm.n, 20)
        self.assertTrue((tm.pos[:, 0] < -3).any() and (tm.pos[:, 0] > 3).any())
        doors = [i for i in range(tm.n) if tm.kind[i] & 1]
        self.assertTrue(any(abs(tm.pos[i][0]) < 0.6 and abs(tm.pos[i][1]) < 0.8 for i in doors), "no doorway point")
        self.assertEqual(tm.area(self.near(-6, 3)), "West")
        self.assertEqual(tm.site_name(self.near(8, 0)), "A")

    def test_wall_blocks_and_doorway_lets_through(self):
        tm = self.tm
        self.assertFalse(tm.sees(self.near(-6, 4.5), self.near(6, 4.5)))
        self.assertTrue(tm.sees(self.near(-6, 4.5), self.near(-3, -4.5)))      # same room
        lane_w = min(range(tm.n), key=lambda i: math.dist(tm.pos[i][:2], (-9, 0)))
        lane_e = min(range(tm.n), key=lambda i: math.dist(tm.pos[i][:2], (8, 0)))
        self.assertTrue(tm.sees(lane_w, lane_e), "straight through the doorway")
        self.assertEqual(tm.sees(lane_w, lane_e), tm.sees(lane_e, lane_w))

    def test_agrees_with_bullet_rays(self):
        tm = self.tm
        phys = PhysicsWorld(NodePath("render"))
        for c, size, hpr, surf in self.level.colliders:
            phys.add_static_box(c, [s * 0.5 for s in size], hpr, surface=surf)
        rng = random.Random(4)
        agree = total = 0
        for _ in range(400):
            i, j = rng.randrange(tm.n), rng.randrange(tm.n)
            a, b = tm.pos[i], tm.pos[j]
            if i == j or math.dist(a, b) > tm.cfg.max_range:
                continue
            e = tm.cfg.eye_stand
            clear = phys.ray_cast(Point3(*a[:2], a[2] + e), Point3(*b[:2], b[2] + e), MASK_SIGHT) is None
            agree += clear == tm.sees(i, j)
            total += 1
        self.assertGreater(total, 100)
        self.assertGreaterEqual(agree / total, 0.95)

    def test_crouch_cover_behind_the_crate(self):
        tm = self.tm
        i = self.near(3.9, 3.0)
        self.assertLess(abs(tm.pos[i][0] - 4.0), 1.6)
        if math.dist(tm.pos[i][:2], (4.0, 3.0)) < 0.9:
            self.assertTrue(tm.cover_toward(i, 1.0, 0.0))                 # crate towards +x at crouch height
            self.assertFalse(tm.cover_toward(i, 1.0, 0.0, high=True))     # not at standing height
        self.assertTrue(tm.cover_toward(self.near(-0.6, 5.0), 1.0, 0.0, high=True))   # the wall

    def test_graph_connects_through_the_doorway_and_spawn_times(self):
        tm = self.tm
        west = self.near(-8, -5)
        east = self.near(8, 5)
        self.assertLess(tm.eta["attack"][west], tm.eta["attack"][east])
        self.assertLess(tm.eta["attack"][east], 1e3, "east room reachable from the west spawn")
        self.assertLess(tm.eta["defend"][east], tm.eta["defend"][west])

    def test_site_spots(self):
        tm = self.tm
        entries = tm.entries("A")
        self.assertTrue(entries, "an entry where the lane enters the site")
        holds = tm.holds("A")
        self.assertTrue(holds)
        for h in holds:
            self.assertEqual(tm.site_name(h["i"]) or "near", tm.site_name(h["i"]) or "near")
            self.assertTrue(any(tm.sees(h["i"], e) for e in entries))

    def test_cache_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.npz"
            self.tm.save(path)
            tm2 = TacticalMap.load(path)
        self.assertIsNotNone(tm2)
        self.assertEqual(tm2.n, self.tm.n)
        self.assertTrue((tm2.vis == self.tm.vis).all())
        self.assertEqual(tm2.holds("A"), self.tm.holds("A"))


class BreakablePanelTests(unittest.TestCase):
    def test_panel_blocks_until_opened(self):
        level, nav = make_level(with_panel=True)
        tm = TacticalMap.build(level, nav)
        self.assertGreater(len(tm.soft_pairs), 0)
        i, j, pid = (int(v) for v in tm.soft_pairs[0])
        self.assertEqual(pid, 0)
        self.assertFalse(tm.sees(i, j))
        tm.on_panel(PANEL)

        class NoWall:                      # the panel has been shot away: every ray is clear
            def ray_cast(self, a, b, mask):
                return None
        while tm.update(NoWall(), budget=64):
            pass
        self.assertTrue(tm.sees(i, j))
        tm.on_panel(None)                  # new round: walls restored
        self.assertFalse(tm.sees(i, j))


if __name__ == "__main__":
    unittest.main()
