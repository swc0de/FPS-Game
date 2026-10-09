"""AI bot agent: body, movement, weapons - driven by ai/brain.py.

A bot is a match ``Participant`` like the player. It uses the same
character controller (gameplay/character.py), the same inventory and
gunplay model (weapons/weapon.py: recoil patterns, movement inaccuracy,
fire rates, reloads) and the same ballistics, so it obeys every rule the
player does - it has to stop to shoot accurately, it runs out of ammo and
it can be wall-banged. Only the decisions come from the brain.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field

from panda3d.core import LVecBase4f, Point3, Vec3

from ai.aim import AimController
from ai.perception import Perception
from engine import paths
from engine.physics import MASK_BULLETS, surface_of
from gameplay.body import CharacterBody
from gameplay.character import KinematicCharacter, MoveInput
from gameplay.damage import Damageable, DamageInfo, DamageResult
from gameplay.lean import Lean, clearance, right_of
from gameplay.match import Participant
from weapons.grenades import Grenade
from weapons.inventory import Inventory
from weapons.weapon import AimContext, angles_to_dir, apply_offset

_CONFIG = None


def load_bot_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        with open(paths.DATA_DIR / "bots.json", "r", encoding="utf-8") as f:
            _CONFIG = json.load(f)
    return _CONFIG


@dataclass
class Intent:
    """What the brain wants this tick."""
    wish: Vec3 = field(default_factory=Vec3)
    walk: bool = False
    crouch: bool = False
    jump: bool = False
    trigger: bool = False
    pressed: bool = False
    lean: float = 0.0               # -1 left .. +1 right (peeking)


def _reload_duration(ws) -> float:
    """How long the reload clip lasts: the weapon's own reload time (shotguns: the shells)."""
    d = ws.d
    if d.fire_mode == "pump":
        shells = max(d.magazine - ws.ammo, 1)
        return float(d.raw.get("reload_start_time", 0.35)) + shells * float(d.raw.get("reload_shell_time", 0.5))
    return d.reload_empty_time if ws.reload_kind == "empty" else d.reload_time


