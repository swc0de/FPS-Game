"""Controllers of the v2 bots: movement, aim policy and shooting.

The brain (ai/v2/brain.py) decides *what* to do a few times per second; these
run every tick and decide *how*, on top of the layers every bot shares
(``AimController``, the character controller, the inventory):

* ``Mover`` follows navmesh paths from the team's ``PathService``.
  * It walks (silently) when the team's possibility field puts enemies near
    the path ahead.
  * It keeps a teammate's width of spacing, and waits behind a doorway a
    teammate is going through instead of stacking into it.
  * It never stops inside a doorway.
* ``AimPolicy`` places the crosshair while moving or holding: at head height
  on the likeliest place an enemy can appear in view (the possibility field
  over the points the bot can see, weighted towards near ones and the
  direction of travel), else along the path. Where an enemy was just seen
  or called, it pre-aims the corner it will come round.
* ``Shooter`` is the trigger discipline of the Milestone 5 brain: fire only
  when the bullets land within the target's angular size, bursts that
  shorten with range, pauses for the recoil to reset, counter-strafing, no
  shooting through teammates. On top: the flick of a new target overshoots
  or undershoots (ai/v2/humanize.py), stress widens the aim error, and a
  pre-fire is a short burst at a believed position the bot cannot see yet.
* ``PeekHelper`` finds the side step that breaks line of sight to a threat
  (to hide between bursts or to reload) and back (to re-peek).
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

from ai.aim import angles_to, wrap180
from ai.v2.pathing import PENDING
from engine.physics import MASK_BULLETS, MASK_SIGHT

HEAD = 1.55
EYE = 1.62


def flat(v: Vec3) -> Vec3:
    return Vec3(v.x, v.y, 0.0)


class Mover:
    def __init__(self, brain):
        self.b = brain
        self.bot = brain.bot
        self.goal_key = None
        self.goal: Point3 | None = None
        self.waiting = 0.0
        self.pending_t = 0.0

    def reset(self) -> None:
        self.goal_key = None
        self.goal = None
        self.waiting = 0.0
        self.b.follower.stop()

    def to(self, goal: Point3, dt: float, key, walk: bool | None = None, via=(), look: Point3 | None = None,
           run: bool = False) -> bool:
        """Move towards goal (through ``via`` lane points). True when arrived.
        ``walk`` None = decide from danger; ``run`` forces running (time pressure)."""
        b = self.b
        bot = self.bot
        pos = bot.position()
        f = b.follower
        if self.goal_key != key or (f.failed and not f.active):
            path = self._plan(pos, goal, via)
            if path == PENDING:
                self.pending_t += dt
                b.look(look or goal + Vec3(0, 0, EYE), dt, 0.5)
                return False
            self.pending_t = 0.0
            self.goal_key = key
            self.goal = Point3(goal)
            if path is None:
                return (flat(goal - pos)).length() < 1.0
            f.follow(path)
        if not f.active:
            return f.arrived or (flat(goal - pos)).length() < 0.8
        wish, crouch, jump = f.update(dt, pos, bot.char.horizontal_speed)
        it = bot.intent
        wish = self._spacing(wish, pos)
        it.wish = wish
        it.crouch = crouch
        it.jump = jump
        if walk is None:
            walk = not run and b.should_walk(pos)
        it.walk = bool(walk)
        if look is None:
            look = b.aim_policy.travel_point(pos, f)
        b.look(look, dt, 0.6)
        return False

    def _plan(self, pos, goal, via):
        b = self.b
        pts = [p for p in via if math.hypot(p[0] - pos.x, p[1] - pos.y) > 2.0]
        if pts:
            k = min(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - pos.x, pts[i][1] - pos.y))
            pts = pts[k:]
        legs = [tuple(pos)] + [tuple(v) for v in pts] + [tuple(goal)]
        path = []
        for a, c in zip(legs, legs[1:]):
            if len(a) == 2:
                a = (a[0], a[1], path[-1][2] if path else pos.z)
            if len(c) == 2:
                c = (c[0], c[1], a[2])
            leg = b.paths.request(a, c, b.bias_key)
            if leg == PENDING:
                return PENDING
            if leg is None:
                continue
            path += leg if not path else leg[1:]
        return path if len(path) >= 2 else None

    def _spacing(self, wish: Vec3, pos: Point3) -> Vec3:
        """Keep out of a teammate's back and out of a doorway someone is in."""
        if wish.lengthSquared() < 1e-6:
            return wish
        b = self.b
        d = Vec3(wish)
        d.normalize()
        slow = 1.0
        for m in b.team.mates_of(b.bot):
            q = m.position()
            v = Vec3(q.x - pos.x, q.y - pos.y, 0)
            dist = v.length()
            if dist < 1e-3 or abs(q.z - pos.z) > 1.5:
                continue
            ahead = v.dot(d) / dist
            if dist < 1.3 and ahead > 0.5:
                slow = min(slow, 0.0 if dist < 0.9 else 0.35)
            elif dist < 3.0 and ahead > 0.6 and b.at_door(q):
                slow = min(slow, 0.0)              # a teammate is going through the doorway ahead
        if slow < 1.0 and b.at_door(pos):
            slow = 1.0                             # never stop in a doorway
        if slow <= 0.0:
            self.waiting += 1.0 / 64.0
            if self.waiting > 2.5:                 # don't wait forever: sidestep past
                side = Vec3(-d.y, d.x, 0) * (1 if (id(b.bot) >> 4) & 1 else -1)
                return (d * 0.4 + side).normalized()
            return Vec3(0, 0, 0)
        self.waiting = 0.0
        return d * slow if slow < 1.0 else wish


