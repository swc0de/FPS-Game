"""v2 knowledge: the possibility field, the delayed radio and the facts a bot keeps."""
import math
import random
import unittest
from types import SimpleNamespace

import numpy as np
from panda3d.core import Point3

from ai.v2.belief import PossibilityField, view_mask
from ai.v2.comms import Radio
from ai.v2.knowledge import BotKnowledge, Fact
from ai.v2.tactical_map import TacticalMap
from tests.test_tactical_map import make_level

LEVEL, NAV = make_level()
TM = TacticalMap.build(LEVEL, NAV)


def near(x, y):
    return TM.nearest((x, y, 0.0))


def run(field, t0, t1, dt=0.05):
    t = t0
    while t < t1:
        t += dt
        field.step(t)
    return t


class BeliefTests(unittest.TestCase):
    def test_round_start_enemies_need_time_to_arrive(self):
        f = PossibilityField(TM, "attack")
        f.reset(10.0, 5)
        west, east = near(-8, 0), near(9, 0)
        self.assertTrue(f.possible(10.5)[west])          # next to the attackers' spawn
        self.assertFalse(f.possible(10.5)[east])         # 17 m away: not yet
        self.assertTrue(f.possible(10.0 + 20.0 / 5.4 + 0.5)[east])

    def test_a_fact_spreads_at_running_speed(self):
        f = PossibilityField(TM, "attack")
        f.reset(0.0, 1)
        f.general.ready[:] = 1e9                          # nothing known except the fact
        f.add_fact(Fact(1, (-8.0, 0.0, 0.0), 0.5, 100.0, "sight"))
        run(f, 100.0, 101.0)
        m = f.possible(101.0)
        self.assertTrue(m[near(-8, 0)])
        self.assertTrue(m[near(-5, 0)])                   # 3 m in 1 s: reachable
        self.assertFalse(m[near(8, 4)])                   # 16 m through the door: not yet
        run(f, 101.0, 106.0)
        self.assertTrue(f.possible(106.0)[near(8, 4)])

    def test_clearing_and_refilling_from_the_doorway(self):
        f = PossibilityField(TM, "attack")
        f.reset(0.0, 1)
        t = run(f, 0.0, 20.0)
        east = TM.pos[:, 0] > 1.0
        self.assertTrue(f.possible(t)[east].all())
        f.observe(east, t)                                # a defender looks at the whole east room
        self.assertFalse(f.possible(t)[east].any())
        t2 = run(f, t, t + 1.0)
        door_side, far_side = near(2, 0), near(10, 5)
        m = f.possible(t2)
        self.assertTrue(m[door_side], "refills next to the doorway first")
        self.assertFalse(m[far_side])

    def test_tracked_enemies_leave_the_rest_clear(self):
        f = PossibilityField(TM, "attack")
        f.reset(0.0, 1)
        t = run(f, 0.0, 10.0)
        f.add_fact(Fact(7, (-8.0, -5.0, 0.0), 0.5, t, "sight"))
        d = f.danger(t)
        self.assertAlmostEqual(d.sum(), 1.0, places=3)
        west = TM.pos[:, 0] < -5.0
        self.assertGreater(d[west].sum(), 0.99)          # the one living enemy is where it was seen

    def test_view_mask_respects_walls_and_smoke(self):
        eye = Point3(-6.0, 4.5, 1.6)
        m = view_mask(TM, eye, -90.0, 120.0)              # looking east (+x)
        self.assertTrue(m[near(-3, 4.5)])
        self.assertFalse(m[near(6, 4.5)])                 # behind the wall
        smoky = view_mask(TM, eye, -90.0, 120.0, smokes=[((-4.0, 4.5, 1.0), 1.5)])
        self.assertFalse(smoky[near(-1.5, 4.5)])


class FakeTeam:
    def __init__(self):
        self.game = SimpleNamespace(loop=SimpleNamespace(time=0.0))
        self.lines = []
        self.facts = []
        self.enemies_alive = 5

    def radio(self, speaker, text, key=None, every=3.0):
        self.lines.append(text)

    def on_radio_fact(self, fact, speaker):
        self.facts.append(fact)


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.team = FakeTeam()
        self.radio = Radio(self.team, TM, random.Random(3), {"delay": 0.55, "delay_range": (0.3, 1.2), "fuzz": 2.5})
        self.me = SimpleNamespace(name="Anvil")

    def test_callouts_are_late_and_imprecise(self):
        true = (8.0, 3.0, 0.0)
        self.radio.sighting(self.me, 11, true, 50.0)
        self.assertEqual(self.radio.update(50.29), [])
        out = []
        t = 50.3
        while not out and t < 52.0:
            out = self.radio.update(t)
            t += 0.05
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertLessEqual(t - 50.0, 1.25)
        self.assertEqual(f.source, "radio")
        self.assertEqual(f.time, 50.0)
        self.assertGreaterEqual(f.radius, 2.5)
        self.assertLess(math.dist(f.pos[:2], true[:2]), 2.5 + 3.0)
        self.assertEqual(f.area, "East")

    def test_rate_limit_and_summary_line(self):
        self.radio.sighting(self.me, 1, (8.0, 3.0, 0.0), 10.0)
        self.assertFalse(self.radio.sighting(self.me, 1, (8.0, 3.2, 0.0), 10.5))
        self.radio.sighting(self.me, 2, (9.0, -2.0, 0.0), 10.2, hurt=True)
        self.radio.update(12.0)
        self.assertIn("Two East, one tagged", self.team.lines)

    def test_missed_calls(self):
        self.radio.miss = 1.0
        self.radio.sighting(self.me, 1, (8.0, 3.0, 0.0), 10.0)
        self.assertEqual(self.radio.update(13.0), [])


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        bot = SimpleNamespace(position=lambda: Point3(0, 0, 0), perception=SimpleNamespace(contacts={}))
        self.k = BotKnowledge(bot)

    def test_shot_from_out_of_sight_gives_a_direction_not_a_position(self):
        f = self.k.on_shot((1.0, 0.0, 0.0), 5.0)          # the bullet travelled towards +x
        self.assertIsNone(f.enemy)
        self.assertLess(f.pos[0], 0.0)                     # the shooter is somewhere towards -x
        self.assertGreater(f.radius, 5.0)
        self.assertEqual(f.direction[:2], (-1.0, -0.0))

    def test_radar_glance_reads_recent_sightings_only(self):
        n = self.k.radar(10.0, [(9.0, 1, (5, 5, 0)), (6.0, 2, (1, 1, 0))])
        self.assertEqual(n, 1)
        self.assertEqual(self.k.facts[1].source, "radar")
        self.assertNotIn(2, self.k.facts)

    def test_newer_facts_replace_older_but_not_sharper_ones(self):
        self.k.add(Fact(1, (0, 0, 0), 0.3, 10.0, "sight"))
        self.assertFalse(self.k.add(Fact(1, (5, 0, 0), 3.5, 9.0, "radio")))         # older
        self.assertFalse(self.k.add(Fact(1, (0.5, 0, 0), 3.5, 10.2, "radio")))      # vaguer and no newer info
        self.assertTrue(self.k.add(Fact(1, (9, 0, 0), 3.5, 14.0, "radio")))
        self.assertEqual(self.k.facts[1].pos[0], 9)


if __name__ == "__main__":
    unittest.main()
