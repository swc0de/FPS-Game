"""Bot decision making: modes, combat micro and task execution.

Every ~0.13 s ``_think`` picks a *mode*:

    engage   a visible enemy the bot has reacted to: aim, shoot, move
    seek     just lost sight of an enemy: pre-aim its last position, push
             a little (or keep holding the angle when guarding a spot)
    alert    heard / was shot by / was told about an enemy it cannot see:
             face that way (holding bots) or keep moving while watching it
    retreat  low health or empty gun near an enemy: fall back to a spot the
             threat cannot see, reload, then return
    task     the team's plan (ai/tactics.py): move along a lane, hold an
             angle, plant, defuse, pick up the bomb, guard, hunt, save

Combat micro (every tick): the aim controller tracks the target with the
difficulty's reaction time, turn speed and aim error; the trigger is pulled
only when the bullets would land within the target's angular size. Bursts
shorten with distance (spray close, burst mid, tap far) and the bot pauses
between bursts so the recoil resets. Like players, bots are inaccurate on
the move, so they counter-strafe to a stop before shooting, crouch for long
shots and strafe between bursts (harder bots more often).

Utility: the team brain can order a grenade at a point; ``solve_throw``
finds a launch velocity whose arc reaches it unobstructed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from panda3d.core import Point3, Vec3

from ai.aim import angles_to, wrap180
from ai.steering import PathFollower
from engine.physics import GRAVITY, MASK_BULLETS, MASK_SIGHT
from gameplay.lean import OFFSET as LEAN_OFFSET, clearance, right_of


@dataclass
class Task:
    kind: str = "idle"           # idle | move | hold | plant | defuse | pickup | guard | hunt | save
    pos: Point3 | None = None
    via: list = field(default_factory=list)   # lane waypoints to pass on the way
    yaw: float | None = None     # facing once there
    look: Point3 | None = None   # point to watch once there
    crouch: bool = False
    walk: bool = False
    walk_near: float = 0.0       # walk (quietly, accurately) within this distance of pos
    wait: bool = False           # stay when arrived (until the team gives a new task)
    tag: str = ""
    issued: float = 0.0


@dataclass
class ThrowOrder:
    key: str
    target: Point3
    deadline: float
    velocity: Vec3 | None = None
    aim_t: float = 0.0


def solve_throw(start: Point3, target: Point3, speed: float, physics=None, prefer_high: bool = False):
    """Launch velocity that lands a grenade on target (no drag/bounces), or None."""
    dx, dy, dz = target.x - start.x, target.y - start.y, target.z - start.z
    d = math.hypot(dx, dy)
    if d < 0.5:
        return Vec3(0, 0, speed * 0.3)
    g = GRAVITY
    v2 = speed * speed
    disc = v2 * v2 - g * (g * d * d + 2 * dz * v2)
    if disc < 0:
        return None
    root = math.sqrt(disc)
    order = (1, -1) if prefer_high else (-1, 1)
    for sign in order:
        theta = math.atan((v2 + sign * root) / (g * d))
        vel = Vec3(dx / d * math.cos(theta) * speed, dy / d * math.cos(theta) * speed, math.sin(theta) * speed)
        if physics is None or _arc_clear(start, vel, d, physics):
            return vel
    return None


def _arc_clear(start: Point3, vel: Vec3, dist: float, physics) -> bool:
    horiz = math.hypot(vel.x, vel.y)
    t_end = dist / max(horiz, 1e-3)
    steps = max(int(t_end / 0.06), 2)
    prev = Point3(start)
    for k in range(1, steps + 1):
        t = t_end * k / steps
        p = Point3(start.x + vel.x * t, start.y + vel.y * t, start.z + vel.z * t - 0.5 * GRAVITY * t * t)
        hit = physics.ray_cast(prev, p, MASK_SIGHT)
        if hit is not None:
            # touching down just before the target is fine
            return k >= steps - 1
        prev = p
    return True


class Brain:
    def __init__(self, bot, team):
        self.bot = bot
        self.team = team
        self.director = team.director
        self.follower = PathFollower(bot.nav)
        self.task = Task()
        self.reset()

    def reset(self) -> None:
        self.task = Task()
        self.mode = "task"
        self.next_think = 0.0
        self.target = None
        self.follower.stop()
        self._path_for = None
        self.arrived = False
        self.arrive_t = 0.0
        self.burst = 0
        self.burst_limit = 3
        self.pause_until = 0.0
        self.strafe_dir = 1.0
        self.strafe_until = 0.0
        self.prefer_head = False
        self.retreat_to: Point3 | None = None
        self.retreat_until = 0.0
        self.seek_contact = None
        self.seek_until = 0.0
        self.alert_pos: Point3 | None = None
        self.alert_until = 0.0
        self.plant_t = 0.0
        self.defuse_t = 0.0
        self.lean = 0.0
        self.lean_check = 0.0
        self.scan_t = 0.0
        self.scan_offset = 0.0
        self.throw: ThrowOrder | None = None
        self.last_report = -10.0
        self.frag_ready = 0.0
        self.engaged_since = 0.0
        self.flashed_until = 0.0
        self.unseen_shots = 0
        self._preaim_key = None
        self._preaim_pt = Point3()
        self._preaim_until = 0.0
        self.debug = ""

    # ------------------------------------------------------------- orders
    def set_task(self, task: Task) -> None:
        task.issued = self.bot.now
        self.task = task
        self._path_for = None
        self.arrived = False
        self.plant_t = 0.0
        self.defuse_t = 0.0

    def order_throw(self, key: str, target: Point3, within: float = 6.0) -> bool:
        if self.bot.weapons.inv.grenades.get(key, 0) <= 0:
            return False
        if self.bot.rng.random() > float(self.bot.profile.get("grenades", 0.6)):
            return False
        self.throw = ThrowOrder(key, Point3(target), self.bot.now + within)
        return True

    def describe(self) -> str:
        t = self.task
        return f"{self.mode}/{t.kind}{':' + t.tag if t.tag else ''} {self.debug}"

    # ------------------------------------------------------------- events
    def on_damaged(self, res) -> None:
        bot = self.bot
        attacker = self.director.agent_of(res.info.attacker)
        if attacker is not None and attacker.side != bot.side:
            bot.perception.on_damaged(attacker, bot.now)
            self.alert_pos = attacker.position()
            self.alert_until = bot.now + 3.0
            c = bot.perception.contacts.get(id(attacker))
            if c is not None and not c.seen:
                self.unseen_shots += 1
            self.next_think = 0.0

    def on_spotted(self, enemy) -> None:
        self.next_think = min(self.next_think, self.bot.now + 0.02)
        self.team.report(self.bot, enemy, enemy.position(), self.bot.now, "seen")

    def on_heard(self, enemy, pos: Point3, loudness: float) -> None:
        if self.mode in ("task", "alert"):
            self.alert_pos = Point3(pos)
            self.alert_until = self.bot.now + 2.5
        self.team.report(self.bot, enemy, pos, self.bot.now, "heard")

    def on_flashed(self, duration: float) -> None:
        self.flashed_until = self.bot.now + duration
        self.bot.aim.release()

    def on_fired(self) -> None:
        self.burst += 1
        if self.burst >= self.burst_limit:
            self.burst = 0
            d = self._target_distance()
            ws = self.bot.weapons.inv.current()
            pause = 0.12 + min(d, 40.0) * 0.009
            if ws is not None and ws.d.fire_mode == "auto" and d > 18:
                pause += 0.1
            self.pause_until = self.bot.now + pause * self.bot.rng.uniform(0.8, 1.3)
            self.burst_limit = self._burst_for(d)

    # --------------------------------------------------------------- tick
    def update(self, dt: float, now: float, locked: bool) -> None:
        bot = self.bot
        it = bot.intent
        it.wish = Vec3(0, 0, 0)
        it.walk = it.crouch = it.jump = False
        it.trigger = False
        bot.perception.update(now, self.team.enemies())
        if locked:
            self._idle_look(dt, now)
            return
        if now >= self.next_think:
            self.next_think = now + bot.rng.uniform(0.11, 0.16)
            self._think(now)
        if now < self.flashed_until:
            self._flashed(dt, now)
            return
        if self.throw is not None and self.mode in ("task", "alert") and self._do_throw(dt, now):
            return
        getattr(self, "_" + self.mode)(dt, now)
        self._choose_lean(now)

    def _watch_point(self) -> Point3 | None:
        """What the bot is looking at right now (for peeking)."""
        if self.mode == "engage" and self.target is not None and self.target.agent.alive:
            return self.target.agent.head_pos()
        if self.mode == "alert" and self.alert_pos is not None:
            return self.alert_pos + Vec3(0, 0, 1.5)
        if self.mode == "task" and self.arrived and self.task.look is not None:
            return self.task.look
        return None

    def _choose_lean(self, now: float) -> None:
        """Peek around corners: lean when what we watch is hidden from the
        upright eye but visible from one side (and only while standing still)."""
        bot = self.bot
        it = bot.intent
        if it.wish.lengthSquared() > 0.04 or self.mode in ("retreat", "seek") or bot.char.horizontal_speed > 1.6:
            self.lean = 0.0
        elif now >= self.lean_check:
            self.lean_check = now + bot.rng.uniform(0.25, 0.4)
            point = self._watch_point()
            if point is None:
                self.lean = 0.0
            else:
                c = bot.char
                eye = Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height)
                phys = self.director.game.physics
                free = clearance(phys, eye, bot.aim.yaw)
                visible = []
                for sign, f in ((-1.0, free[0]), (0.0, 1.0), (1.0, free[1])):
                    p = eye + right_of(bot.aim.yaw) * (sign * LEAN_OFFSET * f)
                    visible.append(f > 0.6 and phys.ray_cast(p, point, MASK_SIGHT) is None)
                keep = self.mode == "engage" and self.lean != 0.0 and visible[1 + int(self.lean)]
                if keep:
                    pass                      # keep peeking while shooting
                elif visible[1]:
                    self.lean = 0.0
                elif visible[2] and not visible[0]:
                    self.lean = 1.0
                elif visible[0] and not visible[2]:
                    self.lean = -1.0
                else:
                    self.lean = 0.0
        it.lean = self.lean

    # -------------------------------------------------------------- think
    def _think(self, now: float) -> None:
        bot = self.bot
        per = bot.perception
        c = per.target(now)
        hp = bot.damageable.health
        if self.mode == "retreat" and now < self.retreat_until:
            # keep falling back unless the enemy shows up and we can shoot back
            ws = bot.weapons.inv.current()
            loaded = ws is not None and ws.d.magazine > 0 and ws.ammo > 0 and not ws.reloading
            if c is None or not loaded:
                return
        if c is not None:
            if self._committed_objective(now):
                self.mode = "task"
                return
            # an interrupted plant / defuse starts over (as in CS)
            self.plant_t = self.defuse_t = 0.0
            if self.mode != "engage" or self.target is not c:
                self.engaged_since = now
                self.prefer_head = bot.rng.random() < float(bot.profile.get("headshot", 0.3))
                self.burst = 0
                self.burst_limit = self._burst_for(self._target_distance(c))
            self.target = c
            self.mode = "engage"
            self.unseen_shots = 0
            if self._should_retreat(c, now):
                self._start_retreat(c.pos, now, 3.0)
            return
        if self.mode == "engage" and self.target is not None:
            self.seek_contact = self.target
            self.seek_until = now + bot.rng.uniform(2.0, 3.5)
            self.target = None
            self.mode = "seek"
            bot.aim.release()
            return
        self.target = None
        if self.mode == "seek" and now < self.seek_until and self.seek_contact is not None \
                and self.seek_contact.agent.alive:
            return
        # shot at by someone we cannot see: find cover after a couple of hits
        if self.unseen_shots >= 2 and hp < 50 and self.task.kind not in ("plant", "defuse"):
            self.unseen_shots = 0
            if self.alert_pos is not None:
                self._start_retreat(self.alert_pos, now, 3.5)
                return
        if self.alert_pos is not None and now < self.alert_until:
            self.mode = "alert"
            return
        a = per.latest(3.5, now, ("sound", "damage"))
        if a is not None and self.task.kind not in ("plant", "defuse"):
            self.alert_pos = Point3(a.pos)
            self.alert_until = now + 2.0
            self.mode = "alert"
            return
        self.mode = "task"
        self._maybe_frag(now)

    def _committed_objective(self, now: float) -> bool:
        """Finish a plant/defuse that is nearly done instead of turning to fight."""
        timers = self.director.rules["timers"]
        if self.task.kind == "plant" and self.plant_t > float(timers["plant_time"]) * 0.75:
            return True
        if self.task.kind == "defuse" and self.defuse_t > 0.0:
            total = float(timers["defuse_time_kit" if self.bot.has_kit else "defuse_time"])
            left = self.director.match.bomb_time_left()
            return left < total - self.defuse_t + 1.5 or self.defuse_t > total * 0.8
        return False

    def _should_retreat(self, c, now: float) -> bool:
        bot = self.bot
        ws = bot.weapons.inv.current()
        d = self._target_distance(c)
        if bot.damageable.health < 20 and d > 10 and bot.rng.random() < 0.35:
            return True
        if ws is not None and ws.d.magazine > 0 and ws.ammo == 0 and d > 6:
            sec = bot.weapons.secondary
            return not (sec is not None and sec.ammo > 0 and ws is not sec)
        return False

    # ------------------------------------------------------------- engage
    def _engage(self, dt: float, now: float) -> None:
        bot = self.bot
        it = bot.intent
        c = self.target
        if c is None or not c.agent.alive:
            self.mode = "task"
            return
        enemy = c.agent
        eye = bot.eye()
        head = enemy.head_pos()
        chest = enemy.center_of_mass()
        # aim at what can be hit: the head if it is all that shows above cover
        use_head = c.head_visible and (self.prefer_head or not c.body_visible)
        point = head if use_head else chest
        dist = (point - eye).length()
        tvel = enemy.velocity() if hasattr(enemy, "velocity") else Vec3(0, 0, 0)
        tspeed = math.hypot(tvel.x, tvel.y)
        bot.aim.acquire(id(enemy), dist, tspeed, bot.char.horizontal_speed)
        bot.aim.update_error(dt, tspeed, dist)
        self._ensure_weapon(dist, now)
        ws = bot.weapons.inv.current()
        recoil = ws.recoil_offset(math.floor(ws.recoil_index)) if ws is not None else (0.0, 0.0)
        # lead a little against strafing targets (reaction lag)
        lead = tvel * 0.05
        off = bot.aim.track(eye, point + lead, dt, recoil)
        tol = bot.aim.fire_tolerance(dist, 0.1 if use_head else 0.2)
        self.debug = f"d{dist:.0f} off{off:.1f}/{tol:.1f}"

        can_fire = ws is not None and off <= tol and now >= self.pause_until and not ws.busy(now)
        per_dmg = bot.perception.last_damage
        if ws is not None:
            if ws.d.magazine > 0 and ws.ammo == 0:
                can_fire = False
                if not ws.reloading:
                    ws.start_reload(now)
            # weak at range: SMGs and pistols wait for a closer shot unless under fire
            limit = {"shotgun": 18.0, "smg": 40.0, "pistol": 40.0}.get(ws.d.cls)
            shot_at = per_dmg is not None and now - per_dmg[0] < 2.0
            if limit is not None and dist > limit and not (shot_at and ws.d.cls != "shotgun"):
                can_fire = False
            if ws.d.fire_mode == "melee":
                can_fire = dist < 1.8 and off < 12
        if can_fire and self._teammate_in_line(eye, point):
            can_fire = False
        # fire
        if can_fire:
            if ws.d.fire_mode == "auto":
                it.trigger = True
            elif ws.d.fire_mode == "melee":
                it.pressed = True
            else:
                it.pressed = True
                self.pause_until = now + self._semi_pause(ws, dist)
        firing = can_fire or (ws is not None and now - bot.last_fired < 0.12)
        # movement
        speed = bot.char.horizontal_speed
        run_gun = ws is not None and (ws.d.cls in ("smg", "shotgun", "knife") and dist < 9)
        if ws is not None and ws.d.fire_mode == "melee":
            it.wish = self._dir_to(enemy.position())
        elif run_gun and firing:
            it.wish = self._dir_to(enemy.position()) * (1.0 if ws.d.cls == "knife" else 0.3)
        elif firing or off <= tol * 2.5:
            if speed > 1.0:
                v = bot.char.vel
                stop = Vec3(-v.x, -v.y, 0)
                stop.normalize()
                it.wish = stop                       # counter-strafe
            it.crouch = self._crouch_fire(ws, dist)
        else:
            it.wish = self._strafe(now, enemy)
        bot.scoped = ws is not None and bool(ws.d.scope) and speed < 1.5
        if now - self.last_report > 1.5:
            self.last_report = now
            self.team.report(bot, enemy, enemy.position(), now, "seen")

    def _teammate_in_line(self, eye: Point3, point: Point3) -> bool:
        """Hold fire while a teammate stands between us and the target."""
        hit = self.bot.game.physics.ray_cast(eye, point, MASK_BULLETS)
        if hit is None:
            return False
        owner = hit.node.getPythonTag("owner") if hit.node is not None else None
        return owner is not None and owner is not self.bot and getattr(owner, "side", None) == self.bot.side \
            or (owner is self.bot.game.player and self.director.player_agent.side == self.bot.side)

    def _ensure_weapon(self, dist: float, now: float) -> None:
        w = self.bot.weapons
        inv = w.inv
        ws = inv.current()
        prim, sec = w.primary, w.secondary

        def usable(x):
            return x is not None and (x.ammo > 0 or x.reserve > 0)
        if ws is None or ws.d.fire_mode == "melee" or inv.slot in ("grenade", "bomb"):
            if usable(prim):
                w.select("primary")
            elif usable(sec):
                w.select("secondary")
            elif inv.slot != "melee":
                w.select("melee")
            return
        if ws is prim and ws.ammo == 0 and usable(sec) and sec.ammo > 0 and dist < 14 \
                and self.bot.profile.get("strafe", 0) > 0.5:
            w.select("secondary")
        elif ws.ammo == 0 and ws.reserve == 0:
            if ws is prim and usable(sec):
                w.select("secondary")
            elif not usable(prim) and not usable(sec):
                w.select("melee")

    def _burst_for(self, dist: float) -> int:
        mx = int(self.bot.profile.get("max_burst", 10))
        r = self.bot.rng
        if dist < 9:
            return mx
        if dist < 20:
            return min(mx, r.randint(3, 6))
        if dist < 35:
            return min(mx, r.randint(2, 3))
        return 1

    def _semi_pause(self, ws, dist: float) -> float:
        if ws.d.fire_mode in ("bolt", "pump"):
            return ws.d.fire_interval + 0.15
        base = max(ws.d.fire_interval, 0.12)
        return base + min(dist, 40.0) * 0.006 * self.bot.rng.uniform(0.6, 1.4)

    def _crouch_fire(self, ws, dist: float) -> bool:
        if ws is None or ws.d.cls in ("smg", "shotgun", "knife"):
            return False
        if dist > 22:
            return self.bot.profile.get("strafe", 0) >= 0.3
        return False

    def _strafe(self, now: float, enemy) -> Vec3:
        bot = self.bot
        if bot.profile.get("strafe", 0) <= 0:
            return Vec3(0, 0, 0)
        if now >= self.strafe_until:
            self.strafe_until = now + bot.rng.uniform(0.3, 0.75)
            self.strafe_dir = -self.strafe_dir if bot.rng.random() < 0.7 else self.strafe_dir
            if bot.rng.random() > float(bot.profile.get("strafe", 0.5)):
                self.strafe_dir = 0.0
        if self.strafe_dir == 0.0:
            return Vec3(0, 0, 0)
        to = enemy.position() - bot.position()
        to.z = 0
        if to.lengthSquared() < 1e-4:
            return Vec3(0, 0, 0)
        to.normalize()
        side = Vec3(-to.y, to.x, 0) * self.strafe_dir
        p = bot.position()
        probe = Point3(p.x + side.x * 1.2, p.y + side.y * 1.2, p.z)
        if not bot.nav.walkable_line(p, probe):
            self.strafe_dir = -self.strafe_dir
            side = -side
        return side

    # -------------------------------------------------------------- seek
    def _seek(self, dt: float, now: float) -> None:
        bot = self.bot
        c = self.seek_contact
        if c is None:
            self.mode = "task"
            return
        look = self._preaim(c.pos, now)
        bot.aim.look_at(bot.eye(), look, dt)
        holding = self.task.kind in ("hold", "guard", "plant", "defuse") and self.arrived
        if not holding and (c.pos - bot.position()).length() > 3.0:
            self._move_to(c.pos, dt, walk=True, look=None, path_key=("seek", round(c.pos.x), round(c.pos.y)))
        self._reload_if_needed(now, 0.5)

    # ------------------------------------------------------------- alert
    def _alert(self, dt: float, now: float) -> None:
        bot = self.bot
        if self.alert_pos is None or now > self.alert_until:
            self.mode = "task"
            self._task(dt, now)
            return
        look = self._preaim(self.alert_pos, now)
        if self.task.kind in ("move", "hunt", "pickup") and not self.arrived:
            self._task(dt, now, look_override=look)
        elif self.task.kind in ("plant", "defuse"):
            self._task(dt, now)
        else:
            if self.task.pos is not None and not self.arrived:
                self._task(dt, now, look_override=look)
            else:
                bot.aim.look_at(bot.eye(), look, dt)
                bot.intent.crouch = self.task.crouch
        self._reload_if_needed(now, 0.6)

    def _preaim(self, target: Point3, now: float) -> Point3:
        """Where an enemy at ``target`` will come into view: the last corner of
        the walking route towards it that the bot can still see (a doorway,
        a wall edge) - rather than staring at the wall the sound came through."""
        key = (round(target.x), round(target.y), round(target.z))
        if key == self._preaim_key and now < self._preaim_until:
            return self._preaim_pt
        bot = self.bot
        eye = bot.eye()
        phys = bot.game.physics
        point = Point3(target.x, target.y, target.z + 1.45)
        if phys.ray_cast(eye, point, MASK_SIGHT) is not None:
            path = bot.nav.find_path(bot.position(), target)
            if path:
                for corner in path[1:]:
                    q = Point3(corner[0], corner[1], corner[2] + 1.45)
                    if phys.ray_cast(eye, q, MASK_SIGHT) is not None:
                        break
                    point = q
        self._preaim_key, self._preaim_pt, self._preaim_until = key, point, now + 1.0
        return point

    # ----------------------------------------------------------- retreat
    def _start_retreat(self, threat: Point3, now: float, hold: float) -> None:
        cover = self.find_cover(threat, 9.0)
        if cover is None:
            return
        self.retreat_to = cover
        self.retreat_until = now + hold + (cover - self.bot.position()).length() / 5.0
        self.alert_pos = Point3(threat)
        self.alert_until = self.retreat_until
        self.mode = "retreat"
        self.team.radio(self.bot, "Taking fire, falling back!")

    def _retreat(self, dt: float, now: float) -> None:
        bot = self.bot
        if self.retreat_to is None or now > self.retreat_until:
            self.mode = "task"
            self._path_for = None
            return
        d = (self.retreat_to - bot.position())
        d.z = 0
        if d.length() > 0.6:
            # back-pedal: keep watching the threat while moving to cover
            look = self.alert_pos + Vec3(0, 0, 1.4) if self.alert_pos is not None else None
            self._move_to(self.retreat_to, dt, walk=False, look=look, path_key=("retreat",))
        else:
            bot.intent.crouch = True
            if self.alert_pos is not None:
                bot.aim.look_at(bot.eye(), self.alert_pos + Vec3(0, 0, 1.4), dt)
        self._reload_if_needed(now, 0.99)

    def find_cover(self, threat: Point3, radius: float) -> Point3 | None:
        """Nearest walkable spot the threat cannot see (eye height on both sides)."""
        bot = self.bot
        me = bot.position()
        phys = bot.game.physics
        eye_t = Point3(threat.x, threat.y, threat.z + 1.6)
        best, best_cost = None, 1e9
        for k in range(14):
            p = bot.nav.random_point(bot.rng, me, radius * (0.4 + 0.6 * k / 14))
            if p is None:
                continue
            q = Point3(p[0], p[1], p[2] + 1.35)
            if phys.ray_cast(eye_t, q, MASK_SIGHT) is None:
                continue
            dist = math.hypot(p[0] - me.x, p[1] - me.y)
            # prefer spots away from the threat
            away = (Vec3(p[0] - me.x, p[1] - me.y, 0)).dot(Vec3(me.x - threat.x, me.y - threat.y, 0).normalized())
            cost = dist - away * 0.5
            if cost < best_cost:
                best, best_cost = Point3(*p), cost
        return best

    # ------------------------------------------------------------- flash
    def _flashed(self, dt: float, now: float) -> None:
        bot = self.bot
        it = bot.intent
        # blind: back off from where we were looking, spray if we were fighting
        it.wish = -bot.forward() * 0.8
        if self.target is not None and self.target.agent.alive and bot.rng.random() < 0.3:
            ws = bot.weapons.inv.current()
            if ws is not None and ws.d.fire_mode == "auto":
                it.trigger = True

    # -------------------------------------------------------------- task
    def _task(self, dt: float, now: float, look_override: Point3 | None = None) -> None:
        bot = self.bot
        t = self.task
        bot.scoped = False
        kind = t.kind
        if kind == "plant":
            self._do_plant(dt, now)
            return
        if kind == "defuse":
            self._do_defuse(dt, now)
            return
        if t.pos is None:
            self._idle_look(dt, now)
            self._reload_if_needed(now, 0.7)
            return
        if not self.arrived:
            walk = t.walk or (t.walk_near > 0 and (t.pos - bot.position()).length() < t.walk_near)
            done = self._move_to(t.pos, dt, walk=walk, look=look_override, path_key=("task", id(t)), via=t.via)
            if done:
                self.arrived = True
                self.arrive_t = now
                self.team.on_arrived(bot, t)
            return
        # holding position
        it = bot.intent
        it.crouch = t.crouch
        d = t.pos - bot.position()
        d.z = 0
        if d.length() > 1.2:
            self.arrived = False
            self._path_for = None
            return
        if look_override is not None:
            bot.aim.look_at(bot.eye(), look_override, dt)
        else:
            self._hold_look(dt, now)
        self._reload_if_needed(now, 0.7)
        if kind in ("hunt", "pickup") and now - self.arrive_t > 1.0:
            self.team.task_done(bot, t)

    def _hold_look(self, dt: float, now: float) -> None:
        """Watch the assigned angle with a slow scan around it."""
        bot = self.bot
        t = self.task
        if now >= self.scan_t:
            self.scan_t = now + bot.rng.uniform(1.5, 3.5)
            self.scan_offset = bot.rng.uniform(-22.0, 22.0)
        if t.look is not None:
            yaw, pitch = angles_to(t.look.x - bot.eye().x, t.look.y - bot.eye().y, t.look.z - bot.eye().z)
        elif t.yaw is not None:
            yaw, pitch = t.yaw, 0.0
        else:
            yaw, pitch = bot.aim.yaw, 0.0
        bot.aim.turn_towards(yaw + self.scan_offset * 0.6, pitch, dt, 0.35)

    def _idle_look(self, dt: float, now: float) -> None:
        bot = self.bot
        if now >= self.scan_t:
            self.scan_t = now + bot.rng.uniform(1.0, 2.5)
            self.scan_offset = wrap180(bot.aim.yaw + bot.rng.uniform(-50, 50))
        bot.aim.turn_towards(self.scan_offset, 0.0, dt, 0.3)

    def _do_plant(self, dt: float, now: float) -> None:
        bot = self.bot
        t = self.task
        d = self.director
        if not bot.weapons.inv.has_bomb:
            self.team.task_done(bot, t)
            return
        site = d.site_at(bot.position())
        if not self.arrived:
            if self._move_to(t.pos, dt, walk=False, look=None, path_key=("plant", id(t))) or \
                    (site is not None and (t.pos - bot.position()).length() < 1.5):
                self.arrived = True
            self.plant_t = 0.0
            return
        if site is None or not bot.char.on_ground:
            self.arrived = False
            self._path_for = None
            return
        inv = bot.weapons.inv
        if inv.slot != "bomb":
            bot.weapons.select("bomb")
        bot.intent.crouch = True
        bot.aim.turn_towards(bot.aim.yaw, -35.0, dt, 0.5)
        if self.plant_t == 0.0:
            self.team.radio(bot, f"Planting at {site['name']}!")
        self.plant_t += dt
        if int((self.plant_t - dt) * 3) != int(self.plant_t * 3):
            bot.game.audio.play_at("plant_tap", bot.position(), 0.5)
        if self.plant_t >= float(d.rules["timers"]["plant_time"]):
            self.plant_t = 0.0
            d.bot_plant(bot, site)

    def _do_defuse(self, dt: float, now: float) -> None:
        bot = self.bot
        d = self.director
        bomb = d.bomb
        if bomb.state != "planted":
            self.team.task_done(bot, self.task)
            return
        to = bomb.pos - bot.position()
        to.z = 0
        if to.length() > 1.3 or abs(bomb.pos.z - bot.position().z) > 1.2:
            self.defuse_t = 0.0
            self._move_to(bomb.pos, dt, walk=False, look=None, path_key=("defuse",))
            return
        bot.intent.crouch = True
        bot.aim.look_at(bot.eye(), bomb.pos, dt)
        if self.defuse_t == 0.0:
            bot.game.audio.play_at("defuse_start", bomb.pos, 0.8)
            self.team.radio(bot, "Defusing!" + ("" if bot.has_kit else " (no kit)"))
        self.defuse_t += dt
        total = float(d.rules["timers"]["defuse_time_kit" if bot.has_kit else "defuse_time"])
        if self.defuse_t >= total:
            self.defuse_t = 0.0
            d.bot_defuse(bot)

    # ---------------------------------------------------------- movement
    def _move_to(self, goal: Point3, dt: float, walk: bool, look: Point3 | None, path_key, via=()) -> bool:
        """Follow a navmesh path (through ``via`` points) to goal. True when arrived."""
        bot = self.bot
        pos = bot.position()
        f = self.follower
        if self._path_for != path_key:
            self._path_for = path_key
            pts = [p for p in via if math.hypot(p[0] - pos.x, p[1] - pos.y) > 2.0]
            # drop lane points already behind us (closest one onwards)
            if pts:
                k = min(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - pos.x, pts[i][1] - pos.y))
                pts = pts[k:]
            path = self._lane_path(pos, pts, goal)
            if path is None:
                return (goal - pos).length() < 1.0
            f.follow(path)
        if f.failed:
            self._path_for = None
            return False
        if not f.active:
            return True
        wish, crouch, jump = f.update(dt, pos, bot.char.horizontal_speed)
        it = bot.intent
        it.wish = wish
        it.crouch = crouch
        it.jump = jump
        it.walk = walk
        if look is None:
            ahead = f.look_ahead(pos, 4.0)
            look = Point3(ahead.x, ahead.y, ahead.z + 1.5) if ahead is not None else None
        if look is not None:
            bot.aim.look_at(bot.eye(), look, dt, 0.55)
        return not f.active and f.arrived

    def _lane_path(self, pos, via, goal):
        nav = self.bot.nav
        bias = self.team.cost_bias(self.bot)
        pts = [tuple(pos)] + [tuple(v) for v in via] + [tuple(goal)]
        path = []
        for a, b in zip(pts, pts[1:]):
            if len(a) == 2:
                a = (a[0], a[1], path[-1][2] if path else pos.z)
            if len(b) == 2:
                b = (b[0], b[1], a[2])
            leg = nav.find_path(a, b, cost_bias=bias)
            if leg is None:
                continue
            path += leg if not path else leg[1:]
        return path if len(path) >= 2 else None

    def _dir_to(self, p: Point3) -> Vec3:
        d = p - self.bot.position()
        d.z = 0
        return d.normalized() if d.lengthSquared() > 1e-4 else Vec3(0, 0, 0)

    def _target_distance(self, c=None) -> float:
        c = c or self.target or self.seek_contact
        if c is None:
            return 15.0
        return (c.pos - self.bot.position()).length()

    def _reload_if_needed(self, now: float, frac: float) -> None:
        ws = self.bot.weapons.inv.current()
        if ws is not None and ws.d.magazine > 0 and ws.ammo < ws.d.magazine * frac and not ws.reloading:
            ws.start_reload(now)

    # ----------------------------------------------------------- grenades
    def _maybe_frag(self, now: float) -> None:
        """Frag an enemy known to hide behind cover nearby."""
        bot = self.bot
        if now < self.frag_ready or bot.weapons.inv.grenades.get("frag", 0) <= 0 or self.throw is not None:
            return
        c = bot.perception.latest(2.5, now, ("sight", "sound"))
        if c is None or c.seen:
            return
        d = (c.pos - bot.position()).length()
        if 7.0 < d < 20.0:
            self.frag_ready = now + 8.0
            self.order_throw("frag", c.pos, 2.5)

    def _do_throw(self, dt: float, now: float) -> bool:
        bot = self.bot
        o = self.throw
        if now > o.deadline or bot.weapons.inv.grenades.get(o.key, 0) <= 0:
            self.throw = None
            return False
        if o.velocity is None:
            g = bot.game.weapon_db.grenades[o.key]
            start = bot.eye()
            o.velocity = solve_throw(start, o.target, g.throw_speed, bot.game.physics) or \
                solve_throw(start, o.target, g.throw_speed, bot.game.physics, prefer_high=True)
            if o.velocity is None:
                self.throw = None
                return False
            inv = bot.weapons.inv
            inv.grenade = o.key
            bot.weapons.select("grenade", force=True)
        v = o.velocity
        yaw, pitch = angles_to(v.x, v.y, v.z)
        left = bot.aim.turn_towards(yaw, pitch, dt, 0.8)
        o.aim_t += dt
        if left < 2.0 and o.aim_t > 0.45 and now >= bot.weapons.grenade_ready:
            bot.weapons.throw(o.key, v)
            names = {"flash": "Flashbang out!", "smoke": "Smoke out!", "frag": "Frag out!"}
            self.team.radio(bot, names.get(o.key, "Grenade!"))
            self.throw = None
        return True
