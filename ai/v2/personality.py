"""Traits and roles: every v2 bot plays with a recognisable style.

Traits are drawn once per bot from a ``Random`` seeded with its name and the
match seed (``crc32``, not Python's salted ``hash``), so "Anvil" plays the
same way every round and every match with the same seed:

    aggression  peeks and pushes rather than holds; takes entry
    patience    holds longer, lurks, waits for teammates and utility
    teamwork    keeps spacing for trades, trades, follows calls, calls more
    utility     throws more and better-timed grenades
    risk        takes duels at a disadvantage, re-peeks the same angle

Values are Beta(2, 2) draws (most bots near the middle, a few extreme).

Roles are assigned per round by the team strategy from traits and loadout:
attackers get entry, trade (the entry's second), support (utility), lurker
and AWPer; defenders get anchor, rotator, support and AWPer.
"""
from __future__ import annotations

import random
import zlib

TRAITS = ("aggression", "patience", "teamwork", "utility", "risk")
ATTACK_ROLES = ("entry", "trade", "support", "lurker", "awper")
DEFEND_ROLES = ("anchor", "rotator", "support", "awper")


def traits_for(name: str, seed: int | None) -> dict[str, float]:
    rng = random.Random(zlib.crc32(f"{name}:{seed if seed is not None else 0}".encode()))
    return {t: round(rng.betavariate(2.0, 2.0), 3) for t in TRAITS}


def _has_sniper(bot) -> bool:
    w = getattr(bot.weapons, "primary", None)
    return w is not None and w.d.cls == "sniper"


def assign_roles(bots: list, side: str, rng, lurker: bool = True, carrier=None) -> dict[int, str]:
    """Role per bot id for this round."""
    roles: dict[int, str] = {}
    left = list(bots)
    for b in left:
        if _has_sniper(b):
            roles[id(b)] = "awper"
    left = [b for b in left if id(b) not in roles]
    if side == "attack":
        if left:
            entry = max(left, key=lambda b: (b.brain.traits["aggression"], b is not carrier, rng.random()))
            roles[id(entry)] = "entry"
            left.remove(entry)
        if left:
            sup = max(left, key=lambda b: (b.brain.traits["utility"] + 0.15 * sum(b.weapons.inv.grenades.values()),
                                           rng.random()))
            roles[id(sup)] = "support"
            left.remove(sup)
        if lurker and len(left) >= 2:
            lurk = max(left, key=lambda b: (b.brain.traits["patience"] - 0.5 * b.brain.traits["teamwork"]
                                            - (0.6 if b is carrier else 0.0), rng.random()))
            roles[id(lurk)] = "lurker"
            left.remove(lurk)
        for b in left:
            roles[id(b)] = "trade"
    else:
        ordered = sorted(left, key=lambda b: (b.brain.traits["patience"] - b.brain.traits["aggression"], rng.random()),
                         reverse=True)
        for k, b in enumerate(ordered):
            if k < 2:
                roles[id(b)] = "anchor"
            elif b.brain.traits["utility"] > 0.6 and "support" not in roles.values():
                roles[id(b)] = "support"
            else:
                roles[id(b)] = "rotator"
    return roles
