"""Team strategy v2: plans, roles and team knowledge for the v2 bots (Milestone 8).

One ``TeamStrategy`` per team (and side) gives orders - ``Task``s, as the
Milestone 5 ``TeamBrain`` it extends - and keeps what the team knows:

* **The possibility field** (ai/v2/belief.py) of where the enemy team can
  be. The team's eyes clear it (one bot's view per tick, round robin); the
  radar, callouts and pings put enemies back on it.
* **The radio** (ai/v2/comms.py): sightings become callouts a moment later,
  snapped to the area; a dying bot that saw its killer gets one last call.
* **The radar** (``reports``): what the team has *seen* in the last few
  seconds, exactly what the human's HUD radar shows; v2 bots glance at it
  now and then (ai/v2/brain.py), never mid-fight.
* **Kills** come from the kill feed (the director's ``kill`` event): the
  number of living enemies, and "you got him" for the killer.
* **Path searches** run in a shared ``PathService`` with a per-tick budget.

Nothing here reads an enemy's position, the hidden charge carrier or an
unseen gadget (ai/audit.py checks it).

Attack plans (one per round, weighted, with an anti-repetition decay, so no
plan dominates; eco rounds favour rush and contact):

    default   spread for map control (the lurk spots and both sites'
              staging points), then a mid-round call: the site where the
              team has seen or heard fewer defenders, and an execute there
    execute   stage on one or two lanes, timed utility (a flash into the
              site, a smoke on the defenders' rotation), entry first and
              his trader a second behind, then clear the site's hold spots
    split     two lanes at once, the groups go together
    fake      two bots show at one site with utility, the rest wait at the
              other; when the fake is seen (or after a while) the main group
              hits and the fakers rotate in behind
    contact   everyone walks one lane quietly until the first information,
              then executes the nearest site
    rush      down the shortest lane, running

Roles (ai/v2/personality.py): entry, trade, support, lurker, AWPer. The
lurker holds a flank spot during the execute, then hits the rotation.

Defence setups (per round, weighted by what the team saw attackers do in
earlier rounds):

    2-1-2     one or two per site in a crossfire from the tactical map, one mid
    stack     three at the site attacked more often so far
    aggro     one or two take a forward spot for early information, then fall
              back to the site
    retake    one per site, the rest between the sites ready to retake

Hold spots that died twice are used less. Rotators move on credible
information only - two different enemies near a site, or one plus utility
there, or a teammate dying there; a lone anchor facing three or more falls
back to wait for the retake. After a plant everyone retakes together with
utility (or saves when there is no time). The attackers' post-plant spots
are a crossfire on the charge from points the defenders' way in cannot see.
"""
from __future__ import annotations

import math

import numpy as np
from panda3d.core import Point3, Vec3

from ai.brain import Task, solve_throw
from ai.gadget_ai import GadgetAI, view_dir, angles_to, emp_grenade_def
from ai.tactics import TeamBrain, zone_distance, _along
from ai.v2.belief import PossibilityField, view_mask
from ai.v2.comms import Radio
from ai.v2.knowledge import Fact
from ai.v2.pathing import PathService
from ai.v2.personality import assign_roles
from engine.physics import MASK_SIGHT

PLANS = ("default", "execute", "split", "fake", "contact", "rush")
# starting weights from head-to-heads against the Milestone 5 bots (executes with utility won most,
# fakes least); within a match they follow the results (``worked``)
PLAN_WEIGHTS = {"default": 0.8, "execute": 1.7, "split": 0.8, "fake": 0.25, "contact": 0.6, "rush": 0.4}
SETUPS = ("2-1-2", "stack", "aggro", "retake")
SETUP_WEIGHTS = {"2-1-2": 1.0, "stack": 0.35, "aggro": 0.15, "retake": 0.8}
GADGET_SIGHT = 30.0


def pick_varied(weights: dict[str, float], history: list[str], rng, decay: float = 0.45, window: int = 5,
                repeat: float = 0.4) -> str:
    """Weighted choice that gets less likely the more often an option was picked lately
    (``decay`` per use in the last ``window``, ``repeat`` again for the last one)."""
    recent = history[-window:]
    opts = list(weights)
    ws = []
    for o in opts:
        w = weights[o] * decay ** recent.count(o)
        if history and history[-1] == o:
            w *= repeat
        if len(history) >= 5 and history.count(o) / len(history) >= 0.36:
            w *= 0.1                             # nothing becomes the plan they always play
        ws.append(w)
    return rng.choices(opts, ws)[0]


