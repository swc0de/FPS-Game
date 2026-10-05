"""Milestone 6 logic that runs without a window: lean, specialists data,
gadget helpers and the bot task chaining."""
import math
import unittest

from panda3d.core import NodePath, Point3, Vec3

from engine.physics import PhysicsWorld
from gameplay.gadgets import emp_grenade_def, gadget_cfg, hpr_facing, load_specialists, side_of
from gameplay.lean import OFFSET, ROLL, TIME, Lean, clearance


def box(physics, x0, y0, z0, x1, y1, z1):
    physics.add_static_box(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2), ((x1 - x0) / 2, (y1 - y0) / 2, (z1 - z0) / 2))


class LeanTests(unittest.TestCase):
    def test_reaches_full_lean_in_lean_time(self):
        lean = Lean()
        dt = 1 / 64
        for _ in range(int(TIME / dt) + 1):
            lean.update(dt, 1.0)
        self.assertAlmostEqual(lean.amount, 1.0)
        self.assertAlmostEqual(lean.offset(), OFFSET)
        self.assertAlmostEqual(lean.roll(), ROLL)
        lean.update(dt, -1.0)
        self.assertLess(lean.amount, 1.0)
        self.assertGreater(lean.amount, 0.0)          # it takes time to swap sides

    def test_interpolation(self):
        lean = Lean()
        lean.update(1 / 64, 1.0)
        self.assertAlmostEqual(lean.value(0.0), 0.0)
        self.assertAlmostEqual(lean.value(0.5), lean.amount * 0.5)

    def test_geometry_limits_the_lean(self):
        lean = Lean()
        for _ in range(30):
            lean.update(1 / 64, -1.0, free_left=0.4)
        self.assertAlmostEqual(lean.amount, -0.4)

    def test_clearance_against_a_wall(self):
        phys = PhysicsWorld(NodePath("root"))
        box(phys, 0.3, -2, 0, 0.5, 2, 3)                # wall 0.3 m to the right of the eye (yaw 0: right = +x)
        free_l, free_r = clearance(phys, Point3(0, 0, 1.6), 0.0)
        self.assertEqual(free_l, 1.0)
        self.assertLess(free_r, 0.5)
        self.assertGreaterEqual(free_r, 0.0)
        # turned around, the wall is on the left
        free_l, free_r = clearance(phys, Point3(0, 0, 1.6), 180.0)
        self.assertLess(free_l, 0.5)
        self.assertEqual(free_r, 1.0)

    def test_body_roll_puts_the_head_over_the_eye(self):
        r = Lean.body_roll(1.0)
        self.assertGreater(r, 20.0)
        self.assertLess(r, 35.0)
        self.assertAlmostEqual(Lean.body_roll(-1.0), -r)


class SpecialistDataTests(unittest.TestCase):
    def test_four_unique_specialists_per_side_with_gadgets(self):
        cfg = load_specialists()
        keys = set()
        for side in ("attack", "defend"):
            self.assertEqual(len(cfg[side]), 4)
            for sp in cfg[side]:
                self.assertNotIn(sp["key"], keys)
                keys.add(sp["key"])
                self.assertIn(sp["gadget"], cfg["gadgets"])
                self.assertGreater(int(cfg["gadgets"][sp["gadget"]]["count"]), 0)
                self.assertTrue(sp["name"] and sp["desc"])
        gadgets = {sp["gadget"] for s in ("attack", "defend") for sp in cfg[s]}
        self.assertEqual(len(gadgets), 8)

    def test_gadget_models_exist(self):
        from weapons.models import model_defs
        defs = model_defs()
        for m in ("gadget_drone", "gadget_camera", "gadget_charge", "gadget_lance", "gadget_wire", "gadget_sensor",
                  "gadget_shield", "gadget_jammer", "grenade_emp"):
            self.assertIn(m, defs)
        self.assertIn(emp_grenade_def().model, defs)

    def test_wall_charge_is_buyable_by_attackers_only(self):
        from gameplay.match import load_rules
        from gameplay.shop import find_item
        from weapons.defs import database
        db, rules = database(), load_rules()
        self.assertIsNotNone(find_item(db, rules, "attack", "wall_charge"))
        self.assertIsNone(find_item(db, rules, "defend", "wall_charge"))
        self.assertEqual(gadget_cfg("wall_charge")["max"], 2)


class HelperTests(unittest.TestCase):
    def test_hpr_facing(self):
        for n in (Vec3(1, 0, 0), Vec3(0, -1, 0), Vec3(0, 0, 1), Vec3(0.6, 0.8, 0)):
            h, p, r = hpr_facing(n)
            fwd = Vec3(-math.sin(math.radians(h)) * math.cos(math.radians(p)),
                       math.cos(math.radians(h)) * math.cos(math.radians(p)), math.sin(math.radians(p)))
            self.assertAlmostEqual((fwd - n.normalized()).length(), 0.0, places=5)

    def test_side_of(self):
        class A:
            side = "attack"

        class P:
            agent = A()
        self.assertEqual(side_of(A()), "attack")
        self.assertEqual(side_of(P()), "attack")
        self.assertIsNone(side_of(None))


class TaskChainTests(unittest.TestCase):
    def test_then_can_be_a_task_or_a_callable(self):
        from ai.brain import Task
        t2 = Task("hold", Point3(1, 2, 0), tag="b")
        t1 = Task("use", Point3(0, 0, 0), then=t2)
        self.assertIs(t1.then, t2)
        t3 = Task("use", Point3(0, 0, 0), then=lambda bot: t2)
        self.assertIs(t3.then(None), t2)


if __name__ == "__main__":
    unittest.main()
