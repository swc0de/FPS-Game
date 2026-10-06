"""Milestone 8 infrastructure: AI selection, the fairness audit and the shared model cache."""
import os
import unittest
from types import SimpleNamespace

from panda3d.core import NodePath

from ai.audit import FairnessAudit
from gameplay.director import parse_ai_spec

HERE = os.path.dirname(os.path.abspath(__file__))


class AiSpecTests(unittest.TestCase):
    def test_single_kind_applies_to_both_teams(self):
        self.assertEqual(parse_ai_spec("v2", "attack"), {0: "v2", 1: "v2"})
        self.assertEqual(parse_ai_spec(None, "attack", "legacy"), {0: "legacy", 1: "legacy"})

    def test_per_team_and_per_side(self):
        self.assertEqual(parse_ai_spec("team0=v2,team1=legacy", "attack"), {0: "v2", 1: "legacy"})
        # sides name the teams by their side at the start of the match
        self.assertEqual(parse_ai_spec("attack=v2,defend=legacy", "attack"), {0: "v2", 1: "legacy"})
        self.assertEqual(parse_ai_spec("attack=v2,defend=legacy", "defend"), {0: "legacy", 1: "v2"})

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            parse_ai_spec("smart", "attack")
        with self.assertRaises(ValueError):
            parse_ai_spec("team2=v2", "attack")


class FakeAgent:
    def __init__(self, name, side):
        self.name = name
        self.side = side

    def position(self):
        return (1.0, 2.0, 0.0)


class FakeBot(FakeAgent):
    def __init__(self, name, side):
        super().__init__(name, side)
        self.perception = SimpleNamespace(contacts={})
        self.brain = SimpleNamespace(ai="v2")


class Reader:
    """Stands for AI code (this file is registered as an AI root below)."""
    def __init__(self, bot):
        self.bot = bot

    def peek(self, agent):
        return agent.position()

    def _scan(self, agent):          # the vision code may read positions: that is how seeing works
        return agent.position()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.audit = FairnessAudit(director=None, roots=[HERE], install=False)
        self.audit.watch_class(FakeAgent)

    def tearDown(self):
        self.audit.uninstall()

    def test_reading_an_unseen_enemy_is_a_violation(self):
        me, enemy = FakeBot("me", "attack"), FakeAgent("them", "defend")
        Reader(me).peek(enemy)
        self.assertEqual(self.audit.total("v2"), 1)

    def test_seen_enemy_teammate_and_vision_are_fine(self):
        me, enemy, mate = FakeBot("me", "attack"), FakeAgent("them", "defend"), FakeAgent("mate", "attack")
        Reader(me).peek(mate)
        Reader(me)._scan(enemy)
        me.perception.contacts[id(enemy)] = SimpleNamespace(seen=True)
        Reader(me).peek(enemy)
        self.assertEqual(self.audit.total(), 0)

    def test_code_outside_the_ai_package_is_not_checked(self):
        audit = FairnessAudit(director=None, roots=[os.path.join(HERE, "no_such_dir")], install=False)
        audit.watch_class(FakeAgent)
        try:
            Reader(FakeBot("me", "attack")).peek(FakeAgent("them", "defend"))
            self.assertEqual(audit.total(), 0)
        finally:
            audit.uninstall()


class FakeMaterial:
    uv_scale = 1.0

    def apply(self, np_):
        pass


class SharedModelTests(unittest.TestCase):
    def test_prototype_is_built_once_and_copied(self):
        import weapons.models as wm
        calls = []
        orig = wm.build_weapon_model

        def counting(*a, **kw):
            calls.append(a[1])
            return orig(*a, **kw)
        wm.build_weapon_model = counting
        try:
            mats = SimpleNamespace(get=lambda key: FakeMaterial())
            parent = NodePath("parent")
            a = wm.shared_weapon_model(mats, "rifle_r7", parent)
            b = wm.shared_weapon_model(mats, "rifle_r7", parent)
            self.assertEqual(calls, ["rifle_r7"])
            self.assertNotEqual(a.root, b.root)
            self.assertEqual(a.anchors.keys(), b.anchors.keys())
            n = len(a.root.findAllMatches("**/+GeomNode"))
            self.assertEqual(n, len(b.root.findAllMatches("**/+GeomNode")))
            self.assertGreater(n, 0)
        finally:
            wm.build_weapon_model = orig


if __name__ == "__main__":
    unittest.main()
