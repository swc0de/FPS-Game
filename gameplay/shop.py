"""Buying: the catalogue (from weapons.json + match.json) and the purchase
rules (buy zone, buy time, team restrictions, carry limits, refunds).

The catalogue is plain data so the buy menu, bots (ai/buy.py) and tests
all share it. Every agent buys into its own inventory (``agent.weapons``).
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = (("pistols", "Pistols"), ("smgs", "SMGs"), ("rifles", "Rifles"), ("heavy", "Heavy"),
              ("grenades", "Grenades"), ("gear", "Gear"))


@dataclass
class ShopItem:
    key: str
    kind: str             # weapon | grenade | equipment
    name: str
    price: int
    category: str
    teams: tuple
    info: str = ""


def catalogue(db, rules: dict, side: str) -> dict[str, list[ShopItem]]:
    cats: dict[str, list[ShopItem]] = {c: [] for c, _ in CATEGORIES}
    for key, w in db.weapons.items():
        if not w.buy_category or side not in w.teams:
            continue
        info = (f"{w.damage:.0f} dmg  |  {w.rpm:.0f} rpm  |  {w.magazine} rds  |  "
                f"armour pen {w.armor_penetration * 100:.0f}%  |  kill ${w.kill_reward}")
        cats[w.buy_category].append(ShopItem(key, "weapon", w.name, w.price, w.buy_category, tuple(w.teams), info))
    for key, g in db.grenades.items():
        info = f"carry {g.max_carry}  |  kill ${g.get('kill_reward', 300)}"
        cats["grenades"].append(ShopItem(key, "grenade", g.name, g.price, "grenades", ("attack", "defend"), info))
    for key, e in rules.get("equipment", {}).items():
        teams = tuple(e.get("teams", ("attack", "defend")))
        if side not in teams:
            continue
        cats["gear"].append(ShopItem(key, "equipment", e["name"], int(e["price"]), "gear", teams, e.get("info", "")))
    for items in cats.values():
        items.sort(key=lambda it: it.price)
    return cats


def find_item(db, rules: dict, side: str, key: str) -> ShopItem | None:
    for items in catalogue(db, rules, side).values():
        for it in items:
            if it.key == key:
                return it
    return None


class Shop:
    """Purchases for the human and the bots (refunds: human only)."""

    def __init__(self, director):
        self.director = director
        self.game = director.game
        self.rules = director.rules
        self.bought: list[tuple[str, int, object]] = []      # (key, price, the object bought) this round

    def new_round(self) -> None:
        self.bought = []

    # ------------------------------------------------------------ rules
    def price(self, agent, item: ShopItem) -> int:
        if item.key == "kevlar_helmet":
            dmg = agent.damageable
            if dmg.armor >= 100 and not dmg.helmet:
                return int(self.rules["equipment"]["kevlar_helmet"].get("helmet_upgrade_price", 350))
        return item.price

    def check(self, agent, item: ShopItem) -> tuple[bool, str]:
        d = self.director
        if not agent.alive:
            return False, "dead"
        if not d.match.can_buy():
            return False, "buy time is over"
        if not d.in_buy_zone(agent):
            return False, "not in the buy zone"
        if agent.side not in item.teams:
            return False, "not available to your team"
        inv = _weapons(self.game, agent).inv
        dmg = agent.damageable
        if item.kind == "weapon":
            ws = inv.weapons.get(self.game.weapon_db.weapons[item.key].slot)
            if ws is not None and ws.d.key == item.key:
                return False, "already carried"
        elif item.kind == "grenade":
            g = self.game.weapon_db.grenades[item.key]
            if inv.grenades.get(item.key, 0) >= g.max_carry:
                return False, "carrying the maximum"
            if inv.grenade_count() >= int(self.rules.get("grenade_limit", 4)):
                return False, "grenade limit reached"
        elif item.key == "kevlar" and dmg.armor >= 100:
            return False, "already wearing armour"
        elif item.key == "kevlar_helmet" and dmg.armor >= 100 and dmg.helmet:
            return False, "already wearing armour and helmet"
        elif item.key == "defuse_kit" and getattr(agent, "has_kit", False):
            return False, "already carried"
        if agent.money < self.price(agent, item):
            return False, "not enough money"
        return True, ""

    # -------------------------------------------------------------- buy
    def buy(self, agent, item: ShopItem) -> tuple[bool, str]:
        ok, why = self.check(agent, item)
        if not ok:
            return False, why
        price = self.price(agent, item)
        agent.money -= price
        weapons = _weapons(self.game, agent)
        inv = weapons.inv
        bought = None
        if item.kind == "weapon":
            wdef = self.game.weapon_db.weapons[item.key]
            old = inv.weapons.get(wdef.slot)
            if old is not None:
                weapons.drop_weapon_state(old)
            bought = inv.make(item.key)
            inv.weapons[wdef.slot] = bought
            weapons.select(wdef.slot, force=True)
        elif item.kind == "grenade":
            inv.give_grenade(item.key, 1)
            bought = item.key
            if inv.slot == "melee" and not inv.has("primary") and not inv.has("secondary"):
                weapons.select("grenade", force=True)
        elif item.key in ("kevlar", "kevlar_helmet"):
            agent.damageable.armor = 100.0
            if item.key == "kevlar_helmet":
                agent.damageable.helmet = True
        elif item.key == "defuse_kit":
            agent.has_kit = True
        if agent.is_human:
            self.bought.append((item.key, price, bought))
            self.game.audio.play_ui("buy", 0.6)
        return True, f"bought {item.name} (${price})"

    def can_refund(self, agent, item: ShopItem) -> bool:
        if not self.director.match.can_buy() or not self.director.in_buy_zone(agent):
            return False
        inv = self.game.weapons.inv
        for key, _, obj in reversed(self.bought):
            if key != item.key:
                continue
            if item.kind == "weapon":
                return obj in inv.weapons.values()
            if item.kind == "grenade":
                return inv.grenades.get(key, 0) > 0
        return False

    def refund(self, agent, item: ShopItem) -> bool:
        if not self.can_refund(agent, item):
            return False
        inv = self.game.weapons.inv
        for i in range(len(self.bought) - 1, -1, -1):
            key, price, obj = self.bought[i]
            if key != item.key:
                continue
            if item.kind == "weapon":
                slot = self.game.weapon_db.weapons[key].slot
                inv.weapons[slot] = None
                self.game.weapons.select(inv.best_slot(), force=True)
            elif item.kind == "grenade":
                inv.grenades[key] -= 1
                if inv.grenades[key] <= 0:
                    del inv.grenades[key]
                if inv.slot == "grenade" and not inv.has("grenade"):
                    self.game.weapons.select(inv.best_slot(), force=True)
            agent.money += price
            del self.bought[i]
            return True
        return False


def _weapons(game, agent):
    """The inventory owner of an agent (bots carry their own; the human uses the player's)."""
    return getattr(agent, "weapons", None) or game.weapons
