"""Gunplay model tests: fire timing, recoil patterns, inaccuracy, reloads, damage."""
import math
import random
import unittest

from gameplay.damage import Damageable, DamageInfo, falloff
from weapons.defs import WeaponDatabase
from weapons.weapon import AimContext, WeaponState, angles_to_dir, apply_offset

TICK = 1.0 / 64.0
DB = WeaponDatabase()


def spray(ws: WeaponState, seconds: float, start: float = 0.0):
    t = start
    shots = []
    pressed = True
    while t < start + seconds:
        ws.update(t, TICK)
        if ws.wants_shot(t, True, pressed):
            shots.append(t)
            ws.on_fired(t)
        pressed = False
        t += TICK
    return shots, t


class WeaponModelTests(unittest.TestCase):
    def test_all_weapons_load(self):
        for key, w in DB.weapons.items():
            self.assertGreater(w.rpm, 0, key)
            self.assertEqual(w.recoil.pattern[0], [0, 0], key)
        self.assertIn("frag", DB.grenades)

    def test_auto_fire_rate_matches_rpm(self):
        ws = WeaponState(DB.weapons["r7"], random.Random(1), infinite_reserve=True)
        shots, _ = spray(ws, 2.0)
        # 600 rpm = 10 shots per second; 30 round magazine is not exceeded
        self.assertEqual(len(shots), 20)
        gaps = [b - a for a, b in zip(shots, shots[1:])]
        self.assertAlmostEqual(sum(gaps) / len(gaps), 0.1, delta=0.01)

    def test_semi_auto_requires_release(self):
        ws = WeaponState(DB.weapons["p9"], random.Random(1))
        shots, _ = spray(ws, 1.0)
        self.assertEqual(len(shots), 1)

    def test_magazine_empties_then_dry_and_auto_reload(self):
        ws = WeaponState(DB.weapons["mx5"], random.Random(1))
        shots, t = spray(ws, 5.0)
        self.assertEqual(len(shots), 30)
        self.assertEqual(ws.ammo, 0)
        # pressing again dry-fires and starts the reload
        ws.wants_shot(t, True, True)
        kinds = [e.kind for e in ws.pop_events()]
        self.assertIn("dry", kinds)
        self.assertIn("reload_start", kinds)
        t2 = t
        while ws.reloading:
            t2 += TICK
            ws.update(t2, TICK)
        self.assertEqual(ws.ammo, 30)
        self.assertEqual(ws.reserve, 90)
        self.assertAlmostEqual(t2 - t, DB.weapons["mx5"].reload_empty_time, delta=0.05)

    def test_shotgun_shell_reload_and_interrupt(self):
        ws = WeaponState(DB.weapons["s12"], random.Random(1))
        ws.ammo = 2
        self.assertTrue(ws.start_reload(0.0))
        t = 0.0
        while t < 1.5:
            t += TICK
            ws.update(t, TICK)
        self.assertGreater(ws.ammo, 2)
        loaded = ws.ammo
        self.assertTrue(ws.wants_shot(t, True, True))   # firing interrupts
        self.assertFalse(ws.reloading)
        self.assertLess(loaded, 8)

    def test_recoil_pattern_and_recovery(self):
        w = DB.weapons["r7"]
        ws = WeaponState(w, random.Random(1), infinite_reserve=True)
        spray(ws, 1.0)  # 10 shots
        self.assertEqual(ws.recoil_index, 10)
        self.assertEqual(ws.recoil_offset(), tuple(w.recoil.pattern[10]))
        vx, vy = ws.view_offset()
        self.assertAlmostEqual(vy, w.recoil.pattern[10][1] * w.recoil.view_follow)
        t = 1.0
        for _ in range(64 * 2):
            t += TICK
            ws.update(t, TICK)
        self.assertEqual(ws.recoil_index, 0.0)

    def test_first_shot_accuracy_vs_movement(self):
        w = DB.weapons["r7"]
        ws = WeaponState(w, random.Random(1))
        still = ws.inaccuracy(AimContext(speed=0, max_speed=4.86))
        crouch = ws.inaccuracy(AimContext(speed=0, max_speed=4.86, crouched=True))
        walking = ws.inaccuracy(AimContext(speed=4.86 * 0.3, max_speed=4.86))
        running = ws.inaccuracy(AimContext(speed=4.86, max_speed=4.86))
        jumping = ws.inaccuracy(AimContext(speed=0, max_speed=4.86, on_ground=False))
        self.assertLess(crouch, still)
        self.assertAlmostEqual(walking, still)       # slow walking keeps accuracy
        self.assertGreater(running, still * 20)
        self.assertGreater(jumping, running)

    def test_first_shot_goes_where_aimed(self):
        ws = WeaponState(DB.weapons["r7"], random.Random(5))
        offsets = ws.shot_offsets(AimContext(crouched=True))
        dx, dy = offsets[0]
        self.assertLess(math.hypot(dx, dy), DB.weapons["r7"].inaccuracy.crouch + 1e-6)

    def test_shotgun_pellets(self):
        ws = WeaponState(DB.weapons["s12"], random.Random(5))
        offs = ws.shot_offsets(AimContext())
        self.assertEqual(len(offs), 9)
        self.assertTrue(all(math.hypot(*o) < 2.6 + 0.6 + 1e-6 for o in offs))

    def test_angles(self):
        d = angles_to_dir(0, 0)
        self.assertAlmostEqual(d[1], 1.0)
        d = angles_to_dir(90, 0)          # heading 90 = facing -X
        self.assertAlmostEqual(d[0], -1.0)
        y, p = apply_offset(0, 0, 1.0, 2.0)   # right & up
        self.assertEqual((y, p), (-1.0, 2.0))


class DamageTests(unittest.TestCase):
    def test_falloff(self):
        self.assertAlmostEqual(falloff(36, 0.97, 0), 36)
        self.assertAlmostEqual(falloff(36, 0.97, 20), 36 * 0.97 ** 2)

    def test_rifle_headshot_through_helmet_kills(self):
        d = Damageable(armor=100, helmet=True)
        w = DB.weapons["r7"]
        dmg = w.damage * DB.hitgroup_multiplier("head", w)
        r = d.take_damage(DamageInfo(dmg, w.armor_penetration, "head"))
        self.assertTrue(r.killed)

    def test_armor_absorbs_body_damage(self):
        d = Damageable(armor=100, helmet=False)
        r = d.take_damage(DamageInfo(36, 0.775, "chest"))
        self.assertAlmostEqual(r.health, 36 * 0.775)
        self.assertAlmostEqual(d.armor, 100 - (36 - 27.9) * 0.5)
        # legs are never armoured
        r = d.take_damage(DamageInfo(27, 0.775, "leg"))
        self.assertAlmostEqual(r.health, 27)

    def test_no_helmet_head(self):
        d = Damageable(armor=100, helmet=False)
        r = d.take_damage(DamageInfo(40, 0.5, "head"))
        self.assertAlmostEqual(r.health, 40)

    def test_armor_break_overflow(self):
        d = Damageable(armor=2, helmet=False)
        r = d.take_damage(DamageInfo(100, 0.5, "chest"))
        # 50 to health, 25 armour wanted, only 2 available -> 23/0.5 = 46 overflow
        self.assertAlmostEqual(r.health, 96)
        self.assertEqual(d.armor, 0)


if __name__ == "__main__":
    unittest.main()
