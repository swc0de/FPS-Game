"""Competitive match flow: rounds, economy, bomb objective rules.

Pure game logic with no engine dependencies, so it is unit-tested directly.
The game feeds it events (damage, kills, plant, defuse, detonation) and
reacts to its callbacks (round reset, HUD/audio events).

Round phases::

    freeze  (players locked at spawn, buying allowed)
      -> live     (round timer runs; buy time continues for a while)
      -> planted  (round timer replaced by the bomb timer)
      -> round_end (winner shown, money paid)
      -> [halftime] -> freeze of the next round ... -> match_end

Win conditions (CS rules):
  * all attackers dead before a plant              -> defenders (elimination)
  * all defenders dead (planted or not)             -> attackers (elimination)
  * round time runs out without a plant             -> defenders (time)
  * bomb detonates (even if every attacker is dead) -> attackers (bomb_detonated)
  * bomb defused                                    -> defenders (bomb_defused)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

SIDES = ("attack", "defend")


def other_side(side: str) -> str:
    return "defend" if side == "attack" else "attack"


def load_rules(path=None) -> dict:
    if path is None:
        from engine import paths
        path = paths.DATA_DIR / "match.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Stats:
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    headshots: int = 0
    damage: float = 0.0
    score: int = 0
    mvps: int = 0
    round_kills: int = 0


class Participant:
    """Anything that plays on a team (the human player, stand-ins, bots).

    Subclasses provide ``alive`` and react to ``reset_for_round``."""

    def __init__(self, name: str, is_human: bool = False):
        self.name = name
        self.is_human = is_human
        self.team: Team | None = None
        self.money = 0
        self.stats = Stats()
        self.alive_flag = True

    @property
    def side(self) -> str:
        return self.team.side if self.team is not None else ""

    @property
    def alive(self) -> bool:
        return self.alive_flag

    def add_money(self, amount: int, cap: int) -> int:
        before = self.money
        self.money = max(0, min(self.money + int(amount), cap))
        return self.money - before


@dataclass
class Team:
    index: int
    side: str
    score: int = 0
    loss_streak: int = 0
    members: list = field(default_factory=list)
    planted_this_round: bool = False

    def alive_members(self) -> list:
        return [m for m in self.members if m.alive]


@dataclass
class RoundResult:
    number: int
    winner_side: str
    reason: str            # elimination | time | bomb_detonated | bomb_defused
    winner_team: int
    mvp: str = ""


class Match:
    PHASES = ("waiting", "freeze", "live", "planted", "round_end", "halftime", "match_end")

    def __init__(self, rules: dict, participants: list[Participant], human_side: str = "attack",
                 on_event: Callable | None = None, on_round_reset: Callable | None = None):
        self.rules = rules
        self.timers = rules["timers"]
        self.eco = rules["economy"]
        self.round_rules = rules["rounds"]
        self.teams = [Team(0, human_side), Team(1, other_side(human_side))]
        self.participants: list[Participant] = []
        self.on_event = on_event or (lambda kind, **data: None)
        self.on_round_reset = on_round_reset or (lambda match, swapped: None)
        for p in participants:
            self.add(p, 0 if p.is_human else 1)
        self.phase = "waiting"
        self.round = 0                      # 1-based number of the current round
        self.phase_time = 0.0               # seconds spent in the current phase
        self.round_clock = 0.0              # seconds since the round went live
        self.bomb_clock = 0.0
        self.history: list[RoundResult] = []
        self.planter: Participant | None = None
        self.defuser: Participant | None = None
        self._round_damage: dict[tuple[int, int], float] = {}
        self._pending: str | None = None    # phase to enter after round_end/halftime

    # ----------------------------------------------------------- roster
    def add(self, p: Participant, team_index: int) -> None:
        team = self.teams[team_index]
        p.team = team
        team.members.append(p)
        p.money = int(self.eco["start_money"])
        self.participants.append(p)

    def team_of_side(self, side: str) -> Team:
        return self.teams[0] if self.teams[0].side == side else self.teams[1]

    def side_alive(self, side: str) -> int:
        return len(self.team_of_side(side).alive_members())

    @property
    def max_money(self) -> int:
        return int(self.eco["max_money"])

    # ---------------------------------------------------------- queries
    @property
    def freeze(self) -> bool:
        return self.phase == "freeze"

    @property
    def is_live(self) -> bool:
        return self.phase in ("live", "planted")

    def can_buy(self) -> bool:
        if self.phase == "freeze":
            return True
        return self.phase in ("live", "planted") and self.round_clock < float(self.timers["buy_time"])

    def buy_time_left(self) -> float:
        if self.phase == "freeze":
            return float(self.timers["buy_time"])
        if self.phase in ("live", "planted"):
            return max(float(self.timers["buy_time"]) - self.round_clock, 0.0)
        return 0.0

    def clock(self) -> float:
        """The number shown at the top of the HUD."""
        if self.phase == "freeze":
            return max(float(self.timers["freeze_time"]) - self.phase_time, 0.0)
        if self.phase == "live":
            return max(float(self.timers["round_time"]) - self.round_clock, 0.0)
        if self.phase == "planted":
            return max(float(self.timers["bomb_timer"]) - self.bomb_clock, 0.0)
        if self.phase == "round_end":
            return max(float(self.timers["round_end_delay"]) - self.phase_time, 0.0)
        if self.phase == "halftime":
            return max(float(self.timers["halftime_delay"]) - self.phase_time, 0.0)
        return 0.0

    def round_time_left(self) -> float:
        """Seconds until the round timer runs out (full time during freeze)."""
        if self.phase == "freeze":
            return float(self.timers["round_time"])
        if self.phase in ("live", "planted"):
            return max(float(self.timers["round_time"]) - self.round_clock, 0.0)
        return 0.0

    def is_halftime_round(self) -> bool:
        return self.round == int(self.round_rules["halftime_after"])

    # ------------------------------------------------------------ flow
    def start(self) -> None:
        self.round = 0
        for t in self.teams:
            t.score = 0
            t.loss_streak = 0
        for p in self.participants:
            p.money = int(self.eco["start_money"])
            p.stats = Stats()
        self.history = []
        self._begin_round(swapped=False, fresh=True)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self.phase_time = 0.0
        self.on_event("phase", phase=phase)

    def _begin_round(self, swapped: bool, fresh: bool = False) -> None:
        self.round += 1
        self.round_clock = 0.0
        self.bomb_clock = 0.0
        self.planter = None
        self.defuser = None
        self._round_damage = {}
        for t in self.teams:
            t.planted_this_round = False
        for p in self.participants:
            p.stats.round_kills = 0
        self.on_round_reset(self, swapped or fresh)
        self.on_event("round_start", round=self.round)
        self._set_phase("freeze")

    def update(self, dt: float) -> None:
        if self.phase in ("waiting", "match_end"):
            self.phase_time += dt
            return
        self.phase_time += dt
        if self.phase == "freeze":
            if self.phase_time >= float(self.timers["freeze_time"]):
                self._set_phase("live")
                self.on_event("live")
        elif self.phase == "live":
            self.round_clock += dt
            if self.round_clock >= float(self.timers["round_time"]):
                self._end_round("defend", "time")
        elif self.phase == "planted":
            self.round_clock += dt
            self.bomb_clock += dt
        elif self.phase == "round_end":
            if self.phase_time >= float(self.timers["round_end_delay"]):
                self._after_round()
        elif self.phase == "halftime":
            if self.phase_time >= float(self.timers["halftime_delay"]):
                self._swap_sides()
                self._begin_round(swapped=True)

    # ---------------------------------------------------------- events in
    def allow_damage(self, victim: Participant | None, attacker: Participant | None) -> bool:
        if victim is None or attacker is None or victim is attacker:
            return True
        if victim.team is attacker.team and not self.rules.get("friendly_fire", False):
            return False
        return True

    def on_damage(self, victim: Participant, attacker: Participant | None, amount: float) -> None:
        if attacker is None or attacker is victim:
            return
        key = (id(attacker), id(victim))
        self._round_damage[key] = self._round_damage.get(key, 0.0) + amount
        attacker.stats.damage += amount

    def on_kill(self, victim: Participant, killer: Participant | None, weapon: str = "", reward: int | None = None,
                headshot: bool = False, kind: str = "bullet") -> None:
        victim.alive_flag = False
        victim.stats.deaths += 1
        assister = None
        if killer is not None and killer is not victim:
            if killer.team is victim.team:
                killer.add_money(-300, self.max_money)
                killer.stats.score -= 1
            else:
                killer.stats.kills += 1
                killer.stats.round_kills += 1
                killer.stats.score += 2
                if headshot:
                    killer.stats.headshots += 1
                amount = int(self.eco["kill_reward_default"] if reward is None else reward)
                gained = killer.add_money(amount, self.max_money)
                if gained:
                    self.on_event("money", who=killer, amount=gained, reason=f"kill ({weapon})")
            threshold = float(self.eco.get("assist_damage", 41))
            best = 0.0
            for p in self.participants:
                if p is killer or p.team is victim.team:
                    continue
                dmg = self._round_damage.get((id(p), id(victim)), 0.0)
                if dmg >= threshold and dmg > best:
                    best, assister = dmg, p
            if assister is not None:
                assister.stats.assists += 1
                assister.stats.score += 1
        self.on_event("kill", killer=killer, victim=victim, weapon=weapon, headshot=headshot, assister=assister,
                      damage_kind=kind)
        self.check_round_end()

    def on_bomb_planted(self, planter: Participant) -> None:
        if self.phase != "live":
            return
        self.planter = planter
        planter.team.planted_this_round = True
        planter.stats.score += 2
        gained = planter.add_money(int(self.eco["plant_reward"]), self.max_money)
        if gained:
            self.on_event("money", who=planter, amount=gained, reason="bomb planted")
        self.bomb_clock = 0.0
        self._set_phase("planted")
        self.on_event("bomb_planted", planter=planter)
        self.check_round_end()

    def bomb_time_left(self) -> float:
        return max(float(self.timers["bomb_timer"]) - self.bomb_clock, 0.0)

    def on_bomb_defused(self, defuser: Participant) -> None:
        if self.phase != "planted":
            return
        self.defuser = defuser
        defuser.stats.score += 2
        gained = defuser.add_money(int(self.eco["defuse_reward"]), self.max_money)
        if gained:
            self.on_event("money", who=defuser, amount=gained, reason="bomb defused")
        self.on_event("bomb_defused", defuser=defuser)
        self._end_round("defend", "bomb_defused")

    def on_bomb_exploded(self) -> None:
        if self.phase != "planted":
            return
        self.on_event("bomb_exploded")
        self._end_round("attack", "bomb_detonated")

    def check_round_end(self) -> None:
        if self.phase not in ("live", "planted"):
            return
        att = self.side_alive("attack")
        dfn = self.side_alive("defend")
        if dfn == 0 and self.team_of_side("defend").members:
            self._end_round("attack", "elimination")
        elif att == 0 and self.phase == "live" and self.team_of_side("attack").members:
            self._end_round("defend", "elimination")
        # attackers all dead after a plant: the bomb keeps ticking

    # -------------------------------------------------------- round end
    def _end_round(self, winner_side: str, reason: str) -> None:
        if self.phase not in ("live", "planted", "freeze"):
            return
        winner = self.team_of_side(winner_side)
        loser = self.team_of_side(other_side(winner_side))
        winner.score += 1
        mvp = self._pick_mvp(winner, reason)
        if mvp is not None:
            mvp.stats.mvps += 1
        result = RoundResult(self.round, winner_side, reason, winner.index, mvp.name if mvp else "")
        self.history.append(result)
        self._pay_round(winner, loser, reason)
        self._set_phase("round_end")
        self.on_event("round_end", result=result)

    def _pick_mvp(self, winner: Team, reason: str) -> Participant | None:
        if reason == "bomb_defused" and self.defuser is not None:
            return self.defuser
        if reason == "bomb_detonated" and self.planter is not None:
            return self.planter
        if not winner.members:
            return None
        best = max(winner.members, key=lambda p: (p.stats.round_kills, p.stats.damage))
        return best if best.stats.round_kills > 0 else None

    def _pay_round(self, winner: Team, loser: Team, reason: str) -> None:
        win_amount = int(self.eco["win"][reason])
        for p in winner.members:
            gained = p.add_money(win_amount, self.max_money)
            if gained:
                self.on_event("money", who=p, amount=gained, reason="round won")
        winner.loss_streak = max(winner.loss_streak - 1, 0)
        loser.loss_streak += 1
        bonuses = self.eco["loss_bonus"]
        bonus = int(bonuses[min(loser.loss_streak, len(bonuses)) - 1])
        if loser.side == "attack" and loser.planted_this_round:
            bonus += int(self.eco["planted_loss_bonus"])
        for p in loser.members:
            gained = p.add_money(bonus, self.max_money)
            if gained:
                self.on_event("money", who=p, amount=gained, reason="round lost")

    def _after_round(self) -> None:
        target = int(self.round_rules["win_score"])
        max_rounds = int(self.round_rules["max_rounds"])
        if any(t.score >= target for t in self.teams) or self.round >= max_rounds:
            self._set_phase("match_end")
            a, b = self.teams
            winner = None if a.score == b.score else (a if a.score > b.score else b)
            self.on_event("match_end", winner=winner)
            return
        if self.is_halftime_round():
            self._set_phase("halftime")
            self.on_event("halftime")
            return
        self._begin_round(swapped=False)

    def _swap_sides(self) -> None:
        for t in self.teams:
            t.side = other_side(t.side)
            t.loss_streak = 0
        for p in self.participants:
            p.money = int(self.eco["start_money"])
        self.on_event("sides_swapped")

    # ---------------------------------------------------------- helpers
    def force_end_round(self, winner_side: str, reason: str = "elimination") -> None:
        """Developer console / tests."""
        if self.phase == "freeze":
            self._set_phase("live")
        self._end_round(winner_side, reason)

    def scoreline(self) -> tuple[int, int]:
        """(attack score, defend score) for the current sides."""
        return self.team_of_side("attack").score, self.team_of_side("defend").score
