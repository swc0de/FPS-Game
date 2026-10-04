"""Bot AI pieces that need no renderer: aiming, grenade arcs, buying, lanes."""
import math
import random
import unittest
from types import SimpleNamespace

from panda3d.core import Point3

from ai.aim import AimController, angles_to, wrap180
from ai.brain import solve_throw
from ai.buy import bot_buy, team_buy_mode
from ai.tactics import build_lanes, zone_distance
from engine.physics import GRAVITY
from gameplay.damage import Damageable
from gameplay.match import Match, Participant, load_rules
from gameplay.shop import Shop
from weapons.defs import WeaponDatabase
from weapons.inventory import Inventory

PROFILES = {
    "easy": {"turn_speed": 240, "snap": 7.0, "aim_error": 4.5, "error_decay": 0.9, "min_error": 0.9,
             "spray_control": 0.25, "fire_tolerance": 2.2},
    "expert": {"turn_speed": 820, "snap": 20.0, "aim_error": 0.9, "error_decay": 3.0, "min_error": 0.12,
               "spray_control": 0.9, "fire_tolerance": 1.0},
}


class AimTests(unittest.TestCase):
    def test_angles_match_panda_convention(self):
        self.assertAlmostEqual(angles_to(0, 1, 0)[0], 0.0)
        self.assertAlmostEqual(angles_to(-1, 0, 0)[0], 90.0)     # +H turns left (towards -X)
        self.assertAlmostEqual(angles_to(0, 1, 1)[1], 45.0)

    def test_turn_speed_is_limited(self):
        a = AimController(PROFILES["easy"], random.Random(1))
        left = a.turn_towards(170.0, 0.0, 1 / 64)
        self.assertLessEqual(abs(wrap180(a.yaw)), 240 / 64 + 1e-6)
        self.assertGreater(left, 160)

    def test_converges_without_overshoot(self):
        a = AimController(PROFILES["expert"], random.Random(1))
        prev = 1e9
        for _ in range(64):
            left = a.turn_towards(40.0, -10.0, 1 / 64)
            self.assertLessEqual(left, prev + 1e-9)
            prev = left
        self.assertLess(prev, 0.05)
        self.assertAlmostEqual(a.yaw, 40.0, delta=0.05)

    def test_expert_reaches_target_faster_than_easy(self):
        def time_to_aim(p):
            a = AimController(p, random.Random(2))
            for k in range(400):
                if a.turn_towards(90.0, 0.0, 1 / 64) < 1.0:
                    return k / 64
            return 99.0
        self.assertLess(time_to_aim(PROFILES["expert"]), time_to_aim(PROFILES["easy"]) * 0.6)

    def test_error_shrinks_while_tracking(self):
        a = AimController(PROFILES["easy"], random.Random(3))
        a.acquire("target", 20.0, 0.0, 0.0)
        start = math.hypot(a.err_x, a.err_y)
        for _ in range(64 * 3):
            a.update_error(1 / 64)
        self.assertLess(math.hypot(a.err_x, a.err_y), start * 0.5 + 0.9)

    def test_residual_error_grows_with_distance(self):
        def settled(distance):
            a = AimController(PROFILES["expert"], random.Random(5))
            a.acquire("t", distance, 0.0, 0.0)
            mags = []
            for k in range(64 * 6):
                a.update_error(1 / 64, 0.0, distance)
                if k > 64 * 3:
                    mags.append(math.hypot(a.err_x, a.err_y))
            return sum(mags) / len(mags)
        self.assertGreater(settled(60.0), settled(5.0) * 1.5)

    def test_spray_compensation_pulls_down(self):
        a = AimController(PROFILES["expert"], random.Random(4))
        a.err_x = a.err_y = 0.0
        a._target_id = "t"
        eye, point = (0, 0, 1.6), (0, 20, 1.6)
        for _ in range(64):
            off = a.track(eye, point, 1 / 64, recoil=(0.0, 4.0))
        # the view points below the target so the kicked bullets land on it
        self.assertLess(a.pitch, -3.0)
        self.assertLess(off, 0.6)


class ThrowTests(unittest.TestCase):
    def _land(self, start, vel, target_z):
        # integrate until the grenade falls back to the target height
        t = 0.0
        while t < 10.0:
            t += 0.001
            z = start.z + vel.z * t - 0.5 * GRAVITY * t * t
            if t > 0.05 and z <= target_z and vel.z - GRAVITY * t < 0:
                return Point3(start.x + vel.x * t, start.y + vel.y * t, z)
        return None

    def test_low_arc_hits_target(self):
        start, target = Point3(0, 0, 1.6), Point3(4, 12, 0.2)
        v = solve_throw(start, target, 18.0)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v.length(), 18.0, places=3)
        land = self._land(start, v, target.z)
        self.assertLess((land - target).length(), 0.1)

    def test_out_of_range_is_none(self):
        self.assertIsNone(solve_throw(Point3(0, 0, 1.6), Point3(0, 60, 0), 15.0))

    def test_high_arc_is_steeper(self):
        start, target = Point3(0, 0, 1.6), Point3(0, 10, 0)
        low = solve_throw(start, target, 16.0)
        high = solve_throw(start, target, 16.0, prefer_high=True)
        self.assertGreater(high.z, low.z)


