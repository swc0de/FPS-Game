"""Milestone 8 v2 bots: the humaniser, traits and roles, utility scoring with
commitment, budgeted path searches, plan variety and the trade rule."""
import random
import unittest
from collections import Counter
from types import SimpleNamespace

from panda3d.core import Point3

from ai.v2.brain import Action, BrainV2
from ai.v2.humanize import DEFAULTS, Humanizer
from ai.v2.knowledge import Fact
from ai.v2.pathing import PENDING, PathService
from ai.v2.personality import assign_roles, traits_for
from ai.v2.strategy import PLAN_WEIGHTS, SETUP_WEIGHTS, TeamStrategy, pick_varied
from tests.test_navmesh import nav_for, path_is_walkable, room_level


class HumanizerTests(unittest.TestCase):
    def make(self, difficulty="normal", seed=1):
        return Humanizer(SimpleNamespace(rng=random.Random(seed), difficulty=difficulty))

    def test_reaction_is_lognormal_with_the_profile_mean(self):
        h = self.make()
        lo, hi = 0.32, 0.5
        draws = [h.reaction(lo, hi) for _ in range(20000)]
        mean = sum(draws) / len(draws)
        self.assertAlmostEqual(mean, (lo + hi) / 2, delta=0.02 * (lo + hi) / 2)
        self.assertGreaterEqual(min(draws), 0.85 * lo - 1e-9)       # never much faster than the profile
        self.assertLessEqual(max(draws), 2.5 * hi + 1e-9)
        slow = sum(1 for d in draws if d > hi * 1.3) / len(draws)
        self.assertGreater(slow, 0.01)                              # now and then much slower

    def test_attention_and_flicks(self):
        h = self.make()
        self.assertEqual(h.attention(90, busy=False), 1.0)
        self.assertEqual(h.attention(90, busy=True), DEFAULTS["normal"]["tunnel"])
        self.assertLess(h.attention(10, busy=True), h.attention(90, busy=True))
        self.assertEqual(h.flick(5.0), 0.0)                         # small corrections do not overshoot
        self.assertTrue(any(h.flick(60.0) != 0.0 for _ in range(10)))

    def test_harder_bots_make_fewer_mistakes(self):
        for kind in ("over_peek", "skip_corner", "reload_open", "late_trade", "ignore_call", "panic"):
            rates = [DEFAULTS[d][kind] for d in ("easy", "normal", "hard", "expert")]
            self.assertEqual(rates, sorted(rates, reverse=True), kind)

    def test_stress_makes_mistakes_likelier_and_decays(self):
        h = self.make()
        calm = sum(h.mistake("over_peek") for _ in range(4000))
        h.add_stress(1.0)
        stressed = sum(h.mistake("over_peek") for _ in range(4000))
        h.stress = 1.0
        self.assertGreater(stressed, calm * 1.2)
        for _ in range(64 * 6):
            h.update(1 / 64)
        self.assertEqual(h.stress, 0.0)


def fake_bot(name, traits, sniper=False, nades=0):
    cls = "sniper" if sniper else "rifle"
    return SimpleNamespace(name=name, brain=SimpleNamespace(traits=traits),
                           weapons=SimpleNamespace(primary=SimpleNamespace(d=SimpleNamespace(cls=cls)),
                                                   inv=SimpleNamespace(grenades={"flash": nades})))


