"""Round flow and economy rules (pure logic, no engine)."""
import unittest

from gameplay.match import Match, Participant, load_rules

RULES = load_rules()
T = RULES["timers"]
ECO = RULES["economy"]


def make(human_side="attack", opponents=5, teammates=0):
    events = []
    resets = []
    ps = [Participant("you", is_human=True)]
    mates = [Participant(f"mate{i}") for i in range(teammates)]
    opps = [Participant(f"opp{i}") for i in range(opponents)]
    m = Match(RULES, [], human_side, on_event=lambda kind, **d: events.append((kind, d)),
              on_round_reset=lambda match, swapped: resets.append(swapped))
    for p in ps + mates:
        m.add(p, 0)
    for p in opps:
        m.add(p, 1)
    return m, ps[0], opps, events, resets


def revive(m):
    for p in m.participants:
        p.alive_flag = True


def run(m, seconds, dt=0.1):
    t = 0.0
    while t < seconds:
        m.update(dt)
        t += dt


class RoundFlowTests(unittest.TestCase):
    def test_freeze_then_live_then_time_win_for_defenders(self):
        m, you, opps, events, _ = make()
        m.start()
        self.assertEqual(m.phase, "freeze")
        self.assertTrue(m.can_buy())
        run(m, T["freeze_time"] + 0.2)
        self.assertEqual(m.phase, "live")
        run(m, T["buy_time"] + 0.2)
        self.assertFalse(m.can_buy())
        run(m, T["round_time"] - T["buy_time"])
        self.assertEqual(m.phase, "round_end")
        self.assertEqual(m.history[-1].winner_side, "defend")
        self.assertEqual(m.history[-1].reason, "time")

    def test_elimination_and_rewards(self):
        m, you, opps, events, _ = make()
        m.start()
        run(m, T["freeze_time"] + 0.2)
        for o in opps:
            m.on_kill(o, you, "r7", reward=300, headshot=True)
        self.assertEqual(m.phase, "round_end")
        self.assertEqual(m.history[-1].reason, "elimination")
        self.assertEqual(you.stats.kills, 5)
        self.assertEqual(you.stats.headshots, 5)
        # 800 start + 5 kills + elimination win
        self.assertEqual(you.money, 800 + 5 * 300 + ECO["win"]["elimination"])
        # losers get the first loss bonus
        self.assertEqual(opps[0].money, 800 + ECO["loss_bonus"][0])
        self.assertEqual(m.history[-1].mvp, "you")

    def test_attackers_dead_after_plant_round_continues(self):
        m, you, opps, events, _ = make()
        m.start()
        run(m, T["freeze_time"] + 0.2)
        m.on_bomb_planted(you)
        self.assertEqual(m.phase, "planted")
        m.on_kill(you, opps[0], "c9", reward=300)
        self.assertEqual(m.phase, "planted")          # bomb still ticking
        m.on_bomb_exploded()
        self.assertEqual(m.history[-1].winner_side, "attack")
        self.assertEqual(m.history[-1].reason, "bomb_detonated")
        # planter reward + detonation win
        self.assertEqual(you.money, 800 + ECO["plant_reward"] + ECO["win"]["bomb_detonated"])

    def test_defuse_win_and_planted_loss_bonus(self):
        m, you, opps, events, _ = make(human_side="defend")
        m.start()
        run(m, T["freeze_time"] + 0.2)
        m.on_bomb_planted(opps[0])
        m.on_bomb_defused(you)
        self.assertEqual(m.history[-1].reason, "bomb_defused")
        self.assertEqual(you.money, 800 + ECO["defuse_reward"] + ECO["win"]["bomb_defused"])
        planter_bonus = ECO["plant_reward"]
        self.assertEqual(opps[0].money, 800 + planter_bonus + ECO["loss_bonus"][0] + ECO["planted_loss_bonus"])
        self.assertEqual(opps[1].money, 800 + ECO["loss_bonus"][0] + ECO["planted_loss_bonus"])

    def test_loss_bonus_streak_and_cap(self):
        m, you, opps, events, _ = make()
        m.start()
        expected = 800
        for i in range(7):
            run(m, T["freeze_time"] + 0.2)
            m.force_end_round("defend", "time")
            expected = min(expected + ECO["loss_bonus"][min(i + 1, 5) - 1], ECO["max_money"])
            self.assertEqual(you.money, expected, f"round {i + 1}")
            run(m, T["round_end_delay"] + 0.2)
            revive(m)
        # a win lowers the streak by one step
        run(m, T["freeze_time"] + 0.2)
        m.force_end_round("attack", "elimination")
        self.assertEqual(m.teams[0].loss_streak, 6)

    def test_money_cap(self):
        m, you, opps, events, _ = make()
        m.start()
        you.money = 15900
        run(m, T["freeze_time"] + 0.2)
        m.on_kill(opps[0], you, "knife", reward=1500)
        self.assertEqual(you.money, ECO["max_money"])

    def test_friendly_fire_filter_and_assists(self):
        m, you, opps, events, _ = make(teammates=1)
        mate = m.teams[0].members[1]
        self.assertFalse(m.allow_damage(mate, you))
        self.assertTrue(m.allow_damage(opps[0], you))
        m.start()
        run(m, T["freeze_time"] + 0.2)
        m.on_damage(opps[0], mate, 60)
        m.on_kill(opps[0], you, "r7", reward=300)
        self.assertEqual(mate.stats.assists, 1)

    def test_halftime_swaps_sides_and_resets_money(self):
        m, you, opps, events, resets = make()
        m.start()
        half = RULES["rounds"]["halftime_after"]
        for r in range(half):
            run(m, T["freeze_time"] + 0.2)
            m.force_end_round("attack" if r % 2 == 0 else "defend")
            run(m, T["round_end_delay"] + 0.2)
            revive(m)
        self.assertEqual(m.phase, "halftime")
        run(m, T["halftime_delay"] + 0.2)
        self.assertEqual(m.phase, "freeze")
        self.assertEqual(you.side, "defend")
        self.assertEqual(opps[0].side, "attack")
        self.assertEqual(you.money, ECO["start_money"])
        self.assertEqual(m.round, half + 1)
        self.assertTrue(resets[-1])                 # world reset told about the swap
        self.assertEqual(m.teams[0].score + m.teams[1].score, half)

    def test_match_ends_at_win_score(self):
        m, you, opps, events, _ = make()
        m.start()
        while m.phase != "match_end":
            run(m, T["freeze_time"] + 0.2)
            m.force_end_round(you.side)
            run(m, max(T["round_end_delay"], T["halftime_delay"]) + 0.3)
            revive(m)
        self.assertEqual(m.teams[0].score, RULES["rounds"]["win_score"])
        self.assertTrue(any(k == "match_end" for k, _ in events))


if __name__ == "__main__":
    unittest.main()
