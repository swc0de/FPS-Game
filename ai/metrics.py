"""Behaviour metrics for bot matches (``--demo bots``, tools/bot_metrics.py).

The collector watches a match from the outside (director events, damage
callbacks and a few wrapped methods) and never changes a decision. Every
number is also split by AI ("legacy" / "v2"), so a head-to-head reports
how each brain played. Definitions:

* deaths while reloading - the victim's gun was mid-reload when it died
* unseen deaths          - the victim had not seen its killer in the 5 s
                           before dying (gun and knife kills)
* traded deaths          - the killer died to one of the victim's teammates
                           within 3 s; "tradeable" deaths had a living
                           teammate within 15 m
* exposure before death  - how long the killer had had the victim in
                           continuous sight when the fatal shot landed
* exposed share          - share of a bot's live time in which at least one
                           enemy bot sees it (sampled at 16 Hz)
* stacking incidents     - two teammates within 0.8 m (bodies almost
                           touching) for 1 s or more, once per episode
* clumps                 - three or more teammates within 2.5 m for 2 s
* stuck                  - a move order without 0.6 m of progress in 5 s;
                           micro-stucks are the path follower's 0.8 s
                           no-progress events
* utility                - grenades thrown per type; a flash is effective
                           when it blinds an enemy for 1 s or more, a frag
                           when it damages an enemy, a smoke when it blocks
                           at least one enemy sighting while it lasts
* plans                  - the attack plan and site of every round
* info leaks (legacy)    - an exact attacker position given to a bot shot by
                           an enemy it had not seen, and an exact killer
                           position broadcast after an unseen kill
"""
from __future__ import annotations

import math
from collections import defaultdict

SAMPLE_EVERY = 4                   # ticks between position samples (16 Hz)
_ACTIVE = None                     # the collector the class-level hooks report to
_HOOKED = False


def _install_hooks() -> None:
    """Class-level wrappers, installed once per process; they report to _ACTIVE."""
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True
    import ai.bot as bot_mod
    import ai.perception as per_mod
    import ai.tactics as tac_mod
    import weapons.grenades as gren_mod

    orig_scan = per_mod.Perception._scan

    def _scan(self, now, enemies):
        col = _ACTIVE
        if col is None:
            return orig_scan(self, now, enemies)
        col._in_scan = self.bot
        try:
            orig_scan(self, now, enemies)
        finally:
            col._in_scan = None
        for c in self.contacts.values():
            if c.seen:
                key = (id(self.bot), id(c.agent))
                col.last_seen[key] = now
                col.round_seen.add(key)
    per_mod.Perception._scan = _scan

    orig_damaged = per_mod.Perception.on_damaged

    def on_damaged(self, attacker, now):
        col = _ACTIVE
        if col is not None and attacker is not None and getattr(attacker, "side", None) != self.bot.side:
            if now - col.last_seen.get((id(self.bot), id(attacker)), -99.0) > 1.0:
                col.leaks["exact_attacker_position_when_shot_by_unseen_enemy"] += 1
        return orig_damaged(self, attacker, now)
    per_mod.Perception.on_damaged = on_damaged

    orig_killed = tac_mod.TeamBrain.on_teammate_killed

    def on_killed(self, victim, killer, now):
        col = _ACTIVE
        if col is not None and killer is not None and killer.side != self.side and self.alive_bots():
            if now - col.last_seen.get((id(victim), id(killer)), -99.0) > 1.0:
                col.leaks["exact_killer_position_broadcast_after_unseen_kill"] += 1
        return orig_killed(self, victim, killer, now)
    tac_mod.TeamBrain.on_teammate_killed = on_killed

    orig_throw = bot_mod.BotWeapons.throw

    def throw(self, key, velocity):
        ok = orig_throw(self, key, velocity)
        if ok and _ACTIVE is not None:
            _ACTIVE.thrown[f"{_ACTIVE.ai_of(self.bot)}:{self.bot.side}:{key}"] += 1
        return ok
    bot_mod.BotWeapons.throw = throw

    orig_det = gren_mod.Grenade.detonate

    def detonate(self):
        col = _ACTIVE
        if col is None:
            return orig_det(self)
        thrower = self.thrower
        rec = {"key": self.d.key, "side": getattr(thrower, "side", ""), "ai": col.ai_of(thrower),
               "t": col.game.loop.time, "pos": tuple(self.pos), "enemy_flashed": 0, "team_flashed": 0,
               "enemy_damaged": 0, "sightings_blocked": 0}
        col.grenades.append(rec)
        col._ctx_grenade = rec
        try:
            return orig_det(self)
        finally:
            col._ctx_grenade = None
    gren_mod.Grenade.detonate = detonate

    orig_flashed = bot_mod.BotAgent.flashed

    def flashed(self, duration, strength):
        col = _ACTIVE
        rec = col._ctx_grenade if col is not None else None
        if rec is not None and duration >= 1.0:
            rec["enemy_flashed" if self.side != rec["side"] else "team_flashed"] += 1
        return orig_flashed(self, duration, strength)
    bot_mod.BotAgent.flashed = flashed