class PersonalityTests(unittest.TestCase):
    def test_traits_are_stable_per_bot_and_seed(self):
        a = traits_for("Anvil", 7)
        self.assertEqual(a, traits_for("Anvil", 7))
        self.assertNotEqual(a, traits_for("Brine", 7))
        self.assertNotEqual(a, traits_for("Anvil", 8))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in a.values()))

    def test_attack_roles(self):
        bots = [fake_bot(n, traits_for(n, 3), sniper=(n == "Echo"), nades=(2 if n == "Bravo" else 0))
                for n in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
        carrier = bots[2]
        roles = assign_roles(bots, "attack", random.Random(1), lurker=True, carrier=carrier)
        self.assertEqual(roles[id(bots[4])], "awper")
        got = Counter(roles.values())
        self.assertEqual(got["entry"], 1)
        self.assertEqual(got["support"], 1)
        self.assertEqual(got["lurker"], 1)
        self.assertEqual(sum(got.values()), 5)
        self.assertNotEqual(roles[id(carrier)], "lurker")

    def test_defence_roles(self):
        bots = [fake_bot(n, traits_for(n, 5)) for n in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
        roles = assign_roles(bots, "defend", random.Random(2))
        got = Counter(roles.values())
        self.assertEqual(got["anchor"], 2)
        self.assertEqual(sum(got.values()), 5)


class StubAction(Action):
    def __init__(self, brain, name, value, min_time=0.5):
        super().__init__(brain)
        self.name = name
        self.value = value
        self.min_time = min_time

    def score(self, ctx):
        return self.value


class StubBrain(BrainV2):
    """Only the scoring loop of BrainV2."""

    def __init__(self, values):
        self.rng = random.Random(4)
        self.human = SimpleNamespace(noise=lambda: 0.0)
        self.shooter = SimpleNamespace(target_id=None, release=lambda: None)
        self.actions = {n: StubAction(self, n, v) for n, v in values.items()}
        self.action = self.actions["task"]
        self.action.started = 0.0

    def _context(self, now):
        return SimpleNamespace(target=None)

    def _comms(self, ctx, now):
        pass

    def _consider_utility(self, ctx, now):
        pass


class UtilityTests(unittest.TestCase):
    def test_best_score_wins(self):
        b = StubBrain({"task": 0.5, "reload": 0.9})
        b.think(10.0)
        self.assertEqual(b.mode, "reload")

    def test_commitment_and_hysteresis(self):
        b = StubBrain({"task": 0.5, "alert": 0.7, "reload": 0.0})
        b.think(10.0)
        self.assertEqual(b.mode, "alert")
        # a slightly better action does not interrupt a fresh one ...
        b.actions["reload"].value = 0.85
        b.think(10.2)
        self.assertEqual(b.mode, "alert")
        # ... nor one that has run its minimum time unless it beats it by a margin
        b.actions["reload"].value = 0.75
        b.think(11.0)
        self.assertEqual(b.mode, "alert")
        b.actions["reload"].value = 0.85
        b.think(11.2)
        self.assertEqual(b.mode, "reload")


class HalftimeTests(unittest.TestCase):
    def test_path_service_follows_the_current_side(self):
        """At halftime the team proxy points at the other side's strategy: a brain must use that
        strategy's path service (a stale one is never updated and every request stays pending)."""
        b = BrainV2.__new__(BrainV2)
        first, second = object(), object()
        b.team = SimpleNamespace(paths=first)
        self.assertIs(b.paths, first)
        b.team.paths = second
        self.assertIs(b.paths, second)


class PathServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nav = nav_for(room_level())

    def test_budgeted_search_then_cached(self):
        svc = PathService(self.nav, budget=4)
        a, b = (-6, -5, 0), (0, 5, 0)
        self.assertEqual(svc.request(a, b), PENDING)
        rounds = 0
        while svc.queue:
            self.assertLessEqual(svc.update(), 4)      # never more than the budget per tick
            rounds += 1
        self.assertGreater(rounds, 1)
        path = svc.request(a, b)
        self.assertNotIn(path, (None, PENDING))
        self.assertTrue(path_is_walkable(self.nav, path))
        self.assertLess(Point3(*path[0]).__sub__(Point3(*a)).length(), 0.6)
        self.assertLess(Point3(*path[-1]).__sub__(Point3(*b)).length(), 0.6)
        hits = svc.stats["hits"]
        self.assertNotIn(svc.request(a, b), (None, PENDING))
        self.assertEqual(svc.stats["hits"], hits + 1)

    def test_same_route_as_the_full_search(self):
        svc = PathService(self.nav, budget=1000)
        a, b = (-6, -5, 0), (6, 8, 0)
        svc.request(a, b)
        svc.update()
        mine = svc.request(a, b)
        full = self.nav.find_path(a, b)
        length = lambda p: sum(Point3(*x).__sub__(Point3(*y)).length() for x, y in zip(p, p[1:]))
        self.assertAlmostEqual(length(mine), length(full), delta=0.5)


class PlanTests(unittest.TestCase):
    def test_no_plan_or_setup_dominates(self):
        for weights in (PLAN_WEIGHTS, SETUP_WEIGHTS):
            for seed in range(5):
                rng = random.Random(seed)
                history = []
                for _ in range(60):
                    history.append(pick_varied(dict(weights), history, rng))
                top = Counter(history).most_common(1)[0][1] / len(history)
                self.assertLess(top, 0.4, (weights, seed))

    def test_eco_favours_cheap_plans(self):
        rng = random.Random(1)
        w = dict(PLAN_WEIGHTS)
        for p in ("rush", "contact"):
            w[p] *= 3.0
        picks = Counter(pick_varied(w, [], rng) for _ in range(2000))
        self.assertGreater(picks["rush"] + picks["contact"], picks["execute"] + picks["default"])


class StubTeam(TeamStrategy):
    """Only the trade rule of TeamStrategy."""

    def __init__(self, bots):
        self.bots_ = bots
        self.deaths = []

    def mates_of(self, bot):
        return [b for b in self.bots_ if b is not bot]


def stub_bot(name, x, facts=()):
    return SimpleNamespace(name=name, position=lambda: Point3(x, 0, 0),
                           brain=SimpleNamespace(knowledge=SimpleNamespace(recent=lambda age, now: list(facts))))


class TradeTests(unittest.TestCase):
    def test_two_closest_trade_with_what_they_know(self):
        heard = Fact(7, (4.0, 9.0, 0.0), 1.5, 9.5, "sound")
        a, b, c = stub_bot("A", 2.0, [heard]), stub_bot("B", 4.0), stub_bot("C", 9.0)
        team = StubTeam([a, b, c])
        victim = SimpleNamespace()
        team.deaths.append({"t": 10.0, "pos": Point3(0, 0, 0), "victim": victim, "guess": (0.0, 12.0, 0.0)})
        fa, spot = team.trade_fact(a, 10.5)
        self.assertIs(fa, heard)                          # its own information first
        self.assertEqual(spot, Point3(0, 0, 0))           # swing from where the teammate died
        fb, _ = team.trade_fact(b, 10.5)
        self.assertEqual(fb.source, "damage")             # where the teammate was looking
        self.assertEqual(fb.pos, (0.0, 12.0, 0.0))
        self.assertIsNone(team.trade_fact(c, 10.5))       # third closest keeps its own angle
        self.assertIsNone(team.trade_fact(a, 13.5))       # too late to trade


if __name__ == "__main__":
    unittest.main()
