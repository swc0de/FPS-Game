"""Buying rules and bomb maths (no engine needed)."""
import unittest

from gameplay.bomb import beep_interval, bomb_damage
from gameplay.damage import Damageable
from gameplay.match import Match, Participant, load_rules
from gameplay.shop import Shop, catalogue, find_item
from weapons.defs import WeaponDatabase
from weapons.inventory import Inventory

DB = WeaponDatabase()
RULES = load_rules()


class FakeWeapons:
    def __init__(self):
        self.inv = Inventory(DB)
        self.dropped = []

    def select(self, slot, force=False):
        self.inv.slot = slot

    def drop_weapon_state(self, ws):
        self.dropped.append(ws.d.key)


class FakeGame:
    def __init__(self):
        self.weapon_db = DB
        self.weapons = FakeWeapons()
        self.audio = type("A", (), {"play_ui": lambda *a, **k: None})()


class Agent(Participant):
    def __init__(self):
        super().__init__("you", is_human=True)
        self.damageable = Damageable()
        self.has_kit = False


class FakeDirector:
    def __init__(self, side="attack"):
        self.game = FakeGame()
        self.rules = RULES
        self.agent = Agent()
        self.match = Match(RULES, [], side)
        self.match.add(self.agent, 0)
        self.match.start()
        self.zone = True

    def in_buy_zone(self, agent):
        return self.zone


def item(side, key):
    return find_item(DB, RULES, side, key)


class ShopTests(unittest.TestCase):
    def test_catalogue_respects_teams(self):
        att = {it.key for items in catalogue(DB, RULES, "attack").values() for it in items}
        dfn = {it.key for items in catalogue(DB, RULES, "defend").values() for it in items}
        self.assertIn("r7", att)
        self.assertNotIn("c9", att)
        self.assertIn("c9", dfn)
        self.assertNotIn("r7", dfn)
        self.assertIn("defuse_kit", dfn)
        self.assertNotIn("defuse_kit", att)
        self.assertNotIn("knife", att)

    def test_buy_weapon_and_refund(self):
        d = FakeDirector()
        shop = Shop(d)
        d.agent.money = 3000
        ok, _ = shop.buy(d.agent, item("attack", "r7"))
        self.assertTrue(ok)
        self.assertEqual(d.agent.money, 300)
        self.assertEqual(d.game.weapons.inv.weapons["primary"].d.key, "r7")
        ok, why = shop.buy(d.agent, item("attack", "r7"))
        self.assertFalse(ok)
        self.assertIn("already", why)
        self.assertTrue(shop.refund(d.agent, item("attack", "r7")))
        self.assertEqual(d.agent.money, 3000)
        self.assertIsNone(d.game.weapons.inv.weapons["primary"])

    def test_buying_a_primary_drops_the_old_one(self):
        d = FakeDirector()
        shop = Shop(d)
        d.agent.money = 10000
        shop.buy(d.agent, item("attack", "mx5"))
        shop.buy(d.agent, item("attack", "r7"))
        self.assertEqual(d.game.weapons.dropped, ["mx5"])

    def test_grenade_limits(self):
        d = FakeDirector()
        shop = Shop(d)
        d.agent.money = 10000
        self.assertTrue(shop.buy(d.agent, item("attack", "flash"))[0])
        self.assertTrue(shop.buy(d.agent, item("attack", "flash"))[0])
        ok, why = shop.buy(d.agent, item("attack", "flash"))
        self.assertFalse(ok)
        self.assertIn("maximum", why)
        self.assertTrue(shop.buy(d.agent, item("attack", "smoke"))[0])
        self.assertTrue(shop.buy(d.agent, item("attack", "frag"))[0])
        self.assertEqual(d.game.weapons.inv.grenade_count(), 4)

    def test_armour_helmet_upgrade_price(self):
        d = FakeDirector()
        shop = Shop(d)
        d.agent.money = 2000
        shop.buy(d.agent, item("attack", "kevlar"))
        self.assertEqual(d.agent.damageable.armor, 100)
        helmet = item("attack", "kevlar_helmet")
        self.assertEqual(shop.price(d.agent, helmet), 350)
        shop.buy(d.agent, helmet)
        self.assertTrue(d.agent.damageable.helmet)
        self.assertEqual(d.agent.money, 2000 - 650 - 350)

    def test_buy_zone_and_time(self):
        d = FakeDirector()
        shop = Shop(d)
        d.agent.money = 5000
        d.zone = False
        self.assertFalse(shop.check(d.agent, item("attack", "r7"))[0])
        d.zone = True
        d.match.update(RULES["timers"]["freeze_time"])      # freeze -> prep
        self.assertTrue(shop.check(d.agent, item("attack", "r7"))[0])
        d.match.update(RULES["timers"].get("prep_time", 0.0))  # prep -> live
        self.assertEqual(d.match.phase, "live")
        self.assertTrue(shop.check(d.agent, item("attack", "r7"))[0])
        d.match.update(RULES["timers"]["buy_time"] + 1.0)
        ok, why = shop.check(d.agent, item("attack", "r7"))
        self.assertFalse(ok)
        self.assertIn("buy time", why)

    def test_defuse_kit_for_defenders(self):
        d = FakeDirector("defend")
        shop = Shop(d)
        d.agent.money = 1000
        self.assertTrue(shop.buy(d.agent, item("defend", "defuse_kit"))[0])
        self.assertTrue(d.agent.has_kit)


class BombMathTests(unittest.TestCase):
    def test_damage_falloff(self):
        b = RULES["bomb"]
        near = bomb_damage(2.0, b["max_damage"], b["sigma"], b["radius"])
        mid = bomb_damage(20.0, b["max_damage"], b["sigma"], b["radius"])
        far = bomb_damage(b["radius"] + 1, b["max_damage"], b["sigma"], b["radius"])
        self.assertGreater(near, 400)       # lethal next to the bomb
        self.assertTrue(80 < mid < near)
        self.assertEqual(far, 0.0)

    def test_beeps_speed_up(self):
        total = RULES["timers"]["bomb_timer"]
        slow, fast = RULES["bomb"]["beep_slow"], RULES["bomb"]["beep_fast"]
        self.assertAlmostEqual(beep_interval(total, total, slow, fast), slow)
        self.assertAlmostEqual(beep_interval(0.0, total, slow, fast), fast)
        self.assertGreater(beep_interval(20, total, slow, fast), beep_interval(5, total, slow, fast))


if __name__ == "__main__":
    unittest.main()
