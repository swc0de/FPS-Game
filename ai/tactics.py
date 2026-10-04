"""Team strategy for bots: one TeamBrain per side gives every bot its task.

Attackers
    At the start of a round the team picks a site and a plan:
    * execute - the team splits over one or two of that site's lanes,
      gathers at each lane's *staging point* outside the site, then goes in
      together after flashing the site and smoking the defenders' rotation;
    * rush    - straight down the shortest lane, no waiting;
    * default - spread over both halves of the map to take control and
      gather information, then commit (to the site where fewer defenders
      were seen) half way through the round.
    Lanes are the map's routes that end in a site. Inside the site the
    attackers clear the defenders' usual spots while the carrier plants;
    after the plant they take positions with a view of the charge, facing
    the way the defenders will come back. A dropped bomb is fetched by the
    closest attacker. With 30 s left the team goes no matter what.

Defenders
    Spread over A, B and Mid at the map's hold spots (each with the angle
    to watch). Sightings and sounds reported by any bot are shared with the
    whole team; two or more enemies near one site make the others rotate
    there. After a plant everyone retakes: they converge on the charge and
    the best placed bot (kit first) defuses while the rest cover; with no
    time left to defuse they save instead.
"""
from __future__ import annotations

import math
import random

from panda3d.core import Point3, Vec3

from ai.brain import Task
from ai.buy import bot_buy, team_buy_mode
from engine.physics import MASK_SIGHT

SITE_AREA = 30.0


def zone_center(z) -> Point3:
    return Point3((z["min"][0] + z["max"][0]) / 2, (z["min"][1] + z["max"][1]) / 2, z["min"][2] + 0.5)


def zone_distance(z, p) -> float:
    dx = max(z["min"][0] - p[0], 0.0, p[0] - z["max"][0])
    dy = max(z["min"][1] - p[1], 0.0, p[1] - z["max"][1])
    return math.hypot(dx, dy)


def build_lanes(level, defend_spawn: Point3) -> dict:
    """Map routes ending in a bomb site, starting away from the defenders' spawn."""
    sites = {z["name"]: z for z in level.zones if z["kind"] == "bombsite"}
    lanes = {name: [] for name in sites}
    for r in level.data.get("test_routes", []):
        pts = [tuple(r["start"][:3])] + [tuple(w) for w in r["waypoints"]]
        if math.hypot(pts[0][0] - defend_spawn.x, pts[0][1] - defend_spawn.y) < 15.0:
            continue
        end = pts[-1]
        for name, z in sites.items():
            if zone_distance(z, end) < 0.5:
                stage_i = 0
                for i in range(len(pts) - 1, -1, -1):
                    if zone_distance(z, pts[i]) >= 10.0:
                        stage_i = i
                        break
                lanes[name].append({"name": r["name"], "points": pts, "stage": stage_i})
    return lanes