class TeamStrategy(TeamBrain):
    ai = "v2"

    def __init__(self, director, side: str):
        from ai.bot import load_bot_config
        self.cfg = load_bot_config().get("v2", {})
        game = director.game
        self.tm = game.tactical_map()
        self.paths = PathService(director.nav, budget=int(self.cfg.get("path_budget", 140)))
        self.enemy_side = "defend" if side == "attack" else "attack"
        self.field = PossibilityField(self.tm, self.enemy_side)
        self.roles: dict[int, str] = {}
        self.enemies_alive = 5
        self.plan_history: list[str] = []
        self.setup_history: list[str] = []
        self.site_seen = {}                    # site -> enemies seen near it early in past rounds
        self.spot_deaths: dict[int, int] = {}  # tactical point -> deaths holding it
        self.attacked: dict[str, int] = {}     # defenders: which site the attackers hit, per round
        self.learned = np.zeros(self.tm.n)     # tactical points where enemies were seen early
        self.worked: dict[str, float] = {}      # plan / setup -> weight from round results
        super().__init__(director, side)
        self.comms = Radio(self, self.tm, self.rng, self.cfg.get("radio"))
        self.gadgets = GadgetAIV2(self)
        for k in (1, 2, 3):
            self.paths.set_bias(k, self._bias(self.rng.randrange(1 << 30)))
        # bias 4 avoids where the team thinks enemies are (rotations, retakes): the field's danger
        # summed per navmesh polygon, refreshed every 2 s
        nav = director.nav
        rects = []
        for q in self.tm._pl:
            n = nav.locate(tuple(q))
            rects.append(int(nav.rect_of[n]) if n >= 0 else -1)
        self._pt_rect = np.array(rects, np.int64)
        self._n_rects = len(nav.rects)
        self._danger_bias_t = -10.0
        # per-site points that need a full path search, once while loading (a search can
        # take 16 ms, too long for a tick): the defenders' way in, the attackers' way back,
        # and walking distances from each site over the tactical map
        self._rot_pts, self._att_pts, self._site_dist = {}, {}, {}
        for name, c in self.site_centers.items():
            for spawn, out, d in (("defend", self._rot_pts, 9.0), ("attack", self._att_pts, 10.0)):
                sp = self.spawn_center.get(spawn)
                path = self.nav.find_path(c, sp) if sp is not None else None
                out[name] = _along(path, d) if path else None
            ci = self.tm.nearest(c)
            self._site_dist[name] = self.tm._dijkstra([ci]) if ci >= 0 else None
        director.listeners.append(self._on_event)
        game.audio.listeners.append(self._on_sound)

    @staticmethod
    def _bias(seed: int):
        def bias(rect: int) -> float:
            h = (rect * 2654435761 + seed) & 0xFFFF
            return 1.0 + 0.35 * h / 0xFFFF
        return bias

    def detach(self) -> None:
        """The director drops this strategy (a new AI choice)."""
        if self._on_event in self.director.listeners:
            self.director.listeners.remove(self._on_event)
        if self._on_sound in self.game.audio.listeners:
            self.game.audio.listeners.remove(self._on_sound)

    @property
    def active(self) -> bool:
        return self.director.team_brains.get(self.side) is self

    # ------------------------------------------------------------- round
    def reset_round(self) -> None:
        super().reset_round()
        self.roles = {}
        self.deaths: list[dict] = []
        self.nades: list[tuple] = []
        self.facts: list[Fact] = []            # what the team learned (radar, radio, pings)
        self._radar_t: dict[int, float] = {}
        self._pending_radar: list[tuple[float, Fact]] = []
        self._tick_t = -1.0
        self._tick_n = 0
        self._think_t = -1.0
        self._thinks = 0
        self._stagger = 0 if self.side == "attack" else 1
        self._bots_t = -1.0
        self._bots: list = []
        self._observe_i = 0
        self._danger = None
        self._danger_t = -1.0
        self.go_t = None
        self.defuse_heard = None
        self.stage_t = 0.0
        self.fake = None
        self.fake_t = None
        self.lurk_go = False
        self.setup = ""
        self.retake_util = False
        self.fell_back: set[int] = set()
        self.rotation_site, self.rotation_t = "", 0.0
        self.early: dict[str, set] = {}
        self.learned_now = np.zeros(self.tm.n)
        self._learned_round: set = set()
        if hasattr(self, "comms"):
            self.comms.reset()

    def round_start(self, now: float) -> None:
        self.reset_round()
        self.gadgets.reset()
        self.round_start_t = now
        d = self.director
        timers = d.rules["timers"]
        live = now + float(timers["freeze_time"]) + float(timers.get("prep_time", 0.0))
        self.live_t = live
        leave = live if self.enemy_side == "attack" else now + float(timers["freeze_time"])
        self.enemies_alive = sum(1 for a in d.match.participants
                                 if a.side == self.enemy_side and getattr(a, "active", True))
        self.field.reset(leave, self.enemies_alive, self._prior())
        bots = self.bots()
        if not bots:
            return
        carrier = d.bomb.carrier if self.side == "attack" else None
        self.roles = assign_roles(bots, self.side, self.rng, lurker=len(bots) >= 4, carrier=carrier)
        if self.side == "attack":
            self._attack_plan(bots, now)
        else:
            self._defend_setup(bots, now)

    def _prior(self) -> np.ndarray:
        tm = self.tm
        p = np.ones(tm.n)
        if self.enemy_side == "defend":
            p += 2.0 * (tm.site_id >= 0)
            for site in tm.sites:
                for h in tm.holds(site):
                    p[h["i"]] += 3.0
                for f in tm.spots.get(site, {}).get("forward", []):
                    p[f["i"]] += 1.0
        else:
            p += 1.5 * ((tm.kind & 4) != 0)
        # where this team has seen enemies in earlier rounds (decayed per round)
        if self.learned.any():
            p += 4.0 * self.learned / max(float(self.learned.max()), 1.0)
        return p

    # ------------------------------------------------------------ per tick
    def tick(self, now: float) -> None:
        """Called by the first v2 brain to update each tick: radio, radar, field, paths."""
        if now == self._tick_t:
            return
        self._tick_t = now
        self.paths.update()
        if self.tm._pending:
            self.tm.update(self.game.physics, budget=24)     # sight lines through opened walls
        self.comms.update(now)
        while self._pending_radar and self._pending_radar[0][0] <= now:
            _, f = self._pending_radar.pop(0)
            self.field.add_fact(f)
        self._tick_n += 1
        if now - self._danger_bias_t > 2.0 and self.director.match.phase in ("live", "planted"):
            self._danger_bias_t = now
            d = self.danger()
            m = self._pt_rect >= 0
            per = np.zeros(self._n_rects)
            np.add.at(per, self._pt_rect[m], d[m])
            table = (1.0 + 3.0 * np.minimum(per, 1.0)).tolist()
            self.paths.set_bias(4, table.__getitem__)
        bots = self.alive_bots()
        phase = self._tick_n + self._stagger
        if bots and phase % 3 == 0 and self.director.match.phase in ("prep", "live", "planted"):
            # one bot's view every third tick (each bot's every ~15 ticks with five); the two
            # teams' upkeep is staggered so it never lands on the same tick
            b = bots[self._observe_i % len(bots)]
            self._observe_i += 1
            mask = self._view(b)
            if mask is not None:
                self.field.observe(mask, now)
                self._danger = None
        if phase % 4 == 2:
            # one field one hop (about 3 m) every fourth tick: with six fields that still
            # spreads faster than anyone runs
            self.field.step(now)
            self._danger = None

    def _view(self, b):
        if b.perception.blind:
            return None
        eye = b.eye()
        smokes = [((c.pos.x, c.pos.y), c.radius) for c in self.game.effects.smokes if c.density > 0.5]
        fov = float(b.profile.get("fov", 110))
        m = view_mask(self.tm, (eye.x, eye.y, eye.z), b.aim.yaw, fov, 60.0, smokes)
        # an enemy in sight is a fact (the radar), not an empty point
        for c in b.perception.contacts.values():
            if c.seen:
                m[self.tm.points_near((c.pos.x, c.pos.y, c.pos.z), 2.5)] = False
        return m

    def danger(self) -> np.ndarray:
        """Expected enemies per tactical point (recomputed when the field changed, at most
        every few ticks)."""
        now = self.game.loop.time
        if self._danger is None or now - self._danger_t > 0.1:
            self._danger = self.field.danger(now)
            self._danger_t = now
        return self._danger

    # ----------------------------------------------------------- roster
    def bots(self) -> list:
        """This side's bots in the round. Every brain asks several times a tick, so the list is
        built once per tick (sides and the round only change between ticks; ``reset_round``
        drops it too)."""
        now = self.game.loop.time
        if now != self._bots_t:
            self._bots_t = now
            self._bots = [b for b in self.director.bots if b.side == self.side and b.active]
        return self._bots

    THINKS_PER_TICK = 3

    def think_slot(self, now: float) -> bool:
        """May one more of the team's bots re-score its actions this tick?"""
        if now != self._think_t:
            self._think_t = now
            self._thinks = 0
        if self._thinks >= self.THINKS_PER_TICK:
            return False
        self._thinks += 1
        return True

    def mates_of(self, bot) -> list:
        return [b for b in self.bots() if b.alive and b is not bot]

    def humans_alive(self) -> int:
        bots = set(map(id, self.director.bots))
        return sum(1 for a in self.members() if id(a) not in bots and a.alive)

    def role_of(self, bot) -> str:
        return self.roles.get(id(bot), "")

    # ------------------------------------------------------- knowledge in
    def report(self, bot, enemy, pos, now: float, kind: str) -> None:
        """Legacy entry point (exact enemy positions): not used by v2 brains."""

    def radar_report(self, fact: Fact) -> None:
        """A teammate sees an enemy: it shows on the radar now, and in the team's picture."""
        k = fact.enemy
        if fact.time - self._radar_t.get(k, -9.0) < 0.3:
            return
        self._radar_t[k] = fact.time
        self.reports.append((fact.time, k, Point3(*fact.pos)))
        if len(self.reports) > 60:
            self.reports = self.reports[-60:]
        self.last_contact_t = fact.time
        f = Fact(k, fact.pos, 1.0, fact.time, "radar")
        self.facts.append(f)
        self._pending_radar.append((fact.time + 0.5, f))
        self._early(f)

    def on_sighting(self, bot, contact, now: float) -> None:
        """A bot fighting an enemy calls it out (ai/v2/comms.py delays and snaps it)."""
        brain = bot.brain
        key = id(contact.agent)
        self.comms.sighting(bot, key, (contact.pos.x, contact.pos.y, contact.pos.z), now,
                            hurt=brain.knowledge.hurt(key), stressed=brain.human.stress > 0.5)

    def on_heard(self, bot, fact: Fact, now: float) -> None:
        """Footsteps or shots a bot heard: a vaguer callout."""
        if fact.enemy is None:
            return
        self.comms.sighting(bot, fact.enemy, fact.pos, now, every=6.0, heard=True)

    def on_radio_fact(self, fact: Fact, speaker: str) -> None:
        self.field.add_fact(fact)
        self.facts.append(fact)
        self.last_contact_t = max(self.last_contact_t, fact.time)
        self._early(fact)
        for b in self.alive_bots():
            if b.name == speaker or b.brain.human.mistake("ignore_call"):
                continue
            b.brain.knowledge.add(fact)

    def intel(self, enemy, pos: Point3, now: float) -> None:
        """A ping (drone, camera, gadget): on the HUD for the whole team."""
        self.reports.append((now, id(enemy), Point3(pos)))
        self.last_contact_t = now
        f = Fact(id(enemy), (pos.x, pos.y, pos.z), 1.0, now, "intel")
        self.facts.append(f)
        self.field.add_fact(f)
        self._early(f)
        for b in self.alive_bots():
            b.brain.knowledge.add(f)

    def _early(self, f: Fact) -> None:
        """Remember where enemies showed in the first 40 s, and near which site (adaptation)."""
        if f.time - getattr(self, "live_t", 0.0) > 40.0 or f.enemy is None:
            return
        if f.radius <= 1.5:
            i = self.tm.nearest(f.pos)
            if i >= 0 and (f.enemy, i) not in self._learned_round:
                self._learned_round.add((f.enemy, i))
                self.learned_now[i] += 1.0
        for name, z in self.sites.items():
            if zone_distance(z, f.pos) < 18.0:
                self.early.setdefault(name, set()).add(f.enemy)

    def on_teammate_killed(self, victim, killer, now: float) -> None:
        pos = victim.position()
        yaw = victim.aim.yaw if hasattr(victim, "aim") else None
        brain = getattr(victim, "brain", None)
        if killer is not None and killer.side != self.side and getattr(brain, "ai", "") == "v2":
            c = victim.perception.contacts.get(id(killer))
            if c is not None and c.source == "sight" and (c.seen or now - c.time < 1.0):
                self.comms.last_words(victim, id(killer), (c.pos.x, c.pos.y, c.pos.z), now,
                                      brain.knowledge.hurt(id(killer)))
            if brain.task.kind in ("hold", "guard") and brain.arrived:
                i = self.tm.nearest(pos)
                if i >= 0:
                    self.spot_deaths[i] = self.spot_deaths.get(i, 0) + 1
        self.deaths.append({"t": now, "pos": Point3(pos), "victim": victim, "guess": self._killer_guess(pos, yaw)})
        self.deaths = self.deaths[-8:]
        self.last_contact_t = now

    def _killer_guess(self, pos: Point3, yaw: float | None):
        """Where the teammate was looking when it died: the likeliest visible spot in that direction."""
        tm = self.tm
        i = tm.nearest(pos)
        if i < 0 or yaw is None:
            return None
        vis = tm.seen_in_view(i, (pos.x, pos.y, pos.z), yaw, 40.0, 35.0)
        dx = tm._xy[:, 0] - pos.x
        dy = tm._xy[:, 1] - pos.y
        dist = np.hypot(dx, dy)
        cand = np.flatnonzero(vis & (dist > 4.0))
        if len(cand) == 0:
            return None
        w = self.danger()[cand] + 0.02 / (1.0 + np.abs(dist[cand] - 14.0))
        j = int(cand[int(np.argmax(w))])
        p = tm.pos[j]
        return (float(p[0]), float(p[1]), float(p[2]))

    def trade_fact(self, bot, now: float):
        """A teammate died close by in the last 3 s: (what this bot knows about the killer,
        where the teammate died) - for the two closest teammates only, the others keep their
        own angles."""
        me = bot.position()
        best, bd = None, 22.0
        for d in self.deaths:
            if now - d["t"] > 3.0 or d["victim"] is bot:
                continue
            dist = (d["pos"] - me).length()
            if dist >= bd:
                continue
            vp = d["pos"]
            closer = sum(1 for m in self.mates_of(bot) if (m.position() - vp).length() < dist)
            if closer >= 2:
                continue
            own = [f for f in bot.brain.knowledge.recent(3.0, now)
                   if f.source in ("sight", "sound", "radio", "radar")
                   and math.hypot(f.pos[0] - vp.x, f.pos[1] - vp.y) < 25.0]
            if own:
                f = max(own, key=lambda f: f.time)
            elif d["guess"] is not None:
                f = Fact(None, d["guess"], 4.0, d["t"], "damage")
            else:
                f = Fact(None, (vp.x, vp.y, vp.z), 3.0, d["t"], "damage")
            best, bd = (f, Point3(vp)), dist
        return best

    def nade_at(self, pos, now: float) -> None:
        self.nades.append((now, (pos[0], pos[1])))
        self.nades = [n for n in self.nades if now - n[0] < 8.0]

    def recent_nade(self, pos, now: float, radius: float = 7.0, within: float = 4.0) -> bool:
        return any(now - t < within and math.hypot(p[0] - pos[0], p[1] - pos[1]) < radius for t, p in self.nades)

    def radio_say(self, bot, text: str, key: str | None = None, every: float = 4.0) -> None:
        self.comms.say(bot, text, key=key, every=every)

    def _on_event(self, kind: str, data: dict) -> None:
        if not self.active:
            return
        if kind == "kill":
            victim, killer = data.get("victim"), data.get("killer")
            if victim is not None and victim.side == self.enemy_side:
                self.enemies_alive = max(self.enemies_alive - 1, 0)
                self.field.enemy_died(id(victim))
                for b in self.alive_bots():
                    b.brain.knowledge.forget(id(victim))
                if killer is not None and getattr(getattr(killer, "brain", None), "team", None) is self:
                    killer.brain.on_kill(victim)
        elif kind == "round_end":
            # what worked: plans / setups that won get picked a little more often
            won = data["result"].winner_side == self.side
            key = self.plan if self.side == "attack" else self.setup
            if key:
                m = self.worked.get(key, 1.0) * (1.25 if won else 0.85)
                self.worked[key] = min(max(m, 0.5), 2.0)
        elif kind == "bomb_planted" and self.side == "defend":
            # the planter was at the charge (it shows on everyone's radar)
            b = self.director.bomb
            f = Fact(None, (b.pos.x, b.pos.y, b.pos.z), 1.5, self.game.loop.time, "intel")
            self.field.add_fact(f)
            self.facts.append(f)

    def recent_facts(self, now: float, max_age: float = 8.0) -> list[Fact]:
        self.facts = [f for f in self.facts if now - f.time < 20.0]
        return [f for f in self.facts if now - f.time <= max_age]

    def site_evidence(self, site: str, now: float, max_age: float = 8.0) -> tuple[int, bool, bool]:
        """(different enemies reported near the site, utility there, a teammate died there)."""
        z = self.sites[site]
        keys = {f.enemy for f in self.recent_facts(now, max_age) if f.enemy is not None
                and zone_distance(z, f.pos) < 16.0}
        util = any(c.density > 0.3 and zone_distance(z, c.pos) < 12.0 for c in self.game.effects.smokes)
        died = any(now - d["t"] < max_age and zone_distance(z, d["pos"]) < 12.0 for d in self.deaths)
        return len(keys), util, died

    # -------------------------------------------------------------- attack
    def _attack_plan(self, bots, now: float) -> None:
        sites = [s for s in self.sites if self.lanes.get(s)]
        if not sites:
            return
        # site: what worked, minus where defenders were seen early, minus repetition
        recent_sites = [h.split(":")[1] for h in self.plan_history[-3:] if ":" in h]
        weights = []
        for s in sites:
            w = 1.0 + 0.3 * self.site_history.get(s, 0) - 0.15 * self.site_seen.get(s, 0)
            w *= 0.6 ** recent_sites.count(s)
            weights.append(max(w, 0.15))
        self.site = self.rng.choices(sites, weights)[0]
        mode = getattr(self, "buy_mode", "full")
        weights = {p: w * self.worked.get(p, 1.0) for p, w in PLAN_WEIGHTS.items()}
        if len(self.lanes[self.site]) < 2 or len(bots) < 4:
            del weights["split"]
        if len(bots) < 4 or len(sites) < 2:
            del weights["fake"]
        if mode == "eco":
            for p in ("rush", "contact"):
                weights[p] *= 3.0
        self.plan = pick_varied(weights, [h.split(":")[0] for h in self.plan_history], self.rng)
        self.plan_history.append(f"{self.plan}:{self.site}")
        self.commit_t = self.live_t + self.rng.uniform(22.0, 40.0)
        lurker = next((b for b in bots if self.roles.get(id(b)) == "lurker"), None)
        if self.plan == "rush":
            lane = min(self.lanes[self.site], key=lambda ln: len(ln["points"]))
            for b in bots:
                self._go_site(b, lane, now, tag="rush")
            self.phase = "exec"
            self.exec_t = now
            self.say(bots[0], f"Rush {self.site}, go go go!")
            return
        if self.plan == "default":
            pool = list(range(len(self.lurks)))
            self.rng.shuffle(pool)
            carrier = self.director.bomb.carrier
            for b in bots:
                if b is carrier or not pool:
                    self._stage(b, self.rng.choice(self.lanes[self.site]))
                    continue
                info = self.lurk_info[pool.pop()]
                b.brain.set_task(Task("hold", self._snap(info[:3]), yaw=float(info[3]), crouch=bool(info[4]),
                                      wait=True, tag="control", walk_near=10.0))
            self.phase = "control"
            self.say(bots[0], "Default, take map control.")
            return
        if self.plan == "contact":
            lane = self.rng.choice(self.lanes[self.site])
            for b in bots:
                self._stage(b, lane, walk=True)
            self.phase = "contact"
            self.say(bots[0], f"Walk {self.site}, quiet until contact.")
            return
        lanes = list(self.lanes[self.site])
        self.rng.shuffle(lanes)
        if self.plan == "fake":
            other = self.rng.choice([s for s in sites if s != self.site])
            fakers = sorted((b for b in bots if b is not self.director.bomb.carrier and b is not lurker),
                            key=lambda b: -(self.roles.get(id(b)) == "support") - b.brain.traits["utility"])[:2]
            flane = self.rng.choice(self.lanes[other])
            for b in fakers:
                self._stage(b, flane, site=other, tag="fake")
            self.fake = {"site": other, "bots": [id(b) for b in fakers], "lane": flane, "shown": None}
            main = [b for b in bots if b not in fakers]
            for i, b in enumerate(main):
                self._stage(b, lanes[0])
            self.phase = "stage"
            self.stage_t = now
            self.say(bots[0], f"Fake {other}, we hit {self.site}.")
            return
        use = lanes[:2] if self.plan == "split" else lanes[:1]
        if self.plan == "execute" and len(bots) >= 4 and len(lanes) > 1 and self.rng.random() < 0.3:
            use = lanes[:2]
        # entry and trade together on the first lane, support with them; others spread
        rank = {"entry": 0, "trade": 1, "support": 2, "awper": 3, "lurker": 4}
        order = sorted(bots, key=lambda b: rank.get(self.roles.get(id(b), ""), 5))
        for i, b in enumerate(order):
            if b is lurker and self.plan in ("execute", "split"):
                self._lurk(b)
                continue
            lane = use[0] if self.roles.get(id(b)) in ("entry", "trade") else use[i % len(use)]
            self._stage(b, lane)
        self.phase = "stage"
        self.stage_t = now
        self.say(bots[0], f"Let's take {self.site}" + (", split." if len(use) > 1 else "."))

    def _stage(self, bot, lane, walk: bool = False, site: str | None = None, tag: str = "stage") -> None:
        pts = lane["points"]
        stage = self._snap(pts[lane["stage"]])
        self.groups[id(bot)] = {"lane": lane, "stage": stage}
        site_c = self.site_centers[site or self.site]
        bot.brain.set_task(Task("move", stage, via=list(pts[:lane["stage"]]), look=site_c + Vec3(0, 0, 1.0),
                                wait=True, tag=tag, walk=walk, walk_near=14.0))

    def _lurk(self, bot) -> None:
        """A flank spot away from the site being hit, watching the defenders' way across."""
        others = [s for s in self.sites if s != self.site and self.lanes.get(s)]
        if not others:
            self._stage(bot, self.rng.choice(self.lanes[self.site]))
            return
        lane = self.rng.choice(self.lanes[others[0]])
        stage = self._snap(lane["points"][lane["stage"]])
        self.groups[id(bot)] = {"lane": lane, "stage": stage, "lurk": True}
        bot.brain.set_task(Task("hold", stage, via=list(lane["points"][:lane["stage"]]),
                                look=self.site_centers[others[0]] + Vec3(0, 0, 1.2), wait=True, tag="lurk",
                                walk=True))
        self.say(bot, f"Lurking {others[0]}.", key="lurk", every=30.0)

    def _go_site(self, bot, lane, now: float, tag: str = "clear") -> None:
        """Into the site along the rest of the lane: the carrier plants, the others clear spots."""
        pts = lane["points"]
        via = list(pts[lane["stage"]:]) if tag != "rush" else list(pts)
        if self.director.bomb.carrier is bot:
            bot.brain.set_task(Task("plant", self._plant_spot(), via=via, tag="plant"))
            return
        spot, look = self._clear_spot(bot, lane)
        bot.brain.set_task(Task("move", spot, via=via, look=look, wait=True, tag=tag))

    def _clear_spot(self, bot, lane):
        """A defenders' hold spot of the site that nobody is clearing yet, near this lane's entry."""
        tm = self.tm
        holds = tm.holds(self.site)
        taken = [b.brain.task.pos for b in self.alive_bots() if b is not bot and b.brain.task.pos is not None
                 and b.brain.task.tag in ("clear", "rush")]
        entry = lane["points"][-1]
        best, bs = None, 1e9
        for h in holds:
            p = tm.pos[h["i"]]
            if any(math.hypot(float(p[0]) - t.x, float(p[1]) - t.y) < 3.0 for t in taken):
                continue
            s = math.hypot(float(p[0]) - entry[0], float(p[1]) - entry[1]) + self.rng.uniform(0, 6)
            if s < bs:
                best, bs = Point3(float(p[0]), float(p[1]), float(p[2])), s
        if best is None:
            best = self._snap(self.site_centers[self.site])
        return best, self.site_centers[self.site] + Vec3(0, 0, 1.2)

    def _update_attack(self, bots, now: float) -> None:
        d = self.director
        bomb = d.bomb
        left = d.match.round_time_left()
        if bomb.state == "planted":
            if not self.planted_handled:
                self.planted_handled = True
                self.site_history[bomb.site] = self.site_history.get(bomb.site, 0) + 1
                self._post_plant(bots, now)
            elif d.match.bomb_time_left() < 7.0 and self.phase != "flee":
                self.phase = "flee"
                self._flee(bots, bomb.pos)
            return
        carrier = bomb.carrier
        if carrier is not None and carrier.is_human and self.phase in ("stage", "control", "contact"):
            for name, z in self.sites.items():
                if zone_distance(z, carrier.position()) < 15.0 and name != self.site and self.lanes.get(name):
                    self.site = name
                    self._execute(bots, now)
                    return
        if bomb.state == "dropped":
            if self.pickup_bot is None or not self.pickup_bot.alive or self.pickup_bot not in bots:
                self.pickup_bot = min(bots, key=lambda b: (b.position() - bomb.pos).length())
                self.pickup_bot.brain.set_task(Task("pickup", Point3(bomb.pos), tag="bomb"))
                self.say(self.pickup_bot, "I'll get the charge.")
        elif self.pickup_bot is not None:
            pb = self.pickup_bot
            self.pickup_bot = None
            if pb.alive and bomb.carrier is pb:
                self.say(pb, "I have the charge.")
                if self.phase in ("exec", "go"):
                    pb.brain.set_task(Task("plant", self._plant_spot(), tag="plant"))
                else:
                    self._stage(pb, self.rng.choice(self.lanes[self.site]))
        if self.phase == "control":
            if now >= self.commit_t or left < 50.0:
                self._mid_round_call(bots, now)
        elif self.phase == "contact":
            hit = any(now - f.time < 3.0 for f in self.recent_facts(now, 3.0)) or \
                any(now - dd["t"] < 3.0 for dd in self.deaths)
            arrived = sum(1 for b in bots if id(b) in self.stage_arrived)
            if hit or arrived >= max(1, len(bots) // 2) or left < 45.0:
                me = bots[0].position()
                self.site = min((s for s in self.sites if self.lanes.get(s)),
                                key=lambda s: (self.site_centers[s] - me).length())
                self._execute(bots, now)
        elif self.phase == "stage":
            if self.fake is not None and not self._fake_done(now):
                if left >= 40.0:
                    return
            main = [b for b in bots if (self.fake is None or id(b) not in self.fake["bots"])
                    and not self.groups.get(id(b), {}).get("lurk")]
            # staged: arrived, or close to the staging point (perhaps busy fighting there)
            staged = [b for b in main if id(b) in self.stage_arrived or
                      (id(b) in self.groups and (b.position() - self.groups[id(b)]["stage"]).length() < 8.0)]
            if staged and self.first_stage_t is None:
                self.first_stage_t = now
            ready = len(staged) >= max(1, math.ceil(len(main) * 0.6))
            if self.plan == "split":
                lanes = {self.groups[id(b)]["lane"]["name"] for b in staged if id(b) in self.groups}
                ready = ready and len(lanes) >= min(2, len({g["lane"]["name"] for g in self.groups.values()}))
            waited = (self.first_stage_t is not None and now - self.first_stage_t > 12.0) or \
                now - self.stage_t > 35.0
            breaching = self.gadgets.breach_busy() and left >= 40.0 and now - self.stage_t < 30.0
            if (ready or waited or left < 35.0) and not breaching:
                self._execute(bots, now)
        elif self.phase == "exec":
            if self.go_t is not None and now >= self.go_t:
                self.go_t = None
                self._go(bots, now)
        elif self.phase == "go":
            c = bomb.carrier
            if c is not None and not c.is_human and c.alive and c.brain.task.kind != "plant":
                c.brain.set_task(Task("plant", self._plant_spot(), tag="plant"))
            lurker = next((b for b in bots if self.roles.get(id(b)) == "lurker"), None)
            if lurker is not None and not self.lurk_go and (now - (self.exec_t or now) > 6.0 or left < 30.0):
                self.lurk_go = True
                self._hit_rotation(lurker, now)
            if left < 25.0:
                for b in bots:
                    if b.brain.task.kind == "hold" and b.brain.task.tag != "rotation":
                        self._go_site(b, self._lane_for(b), now)

    def _fake_done(self, now: float) -> bool:
        """The fake has been shown: utility thrown or contact at the fake site, or time."""
        fk = self.fake
        at = [b for b in self.alive_bots() if id(b) in fk["bots"] and id(b) in self.stage_arrived]
        if fk["shown"] is None and at:
            fk["shown"] = now
            site_c = self.site_centers[fk["site"]]
            for b in at:
                for key in ("smoke", "flash", "frag"):
                    if b.brain.order_throw(key, site_c + Vec3(0, 0, 1.0), 4.0):
                        break
            self.say(at[0], f"Showing {fk['site']}.", key="fake", every=20.0)
        if fk["shown"] is None:
            return now - self.live_t > 35.0
        return now - fk["shown"] > 5.0

    def _mid_round_call(self, bots, now: float) -> None:
        """Default: commit where the team saw or heard fewer defenders."""
        counts = {}
        for name in self.sites:
            if not self.lanes.get(name):
                continue
            n, util, died = self.site_evidence(name, now, 25.0)
            counts[name] = n + (1.0 if died else 0.0) + (0.5 if util else 0.0)
        low = min(counts.values())
        cands = [n for n, c in counts.items() if c == low]
        self.site = self.site if self.site in cands else self.rng.choice(cands)
        self.plan_history[-1] = f"{self.plan}:{self.site}"
        lurker = next((b for b in bots if self.roles.get(id(b)) == "lurker"), None)
        for b in bots:
            if b is lurker:
                self._lurk(b)
            else:
                self._stage(b, self.rng.choice(self.lanes[self.site]))
        self.phase = "stage"
        self.stage_t = now
        self.first_stage_t = None
        self.stage_arrived = set()
        self.say(bots[0], f"Regroup for {self.site}.")

    def _execute(self, bots, now: float) -> None:
        """Utility first, then go a moment later."""
        self.phase = "exec"
        self.exec_t = now
        site = self.site
        site_c = self.site_centers[site]
        throwers = sorted(bots, key=lambda b: (self.roles.get(id(b)) != "support", -b.brain.traits["utility"]))
        flash = smoke = None
        for b in throwers:
            if flash is None and b.weapons.inv.grenades.get("flash", 0) > 0:
                if b.brain.order_throw("flash", self._entry_flash_point(b, site), 3.0):
                    flash = b
                    continue
            if smoke is None and b.weapons.inv.grenades.get("smoke", 0) > 0:
                p = self._rotation_point(site)
                if p is not None and b.brain.order_throw("smoke", p, 4.0):
                    smoke = b
        self.go_t = now + (1.3 if flash is not None else 0.4 if smoke is not None else 0.0)
        if self.go_t <= now:
            self._go(bots, now)
        if flash is not None:
            self.say(flash, f"Flash going {site}.", key="flashgo", every=10.0)

    def _entry_flash_point(self, bot, site: str) -> Point3:
        """A point just inside the site past the entry nearest to the thrower."""
        tm = self.tm
        ents = tm.entries(site)
        site_c = self.site_centers[site]
        if not ents:
            return site_c + Vec3(0, 0, 2.0)
        p = bot.position()
        e = min(ents, key=lambda i: math.hypot(float(tm.pos[i][0]) - p.x, float(tm.pos[i][1]) - p.y))
        q = tm.pos[e]
        v = Vec3(site_c.x - float(q[0]), site_c.y - float(q[1]), 0)
        if v.lengthSquared() > 1e-4:
            v.normalize()
        return Point3(float(q[0]) + v.x * 4.0, float(q[1]) + v.y * 4.0, float(q[2]) + 2.2)

    def _go(self, bots, now: float) -> None:
        self.phase = "go"
        lurker = next((b for b in bots if self.roles.get(id(b)) == "lurker"), None)
        for b in bots:
            if b is lurker and self.plan in ("execute", "split") and b.brain.task.tag == "lurk":
                continue
            self._go_site(b, self._lane_for(b), now)
        self.say(bots[0], f"Go {self.site}! Go!")

    def _lane_for(self, b):
        g = self.groups.get(id(b))
        if g and not g.get("lurk") and g["lane"] in self.lanes.get(self.site, []):
            return g["lane"]
        p = b.position()
        return min(self.lanes[self.site],
                   key=lambda ln: math.hypot(ln["points"][ln["stage"]][0] - p.x, ln["points"][ln["stage"]][1] - p.y))

    def _hit_rotation(self, bot, now: float) -> None:
        """The lurker comes in behind the defenders rotating to the site."""
        p = self._rotation_point(self.site)
        if p is None:
            self._go_site(bot, self._lane_for(bot), now)
            return
        bot.brain.set_task(Task("move", p, look=self.site_centers[self.site] + Vec3(0, 0, 1.2), wait=True,
                                tag="rotation", walk_near=12.0))
        self.say(bot, "Coming from behind.", key="lurkgo", every=30.0)

    def _post_plant(self, bots, now: float) -> None:
        """Play for time: hide a corner away from the charge, out of sight of the defenders' way
        in, each next to a peek spot that sees the charge from a different side. The defuse
        start is heard (``_on_sound``): then everyone swings at once."""
        bomb = self.director.bomb
        spots = self._hide_spots(bomb.pos, len(bots))
        self.peeks = {}
        for b, (hide, peek) in zip(bots, spots):
            self.peeks[id(b)] = peek
            # hiding: the crosshair on the corners the retakers would come round (the team's
            # picture decides), not on the charge it cannot see from here
            look = None if (peek - hide).length() > 0.5 else bomb.pos + Vec3(0, 0, 0.6)
            b.brain.set_task(Task("guard", hide, look=look, wait=True, tag="post"))
        self.phase = "post"
        self.defuse_heard = None
        self.say(bots[0], f"Charge planted at {bomb.site}. Hide and listen for the defuse.")

    def _hide_spots(self, target: Point3, n: int) -> list:
        """(hide, peek) pairs: hide points 6-20 m from the charge that neither the charge nor the
        defenders' way in can see, each with a peek point within 3.5 m that sees the charge."""
        tm = self.tm
        ti = tm.nearest(target)
        if ti < 0:
            return [(p, p) for p, _ in self._crossfire_on(target, n)]
        rot = self._rotation_point(self.director.bomb.site) if self.director.bomb.site else None
        ri = tm.nearest(rot) if rot is not None else -1
        sees_bomb = tm.visible_mask(ti) | tm.visible_mask(ti, low=True)
        seen_in = tm.visible_mask(ri) if ri >= 0 else np.zeros(tm.n, bool)
        dx = tm._xy[:, 0] - target.x
        dy = tm._xy[:, 1] - target.y
        dist = np.hypot(dx, dy)
        near_z = np.abs(tm._z - target.z) < 3.0
        cand = np.flatnonzero(~sees_bomb & ~seen_in & (dist > 6.0) & (dist < 20.0) & near_z)
        # on the attackers' side of the charge: the retake gathers on the defenders' side
        eta_a, eta_d = tm.eta.get("attack"), tm.eta.get("defend")
        if eta_a is not None and eta_d is not None:
            their_side = (eta_d < eta_a).astype(float) * 8.0
        else:
            their_side = np.zeros(tm.n)
        order = sorted(cand.tolist(), key=lambda i: abs(dist[i] - 11.0) + their_side[i] +
                       self.rng.random() * 4.0)[:60]
        out, angs, used = [], [], []
        for i in order:
            peeks = [int(j) for j in tm.points_near(tm._pl[i], 3.5, max_dz=1.0) if sees_bomb[j] and dist[j] > 4.0]
            if not peeks:
                continue
            j = min(peeks, key=lambda j: (tm._xy[j, 0] - tm._xy[i, 0]) ** 2 + (tm._xy[j, 1] - tm._xy[i, 1]) ** 2)
            a = math.atan2(dy[j], dx[j])
            if any(abs((a - b + math.pi) % (2 * math.pi) - math.pi) < math.radians(40) for b in angs):
                continue
            if any(math.hypot(tm._xy[i, 0] - u[0], tm._xy[i, 1] - u[1]) < 4.0 for u in used):
                continue
            out.append((Point3(*tm._pl[i]), Point3(*tm._pl[j])))
            angs.append(a)
            used.append(tm._pl[i])
            if len(out) >= n:
                break
        if len(out) < n:
            out += [(p, p) for p, _ in self._crossfire_on(target, n - len(out))]
        return out

    def _on_sound(self, name: str, pos) -> None:
        """Positional sounds the team can hear: a defuse starting on the planted charge."""
        if name != "defuse_start" or self.side != "attack" or not self.active:
            return
        if self.director.bomb.state != "planted":
            return
        p = Point3(*pos)
        near = [b for b in self.alive_bots() if (b.position() - p).length() < 45.0]
        if not near:
            return
        now = self.game.loop.time
        self.defuse_heard = now
        f = Fact(None, (p.x, p.y, p.z), 1.0, now, "sound")
        self.field.add_fact(f)
        for b in near:
            b.brain.knowledge.add(f)
            b.brain.next_think = now
        self.say(near[0], "They're defusing!", key="defusing", every=5.0)
        self._stop_defuse(now)

    def _stop_defuse(self, now: float) -> None:
        """Everyone swings at once: to its peek spot, crosshair on the charge."""
        bomb = self.director.bomb
        target = bomb.pos + Vec3(0, 0, 0.5)
        for b in self.alive_bots():
            t = b.brain.task
            if t.tag != "post" or t.pos is None:
                continue
            peek = getattr(self, "peeks", {}).get(id(b))
            if peek is None or (peek - t.pos).length() < 0.5:
                peek = self._snap(bomb.pos)
            b.brain.set_task(Task("move", Point3(peek), look=target, wait=True, tag="retake"))

    def _crossfire_on(self, target: Point3, n: int) -> list:
        """Spots with a view of the charge, hidden from the defenders' way in, at different angles."""
        tm = self.tm
        phys = self.game.physics
        ti = tm.nearest(target)
        rot = self._rotation_point(self.director.bomb.site) if self.director.bomb.site else None
        ri = tm.nearest(rot) if rot is not None else -1
        if ti < 0:
            return [(self._snap(target), False)] * n
        dx = tm._xy[:, 0] - target.x
        dy = tm._xy[:, 1] - target.y
        dist = np.hypot(dx, dy)
        cand = np.flatnonzero(tm.visible_mask(ti, low=True) & (dist > 5.0) & (dist < 18.0))
        scored = []
        for i in cand.tolist():
            s = self.rng.random() * 0.6 + (0.8 if tm.cover[i] else 0.0) - abs(dist[i] - 11.0) * 0.05
            if ri >= 0 and tm.sees(ri, i):
                s -= 1.0
            scored.append((s, i))
        scored.sort(reverse=True)
        out, angs = [], []
        for s, i in scored:
            a = math.atan2(dy[i], dx[i])
            if any(abs((a - b + math.pi) % (2 * math.pi) - math.pi) < math.radians(50) for b in angs):
                continue
            p = tm.pos[i]
            q = Point3(float(p[0]), float(p[1]), float(p[2]))
            if phys.ray_cast(q + Vec3(0, 0, 1.0), target + Vec3(0, 0, 0.3), MASK_SIGHT) is not None:
                continue
            out.append((q, not tm.sees(ti, i) or bool(self.rng.random() < 0.4)))
            angs.append(a)
            if len(out) >= n:
                break
        while len(out) < n:
            out.append((self._snap(target + Vec3(self.rng.uniform(-5, 5), self.rng.uniform(-5, 5), 0)), False))
        return out

    # ------------------------------------------------------------- defence
    def _defend_setup(self, bots, now: float) -> None:
        n = len(bots)
        sites = sorted(self.sites)
        hit = {s: self.attacked.get(s, 0) for s in sites}
        weights = {k: w * self.worked.get(k, 1.0) for k, w in SETUP_WEIGHTS.items()}
        if n < 4 or max(hit.values(), default=0) - min(hit.values(), default=0) < 2:
            weights["stack"] *= 0.3              # stack only where the attackers keep going
        if n < 3:
            del weights["aggro"], weights["retake"]
        self.setup = pick_varied(weights, self.setup_history, self.rng, decay=0.6, window=4)
        self.setup_history.append(self.setup)
        rank = {"anchor": 0, "awper": 1, "support": 2, "rotator": 3}
        ordered = sorted(bots, key=lambda b: rank.get(self.roles.get(id(b), ""), 4))
        if self.setup == "stack":
            stack = max(sites, key=lambda s: (hit[s], self.rng.random()))
            plan = [stack] * 3 + [s for s in sites if s != stack][:1] + ["mid"] * max(n - 4, 0)
        elif self.setup == "retake":
            plan = sites[:2] + ["mid"] * max(n - 2, 0)
        else:
            plan = {1: ["mid"], 2: sites[:2], 3: sites[:2] + ["mid"], 4: [sites[0], sites[0], sites[-1], "mid"],
                    5: [sites[0], sites[0], sites[-1], sites[-1], "mid"]}.get(n, (sites * 3)[:n])
            plan = list(plan)
            if n == 4 and self.rng.random() < 0.5:
                plan = [sites[0], sites[-1], sites[-1], "mid"]
        plan = plan[:n]
        # anchors first to the sites, rotators to mid
        plan.sort(key=lambda a: a == "mid")
        used: set[int] = set()
        by_site: dict[str, list] = {}
        for b, area in zip(ordered, plan):
            by_site.setdefault(area, []).append(b)
        for area, group in by_site.items():
            if area == "mid":
                for b in group:
                    self._hold_mid(b, used, now)
            else:
                self._hold_site(group, area, used)
        if self.setup == "aggro":
            pushers = [b for b in bots if self.roles.get(id(b)) != "anchor" and b.brain.traits["aggression"] > 0.4]
            for b in pushers[:2]:
                self._forward(b, now)
        self.phase = "hold"
        self.shuffle_t = now + self.rng.uniform(20.0, 30.0)

    def _hold_task(self, h: dict, tag: str = "hold", walk_near: float = 0.0) -> Task:
        tm = self.tm
        p = h["pos"] if "pos" in h else tm.pos[h["i"]]
        look = None
        if h.get("sees"):
            e = tm.pos[self.rng.choice(h["sees"])]
            look = Point3(float(e[0]), float(e[1]), float(e[2]) + 1.5)
        elif h.get("yaw") is not None:
            r = math.radians(h["yaw"])
            look = Point3(float(p[0]) - math.sin(r) * 10.0, float(p[1]) + math.cos(r) * 10.0, float(p[2]) + 1.5)
        return Task("hold", Point3(float(p[0]), float(p[1]), float(p[2])), look=look, crouch=bool(h.get("crouch")),
                    wait=True, tag=tag, walk_near=walk_near)

    def _site_holds(self, site: str) -> list[dict]:
        """The tactical map's hold spots plus the map's hand-placed defender positions (map data,
        as a player learns a map), each with the entries it can see."""
        cache = self.__dict__.setdefault("_site_holds_cache", {})
        if site in cache:
            return cache[site]
        tm = self.tm
        out = list(tm.holds(site))
        top = max((h["score"] for h in out), default=1.0)
        ents = tm.entries(site)
        for a in self.holds.get(site, []):
            i = tm.nearest(a["pos"])
            if i < 0:
                continue
            sees = [e for e in ents if tm.sees(i, e) or tm.sees(i, e, low=True)]
            out.append({"i": i, "pos": Point3(a["pos"]), "score": top * 1.1, "sees": sees, "crouch": a["crouch"],
                        "yaw": a["yaw"], "authored": True})
        cache[site] = out
        return out

    def _pick_holds(self, site: str, k: int, used: set) -> list[dict]:
        """k hold spots: a crossfire for two, spots that died before used less."""
        tm = self.tm
        holds = [h for h in self._site_holds(site) if h["i"] not in used]
        if not holds:
            return []

        def w(h):
            return max(h["score"], 0.1) * (0.35 ** self.spot_deaths.get(h["i"], 0)) * self.rng.uniform(0.6, 1.4)
        if k >= 2:
            cross = [c for c in tm.spots.get(site, {}).get("crossfires", []) if c[0] not in used and c[1] not in used]
            if cross and self.rng.random() < 0.8:
                byi = {h["i"]: h for h in tm.holds(site)}
                c = max(cross, key=lambda c: w(byi[c[0]]) + w(byi[c[1]]))
                out = [dict(byi[c[0]], sees=[c[2]]), dict(byi[c[1]], sees=[c[2]])]
                rest = sorted((h for h in holds if h["i"] not in (c[0], c[1])), key=lambda h: -w(h))
                return out + rest[:k - 2]
        return sorted(holds, key=lambda h: -w(h))[:k]

    def _hold_site(self, group: list, site: str, used: set, tag: str = "hold", walk_near: float = 0.0) -> None:
        picks = self._pick_holds(site, len(group), used)
        # match spots to guns: the shortest angles to the SMGs and shotguns, the longest to the
        # rifles and the sniper
        tm = self.tm

        def reach(h):
            ents = h.get("sees") or []
            if not ents:
                return 15.0
            p = tm._xy[h["i"]]
            return float(np.mean([np.hypot(*(tm._xy[e] - p)) for e in ents]))

        def wants(b):
            w = b.weapons.primary
            cls = w.d.cls if w is not None else "pistol"
            return {"smg": 0.0, "shotgun": 0.0, "pistol": 0.3, "rifle": 0.7, "sniper": 1.0}.get(cls, 0.6)
        picks = sorted(picks, key=reach)
        group = sorted(group, key=wants)
        for b, h in zip(group, picks):
            used.add(h["i"])
            self.area_of[id(b)] = site
            b.brain.set_task(self._hold_task(h, tag, walk_near))
        for b in group[len(picks):]:
            self._hold_in(b, site, set(), tag=tag, walk_near=walk_near)

    def _hold_mid(self, b, used: set, now: float) -> None:
        self._hold_in(b, "mid", used)

    def _forward(self, b, now: float) -> None:
        """Early information: a forward spot, then back to the site after a while."""
        site = self.area_of.get(id(b))
        if site not in self.sites:
            site = self.rng.choice(sorted(self.sites))
        fw = self.tm.spots.get(site, {}).get("forward", [])
        if not fw:
            return
        f = self.rng.choice(fw[:4])
        hold = b.brain.task
        t = self._hold_task({"i": f["i"], "sees": f["sees"][:3], "crouch": False}, tag="forward")
        t.timeout = (self.live_t - now) + self.rng.uniform(12.0, 20.0)
        t.then = hold
        b.brain.set_task(t)
        self.say(b, f"Playing forward {self.tm.area(f['i'])}.", key="fwd", every=30.0)

    def _update_defend(self, bots, now: float) -> None:
        d = self.director
        bomb = d.bomb
        if bomb.state == "planted":
            if not self.planted_handled and bomb.site:
                self.attacked[bomb.site] = self.attacked.get(bomb.site, 0) + 1
            self._retake(bots, now)
            return
        for name in self.sites:
            n, util, died = self.site_evidence(name, now)
            credible = n >= 2 or (n >= 1 and (util or died)) or (died and util)
            if not credible or now - self.rotated.get(name, -100.0) < 15.0:
                continue
            cur = getattr(self, "rotation_site", "")
            if cur and cur != name and now - self.rotation_t < 30.0:
                # already rotated the other way: only clearly stronger evidence turns them round
                n_cur = self.site_evidence(cur, now, 12.0)[0]
                if n < n_cur + 2:
                    continue
            self.rotated[name] = now
            self.rotation_site, self.rotation_t = name, now
            self.attacked[name] = self.attacked.get(name, 0) + 1
            here = [b for b in bots if self.area_of.get(id(b)) == name]
            movers = [b for b in bots if self.area_of.get(id(b)) != name and
                      not (self.roles.get(id(b)) == "anchor" and self.area_of.get(id(b)) in self.sites)]
            movers.sort(key=lambda b: (b.position() - self.site_centers[name]).length())
            if len(movers) > 2:
                movers = movers[:-1]                   # someone stays to watch the other site
            if movers:
                # not one by one into a contested site: to the defenders' side of it, together,
                # where they cut it off from behind and can retake
                for b in movers:
                    p = self._regroup_point(b, self.site_centers[name])
                    self.area_of[id(b)] = name
                    b.brain.set_task(Task("hold", p, look=self.site_centers[name] + Vec3(0, 0, 1.4), wait=True,
                                          tag="rotate", walk_near=8.0))
                self.say(movers[0], f"Rotating {name}, wait for each other.", key=f"rot:{name}", every=10.0)
            # a lone anchor against three or more falls back for the retake
            if n >= 3 and len(here) <= 1:
                for b in here:
                    if id(b) not in self.fell_back:
                        self.fell_back.add(id(b))
                        p = self._regroup_point(b, self.site_centers[name])
                        b.brain.set_task(Task("hold", p, look=self.site_centers[name] + Vec3(0, 0, 1.4), wait=True,
                                              tag="fallback"))
                        self.say(b, f"Too many {name}, falling back.", key="fb", every=10.0)
            break
        if now >= self.shuffle_t and now - self.last_contact_t > 12.0:
            self.shuffle_t = now + self.rng.uniform(18.0, 30.0)
            b = self.rng.choice(bots)
            if b.brain.task.kind == "hold" and b.brain.mode == "task" and b.brain.task.tag == "hold":
                area = self.area_of.get(id(b), "mid")
                if area in self.sites:
                    used = {self.tm.nearest(m.brain.task.pos) for m in bots if m is not b and m.brain.task.pos}
                    self._hold_site([b], area, used)
                else:
                    self._hold_in(b, area, set())
        left = d.match.round_time_left()
        if left < 35.0 and now - self.last_contact_t > 15.0 and len(bots) > self.enemies_alive:
            have = sum(1 for b in bots if b.brain.task.tag == "hunt")
            hunters = sorted((b for b in bots if b.brain.task.tag != "hunt"),
                             key=lambda b: -b.brain.traits["aggression"])[:max(0, 1 - have)]
            for b in hunters:
                b.brain.set_task(Task("hunt", self._likeliest(), tag="hunt", walk=True))

    def _likeliest(self) -> Point3:
        dg = self.danger()
        i = int(np.argmax(dg)) if dg.any() else self.rng.randrange(self.tm.n)
        p = self.tm.pos[i]
        return Point3(float(p[0]), float(p[1]), float(p[2]))

    def _retake(self, bots, now: float) -> None:
        before = self.retake_phase
        super()._retake(bots, now)
        if before == "gather" and self.retake_phase == "go" and not self.retake_util:
            self.retake_util = True
            bomb = self.director.bomb
            for b in bots:
                if b.weapons.inv.grenades.get("flash", 0) > 0 and b.brain.order_throw(
                        "flash", bomb.pos + Vec3(0, 0, 2.5), 3.0):
                    self.say(b, "Flash in, retake!", key="rflash", every=10.0)
                    break
            for b in bots:
                if b.weapons.inv.grenades.get("smoke", 0) > 0:
                    p = self._attack_side_point(bomb.pos)
                    if p is not None and b.brain.order_throw("smoke", p, 4.0):
                        break

    def _site_of_point(self, p) -> str:
        return min(self.sites, key=lambda n: zone_distance(self.sites[n], p))

    def _attack_side_point(self, target: Point3) -> Point3 | None:
        """A point on the attackers' way back to the charge, to smoke it off."""
        return self._att_pts.get(self._site_of_point(target))

    def _rotation_point(self, site: str) -> Point3 | None:
        """Where defenders coming from their spawn enter the site (precomputed)."""
        return self._rot_pts.get(site)

    def _regroup_point(self, bot, bomb_pos: Point3) -> Point3:
        """A spot about 16 m short of the site on the bot's side, out of the fight (the
        retake gathers there)."""
        p = bot.position()
        if (p - bomb_pos).length() < 18.0:
            return Point3(p)
        dist = self._site_dist.get(self._site_of_point(bomb_pos))
        if dist is None:
            return Point3(p)
        tm = self.tm
        cand = np.flatnonzero((dist > 13.0) & (dist < 20.0))
        if len(cand) == 0:
            return Point3(p)
        d = np.hypot(tm._xy[cand, 0] - p.x, tm._xy[cand, 1] - p.y) + 0.5 * np.abs(dist[cand] - 16.0)
        q = tm.pos[int(cand[int(np.argmin(d))])]
        return Point3(float(q[0]), float(q[1]), float(q[2]))

    # ---------------------------------------------------------------- misc
    def round_over(self) -> None:
        """Adaptation: remember where enemies showed early (attackers: defenders at sites)."""
        for name, keys in self.early.items():
            self.site_seen[name] = 0.6 * self.site_seen.get(name, 0) + len(keys)
        self.learned = self.learned * 0.75 + self.learned_now

    def say(self, bot, text: str, key: str | None = None, every: float = 3.0) -> None:
        self.radio(bot, text, key=key, every=every)

    def update(self, now: float) -> None:
        d = self.director
        if d.match.phase not in ("live", "planted"):
            if d.match.phase == "round_end" and self.phase != "over":
                self.round_over()
                self.phase = "over"
            return
        bots = self.alive_bots()
        if not bots:
            return
        if self.side == "attack":
            self._update_attack(bots, now)
        else:
            self._update_defend(bots, now)


class GadgetAIV2(GadgetAI):
    """The Milestone 6 gadget play for v2 teams, using only gadgets the team has seen."""

    def __init__(self, team):
        self.seen: set[int] = set()
        super().__init__(team)

    def reset(self) -> None:
        super().reset()
        self.seen = set()

    def update(self, now: float) -> None:
        self._look(now)
        super().update(now)

    def _look(self, now: float) -> None:
        tac = self.tac
        side = self.team.side
        gadgets = [g for g in list(tac.deployables) + list(tac.drones)
                   if g.alive and g.side != side and id(g) not in self.seen]
        if not gadgets:
            return
        eyes = self.team.alive_bots()
        for g in gadgets:
            if self._gadget_in_sight(g, eyes):
                self.seen.add(id(g))

    def _visible_gadget(self, bot):
        """The Milestone 6 pick (closest enemy gadget in front, 22 m), limited to gadgets the
        team has actually had in its view cone (``seen``); map cameras are public."""
        g = super()._visible_gadget(bot)
        if g is not None and id(g) not in self.seen and getattr(g, "kind", "") != "camera":
            return None
        return g

    def _gadget_in_sight(self, g, eyes) -> bool:
        """The fairness audit's test (ai/audit.py): in a bot's view cone, 30 m, a clear ray."""
        p = g.center()
        phys = self.game.physics
        for b in eyes:
            e = b.eye()
            v = p - e
            dist = v.length()
            if dist > GADGET_SIGHT or dist < 1e-3:
                continue
            fwd = b.view_dir()
            if v.dot(fwd) / dist < math.cos(math.radians(float(b.profile.get("fov", 110)) * 0.5)):
                continue
            hit = phys.ray_cast(e, p, MASK_SIGHT)
            if hit is None or (hit.pos - p).length() < 0.3:
                return True
        return False

    def _emp(self, bots, now: float) -> None:
        team = self.team
        if self.emp_done or team.phase not in ("exec", "go") or not team.site:
            return
        tac = self.tac
        site_c = team.site_centers[team.site]
        targets = [d for d in list(tac.deployables) + list(tac.cameras)
                   if d.side != "attack" and d.alive and d.electronic
                   and (getattr(d, "kind", "") == "camera" or id(d) in self.seen)]
        targets = [d for d in targets if (d.center() - site_c).length() < 16.0]
        thrower = next((b for b in bots if tac.kit(b).gadget == "emp_grenade" and tac.kit(b).gadget_left > 0), None)
        if not targets or thrower is None:
            return
        tgt = min(targets, key=lambda d: (d.center() - thrower.position()).length())
        start = thrower.eye()
        if (tgt.center() - start).length() > 28.0:
            return
        vel = solve_throw(start, tgt.center(), emp_grenade_def().throw_speed, self.game.physics)
        if vel is None:
            return
        yaw, pitch = angles_to(vel.x, vel.y, vel.z)
        thrower.aim.yaw, thrower.aim.pitch = yaw, max(-60.0, min(60.0, pitch - 8.0))
        from weapons.grenades import Grenade
        self.game.spawn_grenade(Grenade(self.game, emp_grenade_def(), start + view_dir(yaw, 0) * 0.3, vel, thrower))
        tac.kit(thrower).gadget_left -= 1
        self.emp_done = True
        team.radio(thrower, "EMP out!", key="emp", every=6.0)
