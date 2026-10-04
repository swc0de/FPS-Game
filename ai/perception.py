"""What a bot knows about its enemies: sight, sound, damage and team calls.

Vision runs a few times per second (staggered between bots). An enemy is
seen when it is inside the bot's view cone (difficulty ``fov``; a wider
peripheral cone at close range), within range, not hidden by smoke, and a
ray from the bot's eye reaches its head or chest. A flashed bot sees
nothing until the flash wears off.

Hearing uses the game's noise events (footsteps when running, gunshots,
grenades, the bomb) emitted with their source: enemy noises within
``radius * hearing`` become a "sound" contact at a position that gets less
precise with distance.

Contacts remember where an enemy was last known and how (``seen`` now,
earlier sighting, sound, damage or a teammate's callout). The brain uses
the reaction delay: a newly seen enemy only becomes a *target* once
``reaction`` seconds have passed since it appeared.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from panda3d.core import Point3, Vec3

from engine.physics import MASK_SIGHT


@dataclass
class Contact:
    agent: object
    pos: Point3                  # last known feet position
    time: float                  # when that was
    source: str                  # sight | sound | damage | team
    seen: bool = False           # visible this scan
    since: float = 0.0           # start of the current continuous sighting
    react_at: float = 0.0        # when the bot may start shooting at it
    aim_point: Point3 = field(default_factory=Point3)
    head_visible: bool = False
    body_visible: bool = False


class Perception:
    def __init__(self, bot, vision_cfg: dict, profile: dict):
        self.bot = bot
        self.cfg = vision_cfg
        self.p = profile
        self.contacts: dict[int, Contact] = {}
        self.next_scan = bot.rng.uniform(0.0, float(vision_cfg.get("scan_interval", 0.1)))
        self.blind_until = 0.0
        self.noise_seen = 0.0
        self.last_damage: tuple[float, Point3] | None = None

    def reset(self) -> None:
        self.contacts.clear()
        self.blind_until = 0.0
        self.last_damage = None

    @property
    def blind(self) -> bool:
        return self.bot.now < self.blind_until

    # ------------------------------------------------------------- update
    def update(self, now: float, enemies) -> None:
        if now >= self.next_scan:
            self.next_scan = now + float(self.cfg.get("scan_interval", 0.1))
            self._scan(now, enemies)
        self._hear(now)
        # forget stale information
        for k in [k for k, c in self.contacts.items() if not c.agent.alive or now - c.time > 25.0]:
            del self.contacts[k]

    def _scan(self, now: float, enemies) -> None:
        bot = self.bot
        eye = bot.eye()
        yaw = math.radians(bot.aim.yaw)
        fwd = (-math.sin(yaw), math.cos(yaw))
        half_fov = math.radians(float(self.p.get("fov", 110)) * 0.5)
        half_per = math.radians(float(self.cfg.get("peripheral", 160)) * 0.5)
        per_range = float(self.cfg.get("peripheral_range", 14.0))
        rng_max = float(self.cfg.get("range", 90.0))
        blind = self.blind
        game = bot.game
        for e in enemies:
            c = self.contacts.get(id(e))
            visible, head_vis, body_vis, point = False, False, False, None
            if e.alive and not blind:
                head = e.head_pos()
                chest = e.center_of_mass()
                d = chest - eye
                dist = d.length()
                if dist <= rng_max:
                    hd = math.hypot(d.x, d.y)
                    cosang = (d.x * fwd[0] + d.y * fwd[1]) / max(hd, 1e-4)
                    ang = math.acos(max(-1.0, min(1.0, cosang)))
                    if ang <= half_fov or (ang <= half_per and dist <= per_range):
                        head_vis = self._clear(eye, head)
                        body_vis = self._clear(eye, chest)
                        if head_vis or body_vis:
                            target = head if head_vis and not body_vis else chest
                            if game.effects.smoke_between(eye, target) < float(self.cfg.get("smoke_block", 0.55)):
                                visible = True
                                point = target
            if visible:
                if c is None or not c.seen:
                    lo, hi = self.p.get("reaction", [0.3, 0.5])
                    react = self.bot.rng.uniform(lo, hi)
                    if c is not None and now - c.time < 2.0 and c.source == "sight":
                        react *= 0.4                     # just lost sight of it: faster re-acquire
                    elif c is not None and now - c.time < 4.0:
                        react *= 0.75                    # expected (heard / called out)
                    c = Contact(e, e.position(), now, "sight", True, now, now + react)
                    self.contacts[id(e)] = c
                    bot.on_spotted(e)
                c.pos = e.position()
                c.time = now
                c.source = "sight"
                c.seen = True
                c.aim_point = point
                c.head_visible = head_vis
                c.body_visible = body_vis
            elif c is not None and c.seen:
                c.seen = False

    def _clear(self, eye: Point3, point: Point3) -> bool:
        hit = self.bot.game.physics.ray_cast(eye, point, MASK_SIGHT)
        return hit is None

    def _hear(self, now: float) -> None:
        bot = self.bot
        events = bot.game.noise_events
        if not events or events[-1][0] <= self.noise_seen:
            return
        pos = bot.position()
        hear = float(self.p.get("hearing", 1.0))
        newest = self.noise_seen
        for ev in events:
            t, p, loud, radius = ev[0], ev[1], ev[2], ev[3]
            src = ev[4] if len(ev) > 4 else None
            if t <= self.noise_seen:
                continue
            newest = max(newest, t)
            if src is None or src is bot or getattr(src, "side", None) == bot.side or not src.alive:
                continue
            d = (p - pos).length()
            if d > radius * hear:
                continue
            c = self.contacts.get(id(src))
            if c is not None and c.seen:
                continue
            fuzz = min(d * 0.08, 4.0)
            guess = Point3(p.x + bot.rng.uniform(-fuzz, fuzz), p.y + bot.rng.uniform(-fuzz, fuzz), p.z)
            if c is None:
                c = Contact(src, guess, t, "sound")
                self.contacts[id(src)] = c
            elif t >= c.time:
                c.pos, c.time, c.source = guess, t, "sound"
            bot.on_heard(src, guess, loud)
        self.noise_seen = newest

    # ------------------------------------------------------------- inputs
    def on_damaged(self, attacker, now: float) -> None:
        if attacker is None or not hasattr(attacker, "position"):
            return
        self.last_damage = (now, attacker.position())
        c = self.contacts.get(id(attacker))
        if c is None:
            self.contacts[id(attacker)] = Contact(attacker, attacker.position(), now, "damage")
        elif not c.seen:
            c.pos, c.time, c.source = attacker.position(), now, "damage"

    def on_callout(self, enemy, pos: Point3, t: float) -> None:
        c = self.contacts.get(id(enemy))
        if c is None:
            self.contacts[id(enemy)] = Contact(enemy, Point3(pos), t, "team")
        elif not c.seen and t > c.time + 0.5:
            c.pos, c.time, c.source = Point3(pos), t, "team"

    def flashed(self, duration: float) -> None:
        self.blind_until = max(self.blind_until, self.bot.now + duration)
        for c in self.contacts.values():
            c.seen = False

    # ------------------------------------------------------------- queries
    def visible(self) -> list[Contact]:
        return [c for c in self.contacts.values() if c.seen and c.agent.alive]

    def target(self, now: float) -> Contact | None:
        """Visible enemy the bot has reacted to (closest first)."""
        best, best_d = None, 1e9
        me = self.bot.position()
        for c in self.contacts.values():
            if c.seen and c.agent.alive and now >= c.react_at:
                d = (c.pos - me).lengthSquared()
                if d < best_d:
                    best, best_d = c, d
        return best

    def latest(self, max_age: float, now: float, sources=("sight", "sound", "damage", "team")) -> Contact | None:
        best = None
        for c in self.contacts.values():
            if c.agent.alive and now - c.time <= max_age and c.source in sources:
                if best is None or c.time > best.time:
                    best = c
        return best

    def threat_dir(self, now: float) -> Vec3 | None:
        c = self.latest(6.0, now)
        if c is None:
            return None
        d = c.pos - self.bot.position()
        d.z = 0
        return d.normalized() if d.lengthSquared() > 1e-4 else None
