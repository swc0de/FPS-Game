"""Bot perception on a real Bullet world: view cone, walls, reaction delay,
flash blindness and hearing."""
import random
import unittest
from types import SimpleNamespace

from panda3d.core import NodePath, Point3, Vec3

from ai.aim import AimController
from ai.perception import Perception
from engine.physics import PhysicsWorld

VISION = {"range": 90.0, "peripheral": 160.0, "peripheral_range": 14.0, "smoke_block": 0.55, "scan_interval": 0.1}
PROFILE = {"fov": 110, "reaction": [0.3, 0.3], "hearing": 1.0}


class Agent:
    def __init__(self, pos, side="defend"):
        self.pos = Point3(*pos)
        self.side = side
        self.alive = True

    def position(self):
        return Point3(self.pos)

    def head_pos(self):
        return self.pos + Vec3(0, 0, 1.63)

    def center_of_mass(self):
        return self.pos + Vec3(0, 0, 1.1)

    def velocity(self):
        return Vec3(0, 0, 0)


class Bot(Agent):
    def __init__(self, game, pos, yaw=0.0):
        super().__init__(pos, "attack")
        self.game = game
        self.rng = random.Random(1)
        self.aim = AimController({}, self.rng)
        self.aim.yaw = yaw
        self.now = 0.0
        self.spotted = []
        self.heard = []
        self.perception = Perception(self, VISION, PROFILE)
        self.perception.next_scan = 0.0

    def eye(self):
        return self.pos + Vec3(0, 0, 1.67)

    def on_spotted(self, e):
        self.spotted.append(e)

    def on_heard(self, e, pos, loud):
        self.heard.append((e, pos))


def make_game():
    phys = PhysicsWorld(NodePath("root"))
    phys.add_static_box((0, 0, -0.5), (30, 30, 0.5))
    phys.add_static_box((5, 10, 1.5), (2, 0.1, 1.5))           # wall in front of x in [3, 7]
    return SimpleNamespace(physics=phys, effects=SimpleNamespace(smoke_between=lambda a, b: 0.0),
                           noise_events=[])


def scan(bot, enemies, t):
    bot.now = t
    bot.perception.next_scan = 0.0
    bot.perception.update(t, enemies)


class PerceptionTests(unittest.TestCase):
    def test_sees_enemy_in_front_after_reaction_time(self):
        g = make_game()
        bot = Bot(g, (0, 0, 0))
        e = Agent((-3, 15, 0))
        scan(bot, [e], 1.0)
        self.assertEqual(len(bot.perception.visible()), 1)
        self.assertIsNone(bot.perception.target(1.1))            # still reacting
        self.assertIsNotNone(bot.perception.target(1.31))
        self.assertEqual(bot.spotted, [e])

    def test_wall_blocks_sight(self):
        g = make_game()
        bot = Bot(g, (5, 0, 0))
        e = Agent((5, 20, 0))
        scan(bot, [e], 1.0)
        self.assertEqual(bot.perception.visible(), [])

    def test_view_cone_and_peripheral_vision(self):
        g = make_game()
        bot = Bot(g, (0, 0, 0), yaw=90.0)                         # looking towards -X
        far = Agent((-3, 20, 0))                                  # ~81 deg off, 20 m: outside fov
        near = Agent((-3, 8, 0))                                  # ~69 deg off, 8.5 m: peripheral
        scan(bot, [far, near], 1.0)
        seen = [c.agent for c in bot.perception.visible()]
        self.assertNotIn(far, seen)
        self.assertIn(near, seen)

    def test_flashed_bot_is_blind(self):
        g = make_game()
        bot = Bot(g, (0, 0, 0))
        e = Agent((-3, 15, 0))
        bot.now = 1.0
        bot.perception.flashed(2.0)
        scan(bot, [e], 1.5)
        self.assertEqual(bot.perception.visible(), [])
        scan(bot, [e], 3.5)
        self.assertEqual(len(bot.perception.visible()), 1)

    def test_hears_enemy_footsteps_not_teammates(self):
        g = make_game()
        bot = Bot(g, (0, 0, 0))
        enemy = Agent((0, -15, 0), "defend")
        mate = Agent((0, -10, 0), "attack")
        g.noise_events = [(0.5, Point3(0, -15, 0), 1.0, 28.0, enemy), (0.6, Point3(0, -10, 0), 1.0, 28.0, mate),
                          (0.7, Point3(0, -40, 0), 1.0, 28.0, enemy)]
        scan(bot, [enemy], 1.0)
        self.assertEqual([h[0] for h in bot.heard], [enemy])      # the 40 m step is out of range
        c = bot.perception.contacts[id(enemy)]
        self.assertEqual(c.source, "sound")
        self.assertLess((c.pos - enemy.pos).length(), 2.0)
        # already-processed events are not heard twice
        bot.heard.clear()
        scan(bot, [enemy], 1.2)
        self.assertEqual(bot.heard, [])


if __name__ == "__main__":
    unittest.main()
