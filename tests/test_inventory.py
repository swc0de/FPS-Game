"""Inventory / loadout and grenade maths tests."""
import unittest

from weapons.defs import WeaponDatabase
from weapons.grenades import blast_damage, flash_duration
from weapons.inventory import Inventory

DB = WeaponDatabase()


class InventoryTests(unittest.TestCase):
    def make(self):
        inv = Inventory(DB)
        for k in ("r7", "p9", "knife"):
            inv.give_weapon(k)
        return inv

    def test_slots_and_switching(self):
        inv = self.make()
        self.assertEqual(inv.best_slot(), "primary")
        self.assertTrue(inv.select("primary"))
        self.assertEqual(inv.current().d.key, "r7")
        self.assertTrue(inv.select("secondary"))
        self.assertEqual(inv.current().d.key, "p9")
        self.assertEqual(inv.last_slot, "primary")
        self.assertFalse(inv.select("grenade"))      # none carried yet

    def test_replacing_primary_returns_old(self):
        inv = self.make()
        old = inv.give_weapon("sr90")
        self.assertEqual(old.d.key, "r7")
        self.assertEqual(inv.weapons["primary"].d.key, "sr90")

    def test_drop(self):
        inv = self.make()
        inv.select("primary")
        dropped = inv.drop_current()
        self.assertEqual(dropped.d.key, "r7")
        self.assertIsNone(inv.weapons["primary"])
        self.assertEqual(inv.slot, "secondary")
        inv.select("melee")
        self.assertIsNone(inv.drop_current())        # knife can't be dropped

    def test_grenade_limits_and_cycling(self):
        inv = self.make()
        self.assertTrue(inv.give_grenade("flash", 2))
        self.assertFalse(inv.give_grenade("flash"))  # max 2 flashes
        self.assertTrue(inv.give_grenade("frag"))
        self.assertFalse(inv.give_grenade("frag"))   # max 1 frag
        inv.select("grenade")
        self.assertEqual(inv.grenade, "frag")
        inv.select("grenade")                        # pressing again cycles
        self.assertEqual(inv.grenade, "flash")
        self.assertEqual(inv.use_grenade(), "flash")
        self.assertEqual(inv.grenades["flash"], 1)

    def test_weapon_state_survives_drop_and_pickup(self):
        inv = self.make()
        inv.select("primary")
        ws = inv.current()
        ws.ammo = 7
        dropped = inv.drop_current()
        inv2 = Inventory(DB)
        inv2.give_weapon(dropped)
        self.assertEqual(inv2.weapons["primary"].ammo, 7)


class GrenadeMathTests(unittest.TestCase):
    def test_blast_falloff(self):
        self.assertAlmostEqual(blast_damage(0, 9, 98, 1.4), 98)
        self.assertEqual(blast_damage(9.5, 9, 98, 1.4), 0)
        self.assertGreater(blast_damage(2, 9, 98, 1.4), blast_damage(5, 9, 98, 1.4))

    def test_flash(self):
        facing = flash_duration(5, 28, 0.95, 4.6)
        away = flash_duration(5, 28, -0.9, 4.6)
        self.assertGreater(facing, 3.0)
        self.assertLess(away, facing * 0.3)
        self.assertEqual(flash_duration(30, 28, 1.0, 4.6), 0.0)


if __name__ == "__main__":
    unittest.main()