class BotWeapons:
    """Inventory + weapon handling for a bot (the player's PlayerWeapons without input/viewmodel)."""

    def __init__(self, bot):
        self.bot = bot
        self.game = bot.game
        self.db = bot.game.weapon_db
        self.inv = Inventory(self.db, rng=bot.rng)
        self.melee_next = 0.0
        self.grenade_ready = 0.0

    # ------------------------------------------------------------ slots
    def give_loadout(self, weapons, grenades=None) -> None:
        for key in weapons:
            self.inv.give_weapon(key)
        for key, n in (grenades or {}).items():
            self.inv.give_grenade(key, n)
        self.inv.slot = "melee"
        self.select(self.inv.best_slot(), force=True)

    def select(self, slot: str, force: bool = False) -> None:
        changed = self.inv.select(slot)
        if changed or force:
            self._equip()

    def _equip(self) -> None:
        now = self.game.loop.time
        ws = self.inv.current()
        body = self.bot.body
        body.event("switch")
        if ws is not None:
            ws.deploy(now)
            body.set_weapon(ws.d.model, ws.d.cls)
        elif self.inv.slot == "grenade" and self.inv.grenade:
            body.set_weapon(self.db.grenades[self.inv.grenade].model, "grenade")
            self.grenade_ready = now + 0.45
        elif self.inv.slot == "bomb":
            body.set_weapon("bomb_charge", "bomb")
        else:
            body.set_weapon(None)

    def drop_weapon_state(self, ws, throw: float = 3.0) -> None:
        ws.cancel_reload()
        ws.zoom_level = 0
        bot = self.bot
        fwd = Vec3(*angles_to_dir(bot.aim.yaw, 0))
        self.game.pickups.spawn("weapon", ws, bot.eye() - Vec3(0, 0, 0.4) + fwd * 0.4, bot.aim.yaw,
                                vel=fwd * throw + Vec3(0, 0, 1.0) + bot.char.vel)

    @property
    def primary(self):
        return self.inv.weapons.get("primary")

    @property
    def secondary(self):
        return self.inv.weapons.get("secondary")

    # ------------------------------------------------------------- tick
    def update(self, dt: float, now: float, intent: Intent) -> None:
        ws = self.inv.current()
        if ws is None:
            return
        ws.update(now, dt)
        if ws.d.fire_mode == "melee":
            if intent.pressed or intent.trigger:
                self._melee(ws, now)
        elif ws.wants_shot(now, intent.trigger, intent.pressed):
            self._fire(ws, now)
        for ev in ws.pop_events():
            if ev.kind == "reload_start":
                self.bot.body.event("reload", duration=_reload_duration(ws))
            if ev.kind == "reload_start" and ws.d.cls != "shotgun":
                kind = "empty" if ev.data.get("kind") == "empty" else "tactical"
                self.game.audio.play_at(f"reload_{ws.d.key}_{kind}", self.bot.eye(), 0.55)
            elif ev.kind == "shell":
                self.game.audio.play_at("shell_insert", self.bot.eye(), 0.5)

    def aim_context(self, ws) -> AimContext:
        c = self.bot.char
        scoped = bool(ws.d.scope) and self.bot.scoped
        return AimContext(speed=c.horizontal_speed, max_speed=c.cfg["run_speed"] * ws.d.speed,
                          on_ground=c.on_ground, crouched=c.crouched, ads=0.0, scoped=scoped)

    def _fire(self, ws, now: float) -> None:
        bot = self.bot
        game = self.game
        eye = bot.eye()
        offsets = ws.shot_offsets(self.aim_context(ws))
        muzzle = bot.body.muzzle_pos()
        tracer = ws.is_tracer()
        first = None
        for i, (dx, dy) in enumerate(offsets):
            yaw, pitch = apply_offset(bot.aim.yaw, bot.aim.pitch, dx, dy)
            d = Vec3(*angles_to_dir(yaw, pitch))
            first = first or d
            res = game.ballistics.fire(eye, d, ws.d, attacker=bot)
            if res.damage and hasattr(bot.brain, "on_hits"):
                bot.brain.on_hits(res.damage)
            for imp in res.impacts[:3]:
                game.effects.impact(imp.pos, imp.normal, imp.surface, d, imp.exit)
            if (tracer and i == 0) or (ws.d.pellets > 1 and i < 2 and bot.rng.random() < 0.3):
                game.effects.tracer(muzzle, res.end)
        ws.on_fired(now)
        game.effects.muzzle_flash_world(muzzle, first, ws.d.muzzle_flash)
        game.audio.play_shot(ws.d.sound, muzzle, own=False)
        game.notify_noise(eye, 1.0, 70.0, source=bot)
        bot.body.on_fire(ws.d.recoil.punch)
        bot.on_fired(ws)

    def _melee(self, ws, now: float) -> None:
        if now < self.melee_next or now < ws.draw_end:
            return
        self.melee_next = now + 60.0 / ws.d.rpm
        bot = self.bot
        bot.body.event("knife")
        eye = bot.eye()
        d = Vec3(*angles_to_dir(bot.aim.yaw, bot.aim.pitch))
        rng = float(ws.d.raw.get("melee_range", 1.6))
        self.game.audio.play_at("knife_swing", eye, 0.5)
        hit = next((h for h in self.game.physics.ray_cast_all(eye, eye + d * rng, MASK_BULLETS)
                    if h.node.getPythonTag("owner") is not bot), None)
        if hit is None:
            return
        node = hit.node
        pos = Point3(hit.pos)
        owner = node.getPythonTag("owner")
        if owner is not None and owner is not bot and hasattr(owner, "damageable"):
            dmg = ws.d.damage
            fwd = owner.forward() if hasattr(owner, "forward") else None
            if fwd is not None and fwd.dot(d) > 0.5:
                dmg *= float(ws.d.raw.get("backstab_multiplier", 2.8))
            hg = node.getTag("hitgroup") or "chest"
            info = DamageInfo(dmg, ws.d.armor_penetration, hg, "melee", bot, ws.d.key, tuple(pos), tuple(d))
            r = owner.damageable.take_damage(info)
            if r is not None and hasattr(owner, "on_hit"):
                owner.on_hit(r, pos, d)
            self.game.effects.impact(pos, Vec3(hit.normal), node.getTag("surface") or "flesh", d)
            self.game.audio.play_at("knife_hit_body", pos)
        else:
            self.game.effects.impact(pos, Vec3(hit.normal), surface_of(node), d)
            self.game.audio.play_at("knife_hit_wall", pos)
            self.game.destruction.melee_hit(node, pos, d, ws.d.damage)

    def throw(self, key: str, velocity: Vec3) -> bool:
        """Throw a grenade with the given launch velocity (the brain solves the arc)."""
        if self.inv.grenades.get(key, 0) <= 0:
            return False
        self.inv.grenade = key
        if self.inv.use_grenade() is None:
            return False
        bot = self.bot
        g = self.db.grenades[key]
        start = bot.eye() + Vec3(*angles_to_dir(bot.aim.yaw, 0)) * 0.3 - Vec3(0, 0, 0.1)
        self.game.spawn_grenade(Grenade(self.game, g, start, Vec3(velocity), bot))
        self.game.audio.play_at("throw", start, 0.5)
        bot.body.event("throw")
        self.inv.slot = "melee"
        self.select(self.inv.best_slot(), force=True)
        return True


