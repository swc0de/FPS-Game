"""Health, armour and damage resolution (CS-style armour model).

Body armour protects chest/stomach/arms, the helmet protects the head.
For a protected hit:

    health_damage = damage * armor_penetration
    armor_damage  = (damage - health_damage) * absorb_ratio
    if armour runs out, the unabsorbed remainder goes to health.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

ARMORED_GROUPS = ("chest", "stomach", "arm")


@dataclass
class DamageInfo:
    amount: float                    # raw damage after range falloff & hit group multiplier
    armor_penetration: float = 1.0
    hitgroup: str = "chest"
    kind: str = "bullet"             # bullet | melee | explosion | fall
    attacker: object = None
    weapon: str = ""
    pos: tuple = (0, 0, 0)
    direction: tuple = (0, 0, 0)
    penetrated: int = 0              # walls crossed before the hit
    headshot: bool = False


@dataclass
class DamageResult:
    health: float
    armor: float
    killed: bool
    hitgroup: str
    info: DamageInfo


@dataclass
class Damageable:
    health: float = 100.0
    max_health: float = 100.0
    armor: float = 0.0
    helmet: bool = False
    team: str = ""
    name: str = ""
    alive: bool = True
    absorb_ratio: float = 0.5
    on_damage: list[Callable[[DamageResult], None]] = field(default_factory=list)

    def protected(self, hitgroup: str, kind: str) -> bool:
        if self.armor <= 0:
            return False
        if kind == "explosion":
            return True
        if hitgroup == "head":
            return self.helmet
        return hitgroup in ARMORED_GROUPS

    def take_damage(self, info: DamageInfo) -> DamageResult | None:
        if not self.alive:
            return None
        dmg = max(info.amount, 0.0)
        health_dmg = dmg
        armor_dmg = 0.0
        if self.protected(info.hitgroup, info.kind):
            health_dmg = dmg * info.armor_penetration
            armor_dmg = (dmg - health_dmg) * self.absorb_ratio
            if armor_dmg > self.armor:
                # armour broke: the part it could not absorb hits health
                overflow = (armor_dmg - self.armor) / max(self.absorb_ratio, 1e-6)
                health_dmg += overflow
                armor_dmg = self.armor
            self.armor -= armor_dmg
            if self.armor <= 0:
                self.armor = 0.0
                self.helmet = False
        health_dmg = min(health_dmg, self.health)
        self.health -= health_dmg
        killed = self.health <= 0.0
        if killed:
            self.health = 0.0
            self.alive = False
        result = DamageResult(health_dmg, armor_dmg, killed, info.hitgroup, info)
        for cb in list(self.on_damage):
            cb(result)
        return result

    def reset(self, health: float | None = None, armor: float | None = None, helmet: bool | None = None) -> None:
        self.health = self.max_health if health is None else health
        if armor is not None:
            self.armor = armor
        if helmet is not None:
            self.helmet = helmet
        self.alive = True


def falloff(damage: float, range_modifier: float, distance: float) -> float:
    """CS-style exponential range falloff: multiplier per 10 m travelled."""
    return damage * (range_modifier ** (distance / 10.0))
