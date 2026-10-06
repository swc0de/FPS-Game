"""What a v2 bot knows about its enemies, and where it learned it.

Every piece of information is a ``Fact`` with its source and precision:

    sight   the bot sees the enemy (Perception's vision scan); exact
    sound   footsteps, shots, grenades heard (Perception's hearing): the
            position gets fuzzier with distance
    damage  shot by an enemy the bot did not see: only the direction the
            shot came from (the HUD's damage arc), with a guessed range - not
            the shooter's position
    radio   a teammate's callout: area precision, delayed (ai/v2/comms.py)
    radar   a glance at the HUD radar, which shows every enemy a teammate
            spots, for 3 s; bots look at it now and then, never mid-fight
    intel   pings from drones, cameras and gadgets (also on the HUD)

Nothing here ever asks an enemy where it is; Perception's vision scan is the
only code that does, and that is what seeing is (ai/audit.py checks this).
Actions that aim, pre-aim, fire or path because of an enemy name the fact
they act on, so the fairness audit can check every order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from panda3d.core import Point3, Vec3

PRECISION = {"sight": 0.3, "radar": 1.0, "intel": 1.0, "radio": 3.5}


@dataclass
class Fact:
    enemy: int | None            # id() of the enemy, None when unknown
    pos: tuple                   # estimated feet position
    radius: float                # uncertainty (m)
    time: float                  # when it was true
    source: str                  # sight | sound | damage | radio | radar | intel
    speed: float = 5.4           # how fast it could be moving since
    by: str = ""                 # who reported it
    hurt: bool = False           # "tagged": damaged by the reporter (hit markers)
    direction: tuple | None = None   # damage: unit vector from the victim towards the shooter
    area: str = ""

    def age(self, now: float) -> float:
        return now - self.time

    def reach(self, now: float) -> float:
        """How far from pos the enemy can be by now."""
        return self.radius + self.speed * max(now - self.time, 0.0)


class BotKnowledge:
    def __init__(self, bot, cfg: dict | None = None):
        self.bot = bot
        self.cfg = cfg or {}
        self.facts: dict[int, Fact] = {}           # latest fact per enemy
        self.anonymous: list[Fact] = []            # unknown enemies (damage from an unseen shooter)
        self.damage_dealt: dict[int, float] = {}   # what this bot's hits did (hit markers)
        self.radar_next = 0.0
        self._heard_t: dict[int, float] = {}
        self.alive_enemies = 5

    def reset(self) -> None:
        self.facts.clear()
        self.anonymous.clear()
        self.damage_dealt.clear()
        self._heard_t.clear()
        self.radar_next = 0.0

    # --------------------------------------------------------------- input
    def add(self, f: Fact) -> bool:
        if f.enemy is None:
            self.anonymous.append(f)
            if len(self.anonymous) > 8:
                self.anonymous = self.anonymous[-8:]
            return True
        old = self.facts.get(f.enemy)
        if old is not None:
            # a newer but vaguer fact does not replace a sharper one that is still as good
            if f.time < old.time:
                return False
            if old.source == "sight" and f.source != "sight" and old.reach(f.time) <= f.radius:
                return False
        self.facts[f.enemy] = f
        return True

    def from_perception(self, now: float) -> list[Fact]:
        """New sight and sound facts from the bot's own Perception; returns the sightings
        (the caller decides what to call out)."""
        per = self.bot.perception
        seen = []
        for key, c in per.contacts.items():
            if c.seen:
                f = Fact(key, (c.pos.x, c.pos.y, c.pos.z), PRECISION["sight"], now, "sight", speed=5.4)
                self.facts[key] = f
                seen.append(f)
            elif c.source == "sound" and c.time > self._heard_t.get(key, -1.0):
                self._heard_t[key] = c.time
                me = self.bot.position()
                d = math.hypot(c.pos.x - me.x, c.pos.y - me.y)
                self.add(Fact(key, (c.pos.x, c.pos.y, c.pos.z), min(d * 0.08, 4.0) + 1.0, c.time, "sound"))
        return seen

    def on_shot(self, direction, now: float, guess: float = 15.0) -> Fact:
        """Hit by an enemy the bot cannot see: only the direction is known (``direction``
        is the bullet's travel direction)."""
        p = self.bot.position()
        d = Vec3(*direction)
        d.z = 0
        if d.lengthSquared() < 1e-6:
            d = Vec3(0, 1, 0)
        d.normalize()
        back = (-d.x, -d.y, 0.0)
        f = Fact(None, (p.x + back[0] * guess, p.y + back[1] * guess, p.z), guess * 0.8, now, "damage",
                 direction=back)
        self.anonymous.append(f)
        return f

    def on_hit_enemy(self, enemy_key: int, damage: float) -> None:
        self.damage_dealt[enemy_key] = self.damage_dealt.get(enemy_key, 0.0) + damage

    def radar(self, now: float, sightings) -> int:
        """A glance at the HUD radar: (time, enemy key, position) of the team's
        sightings of the last 3 s (exactly what the human's radar shows)."""
        n = 0
        for t, key, pos in sightings:
            if now - t <= 3.0:
                n += self.add(Fact(key, (pos[0], pos[1], pos[2]), PRECISION["radar"], t, "radar"))
        return n

    def forget(self, enemy_key: int) -> None:
        self.facts.pop(enemy_key, None)

    # ------------------------------------------------------------- queries
    def latest(self, max_age: float, now: float, sources=None) -> Fact | None:
        best = None
        for f in list(self.facts.values()) + self.anonymous:
            if now - f.time <= max_age and (sources is None or f.source in sources):
                if best is None or f.time > best.time:
                    best = f
        return best

    def recent(self, max_age: float, now: float, sources=None) -> list[Fact]:
        return [f for f in list(self.facts.values()) + self.anonymous
                if now - f.time <= max_age and (sources is None or f.source in sources)]

    def nearest(self, max_age: float, now: float, sources=None) -> Fact | None:
        me = self.bot.position()
        best, bd = None, 1e9
        for f in self.recent(max_age, now, sources):
            d = math.hypot(f.pos[0] - me.x, f.pos[1] - me.y)
            if d < bd:
                best, bd = f, d
        return best

    def hurt(self, enemy_key: int, threshold: float = 40.0) -> bool:
        return self.damage_dealt.get(enemy_key, 0.0) >= threshold


def fact_point(f: Fact, height: float = 1.45) -> Point3:
    return Point3(f.pos[0], f.pos[1], f.pos[2] + height)