class AimPolicy:
    """Where to put the crosshair when there is no target."""

    def __init__(self, brain):
        self.b = brain
        self.point: Point3 | None = None
        self.fact = None
        self._next = 0.0
        self._alt = None
        self._alt_t = 0.0

    def reset(self) -> None:
        self.point = None
        self._next = 0.0

    def travel_point(self, pos: Point3, follower) -> Point3:
        b = self.b
        now = b.bot.now
        if now >= self._next or self.point is None:
            self._next = now + 0.25
            ahead = follower.look_ahead(pos, 4.0)
            fwd = None
            if ahead is not None:
                fwd = flat(ahead - pos)
                if fwd.lengthSquared() > 1e-6:
                    fwd.normalize()
            p = b.likely_point(pos, fwd, max_range=30.0, cone=110.0)
            if p is not None:
                self.point = p
            elif ahead is not None:
                self.point = Point3(ahead.x, ahead.y, ahead.z + 1.5)
        return self.point if self.point is not None else Point3(pos.x, pos.y + 4, pos.z + EYE)

    def hold_point(self, pos: Point3, default_yaw: float | None, watch: Point3 | None) -> Point3:
        """Holding: the most dangerous visible place, switching now and then between the top two."""
        b = self.b
        now = b.bot.now
        if now >= self._next or self.point is None:
            self._next = now + b.rng.uniform(0.5, 0.9)
            p = b.likely_point(pos, None, max_range=45.0, cone=200.0, second=True)
            if p is not None:
                self.point, self._alt = p
            elif watch is not None:
                self.point, self._alt = watch, None
            elif default_yaw is not None:
                h = math.radians(default_yaw)
                self.point, self._alt = Point3(pos.x - math.sin(h) * 8, pos.y + math.cos(h) * 8, pos.z + EYE), None
        if self._alt is not None and now >= self._alt_t:
            self._alt_t = now + b.rng.uniform(1.2, 2.8)
            self.point, self._alt = self._alt, self.point
        return self.point if self.point is not None else Point3(pos.x, pos.y + 4, pos.z + EYE)


# beyond these distances a weapon class is not worth firing (unless shot at)
RANGE_LIMIT = {"shotgun": 18.0, "smg": 40.0, "pistol": 40.0}


