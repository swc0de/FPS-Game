"""Headless tests for the kinematic character controller (no window needed).

Run:  python -m unittest discover -s tests -v
"""
import unittest

from panda3d.core import NodePath, Vec3

from engine.physics import PhysicsWorld
from gameplay.character import KinematicCharacter, MoveInput

TICK = 1.0 / 64.0


def make_world():
    phys = PhysicsWorld(NodePath("root"))
    # 40x40 floor, top at z=0
    phys.add_static_box((0, 0, -0.5), (20, 20, 0.5), surface="concrete")
    return phys


def run(char, ticks, inp):
    for _ in range(ticks):
        char.step(TICK, inp)


def forward(speed_scale=1.0, **kw):
    return MoveInput(wish_dir=Vec3(0, 1, 0), speed_scale=speed_scale, **kw)


class CharacterTests(unittest.TestCase):
    def test_stands_on_floor(self):
        phys = make_world()
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 1.0))
        run(c, 64, MoveInput())
        self.assertTrue(c.on_ground)
        self.assertAlmostEqual(c.pos.z, 0.0, delta=0.03)

    def test_run_speed_reached(self):
        phys = make_world()
        c = KinematicCharacter(phys)
        c.teleport((0, -10, 0.05))
        run(c, 64, forward())
        self.assertAlmostEqual(c.horizontal_speed, c.cfg["run_speed"], delta=0.15)

    def test_walk_and_crouch_slower(self):
        phys = make_world()
        c = KinematicCharacter(phys)
        c.teleport((0, -10, 0.05))
        run(c, 64, forward(walk=True))
        self.assertAlmostEqual(c.horizontal_speed, c.cfg["walk_speed"], delta=0.15)
        run(c, 64, forward(crouch=True))
        self.assertAlmostEqual(c.horizontal_speed, c.cfg["crouch_speed"], delta=0.15)
        self.assertAlmostEqual(c.height, c.crouch_height, delta=0.01)

    def test_wall_blocks(self):
        phys = make_world()
        phys.add_static_box((0, 3, 1.5), (5, 0.1, 1.5))  # wall face at y=2.9
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 0.05))
        run(c, 128, forward())
        self.assertLess(c.pos.y + c.radius, 2.9 + 0.001)
        self.assertGreater(c.pos.y + c.radius, 2.85)

    def test_slides_along_angled_wall(self):
        phys = make_world()
        phys.add_static_box((0, 3, 1.5), (5, 0.1, 1.5), hpr=(30, 0, 0))
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 0.05))
        run(c, 128, forward())
        # blocked in y but sliding sideways along the wall
        self.assertGreater(abs(c.pos.x), 1.0)

    def test_step_up_stairs(self):
        phys = make_world()
        for i in range(6):  # 0.25 m risers, 0.3 m treads, starting at y=2
            h = 0.25 * (i + 1)
            phys.add_static_box((0, 2.15 + 0.3 * i, h / 2), (1.5, 0.15, h / 2))
        phys.add_static_box((0, 2.0 + 1.8 + 2.0, 0.75), (1.5, 2.0, 0.75))  # landing at 1.5 m
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 0.05))
        run(c, 80, forward())
        self.assertGreater(c.pos.y, 4.0)
        self.assertAlmostEqual(c.pos.z, 1.5, delta=0.05)
        self.assertTrue(c.on_ground)
        # stairs must not bleed speed
        self.assertAlmostEqual(c.horizontal_speed, c.cfg["run_speed"], delta=0.1)

    def test_cannot_step_tall_ledge(self):
        phys = make_world()
        phys.add_static_box((0, 3, 0.35), (2, 1, 0.35))  # 0.7 m box
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 0.05))
        run(c, 128, forward())
        self.assertLess(c.pos.y, 2.0)
        self.assertAlmostEqual(c.pos.z, 0.0, delta=0.03)

    def _run_and_jump(self, box_height, crouch_jump):
        phys = make_world()
        phys.add_static_box((0, 8, box_height / 2), (2, 3, box_height / 2))  # front face y=5
        c = KinematicCharacter(phys)
        c.teleport((0, -4, 0.05))
        # run up and jump ~1.2 m before the box
        while c.pos.y < 5 - c.radius - 1.2:
            c.step(TICK, forward())
        c.step(TICK, forward(jump=True))
        run(c, 45, forward(crouch=crouch_jump))
        run(c, 30, MoveInput())
        return c

    def test_jump_onto_box(self):
        c = self._run_and_jump(0.9, crouch_jump=False)
        self.assertAlmostEqual(c.pos.z, 0.9, delta=0.04)

    def test_crouch_jump_reaches_higher(self):
        c = self._run_and_jump(1.35, crouch_jump=False)
        self.assertLess(c.pos.z, 0.1)          # plain jump fails
        c = self._run_and_jump(1.35, crouch_jump=True)
        self.assertAlmostEqual(c.pos.z, 1.35, delta=0.04)

    def test_walkable_slope(self):
        phys = make_world()
        # 25 degree ramp rising toward +y
        phys.add_static_box((0, 5, 0), (2, 4, 0.2), hpr=(0, 25, 0))
        c = KinematicCharacter(phys)
        c.teleport((0, -1, 0.05))
        run(c, 110, forward())
        self.assertGreater(c.pos.z, 1.0)
        self.assertTrue(c.on_ground)

    def test_steep_slope_blocks(self):
        phys = make_world()
        phys.add_static_box((0, 5, 0), (2, 4, 0.2), hpr=(0, 55, 0))
        c = KinematicCharacter(phys)
        c.teleport((0, -1, 0.05))
        run(c, 160, forward())
        self.assertLess(c.pos.z, 0.8)

    def test_crouch_under_low_ceiling_stays_crouched(self):
        phys = make_world()
        phys.add_static_box((0, 4, 1.4 + 0.25), (2, 2, 0.25))  # ceiling underside at 1.4 m
        c = KinematicCharacter(phys)
        c.teleport((0, 0, 0.05))
        run(c, 20, MoveInput(crouch=True))
        run(c, 180, forward(crouch=True))
        self.assertGreater(c.pos.y, 3.0)       # walked under the ceiling
        run(c, 30, MoveInput(crouch=False))    # try to stand up
        self.assertLess(c.height, 1.41)
        self.assertGreater(c.height, c.crouch_height - 0.01)

    def test_depenetration(self):
        phys = make_world()
        phys.add_static_box((0, 0, 1.0), (0.5, 0.5, 1.0))
        c = KinematicCharacter(phys)
        c.teleport((0.6, 0, 0.05))   # overlapping the box
        run(c, 5, MoveInput())
        self.assertGreaterEqual(c.pos.x - c.radius, 0.5 - 0.02)

    def test_footstep_events(self):
        phys = make_world()
        c = KinematicCharacter(phys)
        events = []
        c.on_event = events.append
        c.teleport((0, -10, 0.05))
        run(c, 128, forward())
        steps = [e for e in events if e.kind == "footstep"]
        self.assertGreater(len(steps), 3)
        self.assertTrue(all(e.surface == "concrete" for e in steps))
        events.clear()
        run(c, 128, forward(walk=True))
        steps = [e for e in events if e.kind == "footstep"]
        self.assertTrue(all(e.loudness == 0.0 for e in steps))


if __name__ == "__main__":
    unittest.main()
