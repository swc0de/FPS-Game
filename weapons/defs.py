"""Weapon/grenade definitions loaded from data/weapons.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from engine import paths

SLOTS = ("primary", "secondary", "melee", "grenade", "objective")


@dataclass
class Inaccuracy:
    stand: float
    crouch: float
    move: float
    air: float
    fire_add: float
    fire_recovery: float
    ads: float = 1.0
    scoped: dict | None = None


@dataclass
class Recoil:
    pattern: list
    view_follow: float = 0.5
    recovery: float = 12.0
    punch: float = 0.5


@dataclass
class WeaponDef:
    key: str
    name: str
    cls: str
    slot: str
    model: str
    price: int
    fire_mode: str          # auto | semi | bolt | pump | melee
    rpm: float
    magazine: int
    reserve: int
    damage: float
    inaccuracy: Inaccuracy
    recoil: Recoil
    pellets: int = 1
    head_multiplier: float = 4.0
    armor_penetration: float = 0.5
    range_modifier: float = 0.9
    max_range: float = 200.0
    penetration: float = 0.0
    reload_time: float = 2.5
    reload_empty_time: float = 2.5
    draw_time: float = 0.6
    speed: float = 1.0
    pellet_spread: float = 0.0
    ads: dict | None = None
    scope: dict | None = None
    tracer_every: int = 0
    shell: str = "rifle"
    sound: str = "rifle_heavy"
    muzzle_flash: float = 1.0
    kill_reward: int = 300
    teams: list = field(default_factory=lambda: ["attack", "defend"])
    buy_category: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def fire_interval(self) -> float:
        return 60.0 / self.rpm

    @property
    def is_gun(self) -> bool:
        return self.fire_mode in ("auto", "semi", "bolt", "pump")


@dataclass
class GrenadeDef:
    key: str
    name: str
    model: str
    price: int
    max_carry: int
    fuse: float
    throw_speed: float
    lob_speed: float
    restitution: float
    friction: float
    speed: float
    raw: dict

    def get(self, k, default=None):
        return self.raw.get(k, default)


class WeaponDatabase:
    def __init__(self, data: dict | None = None):
        if data is None:
            with open(paths.DATA_DIR / "weapons.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        self.raw = data
        self.weapons: dict[str, WeaponDef] = {}
        self.grenades: dict[str, GrenadeDef] = {}
        self.hitgroups = data.get("hitgroups", {})
        self.armor = data.get("armor", {"absorb_ratio": 0.5, "max": 100})
        for key, w in data.get("weapons", {}).items():
            self.weapons[key] = self._weapon(key, w)
        for key, g in data.get("grenades", {}).items():
            self.grenades[key] = GrenadeDef(
                key=key, name=g["name"], model=g.get("model", key), price=int(g.get("price", 0)),
                max_carry=int(g.get("max_carry", 1)), fuse=float(g.get("fuse", 1.6)),
                throw_speed=float(g.get("throw_speed", 16)), lob_speed=float(g.get("lob_speed", 9)),
                restitution=float(g.get("restitution", 0.4)), friction=float(g.get("friction", 0.6)),
                speed=float(g.get("speed", 1.0)), raw=g)

    @staticmethod
    def _weapon(key: str, w: dict) -> WeaponDef:
        inacc = Inaccuracy(**w["inaccuracy"])
        rec = Recoil(**w["recoil"])
        if not rec.pattern or rec.pattern[0] != [0, 0]:
            rec.pattern = [[0, 0]] + list(rec.pattern)
        known = {f for f in WeaponDef.__dataclass_fields__}
        kwargs = {k: v for k, v in w.items() if k in known and k not in ("inaccuracy", "recoil")}
        kwargs.pop("name", None)
        return WeaponDef(key=key, name=w["name"], cls=w["class"], inaccuracy=inacc, recoil=rec, raw=w, **kwargs)

    def hitgroup_multiplier(self, hitgroup: str, wdef: WeaponDef) -> float:
        m = self.hitgroups.get(hitgroup, 1.0)
        if m == "weapon":
            return wdef.head_multiplier
        return float(m)


_DB: WeaponDatabase | None = None


def database() -> WeaponDatabase:
    global _DB
    if _DB is None:
        _DB = WeaponDatabase()
    return _DB
