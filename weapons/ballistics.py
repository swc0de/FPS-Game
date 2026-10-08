"""Hitscan ballistics with material-based wall penetration.

A bullet is a ray from the shooter's eye. To know how thick every object on
the path is, we cast the ray *twice*: forwards (entry points of every shape)
and backwards from the far end (exit points). Each convex shape therefore
yields a solid interval [t_in, t_out] along the ray; overlapping intervals
(e.g. a wall corner made of two boxes) are merged.

Walking the intervals in order:
  * a character hitbox -> apply damage (range falloff, hit-group multiplier,
    armour) and stop,
  * a world solid -> leave an entry impact; the crossing costs
    ``thickness_cm * surface.pen_cost`` from the weapon's penetration budget.
    If the budget is exhausted the bullet stops, otherwise it loses damage
    proportionally, leaves an exit impact and continues.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from panda3d.core import Point3, Vec3

from engine import paths
from engine.physics import EXACT_RAY, MASK_BULLETS, GROUP_HITBOX, PhysicsWorld, surface_of
from gameplay.damage import DamageInfo, DamageResult, falloff

MAX_CROSSINGS = 4


def load_surfaces() -> dict:
    with open(paths.DATA_DIR / "surfaces.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


@dataclass
class Impact:
    pos: Point3
    normal: Vec3
    surface: str
    exit: bool = False                 # True for the exit hole of a penetrated wall
    character: bool = False
    hitgroup: str = ""
    distance: float = 0.0


@dataclass
class ShotResult:
    impacts: list[Impact] = field(default_factory=list)
    damage: list[DamageResult] = field(default_factory=list)
    end: Point3 = field(default_factory=Point3)     # where the bullet stopped
    penetrations: int = 0


@dataclass
class _Interval:
    t_in: float
    t_out: float
    node: object
    in_pos: Point3
    in_normal: Vec3
    out_pos: Point3 | None
    out_normal: Vec3 | None
    hitbox: bool


class Ballistics:
    def __init__(self, physics: PhysicsWorld, weapon_db, surfaces: dict | None = None):
        self.physics = physics
        self.db = weapon_db
        self.surfaces = surfaces or load_surfaces()
        self.destruction = None          # gameplay.destruction.DestructionManager (soft walls)

    def surface(self, name: str) -> dict:
        return self.surfaces.get(name) or self.surfaces["default"]

    def _intervals(self, origin: Point3, end: Point3, ignore_owner) -> list[_Interval]:
        world = self.physics.world
        fw = world.rayTestAll(origin, end, MASK_BULLETS)
        bw = world.rayTestAll(end, origin, MASK_BULLETS)
        entries: dict[int, tuple] = {}
        for h in fw.getHits():
            node = h.getNode()
            key = node.this
            f = h.getHitFraction()
            if key not in entries or f < entries[key][0]:
                entries[key] = (f, node, Point3(h.getHitPos()), Vec3(h.getHitNormal()))
        exits: dict[int, tuple] = {}
        for h in bw.getHits():
            node = h.getNode()
            key = node.this
            f = 1.0 - h.getHitFraction()
            if key not in exits or f > exits[key][0]:
                exits[key] = (f, Point3(h.getHitPos()), Vec3(h.getHitNormal()))
        out = []
        for key, (t_in, node, pos, nrm) in entries.items():
            hitbox = bool((node.getIntoCollideMask() & GROUP_HITBOX).getWord())
            if hitbox and ignore_owner is not None and node.getPythonTag("owner") is ignore_owner:
                continue
            if node.hasPythonTag(EXACT_RAY):
                # a hit capsule: Bullet's entry and exit are approximate (engine/physics.py)
                r = node.getPythonTag(EXACT_RAY)(origin, end)
                if r is None:
                    continue
                t_in, t_out, pos, nrm, out_pos, out_nrm = r
                if out_pos is None:
                    out.append(_Interval(t_in, 1.0, node, pos, nrm, None, None, hitbox))
                else:
                    out.append(_Interval(t_in, t_out, node, pos, nrm, out_pos, out_nrm, hitbox))
                continue
            ex = exits.get(key)
            if ex is None:
                out.append(_Interval(t_in, 1.0, node, pos, nrm, None, None, hitbox))
            else:
                out.append(_Interval(t_in, max(ex[0], t_in), node, pos, nrm, ex[1], ex[2], hitbox))
        out.sort(key=lambda iv: iv.t_in)
        return out

    def fire(self, origin, direction, wdef, attacker=None, damage_scale: float = 1.0) -> ShotResult:
        origin = Point3(*origin)
        d = Vec3(*direction)
        d.normalize()
        length = float(wdef.max_range)
        end = origin + d * length
        result = ShotResult(end=Point3(end))
        intervals = self._intervals(origin, end, attacker)
        power = float(wdef.penetration)
        damage = float(wdef.damage) * damage_scale
        crossed = 0
        cursor = 0.0
        i = 0
        n = len(intervals)
        while i < n:
            iv = intervals[i]
            if iv.t_out <= cursor + 1e-6:
                i += 1
                continue
            dist = max(iv.t_in, cursor) * length
            if iv.hitbox:
                owner = iv.node.getPythonTag("owner")
                hitgroup = iv.node.getTag("hitgroup") or "chest"
                dmg = falloff(damage, wdef.range_modifier, dist) * self.db.hitgroup_multiplier(hitgroup, wdef)
                surface = iv.node.getTag("surface") or "flesh"
                result.impacts.append(Impact(iv.in_pos, iv.in_normal, surface, character=True,
                                             hitgroup=hitgroup, distance=dist))
                if owner is not None and hasattr(owner, "damageable"):
                    info = DamageInfo(dmg, wdef.armor_penetration, hitgroup, "bullet", attacker, wdef.key,
                                      tuple(iv.in_pos), tuple(d), crossed, hitgroup == "head")
                    res = owner.damageable.take_damage(info)
                    if res is not None:
                        result.damage.append(res)
                        if hasattr(owner, "on_hit"):
                            owner.on_hit(res, iv.in_pos, d)
                result.end = Point3(iv.in_pos)
                result.penetrations = crossed
                return result

            # world solid: merge everything overlapping into one segment
            entry_new = iv.t_in >= cursor - 1e-6
            surf = self.surface(surface_of(iv.node))
            if entry_new:
                result.impacts.append(Impact(iv.in_pos, iv.in_normal, surface_of(iv.node), distance=dist))
                if self.destruction is not None and iv.node.getPythonTag("panel") is not None:
                    self.destruction.bullet_hit(iv.node, iv.in_pos, d, damage, int(getattr(wdef, "pellets", 1)))
            seg_start = max(iv.t_in, cursor)
            seg_end = iv.t_out
            cost = (seg_end - seg_start) * length * 100.0 * surf["pen_cost"]
            keep = surf.get("damage_keep", 0.85)
            exit_iv = iv
            j = i + 1
            while j < n and intervals[j].t_in <= seg_end + 1e-5 and not intervals[j].hitbox:
                nxt = intervals[j]
                if nxt.t_out > seg_end:
                    s2 = self.surface(surface_of(nxt.node))
                    cost += (nxt.t_out - seg_end) * length * 100.0 * s2["pen_cost"]
                    keep = min(keep, s2.get("damage_keep", 0.85))
                    seg_end = nxt.t_out
                    exit_iv = nxt
                j += 1
            if seg_end >= 1.0 - 1e-6 or exit_iv.out_pos is None or cost >= power or crossed >= MAX_CROSSINGS:
                result.end = Point3(iv.in_pos) if entry_new else origin + d * (seg_start * length)
                result.penetrations = crossed
                return result
            damage *= keep * (1.0 - 0.6 * cost / max(power, 1e-6))
            power -= cost
            crossed += 1
            result.impacts.append(Impact(exit_iv.out_pos, exit_iv.out_normal, surface_of(exit_iv.node), exit=True,
                                         distance=seg_end * length))
            cursor = seg_end
            i = j
            if damage < 1.0:
                result.end = Point3(exit_iv.out_pos)
                result.penetrations = crossed
                return result
        result.penetrations = crossed
        return result