class BotAgent(Participant):
    def __init__(self, game, name: str, side: str, difficulty: str, nav, seed: int | None = None,
                 appearance_seed: int | None = None):
        """``appearance_seed``: the procedural soldier for this name (characters/); None = the
        Milestone 5 mannequin."""
        super().__init__(name)
        self.appearance_seed = appearance_seed
        cfg = load_bot_config()
        self.game = game
        self.nav = nav
        self.rng = random.Random(seed)
        self.difficulty = difficulty
        self.profile = dict(cfg["difficulty"].get(difficulty) or cfg["difficulty"]["normal"])
        self.char = KinematicCharacter(game.physics)
        self.char.depen_interval = 8
        self.char.on_event = self._on_step
        self.damageable = Damageable(name=name, team=side)
        self.body_side = side
        self.body = self._make_body(side)
        self.aim = AimController(self.profile, self.rng)
        self.lean = Lean()
        self.slow = 1.0                 # razor wire
        self.perception = Perception(self, cfg.get("vision", {}), self.profile)
        self.weapons = BotWeapons(self)
        self.has_kit = False
        self.scoped = False
        self.intent = Intent()
        self.now = 0.0
        self.prev_yaw = 0.0
        self.last_fired = -10.0
        self.shots_fired = 0
        self.brain = None                 # set by the director (ai/brain.py)
        self._bomb_clip: set[str] = set()
        self.active = False               # in the world this round

    def _make_body(self, side: str) -> CharacterBody:
        from gameplay.match import load_rules
        uniform = load_rules()["teams"][side].get("uniform", "uniform_tan")
        helmet = "metal_tan" if side == "attack" else "metal_olive"
        character = (self.name, self.appearance_seed, side) if self.appearance_seed is not None else None
        return CharacterBody(self.game, self, uniform, helmet, name=f"bot:{self.name}", character=character)

    def restyle(self) -> None:
        """New uniform after switching sides at halftime."""
        if self.body_side == self.side:
            return
        self.body.destroy()
        self.body_side = self.side
        self.body = self._make_body(self.side)
        self.weapons.select(self.weapons.inv.best_slot(), force=True)

    # ----------------------------------------------------------- interface
    @property
    def alive(self) -> bool:
        return self.damageable.alive

    def position(self) -> Point3:
        return Point3(self.char.pos)

    def eye(self) -> Point3:
        c = self.char
        return Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height) + right_of(self.aim.yaw) * self.lean.offset()

    def head_pos(self) -> Point3:
        return self.body.parts["head"].getPos(self.game.render)

    def center_of_mass(self) -> Point3:
        c = self.char
        return Point3(c.pos.x, c.pos.y, c.pos.z + c.height * 0.62)

    def forward(self) -> Vec3:
        return Vec3(*angles_to_dir(self.aim.yaw, 0))

    def view_dir(self) -> Vec3:
        return Vec3(*angles_to_dir(self.aim.yaw, self.aim.pitch))

    def velocity(self) -> Vec3:
        return Vec3(self.char.vel)

    # ------------------------------------------------------------- round
    def spawn(self, pos, yaw: float) -> None:
        self.char.teleport(pos)
        self.char.vel = Vec3(0, 0, 0)
        self.char.prev_pos = Point3(self.char.pos)
        self.aim.yaw = self.prev_yaw = yaw
        self.aim.pitch = 0.0
        self.aim.release()
        self.lean.reset()
        self.body.reset()
        self.body.animate(0.0, self.char.pos, yaw, 0.0, 0.0, Vec3(0, 0, 0), True)
        self.perception.reset()
        self.intent = Intent()
        self.scoped = False
        self._bomb_clip = set()
        self.active = True
        if self.brain is not None:
            self.brain.reset()

    def remove(self) -> None:
        self.active = False
        self.body.hide()

    def destroy(self) -> None:
        self.body.destroy()

    # --------------------------------------------------------------- tick
    def fixed_update(self, dt: float, now: float, locked: bool) -> None:
        self.now = now
        if not self.active:
            return
        self.prev_yaw = self.aim.yaw
        if not self.alive:
            self.body.animate(dt, self.char.pos, self.aim.yaw, self.aim.pitch, 0.0, Vec3(0, 0, 0), True)
            return
        if self.brain is not None:
            self.brain.update(dt, now, locked)
            self._bomb_clips()
        it = self.intent
        if locked:
            it.wish = Vec3(0, 0, 0)
            it.jump = False
            it.trigger = it.pressed = False
        self.weapons.update(dt, now, it)
        ws = self.weapons.inv.current()
        scale = ws.d.speed if ws is not None else 1.0
        if self.scoped and ws is not None and ws.d.scope:
            scale *= float(ws.d.scope.get("speed", 0.6)) / max(ws.d.speed, 1e-3)
        self.char.step(dt, MoveInput(wish_dir=it.wish, walk=it.walk, crouch=it.crouch, jump=it.jump,
                                     speed_scale=scale * self.slow))
        it.pressed = False
        c = self.char
        target = 0.0 if locked else it.lean
        if target != 0.0 or self.lean.amount != 0.0:
            free = clearance(self.game.physics, Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height), self.aim.yaw)
        else:
            free = (1.0, 1.0)
        self.lean.update(dt, target, *free)
        crouch = (c.stand_height - c.height) / max(c.stand_height - c.crouch_height, 1e-3)
        self.body.animate(dt, c.pos, self.aim.yaw, self.aim.pitch, crouch, c.vel, c.on_ground, it.walk,
                          lean=self.lean.amount)
        self._auto_pickup()

    def _bomb_clips(self) -> None:
        """Planting and defusing poses while the brain's own plant or defuse timer runs (both
        brains keep one); visual and hit boxes only, the timers decide nothing here."""
        for clip, busy in (("plant", getattr(self.brain, "plant_t", 0.0) > 0.0),
                           ("defuse", getattr(self.brain, "defuse_t", 0.0) > 0.0)):
            if busy != (clip in self._bomb_clip):
                if busy:
                    self.body.event(clip)
                    self._bomb_clip.add(clip)
                else:
                    self.body.stop(clip)
                    self._bomb_clip.discard(clip)

    def frame_update(self, dt: float, alpha: float) -> None:
        if not self.active:
            return
        p = self.char.interpolated_pos(alpha)
        self.body.root.setPos(p)
        dy = (self.aim.yaw - self.prev_yaw + 180.0) % 360.0 - 180.0
        self.body.root.setH(self.prev_yaw + dy * alpha)

    # ------------------------------------------------------------- events
    def _on_step(self, ev) -> None:
        if ev.kind == "land" or ev.loudness <= 0.01:
            return
        self.game.audio.footstep(ev.surface, ev.pos, ev.loudness * 0.9)
        self.game.notify_noise(ev.pos, ev.loudness, 28.0 * ev.loudness, source=self)

    def on_hit(self, res: DamageResult, pos, direction) -> None:
        self.body.on_hit()
        if not res.killed:
            self.body.event("hit", direction=Vec3(*direction) if direction is not None else None)
        if res.killed:
            self.body.die(Vec3(*direction) if direction is not None else None)
            self._drop_on_death()
        if self.brain is not None:
            self.brain.on_damaged(res)

    def _drop_on_death(self) -> None:
        w = self.weapons
        ws = w.primary or w.secondary
        if ws is not None:
            slot = ws.d.slot
            w.inv.weapons[slot] = None
            ws.cancel_reload()
            self.game.pickups.spawn("weapon", ws, self.position() + Vec3(0, 0, 0.3), self.aim.yaw,
                                    vel=Vec3(self.rng.uniform(-1, 1), self.rng.uniform(-1, 1), 1.5))
        self.body.set_weapon(None)

    def on_fired(self, ws) -> None:
        self.last_fired = self.now
        self.shots_fired += 1
        if self.brain is not None:
            self.brain.on_fired()

    def on_spotted(self, enemy) -> None:
        if self.brain is not None:
            self.brain.on_spotted(enemy)

    def on_heard(self, enemy, pos, loudness: float) -> None:
        if self.brain is not None:
            self.brain.on_heard(enemy, pos, loudness)

    def flashed(self, duration: float, strength: float) -> None:
        if duration > 0.3:
            self.perception.flashed(duration)
            if self.brain is not None:
                self.brain.on_flashed(duration)

    def _auto_pickup(self) -> None:
        """Walk over a weapon for an empty slot to take it (like the player)."""
        for p in self.game.pickups.near_feet(self.char.pos):
            if p.kind != "weapon":
                continue
            item = p.item
            slot = item.d.slot if not isinstance(item, str) else self.game.weapon_db.weapons[item].slot
            if self.weapons.inv.weapons.get(slot) is None:
                if isinstance(item, str):
                    item = self.weapons.inv.make(item)
                self.weapons.inv.give_weapon(item)
                p.taken()
                if slot == "primary" or self.weapons.inv.slot in ("melee", "secondary"):
                    self.weapons.select(slot, force=True)

    # ------------------------------------------------------------ debug
    def set_highlight(self, on: bool) -> None:
        self.body.root.setShaderInput("u_emission", LVecBase4f(0.2, 0.2, 0.0, 0) if on else LVecBase4f(0, 0, 0, 0))

    def describe(self) -> str:
        ws = self.weapons.inv.current()
        w = ws.d.key if ws else self.weapons.inv.slot
        b = self.brain.describe() if self.brain is not None else ""
        return f"{self.name} [{self.side}] hp {self.damageable.health:.0f} {w} {b}"


def head_of(agent) -> Point3:
    if hasattr(agent, "head_pos"):
        return agent.head_pos()
    return agent.center_of_mass() + Vec3(0, 0, 0.5)


def dist2d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
