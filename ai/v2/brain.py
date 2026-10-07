"""Bot brain v2: utility-scored actions with commitment (Milestone 8).

A few times per second (``think``, staggered between bots) the brain builds
a context of what the bot knows and scores every action it could take; the
best one runs every tick until the next think. Scores come from
considerations (health and armour, magazine and reserve, weapon versus
range, numbers advantage, a teammate close enough to trade, time left, the
bomb state, how exposed the bot believes it is, cover nearby) shaped by the
bot's traits and role. The running action keeps a bonus until its minimum
time has passed and a challenger must beat it by a margin (hysteresis), so
bots do not dither; a little noise from the bot's own ``Random`` (more on
easy, under stress) keeps two bots in the same spot from always doing the
same thing.

Actions (``ACTIONS``; "task" carries out the team strategy's order, the
others react to the situation):

    fight        a visible enemy the bot has reacted to: aim and shoot
                 (ai/v2/controllers.Shooter), target picked by threat (who
                 aims at me, who I have hurt, who is closest) and kept until
                 a clearly better one shows; stand still or counter-strafe
                 to shoot, crouch at range, strafe only up close, and at
                 range step behind cover between bursts and re-peek
    fallback     out of ammo, badly hurt or flanked, with cover within a few
                 metres: back off into it facing the threat, reload there
                 (never across open ground: with no cover close the bot
                 fights instead)
    reload       no enemy in sight and not exposed to where enemies could
                 be: reload (easy bots sometimes reload in the open)
    trade        a teammate just died close by and the bot knows roughly
                 where the killer is: swing on that spot, pre-aimed
    investigate  heard, shot at or told about an enemy it cannot see:
                 holders pre-aim the corner it will come round; movers slow
                 down and do the same, the aggressive ones push carefully
    reposition   after a kill or a hit on a hold, the enemy knows where the
                 bot is: move to another spot that watches the same area
    avoid        a flashbang in the air in view: turn away; a frag landing
                 close: get out of its radius
    throw        a grenade the team ordered or the bot decided on
    task         the team's order: move (walking near danger, pre-aiming,
                 keeping spacing), hold (crosshair at head height on the
                 likeliest entry), plant, defuse, gadgets, ...

What the bot knows about enemies lives in ``self.knowledge``
(ai/v2/knowledge.py) and the team's possibility field (ai/v2/belief.py);
nothing here reads an enemy's state except through a contact that is
visible right now (ai/audit.py checks it).
"""
from __future__ import annotations

import math

import numpy as np

from panda3d.core import Point3, Vec3

from ai.aim import angles_to, wrap180
from ai.brain import Task, ThrowOrder, solve_throw, BREACH_TAGS
from ai.v2.controllers import AimPolicy, Mover, PeekHelper, Shooter, flat, teammate_in_line, EYE, RANGE_LIMIT
from ai.v2.humanize import Humanizer
from ai.v2.knowledge import BotKnowledge, Fact, fact_point
from ai.v2.pathing import FollowerV2, PENDING
from ai.v2.perception import PerceptionV2
from ai.v2.personality import traits_for
from engine.physics import MASK_SIGHT
from gameplay.lean import OFFSET as LEAN_OFFSET, clearance, right_of


class Ctx:
    """What the bot knows at a think."""
    __slots__ = ("now", "hp", "ws", "mag", "empty", "can_fight", "visible", "target", "threat", "threat_dist",
                 "point", "exposure", "mates_near", "allies", "enemies", "trade", "fact", "lost", "cover",
                 "incoming", "danger_here", "time_left", "bomb", "holding", "outranged", "stale")


class Action:
    name = "action"
    min_time = 0.4

    def __init__(self, brain):
        self.b = brain
        self.started = -1.0

    def score(self, ctx: Ctx) -> float:
        return 0.0

    def start(self, ctx: Ctx, now: float) -> None:
        self.started = now

    def stop(self) -> None:
        pass

    def run(self, dt: float, now: float) -> None:
        pass


# ---------------------------------------------------------------------- actions
class Fight(Action):
    name = "engage"
    min_time = 0.3

    def __init__(self, brain):
        super().__init__(brain)
        self.target = None
        self.hide_until = 0.0
        self.hold_until = 0.0
        self.hide_dir = None
        self.last_seen_t = -10.0
        self.strafe_dir = 1.0
        self.strafe_until = 0.0

    def score(self, ctx):
        if ctx.target is not None:
            # out of the weapon's range, or a long-range duel going nowhere: not worth standing
            # in the open for (the bot gets on with its order, or into cover)
            if ctx.outranged or ctx.stale:
                return 0.0
            if ctx.threat_dist < 12.0:
                return 1.1                       # up close there is no choice
            if ctx.hp < 35 and ctx.cover is not None and ctx.threat_dist > 15.0 and \
                    (ctx.cover[0] - self.b.bot.position()).length() < 3.2:
                return 0.9                       # hurt at range, cover a step away: take it
            return 1.0
        # keep fighting through our own un-peek: behind cover, holding the corner, re-peek
        if self.target is not None and self.target.agent.alive and ctx.now < self.hold_until + 0.4:
            return 0.95
        return 0.0

    def start(self, ctx, now):
        super().start(ctx, now)
        if ctx.target is not None:
            self.target = ctx.target

    def run(self, dt, now):
        b = self.b
        bot = b.bot
        it = bot.intent
        c = b.ctx.target if b.ctx.target is not None else self.target
        if c is None or not c.agent.alive:
            return
        if c is not self.target:
            self.target = c
        b.perception.busy_with = id(c.agent)
        if not c.seen:
            # hidden on purpose: step behind cover, hold the corner it would come round
            # (it knows where we were and may push), then re-peek - never at a rhythm
            f = Fact(id(c.agent), (c.pos.x, c.pos.y, c.pos.z), 0.5, c.time, "sight")
            bot.aim.look_at(bot.eye(), b.preaim(f, now), dt, 0.8)
            if self.hide_dir is not None and now < self.hide_until:
                it.wish = self.hide_dir
            elif self.hide_dir is not None and now < self.hold_until:
                it.crouch = b.traits["patience"] > 0.5
            elif self.hide_dir is not None:
                it.wish = -self.hide_dir                     # swing back out
            b.reload_if(0.99 if now < self.hold_until else 0.0)
            return
        self.last_seen_t = now
        firing, off, tol, use_head = b.shooter.engage(c, dt, now)
        ws = bot.weapons.inv.current()
        dist = (c.pos - bot.position()).length()
        speed = bot.char.horizontal_speed
        enemy = c.agent
        run_gun = ws is not None and ws.d.cls in ("smg", "shotgun", "knife") and dist < 9
        if ws is not None and ws.d.fire_mode == "melee":
            it.wish = b.dir_to(enemy.position())
        elif run_gun and firing:
            it.wish = b.dir_to(enemy.position()) * (1.0 if ws.d.cls == "knife" else 0.3)
        elif firing or off <= tol * 2.5:
            if speed > 1.0:
                v = bot.char.vel
                stop = Vec3(-v.x, -v.y, 0)
                stop.normalize()
                it.wish = stop
            it.crouch = ws is not None and ws.d.cls not in ("smg", "shotgun", "knife") and dist > 22 and \
                bot.profile.get("strafe", 0) >= 0.3
        else:
            it.wish = self._between_bursts(c, dist, now)
        if ws is not None and ws.d.magazine > 0 and ws.ammo == 0 and dist > 8 and self.hide_dir is None:
            # empty at range: duck out of sight to reload
            self._hide(c, now, reload=ws.d.reload_empty_time)
        bot.scoped = ws is not None and bool(ws.d.scope) and speed < 1.5
        if now - b.call_t > 1.5:
            b.call_t = now
            b.team.on_sighting(bot, c, now)

    def _hide(self, c, now: float, reload: float = 0.0) -> None:
        b = self.b
        rng = b.bot.rng
        if not reload and b.human.mistake("over_peek"):
            # stays out in the open between bursts (a mistake the easier bots make more often)
            self.hide_dir = None
            self.hold_until = now + 1.0
            return
        self.hide_dir = b.peek.hide_dir(c.agent.head_pos())
        self.hide_until = now + rng.uniform(0.35, 0.6)
        # how long to hold the corner before peeking again: a short jiggle between bursts (the
        # patient a little longer, never the same twice); a reload waits behind cover
        hold = rng.uniform(0.05, 0.35) + 0.4 * b.traits["patience"] * (1.0 - 0.6 * b.traits["risk"])
        self.hold_until = self.hide_until + max(hold, reload)

    def _between_bursts(self, c, dist: float, now: float) -> Vec3:
        b = self.b
        bot = b.bot
        # long-range duel: step behind cover between bursts, then re-peek
        if dist > 20 and now < b.shooter.pause_until - 0.15 and b.traits["risk"] < 0.75:
            if self.hide_dir is None or now > self.hold_until + 1.0:
                self._hide(c, now)
            if self.hide_dir is not None and now < self.hide_until:
                return self.hide_dir
        if dist < 11 and bot.profile.get("strafe", 0) > 0:
            if now >= self.strafe_until:
                self.strafe_until = now + bot.rng.uniform(0.35, 0.7)
                self.strafe_dir = -self.strafe_dir
            to = flat(c.pos - bot.position())
            if to.lengthSquared() > 1e-4:
                to.normalize()
                side = Vec3(-to.y, to.x, 0) * self.strafe_dir
                p = bot.position()
                if bot.nav.walkable_line(p, (p.x + side.x * 1.2, p.y + side.y * 1.2, p.z)):
                    return side
        return Vec3(0, 0, 0)

    def stop(self):
        self.b.perception.busy_with = None
        self.hide_dir = None