class Shooter:
    """Trigger discipline and aim at a visible target (one per bot)."""

    def __init__(self, brain):
        self.b = brain
        self.bot = brain.bot
        self.reset()

    def reset(self) -> None:
        self.target_id = None
        self.burst = 0
        self.burst_limit = 3
        self.pause_until = 0.0
        self.prefer_head = False
        self.engaged_since = 0.0
        self.prefire_shots = 0

    def acquire(self, c, now: float) -> None:
        bot = self.bot
        if self.target_id == id(c.agent):
            return
        self.target_id = id(c.agent)
        self.engaged_since = now
        self.prefer_head = bot.rng.random() < float(bot.profile.get("headshot", 0.3))
        self.burst = 0
        self.burst_limit = self.burst_for((c.pos - bot.position()).length())
        # a flick onto a new target over- or undershoots, then the hand corrects
        eye = bot.eye()
        yaw, _ = angles_to(c.pos.x - eye.x, c.pos.y - eye.y, c.pos.z + 1.4 - eye.z)
        turn = abs(wrap180(yaw - bot.aim.yaw))
        dist = (c.pos - bot.position()).length()
        tvel = c.agent.velocity() if hasattr(c.agent, "velocity") else Vec3(0, 0, 0)
        bot.aim.acquire(id(c.agent), dist, math.hypot(tvel.x, tvel.y), bot.char.horizontal_speed)
        # a big turn overshoots (or stops short) and the hand corrects: the flick decides which
        # side of the target the usual first-shot error falls on, never how big it is (same aim
        # profile as the Milestone 5 bots)
        over = self.b.human.flick(turn)
        if over != 0.0:
            sign = 1.0 if wrap180(yaw - bot.aim.yaw) > 0 else -1.0
            bot.aim.err_x = abs(bot.aim.err_x) * sign * (1.0 if over > 0 else -1.0)

    def release(self) -> None:
        self.target_id = None
        self.bot.aim.release()

    def burst_for(self, dist: float) -> int:
        mx = int(self.bot.profile.get("max_burst", 10))
        r = self.bot.rng
        if dist < 9:
            return mx
        if dist < 20:
            return min(mx, r.randint(3, 6))
        if dist < 35:
            return min(mx, r.randint(2, 3))
        return 1

    def on_fired(self) -> None:
        self.burst += 1
        if self.burst >= self.burst_limit:
            self.burst = 0
            d = self.b.target_distance()
            ws = self.bot.weapons.inv.current()
            pause = 0.12 + min(d, 40.0) * 0.009
            if ws is not None and ws.d.fire_mode == "auto" and d > 18:
                pause += 0.1
            self.pause_until = self.bot.now + pause * self.bot.rng.uniform(0.8, 1.3)
            self.burst_limit = self.burst_for(d)

    def engage(self, c, dt: float, now: float) -> tuple[bool, float, float, bool]:
        """Aim at a visible contact and pull the trigger when on target.
        Returns (firing, angular offset, tolerance, use_head)."""
        bot = self.bot
        it = bot.intent
        enemy = c.agent
        eye = bot.eye()
        head = enemy.head_pos()
        chest = enemy.center_of_mass()
        use_head = c.head_visible and (self.prefer_head or not c.body_visible)
        point = head if use_head else chest
        dist = (point - eye).length()
        tvel = enemy.velocity() if hasattr(enemy, "velocity") else Vec3(0, 0, 0)
        tspeed = math.hypot(tvel.x, tvel.y)
        self.acquire(c, now)
        bot.aim.update_error(dt, tspeed, dist)
        self.b.ensure_weapon(dist)
        ws = bot.weapons.inv.current()
        recoil = ws.recoil_offset(math.floor(ws.recoil_index)) if ws is not None else (0.0, 0.0)
        off = bot.aim.track(eye, point + tvel * 0.05, dt, recoil)
        tol = bot.aim.fire_tolerance(dist, 0.1 if use_head else 0.2)
        can = ws is not None and off <= tol and now >= self.pause_until and not ws.busy(now)
        if ws is not None:
            if ws.d.magazine > 0 and ws.ammo == 0:
                can = False
                if not ws.reloading:
                    ws.start_reload(now)
            limit = RANGE_LIMIT.get(ws.d.cls)
            shot_at = now - self.b.last_hit_t < 2.0
            if limit is not None and dist > limit and not (shot_at and ws.d.cls != "shotgun"):
                can = False
            if ws.d.fire_mode == "melee":
                can = dist < 1.8 and off < 12
        if can and self.b.teammate_in_line(eye, point):
            can = False
        if can:
            self.pull(ws, dist, now)
        firing = can or (ws is not None and now - bot.last_fired < 0.12)
        it.lean = it.lean
        return firing, off, tol, use_head

    def pull(self, ws, dist: float, now: float) -> None:
        it = self.bot.intent
        if ws.d.fire_mode == "auto":
            it.trigger = True
        else:
            it.pressed = True
            if ws.d.fire_mode != "melee":
                self.pause_until = now + self.semi_pause(ws, dist)

    def semi_pause(self, ws, dist: float) -> float:
        if ws.d.fire_mode in ("bolt", "pump"):
            return ws.d.fire_interval + 0.15
        base = max(ws.d.fire_interval, 0.12)
        return base + min(dist, 40.0) * 0.006 * self.bot.rng.uniform(0.6, 1.4)

    def prefire(self, point: Point3, fact, dt: float, now: float) -> bool:
        """Short burst at a believed position the bot cannot see (yet). True while firing."""
        bot = self.bot
        ws = bot.weapons.inv.current()
        if ws is None or ws.d.fire_mode == "melee" or ws.ammo <= 0 or ws.busy(now):
            return False
        eye = bot.eye()
        rest = bot.aim.look_at(eye, point, dt, 1.0)
        if rest > 2.0 or now < self.pause_until:
            return True
        if self.b.teammate_in_line(eye, point):
            return False
        audit = self.b.director.audit
        if audit is not None:
            audit.check_order(bot, "prefire", point, fact, now)
        self.pull(ws, (point - eye).length(), now)
        self.prefire_shots += 1
        if self.prefire_shots >= 3:
            self.pause_until = now + 0.5
            self.prefire_shots = 0
            return False
        return True