class TeamBrain:
    def __init__(self, director, side: str):
        self.director = director
        self.side = side
        self.game = director.game
        self.nav = director.nav
        self.rng = random.Random(director.rng.random())
        level = self.game.level
        self.sites = {z["name"]: z for z in level.zones if z["kind"] == "bombsite"}
        self.site_centers = {n: zone_center(z) for n, z in self.sites.items()}
        spawns = {s: [Point3(*sp["pos"]) for sp in level.spawns if sp["team"] == s] for s in ("attack", "defend")}
        self.spawn_center = {s: _mean(v) for s, v in spawns.items() if v}
        self.lanes = build_lanes(level, self.spawn_center.get("defend", Point3()))
        self.holds = self._classify(level.data.get("practice_positions", {}).get("defend", []))
        self.lurks = [Point3(*p[:3]) for p in level.data.get("practice_positions", {}).get("attack", [])]
        self.lurk_info = {i: p for i, p in enumerate(level.data.get("practice_positions", {}).get("attack", []))}
        self.site_history = {n: 0 for n in self.sites}
        self.reports: list[tuple[float, int, Point3]] = []
        self._radio_t: dict[str, float] = {}
        self._enemies_t = -1.0
        self._enemies: list = []
        self._bias_seed: dict[int, int] = {}
        self.reset_round()

    # ------------------------------------------------------------- roster
    def bots(self) -> list:
        return [b for b in self.director.bots if b.side == self.side and b.active]

    def alive_bots(self) -> list:
        return [b for b in self.bots() if b.alive]

    def members(self) -> list:
        return [a for a in self.director.match.participants if a.side == self.side]

    def enemies(self) -> list:
        """Living enemies (cached per simulation tick: every bot asks every tick)."""
        tick = self.game.loop.time
        if self._enemies_t != tick:
            self._enemies_t = tick
            self._enemies = [a for a in self.director.match.participants
                             if a.side and a.side != self.side and a.alive and getattr(a, "active", True)]
        return self._enemies

    # -------------------------------------------------------------- setup
    def _classify(self, positions) -> dict:
        out = {"A": [], "B": [], "mid": []}
        for p in positions:
            pos = Point3(*p[:3])
            best, best_d = "mid", SITE_AREA
            for name, c in self.site_centers.items():
                d = math.hypot(pos.x - c.x, pos.y - c.y)
                if d < best_d:
                    best, best_d = name, d
            out.setdefault(best, []).append({"pos": pos, "yaw": float(p[3]), "crouch": bool(p[4])})
        return out

    def reset_round(self) -> None:
        self.plan = ""
        self.site = ""
        self.phase = "setup"
        self.groups: dict[int, dict] = {}        # bot id -> {"lane":..., "stage": Point3}
        self.stage_arrived: set[int] = set()
        self.first_stage_t = None
        self.exec_t = None
        self.commit_t = 0.0
        self.planted_handled = False
        self.retake_phase = ""
        self.gather_t = 0.0
        self.defuser = None
        self.pickup_bot = None
        self.rotated: dict[str, float] = {}
        self.reports = []
        self.area_of: dict[int, str] = {}
        self.shuffle_t = 0.0
        self.last_contact_t = -100.0
        self.round_start_t = 0.0

    def cost_bias(self, bot):
        """Per-bot pseudo-random preference per navmesh polygon: bots heading
        to the same place spread over parallel lines instead of a conga line."""
        seed = self._bias_seed.setdefault(id(bot), self.rng.randrange(1 << 30))

        def bias(rect: int) -> float:
            h = (rect * 2654435761 + seed) & 0xFFFF
            return 1.0 + 0.35 * h / 0xFFFF
        return bias

    # ---------------------------------------------------------------- buy
    def buy(self) -> None:
        d = self.director
        members = self.members()
        cfg = d.bot_config.get("economy", {})
        mode = team_buy_mode(members, d.match, self.side, cfg)
        bots = sorted(self.bots(), key=lambda b: -b.money)
        awper = next((b for b in bots if b.money >= float(cfg.get("awp_min", 5900))), None)
        if awper is not None and self.rng.random() < 0.5:
            awper = None
        kits = 0
        for b in bots:
            kit = self.side == "defend" and kits < 2
            got = bot_buy(b, d.shop, self.game.weapon_db, d.rules, mode, cfg, b is awper, kit, self.rng)
            kits += "defuse_kit" in got
        self.buy_mode = mode

    # ------------------------------------------------------- round start
    def round_start(self, now: float) -> None:
        self.reset_round()
        self.round_start_t = now
        bots = self.bots()
        if not bots:
            return
        if self.side == "attack":
            self._attack_plan(bots, now)
        else:
            self._defend_setup(bots, now)

    def _attack_plan(self, bots, now: float) -> None:
        sites = [s for s in self.sites if self.lanes.get(s)]
        if not sites:
            return
        # prefer sites that worked before, with a good share of randomness
        weights = [1.0 + 0.5 * self.site_history.get(s, 0) for s in sites]
        self.site = self.rng.choices(sites, weights)[0]
        r = self.rng.random()
        self.plan = "rush" if r < 0.18 else ("default" if r < 0.45 else "execute")
        d = self.director
        self.commit_t = now + float(d.rules["timers"]["freeze_time"]) + self.rng.uniform(25.0, 45.0)
        if self.plan == "rush":
            lane = min(self.lanes[self.site], key=lambda ln: len(ln["points"]))
            for b in bots:
                self._go_site(b, lane, now)
            self.phase = "exec"
            self.exec_t = now
            self.radio(bots[0], f"Rush {self.site}, go go go!")
        elif self.plan == "default":
            pool = list(range(len(self.lurks)))
            self.rng.shuffle(pool)
            carrier = self.director.bomb.carrier
            for b in bots:
                if b is carrier or not pool:
                    lane = self.rng.choice(self.lanes[self.site])
                    self._stage(b, lane)
                    continue
                info = self.lurk_info[pool.pop()]
                b.brain.set_task(Task("hold", Point3(*info[:3]), yaw=float(info[3]), crouch=bool(info[4]),
                                      wait=True, tag="control"))
            self.phase = "control"
            self.radio(bots[0], "Spread out, take map control.")
        else:
            lanes = list(self.lanes[self.site])
            self.rng.shuffle(lanes)
            use = lanes[:2] if len(bots) >= 3 and len(lanes) > 1 and self.rng.random() < 0.6 else lanes[:1]
            for i, b in enumerate(bots):
                self._stage(b, use[i % len(use)])
            self.phase = "stage"
            self.radio(bots[0], f"Let's take {self.site}" + (" from two sides." if len(use) > 1 else "."))

    def _stage(self, bot, lane) -> None:
        pts = lane["points"]
        st = pts[lane["stage"]]
        stage = self._snap(st)
        self.groups[id(bot)] = {"lane": lane, "stage": stage}
        site_c = self.site_centers[self.site]
        bot.brain.set_task(Task("move", stage, via=list(pts[:lane["stage"]]), look=site_c + Vec3(0, 0, 1.0),
                                wait=True, tag="stage"))

    def _go_site(self, bot, lane, now: float) -> None:
        """Into the site along the rest of the lane: the carrier plants, the others clear spots."""
        pts = lane["points"]
        via = list(pts[lane["stage"]:])
        if self.director.bomb.carrier is bot:
            bot.brain.set_task(Task("plant", self._plant_spot(), via=via, tag="plant"))
            return
        spots = [h["pos"] for h in self.holds.get(self.site, [])] or [self.site_centers[self.site]]
        taken = {id(t.pos) for t in (b.brain.task for b in self.alive_bots()) if t.pos is not None}
        free = [s for s in spots if id(s) not in taken] or spots
        spot = self.rng.choice(free)
        bot.brain.set_task(Task("move", spot, via=via, look=self.site_centers[self.site] + Vec3(0, 0, 1.2),
                                wait=True, tag="clear"))

    def _plant_spot(self) -> Point3:
        z = self.sites[self.site]
        c = self.site_centers[self.site]
        for _ in range(20):
            p = self.nav.random_point(self.rng, (c.x, c.y, z["min"][2] + 0.6), 4.0)
            if p is not None and zone_distance(z, p) == 0.0:
                return Point3(*p)
        return self._snap(c)

    def _defend_setup(self, bots, now: float) -> None:
        n = len(bots)
        plan = {1: ["mid"], 2: ["A", "B"], 3: ["A", "B", "mid"], 4: ["A", "A", "B", "mid"],
                5: ["A", "A", "B", "B", "mid"]}.get(n, ["A", "A", "B", "B", "mid"] + ["mid"] * (n - 5))
        plan = list(plan)
        if n == 4 and self.rng.random() < 0.5:
            plan = ["A", "B", "B", "mid"]
        if n >= 4 and self.rng.random() < 0.15:          # stack a site
            stack = self.rng.choice(["A", "B"])
            plan = [stack] * 3 + ["mid"] * (n - 3)
        self.rng.shuffle(plan)
        used: set[int] = set()
        for b, area in zip(bots, plan):
            self._hold_in(b, area, used)
        self.phase = "hold"
        self.shuffle_t = now + self.rng.uniform(20.0, 30.0)

    def _hold_in(self, bot, area: str, used: set, tag: str = "hold", walk_near: float = 0.0) -> None:
        pool = [h for h in self.holds.get(area, []) if id(h) not in used] or self.holds.get(area, []) or \
            [h for v in self.holds.values() for h in v]
        if not pool:
            return
        h = self.rng.choice(pool)
        used.add(id(h))
        self.area_of[id(bot)] = area
        bot.brain.set_task(Task("hold", h["pos"], yaw=h["yaw"], crouch=h["crouch"], wait=True, tag=tag,
                                walk_near=walk_near))

    # ------------------------------------------------------------- events
    def report(self, bot, enemy, pos: Point3, now: float, kind: str) -> None:
        """A bot saw or heard an enemy: share it with the team."""
        self.reports.append((now, id(enemy), Point3(pos)))
        self.last_contact_t = now
        if len(self.reports) > 60:
            self.reports = self.reports[-60:]
        for b in self.alive_bots():
            if b is not bot:
                b.perception.on_callout(enemy, pos, now)
        where = self.game.level.callout_at(pos.x, pos.y) or "somewhere"
        if kind == "seen":
            self.radio(bot, f"Enemy spotted: {where}", key=f"seen:{where}", every=6.0)
        else:
            self.radio(bot, f"Footsteps near {where}", key=f"heard:{where}", every=8.0)

    def on_teammate_killed(self, victim, killer, now: float) -> None:
        if killer is not None and killer.side != self.side:
            self.reports.append((now, id(killer), killer.position()))
            for b in self.alive_bots():
                b.perception.on_callout(killer, killer.position(), now)

    def on_arrived(self, bot, task: Task) -> None:
        if task.tag == "stage":
            self.stage_arrived.add(id(bot))
            if self.first_stage_t is None:
                self.first_stage_t = bot.now

    def task_done(self, bot, task: Task) -> None:
        if task.kind in ("hunt", "pickup", "plant", "defuse"):
            bot.brain.set_task(Task("idle"))

    def radio(self, bot, text: str, key: str | None = None, every: float = 3.0) -> None:
        now = self.game.loop.time
        k = f"{self.side}:{key or text}"
        if now - self._radio_t.get(k, -100.0) < every:
            return
        self._radio_t[k] = now
        self.director.radio(bot, text)

    # ------------------------------------------------------------- update
    def update(self, now: float) -> None:
        d = self.director
        if d.match.phase not in ("live", "planted"):
            return
        bots = self.alive_bots()
        if not bots:
            return
        if self.side == "attack":
            self._update_attack(bots, now)
        else:
            self._update_defend(bots, now)

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
        # human carrier near a site: follow their lead
        carrier = bomb.carrier
        if carrier is not None and carrier.is_human and self.phase in ("stage", "control"):
            for name, z in self.sites.items():
                if zone_distance(z, carrier.position()) < 15.0 and name != self.site and self.lanes.get(name):
                    self.site = name
                    self._execute(bots, now)
                    return
        # dropped bomb: closest bot fetches it
        if bomb.state == "dropped":
            if self.pickup_bot is None or not self.pickup_bot.alive or self.pickup_bot not in bots:
                self.pickup_bot = min(bots, key=lambda b: (b.position() - bomb.pos).length())
                self.pickup_bot.brain.set_task(Task("pickup", Point3(bomb.pos), tag="bomb"))
                self.radio(self.pickup_bot, "I'll get the charge.")
        elif self.pickup_bot is not None:
            pb = self.pickup_bot
            self.pickup_bot = None
            if pb.alive and bomb.carrier is pb:
                self.radio(pb, "I have the charge.")
                if self.phase == "exec":
                    pb.brain.set_task(Task("plant", self._plant_spot(), tag="plant"))
                else:
                    lane = self.rng.choice(self.lanes[self.site])
                    self._stage(pb, lane)
        if self.phase == "control" and (now >= self.commit_t or left < 50.0):
            self.site = self._quieter_site(now)
            for b in bots:
                lane = self.rng.choice(self.lanes[self.site])
                self._stage(b, lane)
            self.phase = "stage"
            self.first_stage_t = None
            self.stage_arrived = set()
            self.radio(bots[0], f"Regroup for {self.site}.")
        elif self.phase == "stage":
            staged = [b for b in bots if id(b) in self.stage_arrived]
            ready = len(staged) >= max(1, math.ceil(len(bots) * 0.6))
            waited = self.first_stage_t is not None and now - self.first_stage_t > 12.0
            if ready or waited or left < 35.0:
                self._execute(bots, now)
        elif self.phase == "exec":
            # carrier inside: plant; time pressure keeps everyone moving in
            c = bomb.carrier
            if c is not None and not c.is_human and c.alive and c.brain.task.kind != "plant":
                c.brain.set_task(Task("plant", self._plant_spot(), tag="plant"))
            if left < 25.0:
                for b in bots:
                    if b.brain.task.kind == "hold":
                        self._go_site(b, self.rng.choice(self.lanes[self.site]), now)

    def _execute(self, bots, now: float) -> None:
        self.phase = "exec"
        self.exec_t = now
        site_c = self.site_centers[self.site]
        # utility: one flash into the site, one smoke on the defenders' way back
        flasher = next((b for b in bots if b.weapons.inv.grenades.get("flash", 0) > 0), None)
        if flasher is not None:
            flasher.brain.order_throw("flash", site_c + Vec3(0, 0, 1.5), 4.0)
        smoker = next((b for b in bots if b is not flasher and b.weapons.inv.grenades.get("smoke", 0) > 0), None)
        if smoker is not None:
            p = self._rotation_point(self.site)
            if p is not None:
                smoker.brain.order_throw("smoke", p, 5.0)
        for b in bots:
            g = self.groups.get(id(b))
            lane = g["lane"] if g else min(self.lanes[self.site],
                                           key=lambda ln: (Point3(*ln["points"][ln["stage"]][:2], 0) -
                                                           Point3(b.position().x, b.position().y, 0)).length())
            self._go_site(b, lane, now)
        self.radio(bots[0], f"Go {self.site}! Go!")

    def _rotation_point(self, site: str) -> Point3 | None:
        """Where defenders rotating from their spawn enter the site."""
        dspawn = self.spawn_center.get("defend")
        if dspawn is None:
            return None
        path = self.nav.find_path(self.site_centers[site], dspawn)
        if not path:
            return None
        return _along(path, 9.0)

    def _quieter_site(self, now: float) -> str:
        counts = {n: 0 for n in self.sites if self.lanes.get(n)}
        for t, _, p in self.reports:
            if now - t < 30.0:
                for n in counts:
                    if zone_distance(self.sites[n], p) < SITE_AREA:
                        counts[n] += 1
        low = min(counts.values())
        return self.rng.choice([n for n, c in counts.items() if c == low])

    def _post_plant(self, bots, now: float) -> None:
        bomb = self.director.bomb
        spots = self._post_plant_spots(bomb.pos, len(bots))
        dspawn = self.spawn_center.get("defend")
        approach = None
        if dspawn is not None:
            path = self.nav.find_path(bomb.pos, dspawn)
            if path:
                approach = _along(path, 12.0)
        for b, s in zip(bots, spots):
            look = (approach + Vec3(0, 0, 1.4)) if approach is not None and self.rng.random() < 0.7 \
                else bomb.pos + Vec3(0, 0, 0.5)
            b.brain.set_task(Task("guard", s, look=look, crouch=self.rng.random() < 0.4, wait=True, tag="post"))
        self.radio(bots[0], f"Charge planted at {bomb.site}. Hold it!")

    def _flee(self, bots, bomb_pos: Point3) -> None:
        """Get out of the blast before the charge goes off."""
        for b in bots:
            p = b.position()
            if (p - bomb_pos).length() > 32.0:
                continue
            away = Vec3(p.x - bomb_pos.x, p.y - bomb_pos.y, 0)
            if away.lengthSquared() < 1e-4:
                away = Vec3(1, 0, 0)
            away.normalize()
            best = None
            for k in range(16):
                q = self.nav.random_point(self.rng, (p.x + away.x * 26, p.y + away.y * 26, p.z), 10.0)
                if q is not None and (Point3(*q) - bomb_pos).length() > 30.0:
                    best = Point3(*q)
                    break
            if best is None:
                best = self._snap(self.spawn_center.get("attack", p))
            b.brain.set_task(Task("move", best, wait=True, tag="flee"))
        self.radio(bots[0], "Charge is about to blow, get clear!")

    def _post_plant_spots(self, bomb_pos: Point3, n: int) -> list[Point3]:
        phys = self.game.physics
        target = bomb_pos + Vec3(0, 0, 0.3)
        out: list[Point3] = []
        for _ in range(40):
            p = self.nav.random_point(self.rng, (bomb_pos.x, bomb_pos.y, bomb_pos.z), 13.0)
            if p is None:
                continue
            q = Point3(*p)
            if (q - bomb_pos).length() < 5.0 or any((q - o).length() < 3.0 for o in out):
                continue
            if phys.ray_cast(q + Vec3(0, 0, 1.4), target, MASK_SIGHT) is not None:
                continue
            out.append(q)
            if len(out) >= n:
                break
        while len(out) < n:
            out.append(self._snap(bomb_pos + Vec3(self.rng.uniform(-4, 4), self.rng.uniform(-4, 4), 0)))
        return out

    # ------------------------------------------------------------ defense
    def _update_defend(self, bots, now: float) -> None:
        d = self.director
        bomb = d.bomb
        if bomb.state == "planted":
            self._retake(bots, now)
            return
        # rotate when two or more enemies show up near one site
        for name, z in self.sites.items():
            seen = {eid for t, eid, p in self.reports if now - t < 6.0 and zone_distance(z, p) < 20.0}
            carrier = bomb.carrier
            if carrier is not None and any(eid == id(carrier) for t, eid, p in self.reports
                                           if now - t < 6.0 and zone_distance(z, p) < 20.0):
                seen.add(-1)
            if len(seen) >= 2 and now - self.rotated.get(name, -100.0) > 15.0:
                self.rotated[name] = now
                movers = [b for b in bots if self.area_of.get(id(b)) != name]
                if len(movers) > 2:
                    movers = movers[:-1]           # someone stays to watch the other site
                used: set[int] = set()
                for b in movers:
                    self._hold_in(b, name, used, tag="rotate", walk_near=12.0)
                if movers:
                    self.radio(movers[0], f"They're at {name}! Rotating.")
                break
        # an occasional repositioning keeps the setup less predictable
        if now >= self.shuffle_t and now - self.last_contact_t > 12.0:
            self.shuffle_t = now + self.rng.uniform(18.0, 30.0)
            b = self.rng.choice(bots)
            if b.brain.task.kind == "hold" and b.brain.mode == "task":
                self._hold_in(b, self.area_of.get(id(b), "mid"), set())
        # late in the round with no information: go hunting
        left = d.match.round_time_left()
        if left < 35.0 and now - self.last_contact_t > 15.0:
            have = sum(1 for b in bots if b.brain.task.tag == "hunt")
            hunters = [b for b in bots if b.brain.task.tag != "hunt"][:max(0, 2 - have)]
            for b in hunters:
                target = self._latest_report_pos() or self.rng.choice(list(self.site_centers.values()))
                b.brain.set_task(Task("hunt", self._snap(target), tag="hunt"))

    def _retake(self, bots, now: float) -> None:
        d = self.director
        bomb = d.bomb
        left = d.match.bomb_time_left()
        t = d.rules["timers"]
        def eta(b):
            dist = (b.position() - bomb.pos).length()
            return dist / 5.0 + float(t["defuse_time_kit" if b.has_kit else "defuse_time"])
        if not self.planted_handled:
            # regroup out of sight of the site first, then retake together
            self.planted_handled = True
            self.retake_phase = "gather"
            self.gather_t = now
            self.defuser = None
            self.radio(bots[0], f"Charge is down at {bomb.site}. Group up for the retake!")
            for b in bots:
                b.brain.set_task(Task("move", self._regroup_point(b, bomb.pos), look=bomb.pos + Vec3(0, 0, 1.4),
                                      tag="regroup", wait=True))
        if self.retake_phase == "gather":
            ready = [b for b in bots if b.brain.task.tag == "regroup" and b.brain.arrived]
            pressed = left - max(eta(b) for b in bots) < 8.0
            if len(ready) >= min(2, len(bots)) or pressed or now - self.gather_t > 10.0:
                self.retake_phase = "go"
                self.radio(bots[0], "Go go go, retake!")
                for b in bots:
                    b.brain.set_task(Task("move", self._snap(bomb.pos), tag="retake", wait=True))
            else:
                return
        # who defuses: kit holders and the closest go first
        if self.defuser is not None and (not self.defuser.alive or self.defuser not in bots):
            self.defuser = None
        if self.defuser is None:
            cand = min(bots, key=eta)
            need = eta(cand)
            if need > left + 0.5:
                if self.retake_phase != "save":
                    self.retake_phase = "save"
                    self.radio(cand, "No time, save!")
                    spawn = self.spawn_center.get("defend")
                    for b in bots:
                        if spawn is not None:
                            b.brain.set_task(Task("hold", self._snap(spawn), tag="save", wait=True))
                return
            close = (cand.position() - bomb.pos).length() < 14.0
            quiet = now - self.last_contact_t > 2.0
            if (close and quiet) or need > left - 4.0:
                self.defuser = cand
                cand.brain.set_task(Task("defuse", Point3(bomb.pos), tag="defuse"))
                for b in bots:
                    if b is not cand:
                        look = self._latest_report_pos() or bomb.pos
                        b.brain.set_task(Task("guard", self._snap(bomb.pos + Vec3(self.rng.uniform(-5, 5),
                                                                                  self.rng.uniform(-5, 5), 0)),
                                              look=look + Vec3(0, 0, 1.4), wait=True, tag="cover"))

    def _regroup_point(self, bot, bomb_pos: Point3) -> Point3:
        """A spot on the bot's own way to the charge, ~16 m short of it."""
        p = bot.position()
        if (p - bomb_pos).length() < 18.0:
            return Point3(p)
        path = self.nav.find_path(bomb_pos, p)
        if not path:
            return Point3(p)
        return _along(path, 16.0)

    def _latest_report_pos(self) -> Point3 | None:
        return self.reports[-1][2] if self.reports else None

    # ------------------------------------------------------------ helpers
    def _snap(self, p) -> Point3:
        q = self.nav.snap((p[0], p[1], p[2] if len(p) > 2 else 0.0), search=10)
        return Point3(*q) if q is not None else Point3(p[0], p[1], p[2] if len(p) > 2 else 0.0)


def _mean(points) -> Point3:
    n = len(points)
    return Point3(sum(p.x for p in points) / n, sum(p.y for p in points) / n, sum(p.z for p in points) / n)


def _along(path, dist: float) -> Point3:
    left = dist
    for a, b in zip(path, path[1:]):
        seg = math.dist(a[:2], b[:2])
        if seg >= left:
            t = left / max(seg, 1e-6)
            return Point3(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
        left -= seg
    return Point3(*path[-1])
