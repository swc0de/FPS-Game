"""Loadout slots (CS-style): primary, secondary, melee, grenades."""
from __future__ import annotations

import random

from weapons.defs import WeaponDatabase
from weapons.weapon import WeaponState

SLOT_ORDER = ("primary", "secondary", "melee", "grenade", "bomb")
GRENADE_ORDER = ("frag", "flash", "smoke")


class Inventory:
    def __init__(self, db: WeaponDatabase, infinite_reserve: bool = False, rng: random.Random | None = None):
        self.db = db
        self.infinite_reserve = infinite_reserve
        self.rng = rng or random.Random()
        self.weapons: dict[str, WeaponState | None] = {"primary": None, "secondary": None, "melee": None}
        self.grenades: dict[str, int] = {}
        self.has_bomb = False               # attackers: the breach charge (slot 5)
        self.slot = "melee"
        self.grenade = None
        self.last_slot = None

    # ------------------------------------------------------------- give
    def make(self, key: str) -> WeaponState:
        return WeaponState(self.db.weapons[key], self.rng, self.infinite_reserve)

    def give_weapon(self, item) -> WeaponState | None:
        """Add a weapon (key or WeaponState). Returns the weapon it replaced."""
        ws = self.make(item) if isinstance(item, str) else item
        slot = ws.d.slot
        old = self.weapons.get(slot)
        self.weapons[slot] = ws
        return old

    def give_grenade(self, key: str, count: int = 1) -> bool:
        g = self.db.grenades[key]
        have = self.grenades.get(key, 0)
        if have >= g.max_carry:
            return False
        self.grenades[key] = min(have + count, g.max_carry)
        return True

    def refill_ammo(self) -> None:
        for ws in self.weapons.values():
            if ws is not None:
                ws.reserve = ws.d.reserve

    def refill_grenades(self) -> None:
        for key, g in self.db.grenades.items():
            self.grenades[key] = g.max_carry

    # ----------------------------------------------------------- select
    def current(self) -> WeaponState | None:
        if self.slot in ("grenade", "bomb"):
            return None
        return self.weapons.get(self.slot)

    def has(self, slot: str) -> bool:
        if slot == "grenade":
            return any(c > 0 for c in self.grenades.values())
        if slot == "bomb":
            return self.has_bomb
        return self.weapons.get(slot) is not None

    def select(self, slot: str) -> bool:
        """Switch slot. Pressing the grenade slot again cycles grenade types."""
        if not self.has(slot):
            return False
        if slot == "grenade":
            available = [g for g in GRENADE_ORDER if self.grenades.get(g, 0) > 0]
            if self.slot == "grenade" and self.grenade in available and len(available) > 1:
                self.grenade = available[(available.index(self.grenade) + 1) % len(available)]
                return True
            if self.grenade not in available:
                self.grenade = available[0]
        if slot == self.slot and slot != "grenade":
            return False
        self.last_slot = self.slot
        self.slot = slot
        return True

    def cycle(self, direction: int) -> bool:
        owned = [s for s in SLOT_ORDER if self.has(s)]
        if not owned:
            return False
        i = owned.index(self.slot) if self.slot in owned else 0
        return self.select(owned[(i + direction) % len(owned)])

    def best_slot(self) -> str:
        for s in SLOT_ORDER:
            if self.has(s):
                return s
        return "melee"

    def clear(self) -> None:
        """Strip everything (death / side switch)."""
        self.weapons = {"primary": None, "secondary": None, "melee": None}
        self.grenades = {}
        self.has_bomb = False
        self.grenade = None
        self.slot = "melee"
        self.last_slot = None

    def grenade_count(self) -> int:
        return sum(self.grenades.values())

    def drop_current(self) -> WeaponState | None:
        if self.slot in ("melee", "grenade", "bomb"):
            return None
        ws = self.weapons.get(self.slot)
        self.weapons[self.slot] = None
        self.slot = self.best_slot()
        return ws

    def use_grenade(self) -> str | None:
        key = self.grenade
        if key is None or self.grenades.get(key, 0) <= 0:
            return None
        self.grenades[key] -= 1
        if self.grenades[key] <= 0:
            del self.grenades[key]
        return key

    def speed_scale(self) -> float:
        ws = self.current()
        if ws is not None:
            return ws.d.speed
        return 1.0