DB = WeaponDatabase()
RULES = load_rules()


class FakeWeapons:
    def __init__(self):
        self.inv = Inventory(DB)

    @property
    def primary(self):
        return self.inv.weapons.get("primary")

    def select(self, slot, force=False):
        self.inv.slot = slot

    def drop_weapon_state(self, ws):
        pass


class FakeBot(Participant):
    def __init__(self, name, money):
        super().__init__(name)
        self.damageable = Damageable()
        self.has_kit = False
        self.money = money
        self.weapons = FakeWeapons()


class FakeDirector:
    def __init__(self, side):
        self.game = SimpleNamespace(weapon_db=DB, weapons=None, audio=None)
        self.rules = RULES
        self.match = Match(RULES, [], side)

    def in_buy_zone(self, agent):
        return True


class BuyTests(unittest.TestCase):
    def setUp(self):
        self.d = FakeDirector("attack")
        self.bots = [FakeBot(f"b{i}", 800) for i in range(5)]
        for b in self.bots:
            self.d.match.add(b, 0)
        self.d.match.start()
        self.shop = Shop(self.d)
        self.cfg = {"full_buy": 3800, "save_below": 2400, "awp_min": 5900}

    def _buy(self, bot, mode, awper=False, kit=False):
        return bot_buy(bot, self.shop, DB, RULES, mode, self.cfg, awper, kit, random.Random(1))

    def test_modes(self):
        m = self.d.match
        self.assertEqual(team_buy_mode(self.bots, m, "attack", self.cfg), "pistol")
        m.round = 5
        for b in self.bots:
            b.money = 1500
        self.assertEqual(team_buy_mode(self.bots, m, "attack", self.cfg), "eco")
        for b in self.bots:
            b.money = 3000
        self.assertEqual(team_buy_mode(self.bots, m, "attack", self.cfg), "force")
        for b in self.bots:
            b.money = 5000
        self.assertEqual(team_buy_mode(self.bots, m, "attack", self.cfg), "full")

    def test_full_buy_gets_side_rifle_and_armour(self):
        b = self.bots[0]
        b.money = 5000
        got = self._buy(b, "full")
        self.assertIn("r7", got)
        self.assertIn("kevlar_helmet", got)
        self.assertGreaterEqual(b.money, 0)
        self.assertEqual(b.weapons.inv.weapons["primary"].d.key, "r7")

    def test_awper_buys_sniper(self):
        b = self.bots[0]
        b.money = 7000
        self.assertIn("sr90", self._buy(b, "full", awper=True))

    def test_eco_saves(self):
        b = self.bots[0]
        b.money = 1100
        self._buy(b, "eco")
        self.assertGreaterEqual(b.money, 900)

    def test_never_overspends(self):
        rng = random.Random(9)
        for mode in ("pistol", "eco", "force", "full"):
            for _ in range(20):
                b = FakeBot("x", rng.randrange(0, 9000))
                self.d.match.add(b, 0)
                bot_buy(b, self.shop, DB, RULES, mode, self.cfg, rng.random() < 0.3, True, rng)
                self.assertGreaterEqual(b.money, 0)


class LaneTests(unittest.TestCase):
    def test_compound_lanes(self):
        from tests.test_maps import build
        data, ctx = build("compound")
        level = SimpleNamespace(zones=ctx.zones, data=data)
        defend = [s["pos"] for s in ctx.spawns if s["team"] == "defend"]
        center = Point3(sum(p[0] for p in defend) / 5, sum(p[1] for p in defend) / 5, 0)
        lanes = build_lanes(level, center)
        self.assertEqual(sorted(lanes), ["A", "B"])
        self.assertEqual(len(lanes["A"]), 3)
        self.assertEqual(len(lanes["B"]), 3)
        sites = {z["name"]: z for z in ctx.zones if z["kind"] == "bombsite"}
        for name, ls in lanes.items():
            for ln in ls:
                stage = ln["points"][ln["stage"]]
                self.assertGreaterEqual(zone_distance(sites[name], stage), 10.0, ln["name"])
                self.assertLess(zone_distance(sites[name], ln["points"][-1]), 0.5)


if __name__ == "__main__":
    unittest.main()
