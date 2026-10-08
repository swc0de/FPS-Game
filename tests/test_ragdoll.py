"""Death ragdolls (gameplay/ragdoll.py): they fall, settle and freeze, bend joints the right way,
never block a bullet, a movement or a line of sight, and repeat exactly."""
import unittest

import numpy as np
from panda3d.core import NodePath, Vec3

from engine.physics import MASK_BULLETS, MASK_MOVEMENT, MASK_SIGHT, PhysicsWorld
from gameplay import skeleton as sk
from gameplay.ragdoll import Ragdoll


def _world():
    render = NodePath("render")
    phys = PhysicsWorld(render)
    phys.add_static_box((0, 0, -0.5), (20, 20, 0.5))
    root = render.attachNewNode("body")
    root.setH(30.0)
    return phys, root


def _run(impulse=Vec3(0, 6, 0), seconds=7.0, watch=None):
    phys, root = _world()
    rd = Ragdoll(phys, root, sk.REST_WORLD.copy(), Vec3(0, 0, 0), impulse)
    for _ in range(int(seconds * 64)):
        phys.step(1 / 64)
        if watch is not None:
            watch(rd)
        if not rd.update(1 / 64):
            break
    return phys, rd


class RagdollTests(unittest.TestCase):
    def test_falls_settles_and_freezes(self):
        phys, rd = _run()
        self.assertTrue(rd.frozen)
        self.assertEqual(phys.world.getNumRigidBodies(), 1)          # only the floor is left
        self.assertEqual(phys.world.getNumConstraints(), 0)
        heights = [m[3, 2] for m in rd._last.values()]
        self.assertLess(rd._last["head"][3, 2], 0.5)                   # on the floor
        self.assertGreater(min(heights), -0.05)                        # not through it

    def test_visual_only(self):
        phys, root = _world()
        rd = Ragdoll(phys, root, sk.REST_WORLD.copy(), Vec3(0, 0, 0))
        for np_ in rd.bodies.values():
            mask = np_.node().getIntoCollideMask()
            for m in (MASK_BULLETS, MASK_MOVEMENT, MASK_SIGHT):
                self.assertTrue((mask & m).isZero())
        rd.remove()

    def test_hinges_stay_in_their_limits(self):
        # Bullet's limits are soft: a little give while the body lands, none once it has settled
        peak, rest = [0.0], [0.0]

        def watch(rd):
            over = [max(c.getLowerLimit() - c.getHingeAngle(), c.getHingeAngle() - c.getUpperLimit(), 0.0)
                    for c in rd.constraints if hasattr(c, "getHingeAngle")]
            if over:
                peak[0] = max(peak[0], max(over))
                rest[0] = max(over)

        for impulse in (Vec3(0, 6, 0), Vec3(0, -6, 0), Vec3(6, 0, 0)):
            rest[0] = 0.0
            _run(impulse, 7.0, watch)
            self.assertLess(rest[0], 2.0)
        self.assertLess(peak[0], 20.0)

    def test_repeats_exactly(self):
        a = _run()[1]._last
        b = _run()[1]._last
        for bone in a:
            np.testing.assert_array_equal(a[bone], b[bone])


if __name__ == "__main__":
    unittest.main()