class MetricsCollector:
    def __init__(self, game, director=None):
        global _ACTIVE
        _install_hooks()
        _ACTIVE = self
        self.game = game
        d = director or game.director
        self.d = d
        d.listeners.append(self._event)
        self.tick_n = 0
        self.round_t0 = 0.0
        self.rounds: list[dict] = []
        self.plans: list[dict] = []
        self.deaths: list[dict] = []
        self.last_seen: dict[tuple[int, int], float] = {}
        self.round_seen: set[tuple[int, int]] = set()
        self.exposed_t = defaultdict(float)
        self.alive_t = defaultdict(float)
        self.pairs: dict[tuple, list] = {}
        self.stacking: list[dict] = []
        self.clumps: list[dict] = []
        self._clump_since: dict[str, float] = {}
        self.thrown = defaultdict(int)
        self.grenades: list[dict] = []
        self.leaks = defaultdict(int)
        self.hold_spots: list[tuple] = []
        self._holds_sampled = False
        self._ctx_grenade = None
        self._in_scan = None
        self.plants = 0
        self.defuses = 0
        self.hits = defaultdict(int)
        self.stuck: dict[int, list] = {}
        self.stuck_reports: list[str] = []
        self._plan_logged = False
        fx = game.effects
        orig_smoke = fx.smoke_between

        def smoke_between(a, b):
            v = orig_smoke(a, b)
            bot = self._in_scan
            if bot is not None and v >= 0.55:
                self._smoke_block(bot, a, b)
            return v
        fx.smoke_between = smoke_between
        for b in d.bots:
            b.damageable.on_damage.append(lambda res, b=b: self._damage(b, res))

    def ai_of(self, agent) -> str:
        return self.d.ai_of(agent) if agent is not None and hasattr(self.d, "ai_of") else "legacy"

    # -------------------------------------------------------------- hooks
    def _smoke_block(self, bot, a, b) -> None:
        now = self.game.loop.time
        for rec in self.grenades:
            if rec["key"] != "smoke" or rec["side"] == bot.side or now - rec["t"] > 20.0 or rec["pos"] is None:
                continue
            if _seg_point_dist(a, b, rec["pos"]) < 4.5:
                rec["sightings_blocked"] += 1

    def _damage(self, victim, res) -> None:
        info = res.info
        attacker = self.d.agent_of(info.attacker)
        if info.kind == "bullet" and attacker is not None:
            self.hits[self.ai_of(attacker)] += 1
        rec = self._ctx_grenade
        if rec is not None and info.weapon == "frag" and attacker is not None and attacker.side != victim.side:
            rec["enemy_damaged"] += 1

    # -------------------------------------------------------------- events
    def _event(self, kind: str, data: dict) -> None:
        now = self.game.loop.time
        m = self.d.match
        if kind == "round_start":
            self.round_seen = set()
            self._holds_sampled = False
            self.pairs = {}
            self.stuck = {}
            self._plan_logged = False
        elif kind == "live":
            self.round_t0 = now
        elif kind == "kill":
            self._kill(data, now)
        elif kind == "bomb_planted":
            self.plants += 1
        elif kind == "bomb_defused":
            self.defuses += 1
        elif kind == "round_end":
            r = data["result"]
            team = next((t for t in m.teams if t.side == r.winner_side), None)
            self.rounds.append({"round": m.round, "winner": r.winner_side, "reason": r.reason,
                                "winner_ai": self.d.team_ai.get(team.index, "legacy") if team is not None else "",
                                "attack_ai": self._side_ai("attack"), "length": now - self.round_t0})

    def _side_ai(self, side: str) -> str:
        team = next((t for t in self.d.match.teams if t.side == side), None)
        return self.d.team_ai.get(team.index, "legacy") if team is not None and hasattr(self.d, "team_ai") else ""

    def _kill(self, data: dict, now: float) -> None:
        k, v = data.get("killer"), data["victim"]
        weapon = data.get("weapon", "")
        kind = "bullet" if weapon not in ("frag", "bomb", "world", "") else weapon or "world"
        rec = {"t": now, "round": self.d.match.round, "victim": v.name, "victim_side": v.side,
               "victim_ai": self.ai_of(v), "killer": k.name if k is not None else None,
               "killer_side": k.side if k is not None else None, "killer_ai": self.ai_of(k) if k else None,
               "weapon": weapon, "kind": kind, "headshot": bool(data.get("headshot")),
               "traded": False, "tradeable": False}
        ws = v.weapons.inv.current() if hasattr(v, "weapons") else None
        rec["reloading"] = bool(ws is not None and ws.reloading)
        rec["empty_mag"] = bool(ws is not None and ws.d.magazine > 0 and ws.ammo == 0)
        if k is not None and k.side != v.side:
            seen_t = self.last_seen.get((id(v), id(k)), -99.0)
            rec["victim_saw_killer_5s"] = now - seen_t <= 5.0
            rec["victim_ever_saw_killer_this_round"] = (id(v), id(k)) in self.round_seen
            c = k.perception.contacts.get(id(v)) if hasattr(k, "perception") else None
            rec["exposure_before_death"] = (now - c.since) if (c is not None and c.seen) else None
            rec["distance"] = (k.position() - v.position()).length()
            rec["victim_mode"] = _mode(v)
            rec["killer_mode"] = _mode(k)
            mates = [b for b in self.d.bots if b.side == v.side and b is not v and b.active and b.alive]
            rec["tradeable"] = any((b.position() - v.position()).length() < 15.0 for b in mates)
        if k is not None:
            for d in self.deaths:
                if d["killer"] == v.name and d["victim_side"] == k.side and now - d["t"] <= 3.0 and not d["traded"]:
                    d["traded"] = True
        self.deaths.append(rec)

    # ---------------------------------------------------------------- tick
    def tick(self, dt: float) -> None:
        self.tick_n += 1
        if self.tick_n % SAMPLE_EVERY:
            return
        d = self.d
        m = d.match
        if m.phase not in ("live", "planted"):
            return
        now = self.game.loop.time
        sdt = dt * SAMPLE_EVERY
        if not self._plan_logged:
            self._plan_logged = True
            tb = d.team_brains.get("attack")
            if tb is not None:
                self.plans.append({"round": m.round, "plan": getattr(tb, "plan", ""), "site": getattr(tb, "site", ""),
                                   "ai": self._side_ai("attack")})
        bots = [b for b in d.bots if b.active and b.alive]
        for b in bots:
            self.alive_t[b.name] += sdt
            if any(e.side != b.side and (c := e.perception.contacts.get(id(b))) is not None and c.seen
                   for e in bots):
                self.exposed_t[b.name] += sdt
        for side in ("attack", "defend"):
            team = [b for b in bots if b.side == side]
            for i, a in enumerate(team):
                pa = a.position()
                for b in team[i + 1:]:
                    pb = b.position()
                    key = (a.name, b.name)
                    dist = math.hypot(pa.x - pb.x, pa.y - pb.y)
                    rec = self.pairs.get(key)
                    if dist < 0.8 and abs(pa.z - pb.z) < 1.0:
                        if rec is None:
                            self.pairs[key] = [now, False]
                        elif not rec[1] and now - rec[0] >= 1.0:
                            rec[1] = True
                            self.stacking.append({"t": now, "round": m.round, "pair": key, "ai": self.ai_of(a),
                                                  "where": self.game.level.callout_at(pa.x, pa.y),
                                                  "modes": (_mode(a), _mode(b))})
                    elif rec is not None and dist > 1.2:
                        del self.pairs[key]
            clump = any(sum(1 for b in team if (b.position() - a.position()).length() < 2.5) >= 3 for a in team)
            if clump:
                since = self._clump_since.get(side)
                if since is None:
                    self._clump_since[side] = now
                elif since >= 0 and now - since >= 2.0:
                    self._clump_since[side] = -1.0
                    self.clumps.append({"t": now, "round": m.round, "side": side, "ai": self._side_ai(side)})
            else:
                self._clump_since.pop(side, None)
        if not self._holds_sampled and now - self.round_t0 > 10.0:
            self._holds_sampled = True
            for b in bots:
                t = b.brain.task
                if b.side == "defend" and t.kind == "hold" and t.pos is not None:
                    self.hold_spots.append((round(t.pos.x, 1), round(t.pos.y, 1)))
        self._stuck_check(bots, now)

    def _stuck_check(self, bots, now: float) -> None:
        for b in bots:
            br = b.brain
            moving = br.follower.active and b.intent.wish.lengthSquared() > 0.25 and \
                getattr(br, "mode", "task") in ("task", "alert", "seek", "retreat", "move")
            rec = self.stuck.get(id(b))
            p = b.position()
            if not moving or rec is None:
                self.stuck[id(b)] = [p, now, False]
                continue
            if (p - rec[0]).length() > 0.6:
                self.stuck[id(b)] = [p, now, False]
            elif now - rec[1] > 5.0 and not rec[2]:
                rec[2] = True
                msg = (f"{b.name} ({self.ai_of(b)}) stuck at ({p.x:.1f}, {p.y:.1f}, {p.z:.2f}) "
                       f"{self.game.level.callout_at(p.x, p.y)} - {br.describe()}")
                self.stuck_reports.append(msg)
                self.game.log(f"[bots] STUCK {msg}")

    # ------------------------------------------------------------- summary
    def summary(self) -> dict:
        out = self._summary(None)
        kinds = sorted({self.ai_of(b) for b in self.d.bots})
        if len(kinds) > 1:
            out["by_ai"] = {k: self._summary(k) for k in kinds}
            n = len(self.rounds)
            out["head_to_head"] = {k: _wilson(sum(1 for r in self.rounds if r["winner_ai"] == k), n) for k in kinds}
            for k in kinds:
                for side in ("attack", "defend"):
                    rs = [r for r in self.rounds if (r["attack_ai"] == k) == (side == "attack")]
                    out["head_to_head"][k][f"won_on_{side}"] = sum(1 for r in rs if r["winner_ai"] == k)
                    out["head_to_head"][k][f"rounds_on_{side}"] = len(rs)
        out["deaths"] = self.deaths
        out["stacking"] = self.stacking
        out["rounds_detail"] = self.rounds
        out["plans_detail"] = self.plans
        if self.d.audit is not None:
            out["audit"] = self.d.audit.summary()
        return out

    def _summary(self, ai: str | None) -> dict:
        def mine(name_ai):
            return ai is None or name_ai == ai
        rounds = self.rounds
        wins = defaultdict(int)
        reasons = defaultdict(int)
        for r in rounds:
            wins[r["winner"]] += 1
            reasons[f"{r['winner']}/{r['reason']}"] += 1
        pvp = [x for x in self.deaths if x.get("killer_side") and x["killer_side"] != x["victim_side"]
               and mine(x["victim_ai"])]
        gun = [x for x in pvp if x["kind"] == "bullet"]
        tradeable = [x for x in pvp if x["tradeable"]]
        exp = [x["exposure_before_death"] for x in gun if x.get("exposure_before_death") is not None]
        plans = defaultdict(int)
        plan_sites = defaultdict(int)
        my_plans = [p for p in self.plans if mine(p.get("ai", ai))]
        for p in my_plans:
            plans[p["plan"]] += 1
            plan_sites[f"{p['plan']} {p['site']}"] += 1
        holds = defaultdict(int)
        for h in self.hold_spots:
            holds[h] += 1
        util = {}
        for key in ("flash", "smoke", "frag"):
            recs = [g for g in self.grenades if g["key"] == key and mine(g["ai"])]
            if key == "flash":
                eff = [g for g in recs if g["enemy_flashed"] > 0]
                extra = {"team_flashes": sum(1 for g in recs if g["team_flashed"] > 0)}
            elif key == "frag":
                eff = [g for g in recs if g["enemy_damaged"] > 0]
                extra = {}
            else:
                eff = [g for g in recs if g["sightings_blocked"] > 0]
                extra = {"enemy_sightings_blocked": sum(g["sightings_blocked"] for g in recs)}
            util[key] = {"detonated": len(recs), "effective": len(eff),
                         "effective_pct": 100.0 * len(eff) / max(len(recs), 1), **extra}
        names = {b.name for b in self.d.bots if mine(self.ai_of(b))}
        alive = sum(v for k, v in self.alive_t.items() if k in names)
        exposed = sum(v for k, v in self.exposed_t.items() if k in names)
        micro = sum(b.brain.follower.stuck_events for b in self.d.bots if b.name in names)
        deaths_n = sum(1 for x in self.deaths if mine(x["victim_ai"]))
        fired = sum(b.shots_fired for b in self.d.bots if b.name in names)
        hits = sum(v for k, v in self.hits.items() if mine(k))
        stack = [s for s in self.stacking if mine(s.get("ai"))]
        clumps = [c for c in self.clumps if mine(c.get("ai"))]
        stuck = [s for s in self.stuck_reports if ai is None or f"({ai})" in s]
        kills = [x for x in self.deaths if x.get("killer_side") and x["killer_side"] != x["victim_side"]
                 and (ai is None or x.get("killer_ai") == ai)]
        thrown = {k: v for k, v in self.thrown.items() if ai is None or k.startswith(ai + ":")}
        return {
            "rounds": len(rounds),
            "wins_by_side": dict(wins),
            "attack_win_pct": 100.0 * wins.get("attack", 0) / max(len(rounds), 1),
            "results": dict(reasons),
            "avg_round_s": sum(r["length"] for r in rounds) / max(len(rounds), 1),
            "kills": len(kills),
            "deaths_total": deaths_n,
            "headshot_pct": 100.0 * sum(1 for x in kills if x["headshot"]) / max(len(kills), 1),
            "plants": self.plants, "defuses": self.defuses,
            "shots": fired, "bullet_hits": hits, "accuracy_pct": 100.0 * hits / max(fired, 1),
            "deaths_while_reloading": sum(1 for x in pvp if x["reloading"]),
            "deaths_while_reloading_pct": 100.0 * sum(1 for x in pvp if x["reloading"]) / max(len(pvp), 1),
            "deaths_with_empty_mag": sum(1 for x in pvp if x["empty_mag"]),
            "unseen_deaths": sum(1 for x in gun if not x.get("victim_saw_killer_5s")),
            "unseen_deaths_pct": 100.0 * sum(1 for x in gun if not x.get("victim_saw_killer_5s")) / max(len(gun), 1),
            "never_saw_killer_this_round_pct":
                100.0 * sum(1 for x in gun if not x.get("victim_ever_saw_killer_this_round")) / max(len(gun), 1),
            "traded_pct": 100.0 * sum(1 for x in pvp if x["traded"]) / max(len(pvp), 1),
            "traded_of_tradeable_pct": 100.0 * sum(1 for x in tradeable if x["traded"]) / max(len(tradeable), 1),
            "tradeable_deaths": len(tradeable),
            "exposure_before_death_s": {"mean": sum(exp) / max(len(exp), 1), "n": len(exp)},
            "exposed_share_pct": 100.0 * exposed / max(alive, 1e-6),
            "exposed_s_per_death": exposed / max(deaths_n, 1),
            "stacking_incidents": len(stack),
            "stacking_per_round": len(stack) / max(len(rounds), 1),
            "stacking_where": _count(s["where"] for s in stack),
            "clumps": len(clumps),
            "stuck_bots": len(stuck),
            "stuck_reports": stuck,
            "micro_stuck_events": micro,
            "grenades_thrown": thrown,
            "utility": util,
            "attack_plans": dict(plans),
            "attack_plans_pct": {k: 100.0 * v / max(len(my_plans), 1) for k, v in plans.items()},
            "attack_plan_sites": dict(plan_sites),
            "defender_hold_spots": {"distinct": len(holds), "samples": len(self.hold_spots),
                                    "top": [[list(k), v] for k, v in sorted(holds.items(), key=lambda kv: -kv[1])[:8]]},
            "info_leaks": dict(self.leaks),
            "info_leaks_per_round": {k: v / max(len(rounds), 1) for k, v in self.leaks.items()},
        }

    def report_lines(self) -> list[str]:
        """Short human-readable summary for the demo log."""
        s = self.summary()
        out = []
        groups = [("all", s)] + list(s.get("by_ai", {}).items())
        for name, g in groups:
            out.append(f"[bots] {name}: deaths reloading {g['deaths_while_reloading_pct']:.0f}%, unseen deaths "
                       f"{g['unseen_deaths_pct']:.0f}%, traded {g['traded_pct']:.0f}%, stacking "
                       f"{g['stacking_per_round']:.2f}/round, stuck {g['stuck_bots']}, exposed "
                       f"{g['exposed_share_pct']:.0f}%, grenades {sum(u['detonated'] for u in g['utility'].values())}"
                       f" (effective {sum(u['effective'] for u in g['utility'].values())})")
        if s["attack_plans_pct"]:
            out.append("[bots] attack plans: " + ", ".join(f"{k} {v:.0f}%" for k, v in
                                                        sorted(s["attack_plans_pct"].items(), key=lambda kv: -kv[1])))
        for k, h in s.get("head_to_head", {}).items():
            out.append(f"[bots] head to head: {k} won {h['wins']}/{h['n']} rounds = {100 * h['p']:.0f}% "
                       f"(95% CI {100 * h['lo']:.0f}-{100 * h['hi']:.0f}%), on attack {h['won_on_attack']}/"
                       f"{h['rounds_on_attack']}, on defence {h['won_on_defend']}/{h['rounds_on_defend']}")
        return out


def _mode(agent) -> str:
    br = getattr(agent, "brain", None)
    if br is None:
        return ""
    return getattr(br, "mode", "")


def _wilson(k: int, n: int, z: float = 1.96) -> dict:
    if n == 0:
        return {"wins": 0, "n": 0, "p": 0.0, "lo": 0.0, "hi": 0.0}
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {"wins": k, "n": n, "p": p, "lo": max(c - h, 0.0), "hi": min(c + h, 1.0)}


def _count(items) -> dict:
    out = defaultdict(int)
    for i in items:
        out[i or "?"] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _seg_point_dist(a, b, p) -> float:
    ax, ay, az = a[0], a[1], a[2]
    dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
    L2 = dx * dx + dy * dy + dz * dz
    t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy + (p[2] - az) * dz) / L2))
    qx, qy, qz = ax + dx * t, ay + dy * t, az + dz * t
    return math.sqrt((p[0] - qx) ** 2 + (p[1] - qy) ** 2 + (p[2] - qz) ** 2)