class FallBack(Action):
    name = "retreat"
    min_time = 0.8

    def __init__(self, brain):
        super().__init__(brain)
        self.spot = None
        self.threat = None
        self.until = 0.0

    def score(self, ctx):
        threat = ctx.threat
        if threat is None or ctx.cover is None:
            return 0.0
        b = self.b
        ws = ctx.ws
        need = 0.0
        sec = b.bot.weapons.secondary
        # running for cover across open ground under fire is worse than fighting it out
        close = (ctx.cover[0] - b.bot.position()).length() < 3.2
        if ws is not None and ws.d.magazine > 0 and ws.ammo == 0:
            pistol = sec is not None and sec is not ws and sec.ammo > 0
            if ctx.threat_dist > 25 or not pistol:
                need = 1.15
        elif ctx.hp < 35 and ctx.target is not None and ctx.threat_dist > 15.0 and close:
            need = 0.97                          # hurt at range, cover a step away
        elif ctx.hp < 30 and ctx.enemies > ctx.allies and (close or ctx.target is None):
            need = 0.97
        elif (ctx.stale or ctx.outranged) and ctx.holding:
            need = 0.9                           # out of its sight (it outguns us here), peek later
        elif ctx.visible and len(ctx.visible) >= 2 and ctx.enemies > ctx.allies and b.traits["risk"] < 0.6:
            need = 0.96
        if self.spot is not None and ctx.now < self.until:
            need = max(need, 0.9)
        return need

    def start(self, ctx, now):
        super().start(ctx, now)
        self.spot, _ = ctx.cover
        self.threat = Point3(ctx.threat)
        self.until = now + 2.5 + (self.spot - self.b.bot.position()).length() / 4.0
        self.b.say("Falling back!", key="fallback", every=6.0)

    def run(self, dt, now):
        b = self.b
        bot = b.bot
        look = self.threat + Vec3(0, 0, 1.45)
        if self.spot is None:
            b.look(look, dt, 0.8)
            b.reload_if(0.99)
            return
        d = flat(self.spot - bot.position())
        if d.length() > 0.5:
            b.mover.to(self.spot, dt, ("fallback", round(self.spot.x, 1), round(self.spot.y, 1)), walk=False,
                       look=look, run=True)
        else:
            bot.intent.crouch = True
            b.look(look, dt, 0.8)
        b.reload_if(0.99)
        if now > self.until:
            self.spot = None


class Reload(Action):
    name = "reload"
    min_time = 0.6

    def score(self, ctx):
        ws = ctx.ws
        if ws is None or ws.d.magazine <= 0 or ws.reloading or ctx.target is not None:
            return 0.0
        if ws.ammo >= ws.d.magazine or ws.reserve <= 0:
            return 0.0
        b = self.b
        safe = ctx.exposure < 0.08 and (ctx.fact is None or ctx.now - ctx.fact.time > 2.0)
        if ctx.mag < 0.25:
            return 0.92 if safe or b.human.mistake("reload_open") else 0.6
        if safe and ctx.mag < 0.9:
            return 0.45 + 0.3 * (1.0 - ctx.mag)
        return 0.0

    def run(self, dt, now):
        b = self.b
        ws = b.bot.weapons.inv.current()
        if ws is not None and not ws.reloading and ws.ammo < ws.d.magazine:
            ws.start_reload(now)
        b.actions["task"].run(dt, now)          # keep doing the order while the magazine goes in


class Trade(Action):
    name = "trade"
    min_time = 0.6

    def __init__(self, brain):
        super().__init__(brain)
        self.fact = None
        self.spot = None
        self.until = 0.0

    def score(self, ctx):
        tr = ctx.trade
        if tr is None or ctx.target is not None:
            return 0.0
        b = self.b
        if b.task.kind in ("plant", "defuse"):
            return 0.0                           # the carrier / defuser keeps to the objective
        if ctx.mag < 0.2 or ctx.hp < 20:
            return 0.0
        if self.fact is not None and ctx.now < self.until:
            return 0.9
        return 0.84 + 0.12 * b.traits["teamwork"]

    def start(self, ctx, now):
        super().start(ctx, now)
        self.fact, self.spot = ctx.trade
        late = self.b.human.mistake("late_trade")
        self.until = now + 2.6 + (0.9 if late else 0.0)
        self.wait = now + (0.9 if late else 0.0)
        if not late:
            self.b.say("I'll trade you!", key="trade", every=8.0)

    def run(self, dt, now):
        b = self.b
        bot = b.bot
        if b.ctx.trade is not None:
            self.fact, self.spot = b.ctx.trade         # what the bot knows may have sharpened
        if self.fact is None:
            return
        pt = fact_point(self.fact)
        if now < self.wait:
            b.look(pt, dt, 0.9)
            return
        if b.peek.exposed_to(pt):
            b.look(pt, dt, 1.0)
            if b.precise(self.fact, now) and b.can_prefire():
                b.shooter.prefire(pt, self.fact, dt, now)
            return
        # swing from where the teammate died: the killer was in sight from there
        goal = self.spot
        if (goal - bot.position()).length() < 1.0:
            goal = Point3(*self.fact.pos)
        b.mover.to(goal, dt, ("trade", round(goal.x), round(goal.y)), walk=False, look=pt, run=True)


