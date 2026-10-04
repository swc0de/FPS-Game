"""Bot purchases: a team-wide buy decision, then each bot spends its money.

The team decides like a CS team would:

* pistol round (first round of each half): armour or utility;
* full buy when the team can afford rifles and armour on average;
* eco (save) when it cannot, so the loss bonus builds up;
* force buy in between, or when losing the round would lose the match.

A rich bot always buys even during a team eco. One bot per team is the
designated sniper when it can afford the SR-90 with armour.
"""
from __future__ import annotations

from gameplay.shop import find_item

PRIMARY_BY_SIDE = {"attack": "r7", "defend": "c9"}


def team_buy_mode(members, match, side: str, eco_cfg: dict) -> str:
    rounds = match.rules["rounds"]
    first_of_half = match.round == 1 or match.round == int(rounds["halftime_after"]) + 1
    if first_of_half:
        return "pistol"
    if not members:
        return "eco"
    avg = sum(m.money for m in members) / len(members)
    win = int(rounds["win_score"])
    enemy = next((t for t in match.teams if t.side != side), None)
    if enemy is not None and enemy.score == win - 1:
        return "full" if avg >= float(eco_cfg.get("full_buy", 3800)) else "force"
    if avg >= float(eco_cfg.get("full_buy", 3800)):
        return "full"
    if avg >= float(eco_cfg.get("save_below", 2400)):
        return "force"
    return "eco"


def bot_buy(bot, shop, db, rules: dict, mode: str, eco_cfg: dict, awper: bool, kit_buyer: bool, rng) -> list[str]:
    side = bot.side
    bought: list[str] = []

    def buy(key: str) -> bool:
        it = find_item(db, rules, side, key)
        if it is None:
            return False
        ok, _ = shop.buy(bot, it)
        if ok:
            bought.append(key)
        return ok

    def afford(key: str, keep: int = 0) -> bool:
        it = find_item(db, rules, side, key)
        return it is not None and bot.money - shop.price(bot, it) >= keep

    has_primary = bot.weapons.primary is not None
    if mode == "pistol":
        if rng.random() < 0.65:
            buy("kevlar")
        else:
            buy("flash")
            buy("smoke")
        if side == "defend" and kit_buyer and bot.money >= 400:
            buy("defuse_kit")
        return bought
    if mode == "eco" and bot.money < 4500:
        if bot.money > 1200 and rng.random() < 0.5:
            buy("flash")
        return bought
    if mode == "force" and bot.money < 4500:
        if not has_primary:
            for key in (PRIMARY_BY_SIDE[side], "mx5", "s12"):
                if afford(key, 650 if key != PRIMARY_BY_SIDE[side] else 0):
                    buy(key)
                    break
        if afford("kevlar"):
            buy("kevlar")
        if afford("flash", 200):
            buy("flash")
        return bought
    # full buy
    if not has_primary:
        if awper and afford("sr90", 1000):
            buy("sr90")
        elif afford(PRIMARY_BY_SIDE[side]):
            buy(PRIMARY_BY_SIDE[side])
        elif afford("mx5"):
            buy("mx5")
    if afford("kevlar_helmet"):
        buy("kevlar_helmet")
    elif afford("kevlar"):
        buy("kevlar")
    if side == "defend" and kit_buyer and afford("defuse_kit"):
        buy("defuse_kit")
    for key, chance in (("smoke", 0.8), ("flash", 0.9), ("frag", 0.6), ("flash", 0.4)):
        if rng.random() < chance and afford(key, 100):
            buy(key)
    return bought