class PeekHelper:
    """Side steps that break (hide) or open (peek) the line of sight to a point."""

    def __init__(self, brain):
        self.b = brain
        self.bot = brain.bot

    def hide_dir(self, threat: Point3, step: float = 0.9) -> Vec3 | None:
        """Sideways direction (unit) where, ``step`` metres away, the threat cannot see the bot."""
        bot = self.bot
        pos = bot.position()
        phys = bot.game.physics
        to = flat(threat - pos)
        if to.lengthSquared() < 1e-4:
            return None
        to.normalize()
        side = Vec3(-to.y, to.x, 0)
        for sgn in (1.0, -1.0):
            d = side * sgn
            q = Point3(pos.x + d.x * step, pos.y + d.y * step, pos.z)
            if not bot.nav.walkable_line((pos.x, pos.y, pos.z), (q.x, q.y, q.z)):
                continue
            eye = Point3(q.x, q.y, q.z + EYE)
            if phys.ray_cast(eye, threat, MASK_SIGHT) is not None:
                return d
            low = Point3(q.x, q.y, q.z + 1.05)
            if phys.ray_cast(low, threat, MASK_SIGHT) is not None:
                return d
        return None

    def exposed_to(self, point: Point3) -> bool:
        bot = self.bot
        return bot.game.physics.ray_cast(bot.eye(), point, MASK_SIGHT) is None


def teammate_in_line(bot, director, eye: Point3, point: Point3) -> bool:
    hit = bot.game.physics.ray_cast(eye, point, MASK_BULLETS)
    if hit is None:
        return False
    owner = hit.node.getPythonTag("owner") if hit.node is not None else None
    if owner is None or owner is bot:
        return False
    if getattr(owner, "side", None) == bot.side:
        return True
    return owner is bot.game.player and director.player_agent.side == bot.side