class Investigate(Action):
    name = "alert"
    min_time = 0.5

    def __init__(self, brain):
        super().__init__(brain)
        self.key = None
        self.hold_until = 0.0

    def score(self, ctx):
        if ctx.target is not None:
            return 0.0
        f = ctx.fact or ctx.lost
        if f is None:
            return 0.0
        if self.b.swinging(ctx.now):
            return 0.95                          # our flash is popping: go now
        age = ctx.now - f.time
        if age > 6.0:
            return 0.0
        return 0.55 + 0.25 * max(0.0, 1.0 - age / 4.0) + (0.1 if ctx.holding else 0.0)

    def run(self, dt, now):
        b = self.b
        bot = b.bot
        f = b.ctx.fact or b.ctx.lost
        if f is None:
            b.actions["task"].run(dt, now)
            return
        look = b.preaim(f, now)
        t = b.task
        if b.swinging(now):
            # swing wide onto the corner while they are blind
            goal = Point3(*b.swing[0].pos)
            b.mover.to(goal, dt, ("swing", round(goal.x), round(goal.y)), walk=False, look=look, run=True)
            return
        if b.ctx.holding:
            bot.intent.crouch = t.crouch
            b.look(look, dt, 0.8)
            return
        key = (round(f.pos[0] / 6.0), round(f.pos[1] / 6.0))
        if key != self.key:
            # new information close by: stop and hold the angle for a moment (the patient ones
            # longer); a distant or vague callout only turns the crosshair
            self.key = key
            d = math.hypot(f.pos[0] - bot.position().x, f.pos[1] - bot.position().y)
            near = d < (12.0 if f.source == "radio" else 20.0)
            self.hold_until = now + (bot.rng.uniform(0.6, 1.2) + 1.0 * b.traits["patience"] if near else 0.0)
        pushing = b.traits["aggression"] > 0.6 and f.source in ("sound", "radio") and b.ctx.allies >= b.ctx.enemies
        if pushing and (Point3(*f.pos) - bot.position()).length() > 6.0:
            goal = Point3(*f.pos)
            b.mover.to(goal, dt, ("push", round(goal.x), round(goal.y)), walk=True, look=look)
            return
        hurry = t.tag in ("rush", "retake", "flee", "plant") or (b.ctx.time_left or 99.0) < 30.0
        if t.kind != "idle" and not b.arrived and (now >= self.hold_until or hurry):
            b.actions["task"].run(dt, now, look_override=look)     # carry on, crosshair on the threat
            bot.intent.walk = not hurry
            return
        b.look(look, dt, 0.8)
        bot.intent.walk = True


class Reposition(Action):
    name = "reposition"
    min_time = 1.0

    def __init__(self, brain):
        super().__init__(brain)
        self.spot = None
        self.reason_t = -10.0

    def score(self, ctx):
        b = self.b
        if (ctx.target is not None and not (ctx.stale or ctx.outranged)) or b.reposition_t < 0 or \
                ctx.now - b.reposition_t > 6.0:
            return 0.0
        if self.spot is not None:
            return 0.8
        if not ctx.holding:
            return 0.0
        return 0.74 - 0.2 * b.traits["aggression"]

    def start(self, ctx, now):
        super().start(ctx, now)
        self.spot = self.b.new_hold_spot()
        if self.spot is None:
            self.b.reposition_t = -1.0

    def run(self, dt, now):
        b = self.b
        if self.spot is None:
            b.reposition_t = -1.0
            return
        if b.mover.to(self.spot, dt, ("repos", round(self.spot.x, 1), round(self.spot.y, 1))):
            t = b.task
            t.pos = Point3(self.spot)
            b.arrived = True
            b.arrive_t = now
            self.spot = None
            b.reposition_t = -1.0


class Avoid(Action):
    name = "avoid"
    min_time = 0.2

    def score(self, ctx):
        g = ctx.incoming
        if g is None:
            return 0.0
        return 1.25 if g[0] == "flash" else 1.1

    def run(self, dt, now):
        b = self.b
        bot = b.bot
        g = b.ctx.incoming
        if g is None:
            return
        kind, pos = g
        if kind == "flash":
            # look away: turn the back to the grenade
            eye = bot.eye()
            away = Point3(eye.x - (pos.x - eye.x), eye.y - (pos.y - eye.y), eye.z)
            b.look(away, dt, 1.0)
        else:
            d = flat(bot.position() - pos)
            if d.lengthSquared() > 1e-4:
                d.normalize()
                bot.intent.wish = d


class Throw(Action):
    name = "throw"
    min_time = 0.3

    def score(self, ctx):
        if self.b.throw is None or ctx.target is not None:
            return 0.0
        return 0.9

    def run(self, dt, now):
        if not self.b.do_throw(dt, now):
            self.b.throw = None


class DoTask(Action):
    name = "task"
    min_time = 0.0

    def score(self, ctx):
        b = self.b
        if b.committed_objective(ctx.now):
            return 1.3
        kind = b.task.kind
        if kind in ("plant", "defuse") and ctx.target is None:
            # the objective comes first when nobody is in sight: in the site (or at the
            # charge) nothing but a fight or a grenade at the feet beats it
            here = b.director.site_at(b.bot.position()) is not None if kind == "plant" else \
                (b.director.bomb.pos - b.bot.position()).length() < 6.0
            return 0.97 if here else 0.7
        return 0.5

    def run(self, dt, now, look_override: Point3 | None = None):
        b = self.b
        c = b.ctx.target
        if look_override is None and c is not None and c.seen and (b.arrived or b.task.pos is None):
            look_override = Point3(c.pos.x, c.pos.y, c.pos.z + 1.45)     # keep an eye on it
        b.run_task(dt, now, look_override)


ACTIONS = (Fight, FallBack, Reload, Trade, Investigate, Reposition, Avoid, Throw, DoTask)


# ------------------------------------------------------------------------ brain
class BrainV2:
    ai = "v2"

    def __init__(self, bot, team):
        from ai.bot import load_bot_config
        self.bot = bot
        self.team = team
        self.director = team.director
        cfg = load_bot_config()
        self.cfg = cfg.get("v2", {})
        self.rng = bot.rng
        self.traits = traits_for(bot.name, self.director.seed)
        self.human = Humanizer(bot, self.cfg.get("humanize"))
        self.knowledge = BotKnowledge(bot)
        self.perception = PerceptionV2(bot, cfg.get("vision", {}), bot.profile, self.human, self.knowledge)
        bot.perception = self.perception
        # ``team`` is the director's proxy for the bot's *current* side: its path service and
        # field change at halftime, so they are looked up through it, never kept
        self.follower = FollowerV2(bot.nav, team.paths)
        self.bias_key = 1 + (bot.rng.randrange(3))
        self.follower.bias_key = self.bias_key
        self.tm = team.tm
        self.mover = Mover(self)
        self.aim_policy = AimPolicy(self)
        self.shooter = Shooter(self)
        self.peek = PeekHelper(self)
        self.actions = {a.name: a for a in (cls(self) for cls in ACTIONS)}
        self.task = Task()
        self.reset()

    # --------------------------------------------------------------- state
    def reset(self) -> None:
        self.task = Task()
        self.mode = "task"
        self.action = self.actions["task"]
        self.action_t = 0.0
        self.scores: list[tuple[str, float]] = []
        self.next_think = 0.0
        self.target = None
        self.ctx = Ctx()
        for k in Ctx.__slots__:
            setattr(self.ctx, k, None)
        self.ctx.visible = []
        self.arrived = False
        self.arrive_t = 0.0
        self.plant_t = 0.0
        self.defuse_t = 0.0
        self.use_t = 0.0
        self.lean = 0.0
        self.lean_check = 0.0
        self.gadget_target = None
        self.deferred = None
        self.throw: ThrowOrder | None = None
        self.flashed_until = 0.0
        self.last_hit_t = -10.0
        self.call_t = -10.0
        self.radar_t = 0.0
        self.reposition_t = -1.0
        self.kill_t = -10.0
        self.dealt_t = -10.0
        self.ignore: dict[int, float] = {}
        self.nade_t = 0.0
        self.swing = None
        self.frozen_until = 0.0
        self.debug = ""
        self.knowledge.reset()
        self.human.reset()
        self.shooter.reset()
        self.mover.reset()
        self.aim_policy.reset()
        self.perception.busy_with = None
        for a in self.actions.values():
            a.started = -1.0
        self._preaim_key = None
        self._preaim_pt = None
        self._preaim_until = 0.0
        self._danger_cache = (None, 0.0, 0.0)

    # -------------------------------------------------------------- orders
    def set_task(self, task: Task, force: bool = False) -> None:
        if not force and self.task.tag in BREACH_TAGS and task.tag not in BREACH_TAGS:
            self.deferred = task
            return
        task.issued = self.bot.now
        self.task = task
        self.mover.goal_key = None
        self.arrived = False
        self.plant_t = 0.0
        self.defuse_t = 0.0
        self.use_t = 0.0

    def next_task(self) -> None:
        nxt = self.task.then
        if callable(nxt):
            nxt = nxt(self.bot)
        if self.deferred is not None and not (isinstance(nxt, Task) and nxt.tag in BREACH_TAGS):
            nxt, self.deferred = self.deferred, None
        self.set_task(nxt if isinstance(nxt, Task) else Task("idle"), force=True)

    def order_throw(self, key: str, target: Point3, within: float = 6.0) -> bool:
        if self.bot.weapons.inv.grenades.get(key, 0) <= 0:
            return False
        self.throw = ThrowOrder(key, Point3(target), self.bot.now + within)
        return True

    @property
    def paths(self):
        return self.team.paths

    @property
    def follower_active(self) -> bool:
        return self.follower.active

    def describe(self) -> str:
        t = self.task
        top = " ".join(f"{n}:{s:.2f}" for n, s in self.scores[:3])
        role = self.team.role_of(self.bot)
        return f"{self.mode}/{t.kind}{':' + t.tag if t.tag else ''} [{role}] {top} {self.debug}"

    def info(self) -> list[str]:
        """Console ``botinfo <name>``: everything behind the current decision."""
        bot = self.bot
        now = bot.now
        team = self.team
        t = self.task
        out = [f"{bot.name} [{bot.side}, v2, {bot.difficulty}] role {team.role_of(bot) or '-'}  "
               f"plan {getattr(team, 'plan', '') or getattr(team, 'setup', '') or '-'} {getattr(team, 'site', '')}",
               "traits " + "  ".join(f"{k} {v:.2f}" for k, v in self.traits.items()),
               f"stress {self.human.stress:.2f}  action {self.mode} ("
               + ", ".join(f"{n} {s:.2f}" for n, s in self.scores[:3]) + ")",
               f"task {t.kind}{':' + t.tag if t.tag else ''} at "
               + (f"({t.pos.x:.1f}, {t.pos.y:.1f})" if t.pos is not None else "-")
               + (" arrived" if self.arrived else "") + (f", throw {self.throw.key}" if self.throw else "")]
        a = self.action
        spot = getattr(a, "spot", None)
        if spot is not None:
            out.append(f"{a.name} spot ({spot.x:.1f}, {spot.y:.1f})")
        c = self.ctx
        if c.target is not None:
            out.append(f"target {c.target.agent.name} at {c.threat_dist:.0f} m"
                       + (" (out of range)" if c.outranged else ""))
        for label, f in (("fact", c.fact), ("lost", c.lost), ("trade", c.trade[0] if c.trade else None)):
            if f is not None:
                out.append(f"{label}: {f.source} {now - f.time:.1f}s ago r{f.radius:.1f} "
                           f"at ({f.pos[0]:.1f}, {f.pos[1]:.1f}) {f.area}")
        out.append(f"exposure {c.exposure or 0.0:.2f}  danger within 15 m {c.danger_here or 0.0:.2f}  "
                   f"knows {len(self.knowledge.facts)} enemies, {len(self.knowledge.anonymous)} unknown")
        return out

    # -------------------------------------------------------------- events
    def on_damaged(self, res) -> None:
        bot = self.bot
        now = bot.now
        attacker = self.director.agent_of(res.info.attacker)
        if attacker is None or attacker.side == bot.side:
            return
        self.last_hit_t = now
        self.human.add_stress(0.35)
        c = self.perception.contacts.get(id(attacker))
        if c is None or not c.seen:
            if res.info.kind != "explosion":
                self.knowledge.on_shot(res.info.direction, now)
            if self.human.mistake("panic"):
                self.frozen_until = now + self.rng.uniform(0.3, 0.7)     # flinches before it acts
            if self.ctx.holding:
                self.reposition_t = now
        self.next_think = 0.0

    def on_spotted(self, enemy) -> None:
        self.next_think = min(self.next_think, self.bot.now + 0.02)

    def on_heard(self, enemy, pos, loudness: float) -> None:
        if self.action.name in ("task", "hold"):
            self.next_think = min(self.next_think, self.bot.now + 0.05)

    def on_flashed(self, duration: float) -> None:
        self.flashed_until = self.bot.now + duration
        self.human.add_stress(0.3)
        self.bot.aim.release()

    def on_fired(self) -> None:
        self.shooter.on_fired()

    def on_hits(self, results) -> None:
        """Hit markers: this bot's shot hurt someone - the enemy it was shooting at."""
        key = self.shooter.target_id
        if key is None:
            return
        self.dealt_t = self.bot.now
        for r in results:
            self.knowledge.on_hit_enemy(key, r.info.amount)

    def on_kill(self, victim) -> None:
        """This bot killed an enemy (the director's kill event)."""
        self.kill_t = self.bot.now
        self.knowledge.forget(id(victim))
        if self.ctx.holding:
            self.reposition_t = self.bot.now

    # ---------------------------------------------------------------- tick
    def update(self, dt: float, now: float, locked: bool) -> None:
        bot = self.bot
        it = bot.intent
        it.wish = Vec3(0, 0, 0)
        it.walk = it.crouch = it.jump = False
        it.trigger = False
        self.team.tick(now)
        if self.follower.service is not self.team.paths:
            self.follower.service = self.team.paths          # sides swapped at halftime
        self.perception.update(now, self.team.enemies())
        self.human.update(dt)
        if locked:
            self._locked_look(dt, now)
            return
        if now >= self.next_think:
            self.next_think = now + self.rng.uniform(0.11, 0.16)
            self.think(now)
        if now < self.flashed_until:
            self._flashed(dt, now)
            return
        if now < self.frozen_until:
            return                                   # panicked for a moment
        self.action.run(dt, now)
        if it.wish.lengthSquared() < 0.01:
            self._personal_space()
        self._shoot_gadget(dt, now)
        self._choose_lean(now)

    # --------------------------------------------------------------- think
    def think(self, now: float) -> None:
        ctx = self._context(now)
        self.ctx = ctx
        noise = self.human.noise()
        scores = []
        for name, a in self.actions.items():
            s = a.score(ctx)
            if s <= 0.0:
                continue
            if a is self.action:
                s += 0.08 if now - a.started >= a.min_time else 0.25
            elif name not in ("engage", "avoid", "task"):
                s += self.rng.gauss(0.0, noise * 0.05)
            scores.append((name, s))
        scores.sort(key=lambda x: -x[1])
        self.scores = scores
        best = self.actions[scores[0][0]] if scores else self.actions["task"]
        if best is not self.action:
            self.action.stop()
            self.action = best
            best.start(ctx, now)
        self.mode = best.name
        self.target = ctx.target
        if self.shooter.target_id is not None and best.name != "engage":
            self.shooter.release()
        self._comms(ctx, now)
        self._consider_utility(ctx, now)

    def _consider_utility(self, ctx, now: float) -> None:
        """The bot's own grenades: a pop-flash over the corner a fresh fact puts an enemy behind
        (then swing while it is blind), or a frag onto an enemy that is not moving off."""
        if self.throw is not None or ctx.target is not None or now < self.nade_t:
            return
        f = ctx.fact
        if f is None or f.source == "damage" or now - f.time > 2.5 or f.radius > 5.0:
            return
        bot = self.bot
        pos = bot.position()
        d = math.hypot(f.pos[0] - pos.x, f.pos[1] - pos.y)
        if d < 6.0 or d > 30.0:
            return
        self.nade_t = now + 2.5
        if bot.game.physics.ray_cast(bot.eye(), fact_point(f, 1.4), MASK_SIGHT) is None:
            return                                   # in plain view: that is for the gun
        if self.team.recent_nade(f.pos, now):
            return                                   # a teammate just threw there
        inv = bot.weapons.inv.grenades
        u = self.traits["utility"]
        if inv.get("flash", 0) > 0 and self.rng.random() < 0.3 + 0.5 * u and not self.human.mistake("skip_corner"):
            if self.order_throw("flash", Point3(f.pos[0], f.pos[1], f.pos[2] + 2.2), 2.0):
                self.swing = (f, None)
                self.team.nade_at(f.pos, now)
                self.say("Flashing!", key="popflash", every=4.0)
        elif inv.get("frag", 0) > 0 and d > 9.0 and self.rng.random() < 0.25 + 0.5 * u:
            if self.order_throw("frag", Point3(f.pos[0], f.pos[1], f.pos[2] + 0.3), 2.0):
                self.team.nade_at(f.pos, now)

    def swinging(self, now: float) -> bool:
        """Our own flash just popped (or is about to) over the corner: time to peek."""
        sw = self.swing
        if sw is None or sw[1] is None:
            return False
        if now > sw[1] + 3.2:
            self.swing = None
            return False
        return now >= sw[1] + 1.3

    def _context(self, now: float) -> Ctx:
        bot = self.bot
        ctx = Ctx()
        ctx.now = now
        ctx.hp = bot.damageable.health
        ws = bot.weapons.inv.current()
        ctx.ws = ws
        if ws is not None and ws.d.magazine > 0:
            ctx.mag = ws.ammo / ws.d.magazine
            ctx.empty = ws.ammo == 0
        else:
            ctx.mag, ctx.empty = 1.0, False
        per = self.perception
        seen = self.knowledge.from_perception(now)
        for f in seen:
            self.team.radar_report(f)
        for f in self.knowledge.pop_heard():
            self.team.on_heard(bot, f, now)
        vis = [c for c in per.contacts.values() if c.seen and c.agent.alive and now >= c.react_at]
        ctx.visible = vis
        ctx.target = self._pick_target(vis, now)
        pos = bot.position()
        ctx.lost = None
        if ctx.target is None:
            recent = [c for c in per.contacts.values() if c.agent.alive and not c.seen and c.source == "sight"
                      and now - c.time < 3.0]
            if recent:
                c = max(recent, key=lambda c: c.time)
                ctx.lost = Fact(id(c.agent), (c.pos.x, c.pos.y, c.pos.z), 0.5, c.time, "sight")
        if ctx.target is not None:
            ctx.threat = Point3(ctx.target.pos)
        elif ctx.lost is not None:
            ctx.threat = Point3(*ctx.lost.pos)
        else:
            ctx.threat = None
        ctx.threat_dist = (ctx.threat - pos).length() if ctx.threat is not None else 99.0
        ctx.time_left = self.director.match.round_time_left()
        ctx.outranged = False
        if ctx.target is not None and ws is not None:
            limit = RANGE_LIMIT.get(ws.d.cls)
            if limit is not None and ctx.threat_dist > limit and now - self.last_hit_t > 2.0:
                ctx.outranged = True
        ctx.stale = self._stale(ctx, now)
        if ctx.outranged and self.ctx.holding and self.reposition_t < 0:
            self.reposition_t = now              # seen from beyond our gun's reach: move off this spot
        ctx.point = self.tm.nearest(pos)
        team = self.team
        ctx.allies = len(team.alive_bots()) + team.humans_alive()
        ctx.enemies = team.enemies_alive
        ctx.exposure = self.exposure(ctx.point)
        ctx.danger_here = self.danger_near(pos, 15.0)
        # radar glance (never mid-fight)
        if ctx.target is None and now >= self.radar_t:
            self.radar_t = now + self.human.radar_interval()
            self.knowledge.radar(now, team.reports[-30:])
        ctx.fact = self._relevant_fact(now, pos)
        ctx.trade = team.trade_fact(bot, now)
        ctx.cover = self.find_cover(ctx.threat) if ctx.threat is not None else None
        ctx.incoming = self._incoming(now)
        ctx.bomb = self.director.bomb.state
        t = self.task
        ctx.holding = t.kind in ("hold", "guard") and self.arrived
        ctx.mates_near = None
        return ctx

    def _stale(self, ctx, now: float) -> bool:
        """A visible enemy not worth fighting right now: a long-range duel in which neither
        side lands anything (left alone for a few seconds), or one far off while the bot
        has a charge to plant in little time."""
        c = ctx.target
        if c is None or ctx.outranged:
            return False
        key = id(c.agent)
        d = ctx.threat_dist
        if now - self.last_hit_t < 2.0:
            return False                         # being shot: fight back (or take close cover)
        if now < self.ignore.get(key, 0.0) and d > 18.0:
            return True
        engaged = now - self.shooter.engaged_since if self.shooter.target_id == key else 0.0
        if d > 25.0 and engaged > 3.0 and now - self.dealt_t > 3.0:
            self.ignore[key] = now + self.rng.uniform(4.0, 7.0)
            if self.ctx.holding or self.task.wait:
                self.reposition_t = now
            return True
        if self.task.kind == "plant" and d > 15.0 and ctx.time_left < 35.0:
            return True
        return False

    def _pick_target(self, vis, now: float):
        if not vis:
            return None
        bot = self.bot
        me = bot.position()
        best, best_s = None, -1.0
        for c in vis:
            d = max((c.pos - me).length(), 1.0)
            s = 10.0 / (d + 6.0)
            e = c.agent
            fwd = e.forward() if hasattr(e, "forward") else None
            if fwd is not None:
                to_me = flat(me - c.pos)
                if to_me.lengthSquared() > 1e-4:
                    to_me.normalize()
                    if fwd.dot(to_me) > 0.85:
                        s *= 1.8                       # it is aiming at me
            if self.knowledge.hurt(id(e)):
                s *= 1.3
            if id(e) == self.shooter.target_id:
                s *= 1.6                               # stay on the current target
            if s > best_s:
                best, best_s = c, s
        return best

    def _relevant_fact(self, now: float, pos: Point3):
        best, bs = None, 0.0
        for f in self.knowledge.recent(6.0, now):
            if f.source == "sight":
                continue
            d = math.hypot(f.pos[0] - pos.x, f.pos[1] - pos.y)
            if d > 35.0:
                continue
            s = (1.0 - (now - f.time) / 6.0) * (1.0 / (1.0 + d / 15.0))
            if s > bs:
                best, bs = f, s
        return best

    def _incoming(self, now: float):
        """A grenade in flight the bot can see: (kind, position) or None."""
        bot = self.bot
        eye = bot.eye()
        fwd = bot.view_dir()
        phys = bot.game.physics
        for g in bot.game.grenades:
            if g.detonated or g.done or g.d.key not in ("flash", "frag"):
                continue
            p = g.pos
            v = p - eye
            dist = v.length()
            if dist > 22.0 or dist < 0.3:
                continue
            if v.dot(fwd) / dist < 0.35:
                continue
            if phys.ray_cast(eye, p, MASK_SIGHT) is not None:
                continue
            if g.d.key == "flash" and g.age > 0.5:
                return ("flash", Point3(p))
            if g.d.key == "frag" and dist < 7.0 and g.age > 0.4:
                return ("frag", Point3(p))
        return None

    def _comms(self, ctx, now: float) -> None:
        if ctx.target is not None and now - self.call_t > 1.5:
            self.call_t = now
            self.team.on_sighting(self.bot, ctx.target, now)

    # ---------------------------------------------------------- knowledge
    def exposure(self, point: int) -> float:
        """Expected enemies that could see this point right now (team possibility field)."""
        if point < 0:
            return 0.0
        danger = self.team.danger()
        vis = self.tm.visible_mask(point)
        return float(danger[vis].sum())

    def danger_near(self, pos: Point3, radius: float) -> float:
        key, t, val = self._danger_cache
        k = (round(pos.x), round(pos.y), round(radius))
        if key == k and self.bot.now - t < 0.3:
            return val
        idx = self.tm.points_near((pos.x, pos.y, pos.z), radius)
        val = float(self.team.danger()[idx].sum()) if len(idx) else 0.0
        self._danger_cache = (k, self.bot.now, val)
        return val

    def should_walk(self, pos: Point3) -> bool:
        """Walk (quiet, accurate) close to where enemies probably are: a fresh fact within 20 m,
        or a real chance of an enemy within 15 m; run when time is short or rushing."""
        t = self.task
        if t.tag in ("rush", "flee", "retake") or self.team.plan == "rush":
            return False
        left = self.ctx.time_left
        if left is not None and left < 30.0:
            return False
        now = self.bot.now
        for f in self.knowledge.recent(4.0, now):
            if f.source != "damage" and math.hypot(f.pos[0] - pos.x, f.pos[1] - pos.y) < 20.0:
                return True
        # (measured: the danger within 15 m is about 0.05 typically and over 0.3 in the
        # hottest tenth of the places attackers go)
        return self.danger_near(pos, 15.0) > 0.08 + 0.15 * self.traits["aggression"]

    def likely_point(self, pos: Point3, fwd: Vec3 | None, max_range: float, cone: float, second: bool = False,
                     min_weight: float = 0.0, focus: Point3 | None = None):
        """Head-height point where an enemy would most likely come into view: the visible
        corners and doorways (tactical points next to hidden ones), weighted by how many
        enemies the team's picture puts in the hidden places behind them - crosshair placement.
        Recent callouts and sounds count extra."""
        tm = self.tm
        i = tm.nearest(pos)
        if i < 0:
            return None
        danger = self.team.danger()
        now = self.bot.now
        extra = None
        for f in self.knowledge.recent(5.0, now):
            if f.source in ("sound", "radio", "radar", "intel"):
                j = tm.nearest(f.pos)
                if j >= 0:
                    if extra is None:
                        extra = danger.copy()
                    extra[j] += 2.0
        if extra is not None:
            danger = extra
        vis = tm.visible_mask(i)
        src, dst = tm.edge_src, tm.edge_dst
        edge = vis[src] & ~vis[dst]                  # from a visible point into a hidden one
        w = np.zeros(tm.n)
        np.add.at(w, src[edge], danger[dst[edge]])
        w += danger * vis                            # (enemies possibly in view already)
        xy = tm._xy
        dx = xy[:, 0] - pos.x
        dy = xy[:, 1] - pos.y
        dist = (dx * dx + dy * dy) ** 0.5
        w *= (dist <= max_range) * (dist > 1.5) / (1.0 + dist / 20.0)
        if focus is not None:
            # the entry this spot was picked to watch: corners near it count several times over
            fd = np.hypot(xy[:, 0] - focus.x, xy[:, 1] - focus.y)
            w *= 1.0 + 3.0 * np.exp(-fd / 4.0)
            w += 0.02 * vis * np.exp(-fd / 2.0)          # and with no danger anywhere, it is the angle
        if fwd is not None and cone < 360.0:
            cosang = (dx * fwd.x + dy * fwd.y) / (dist + 1e-6)
            w = w * (cosang >= math.cos(math.radians(cone * 0.5)))
        if not w.any() or w.max() < min_weight:
            return None
        order = w.argsort()[::-1]
        p1 = tm.pos[order[0]]
        a = Point3(float(p1[0]), float(p1[1]), float(p1[2]) + 1.55)
        if not second:
            return a
        if len(order) > 1 and w[order[1]] > w[order[0]] * 0.6:
            p2 = tm.pos[order[1]]
            return a, Point3(float(p2[0]), float(p2[1]), float(p2[2]) + 1.55)
        return a, None

    def preaim(self, f: Fact, now: float) -> Point3:
        """Where an enemy at fact f will come into view: the last visible corner of
        the walk towards it (a doorway, a wall edge), at head height."""
        key = (round(f.pos[0]), round(f.pos[1]))
        if key == self._preaim_key and now < self._preaim_until:
            return self._preaim_pt
        bot = self.bot
        eye = bot.eye()
        phys = bot.game.physics
        point = fact_point(f, 1.55)
        if phys.ray_cast(eye, point, MASK_SIGHT) is not None:
            path = self.paths.request(bot.position(), f.pos, 0)
            if path is not None and path != PENDING:
                for corner in path[1:]:
                    q = Point3(corner[0], corner[1], corner[2] + 1.55)
                    if phys.ray_cast(eye, q, MASK_SIGHT) is not None:
                        break
                    point = q
        self._preaim_key, self._preaim_pt, self._preaim_until = key, point, now + 0.8
        return point

    def find_cover(self, threat: Point3):
        """Nearest walkable tactical point within 6 m that the threat cannot see
        (standing), checked with a real ray; (point, index) or None."""
        tm = self.tm
        pos = self.bot.position()
        ti = tm.nearest(threat)
        if ti < 0:
            return None
        near = tm.points_near((pos.x, pos.y, pos.z), 6.0, max_dz=1.0)
        if len(near) == 0:
            return None
        seen_by = tm.visible_mask(ti)
        cand = [int(i) for i in near if not seen_by[i]]
        if not cand:
            return None
        cand.sort(key=lambda i: math.hypot(float(tm.pos[i][0]) - pos.x, float(tm.pos[i][1]) - pos.y))
        phys = self.bot.game.physics
        teye = Point3(threat.x, threat.y, threat.z + 1.6)
        for i in cand[:4]:
            p = tm.pos[i]
            q = Point3(float(p[0]), float(p[1]), float(p[2]) + 1.4)
            if phys.ray_cast(teye, q, MASK_SIGHT) is not None:
                return Point3(float(p[0]), float(p[1]), float(p[2])), i
        return None

    def new_hold_spot(self) -> Point3 | None:
        """Another place watching the same area as the current hold (repositioning)."""
        tm = self.tm
        t = self.task
        if t.pos is None:
            return None
        here = tm.nearest(t.pos)
        if here < 0:
            return None
        site = tm.site_name(here)
        opts = [h["i"] for h in tm.holds(site)] if site else []
        if not opts:
            opts = [int(i) for i in tm.points_near((t.pos.x, t.pos.y, t.pos.z), 9.0) if tm.cover[i]]
        mates = [m.brain.task.pos for m in self.team.mates_of(self.bot) if m.brain.task.pos is not None]
        best, bs = None, -1e9
        for i in opts:
            p = tm.pos[i]
            d = math.hypot(float(p[0]) - t.pos.x, float(p[1]) - t.pos.y)
            if d < 3.0 or d > 14.0:
                continue
            if any(math.hypot(float(p[0]) - m.x, float(p[1]) - m.y) < 3.0 for m in mates):
                continue
            s = -d * 0.2 + self.rng.random()
            if s > bs:
                best, bs = Point3(float(p[0]), float(p[1]), float(p[2])), s
        return best

    def precise(self, f: Fact, now: float) -> bool:
        return f.radius <= 2.5 and now - f.time <= 1.6

    def can_prefire(self) -> bool:
        diff = self.bot.difficulty
        return diff in ("hard", "expert") or (diff == "normal" and self.traits["aggression"] > 0.5)

    # ------------------------------------------------------------ helpers
    def look(self, point: Point3, dt: float, speed: float = 0.7) -> None:
        self.bot.aim.look_at(self.bot.eye(), point, dt, speed)

    def dir_to(self, p: Point3) -> Vec3:
        d = flat(p - self.bot.position())
        return d.normalized() if d.lengthSquared() > 1e-4 else Vec3(0, 0, 0)

    def target_distance(self) -> float:
        c = self.ctx.target if self.ctx is not None else None
        if c is None:
            return 15.0
        return (c.pos - self.bot.position()).length()

    def teammate_in_line(self, eye: Point3, point: Point3) -> bool:
        return teammate_in_line(self.bot, self.director, eye, point)

    def at_door(self, pos) -> bool:
        i = self.tm.nearest(pos)
        if i < 0 or not (int(self.tm.kind[i]) & 1):
            return False
        p = self.tm.pos[i]
        return math.hypot(float(p[0]) - pos[0], float(p[1]) - pos[1]) < 1.2

    def say(self, text: str, key: str | None = None, every: float = 4.0) -> None:
        self.team.radio_say(self.bot, text, key=key, every=every)

    def reload_if(self, frac: float) -> None:
        ws = self.bot.weapons.inv.current()
        if ws is not None and ws.d.magazine > 0 and ws.ammo < ws.d.magazine * frac and not ws.reloading \
                and ws.reserve > 0:
            ws.start_reload(self.bot.now)

    def ensure_weapon(self, dist: float) -> None:
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
        if ws is prim and ws.ammo == 0 and usable(sec) and sec.ammo > 0 and dist < 25:
            w.select("secondary")                # faster than a reload with an enemy in view
        elif ws.ammo == 0 and ws.reserve == 0:
            if ws is prim and usable(sec):
                w.select("secondary")
            elif not usable(prim) and not usable(sec):
                w.select("melee")

    def committed_objective(self, now: float) -> bool:
        timers = self.director.rules["timers"]
        if self.task.kind == "plant" and self.plant_t > float(timers["plant_time"]) * 0.75:
            return True
        if self.task.kind == "defuse" and self.defuse_t > 0.0:
            total = float(timers["defuse_time_kit" if self.bot.has_kit else "defuse_time"])
            left = self.director.match.bomb_time_left()
            return left < total - self.defuse_t + 1.5 or self.defuse_t > total * 0.8
        return False

    # -------------------------------------------------------------- task
    def run_task(self, dt: float, now: float, look_override: Point3 | None = None) -> None:
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
        if t.timeout and now - t.issued > t.timeout:
            self.next_task()
            return
        if t.pos is None:
            self._idle_look(dt, now)
            self.reload_if(0.6)
            return
        if not self.arrived:
            walk = True if t.walk else None
            if t.walk_near > 0 and (t.pos - bot.position()).length() < t.walk_near:
                walk = True
            run = t.tag in ("rush", "flee", "retake") or self.ctx.time_left is not None and self.ctx.time_left < 20
            done = self.mover.to(t.pos, dt, ("task", id(t)), walk=walk, via=t.via, look=look_override, run=run)
            if done:
                self.arrived = True
                self.arrive_t = now
                self.team.on_arrived(bot, t)
            return
        it = bot.intent
        it.crouch = t.crouch
        d = flat(t.pos - bot.position())
        if d.length() > 1.2:
            self.arrived = False
            self.mover.goal_key = None
            return
        if kind == "use" and t.look is not None:
            rest = bot.aim.look_at(bot.eye(), t.look, dt, 0.8)
            if rest < 2.5:
                self.use_t += dt
                if self.use_t >= t.action_time:
                    self.use_t = 0.0
                    if t.action is None or t.action(bot):
                        self.next_task()
            return
        if t.until is not None and t.until(bot):
            self.next_task()
            return
        if look_override is not None:
            self.look(look_override, dt, 0.8)
        else:
            look = self.aim_policy.hold_point(bot.position(), t.yaw, t.look)
            self.look(look, dt, 0.7)
        if kind in ("hunt", "pickup") and now - self.arrive_t > 1.0:
            self.team.task_done(bot, t)

    def _idle_look(self, dt: float, now: float) -> None:
        bot = self.bot
        look = self.aim_policy.hold_point(bot.position(), bot.aim.yaw, None)
        self.look(look, dt, 0.35)

    def _locked_look(self, dt: float, now: float) -> None:
        """Freeze time / waiting at spawn: look around a bit."""
        bot = self.bot
        if now >= self.aim_policy._next:
            self.aim_policy._next = now + self.rng.uniform(1.0, 2.5)
            self._locked_yaw = wrap180(bot.aim.yaw + self.rng.uniform(-50, 50))
        bot.aim.turn_towards(getattr(self, "_locked_yaw", bot.aim.yaw), 0.0, dt, 0.3)

    def _do_plant(self, dt: float, now: float) -> None:
        bot = self.bot
        t = self.task
        d = self.director
        if not bot.weapons.inv.has_bomb:
            self.team.task_done(bot, t)
            return
        site = d.site_at(bot.position())
        if not self.arrived:
            walk = False if t.tag == "rush" or self.team.plan == "rush" else None
            if self.mover.to(t.pos, dt, ("plant", id(t)), walk=walk, via=t.via) or \
                    (site is not None and (t.pos - bot.position()).length() < 1.5):
                self.arrived = True
            self.plant_t = 0.0
            return
        if site is None or not bot.char.on_ground:
            self.arrived = False
            self.mover.goal_key = None
            return
        if bot.weapons.inv.slot != "bomb":
            bot.weapons.select("bomb")
        bot.intent.crouch = True
        bot.aim.turn_towards(bot.aim.yaw, -35.0, dt, 0.5)
        if self.plant_t == 0.0:
            self.say(f"Planting at {site['name']}!", key="plant", every=5.0)
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
        to = flat(bomb.pos - bot.position())
        if to.length() > 1.3 or abs(bomb.pos.z - bot.position().z) > 1.2:
            self.defuse_t = 0.0
            self.mover.to(bomb.pos, dt, ("defuse",), walk=False, run=True)
            return
        bot.intent.crouch = True
        bot.aim.look_at(bot.eye(), bomb.pos, dt)
        if self.defuse_t == 0.0:
            bot.game.audio.play_at("defuse_start", bomb.pos, 0.8)
            self.say("Defusing!" + ("" if bot.has_kit else " (no kit)"), key="defuse", every=5.0)
        self.defuse_t += dt
        total = float(d.rules["timers"]["defuse_time_kit" if bot.has_kit else "defuse_time"])
        if self.defuse_t >= total:
            self.defuse_t = 0.0
            d.bot_defuse(bot)

    # ---------------------------------------------------------- grenades
    def do_throw(self, dt: float, now: float) -> bool:
        bot = self.bot
        o = self.throw
        if o is None or now > o.deadline or bot.weapons.inv.grenades.get(o.key, 0) <= 0:
            return False
        if o.velocity is None:
            # the target is usually round a corner or across the map: try points along the
            # walking route towards it, farthest first, until one has a clear arc (two a tick)
            if getattr(o, "cands", None) is None:
                o.cands = self._throw_candidates(o)
                if o.cands is None:
                    return True                      # route still being searched: keep the order
            g = bot.game.weapon_db.grenades[o.key]
            start = bot.eye()
            phys = bot.game.physics
            for _ in range(2):
                if not o.cands:
                    return False
                c = o.cands.pop(0)
                o.velocity = solve_throw(start, c, g.throw_speed, phys) or \
                    solve_throw(start, c, g.throw_speed, phys, prefer_high=True)
                if o.velocity is not None:
                    o.target = c
                    break
            if o.velocity is None:
                return True
            bot.weapons.inv.grenade = o.key
            bot.weapons.select("grenade", force=True)
        v = o.velocity
        yaw, pitch = angles_to(v.x, v.y, v.z)
        left = bot.aim.turn_towards(yaw, pitch, dt, 0.8)
        o.aim_t += dt
        if left < 2.0 and o.aim_t > 0.45 and now >= bot.weapons.grenade_ready:
            bot.weapons.throw(o.key, v)
            if o.key == "flash" and self.swing is not None and self.swing[1] is None:
                self.swing = (self.swing[0], now)
            names = {"flash": "Flashbang out!", "smoke": "Smoke out!", "frag": "Frag out!"}
            self.say(names.get(o.key, "Grenade!"), key="nade", every=2.0)
            self.throw = None
        return True

    def _throw_candidates(self, o) -> list | None:
        """Throw targets for an order, best first: the target itself, then points every 2.5 m
        back along the walking route to it (a flash goes off 2 m up, so it is seen round the
        corner; smokes and frags land). None while the route is still being searched."""
        bot = self.bot
        up = 2.0 if o.key == "flash" else 0.3
        tgt = Point3(o.target)
        floor = Point3(tgt.x, tgt.y, tgt.z - (2.2 if o.key == "flash" else 0.3))
        out = [tgt]
        path = self.paths.request(bot.position(), (floor.x, floor.y, floor.z), 0)
        if path == PENDING:
            return None
        if path:
            pts = []
            acc = 0.0
            rev = list(reversed(path))
            for a, b in zip(rev, rev[1:]):
                seg = math.dist(a[:2], b[:2])
                t = 2.5 - acc if acc > 0 else 2.5
                while t < seg:
                    k = t / seg
                    pts.append(Point3(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k,
                                      a[2] + (b[2] - a[2]) * k + up))
                    t += 2.5
                acc = (acc + seg) % 2.5
            me = bot.position()
            out += [q for q in pts if (q - me).length() > 4.0][:10]
        return out

    # ------------------------------------------------------------ misc
    def _flashed(self, dt: float, now: float) -> None:
        bot = self.bot
        it = bot.intent
        cover = self.ctx.cover
        if cover is not None:
            d = flat(cover[0] - bot.position())
            if d.lengthSquared() > 0.25:
                it.wish = d.normalized()
        else:
            it.wish = -bot.forward() * 0.8

    def _personal_space(self) -> None:
        """Standing still on top of a teammate: step apart (one grenade or spray gets both)."""
        bot = self.bot
        p = bot.position()
        for m in self.team.mates_of(bot):
            q = m.position()
            dx, dy = p.x - q.x, p.y - q.y
            d = math.hypot(dx, dy)
            if d < 1.1 and abs(p.z - q.z) < 1.2:
                if d < 1e-3:
                    dx, dy, d = (1.0, 0.0, 1.0) if id(bot) > id(m) else (-1.0, 0.0, 1.0)
                if bot.nav.walkable_line(p, (p.x + dx / d * 0.8, p.y + dy / d * 0.8, p.z)):
                    bot.intent.wish = Vec3(dx / d, dy / d, 0) * 0.6
                    bot.intent.walk = True
                return

    def _shoot_gadget(self, dt: float, now: float) -> None:
        g = self.gadget_target
        if g is None:
            return
        if not g.alive or self.action.name != "task" or self.task.kind in ("plant", "defuse", "use"):
            self.gadget_target = None
            return
        bot = self.bot
        eye = bot.eye()
        point = g.center()
        if (point - eye).length() > 25.0:
            self.gadget_target = None
            return
        rest = bot.aim.look_at(eye, point, dt, 1.0)
        ws = bot.weapons.inv.current()
        if ws is None or ws.d.fire_mode == "melee":
            self.ensure_weapon((point - eye).length())
            return
        if rest < 1.2 and now >= self.shooter.pause_until:
            bot.intent.trigger = True
            bot.intent.pressed = True
            self.shooter.pause_until = now + 0.18

    def _watch_point(self) -> Point3 | None:
        c = self.ctx.target if self.ctx is not None else None
        if self.action.name == "engage" and c is not None and c.seen and c.agent.alive:
            return c.agent.head_pos()
        if self.action.name in ("task", "alert") and self.arrived and self.aim_policy.point is not None:
            return self.aim_policy.point
        return None

    def _choose_lean(self, now: float) -> None:
        bot = self.bot
        it = bot.intent
        if it.wish.lengthSquared() > 0.04 or self.action.name in ("retreat", "reposition", "trade") or \
                bot.char.horizontal_speed > 1.6:
            self.lean = 0.0
        elif now >= self.lean_check:
            self.lean_check = now + self.rng.uniform(0.25, 0.4)
            point = self._watch_point()
            if point is None:
                self.lean = 0.0
            else:
                c = bot.char
                eye = Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height)
                phys = bot.game.physics
                free = clearance(phys, eye, bot.aim.yaw)
                visible = []
                for sign, f in ((-1.0, free[0]), (0.0, 1.0), (1.0, free[1])):
                    p = eye + right_of(bot.aim.yaw) * (sign * LEAN_OFFSET * f)
                    visible.append(f > 0.6 and phys.ray_cast(p, point, MASK_SIGHT) is None)
                keep = self.action.name == "engage" and self.lean != 0.0 and visible[1 + int(self.lean)]
                if keep:
                    pass
                elif visible[1]:
                    self.lean = 0.0
                elif visible[2] and not visible[0]:
                    self.lean = 1.0
                elif visible[0] and not visible[2]:
                    self.lean = -1.0
                else:
                    self.lean = 0.0
        it.lean = self.lean
