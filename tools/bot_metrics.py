#!/usr/bin/env python3
"""Measure a seeded bot match: game-logic time per tick and behaviour metrics.

This is the Phase-0 baseline harness of the bot overhaul (docs/OVERHAUL_PLAN.md).
It runs the normal ``--demo bots`` spectated match and observes it from the
outside: it wraps a few methods on the classes and instances that already
exist, and never changes a decision. The same numbers are measured again after
the overhaul, on the same machine and seeds, so the two can be compared.

    python tools/bot_metrics.py --seed 1 --rounds 24 --full-match --out docs/baseline/legacy_normal_seed1.json
    xvfb-run -a python tools/bot_metrics.py ...      # headless Linux
    python tools/bot_metrics.py --compare a.json b.json   # markdown table of key metrics

Timing (``TickProfiler``)
    ``Game._fixed_update`` is the whole 64 Hz simulation tick (player, physics,
    grenades, bots, team brains, gadgets, destruction). Each tick is timed with
    ``perf_counter`` and split by subsystem: bot brains (``Brain.update``,
    which includes perception), perception alone, team brains, gadget AI,
    character movement, body animation, the Bullet step, the Siege layer and
    the destruction flush. "AI" is brains + team brains + gadget AI. Ticks are
    grouped by match phase; the live and planted phases are the ones that
    matter (bots are idle during freeze and round end).

    "AI decisions" removes what the AI triggers but does not decide with:
    building weapon and gadget models, throwing drones, planting the charge,
    the HUD text of radio messages, and Python garbage-collection pauses that happen to fall inside the AI.
    Expensive calls (path queries, cover search, vision...) are timed one by
    one, and every tick with more than 4 ms of AI is kept with its breakdown.

Behaviour (``MetricsCollector``), definitions
    deaths while reloading  the victim's gun was mid-reload when it died
    unseen deaths           the victim had not seen its killer in the 5 s
                            before dying (bullet and knife kills)
    traded deaths           the killer was killed by one of the victim's
                            teammates within 3 s; "tradeable" deaths had a
                            living teammate within 15 m
    exposure before death   how long the killer had had the victim in
                            continuous sight when the fatal shot landed
    exposed share           share of a bot's live time in which at least one
                            enemy bot sees it (sampled at 16 Hz)
    stacking incidents      two teammates within 0.8 m (bodies almost touching)
                            for 1 s or more, once per episode
    clumps                  three or more teammates within 2.5 m for 2 s
    stuck                   the demo's own check (a move order without 0.6 m
                            of progress in 5 s); micro-stucks are the path
                            follower's 0.8 s no-progress events
    utility                 grenades thrown per type; a flash is effective
                            when it blinds an enemy for 1 s or more, a frag
                            when it damages an enemy, a smoke when it blocks
                            at least one enemy sighting while it lasts
    plans                   the attack plan and site picked every round
    info leaks              decisions fed by information a player would not
                            have: an exact attacker position on being shot by
                            an enemy the bot had not seen, and an exact killer
                            position broadcast to the team when a teammate dies
                            without having seen its killer
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PERF = time.perf_counter
SAMPLE_EVERY = 4                       # ticks between position samples (16 Hz)


# --------------------------------------------------------------------- timing
class TickProfiler:
    SUBSYSTEMS = (
        ("ai.brain.Brain", "update", "brains"),
        ("ai.perception.Perception", "update", "perception"),
        ("ai.tactics.TeamBrain", "update", "team"),
        ("ai.gadget_ai.GadgetAI", "update", "gadget_ai"),
        ("gameplay.character.KinematicCharacter", "step", "movement"),
        ("gameplay.body.CharacterBody", "animate", "animation"),
        ("engine.physics.PhysicsWorld", "step", "bullet"),
        ("gameplay.tactical.Tactical", "fixed_update", "tactical"),
        ("gameplay.destruction.DestructionManager", "flush", "destruction"),
    )

    # expensive calls inside the AI, timed to explain spikes (prefixed "q:")
    QUERIES = (
        ("ai.navmesh.NavMesh", "find_path", "q:find_path"),
        ("ai.brain.Brain", "find_cover", "q:find_cover"),
        ("ai.brain.Brain", "_preaim", "q:preaim"),
        ("ai.brain.Brain", "_choose_lean", "q:lean"),
        ("ai.brain.Brain", "_think", "q:think"),
        ("ai.brain.Brain", "_lane_path", "q:lane_path"),
        ("ai.brain.Brain", "_do_throw", "q:throw"),
        ("ai.perception.Perception", "_scan", "q:vision"),
        ("ai.perception.Perception", "_hear", "q:hearing"),
        ("ai.tactics.TeamBrain", "_post_plant_spots", "q:post_plant_spots"),
        ("ai.tactics.TeamBrain", "_flee", "q:flee"),
        ("ai.gadget_ai.GadgetAI", "_breach_update", "q:breach"),
        ("ai.gadget_ai.GadgetAI", "_defend_setup", "q:defend_setup"),
        # not decisions, but they run inside the AI's call path
        ("gameplay.body.CharacterBody", "set_weapon", "q:set_weapon_model"),
        ("gameplay.tactical.Tactical", "deploy", "q:deploy_gadget"),
        ("gameplay.tactical.Tactical", "throw_drone", "q:throw_drone"),
        ("gameplay.tactical.Tactical", "use_gadget", "q:use_gadget"),
        ("gameplay.bomb.Bomb", "plant", "q:bomb_plant"),
        ("ai.bot.BotWeapons", "_fire", "q:fire"),
        ("gameplay.director.MatchDirector", "radio", "q:radio_hud"),
    )
    # side effects (model building, effects) that the AI triggers but that are not decisions
    SIDE_EFFECTS = ("q:set_weapon_model", "q:deploy_gadget", "q:throw_drone", "q:use_gadget", "q:bomb_plant",
                    "q:radio_hud")

    def __init__(self, game, detail: bool = True):
        self.game = game
        self.current: dict[str, float] | None = None
        self.ticks: list[tuple] = []            # (phase, total, ai, {sub: ms})
        self.brain_max = 0.0                    # slowest single Brain.update
        self.calls = defaultdict(list)          # query -> [ms per call]
        self.spikes: list[dict] = []            # ticks where the AI took > 4 ms
        self._who: dict = {}
        self.ai_depth = 0
        self.gc_pauses: list[float] = []
        self._gc_t0 = 0.0
        import gc

        def gc_cb(phase, info):
            if phase == "start":
                self._gc_t0 = PERF()
                return
            ms = (PERF() - self._gc_t0) * 1000.0
            self.gc_pauses.append(ms)
            if self.current is not None:
                self.current["gc"] += ms
                if self.ai_depth > 0:
                    self.current["gc_in_ai"] += ms
        gc.callbacks.append(gc_cb)
        if detail:
            for spec in self.SUBSYSTEMS + self.QUERIES:
                m, meth, key = spec
                self._wrap_class(m.rsplit(".", 1)[0], m.rsplit(".", 1)[1], meth, key)
        orig = game._fixed_update

        def timed(dt):
            self.current = defaultdict(float)
            self._who = {}
            t0 = PERF()
            orig(dt)
            total = (PERF() - t0) * 1000.0
            sub = self.current
            self.current = None
            d = game.director
            phase = d.match.phase if d is not None else "-"
            ai = sub.get("brains", 0.0) + sub.get("team", 0.0) + sub.get("gadget_ai", 0.0)
            side = sum(sub.get(k, 0.0) for k in self.SIDE_EFFECTS) + sub.get("gc_in_ai", 0.0)
            keep = {k: v for k, v in sub.items() if not k.startswith("q:")}
            keep["ai_decisions"] = max(ai - side, 0.0)
            self.ticks.append((phase, total, ai, keep))
            if ai > 4.0:
                self.spikes.append({"t": game.loop.time, "round": d.match.round if d else 0, "phase": phase,
                                    "ai_ms": ai, "tick_ms": total,
                                    "parts": {k: round(v, 2) for k, v in sorted(sub.items(), key=lambda kv: -kv[1])
                                              if v > 0.3},
                                    "slowest_bot": self._who.get("bot", "")})
            for cb in self.after_tick:
                cb(dt)
        self.after_tick: list = []
        game._fixed_update = timed

    def _wrap_class(self, module: str, cls_name: str, meth: str, key: str) -> None:
        import importlib
        cls = getattr(importlib.import_module(module), cls_name)
        orig = getattr(cls, meth)
        prof = self

        is_ai = key in ("brains", "team", "gadget_ai")

        def wrapped(self_, *a, **kw):
            t0 = PERF()
            if is_ai:
                prof.ai_depth += 1
            try:
                return orig(self_, *a, **kw)
            finally:
                if is_ai:
                    prof.ai_depth -= 1
                ms = (PERF() - t0) * 1000.0
                if prof.current is not None:
                    prof.current[key] += ms
                    if key.startswith("q:"):
                        prof.calls[key].append(ms)
                    elif key == "brains" and ms > prof._who.get("ms", 0.0):
                        prof._who = {"ms": ms, "bot": self_.bot.describe()[:120]}
                if key == "brains" and ms > prof.brain_max:
                    prof.brain_max = ms
        setattr(cls, meth, wrapped)

    def summary(self) -> dict:
        import numpy as np
        out = {}
        groups = {"live": [t for t in self.ticks if t[0] in ("live", "planted")], "all": self.ticks}
        for name, ticks in groups.items():
            if not ticks:
                continue
            tot = np.array([t[1] for t in ticks])
            ai = np.array([t[2] for t in ticks])
            subs = sorted({k for t in ticks for k in t[3]})
            out[name] = {
                "ticks": len(ticks),
                "total_ms": _stats(tot),
                "ai_ms": _stats(ai),
                "ai_ticks_over_4ms": int((ai > 4.0).sum()),
                "ai_decisions_ms": _stats(np.array([t[3].get("ai_decisions", 0.0) for t in ticks])),
                "ai_decision_ticks_over_4ms": int(sum(1 for t in ticks if t[3].get("ai_decisions", 0.0) > 4.0)),
                "subsystems_mean_ms": {k: float(np.mean([t[3].get(k, 0.0) for t in ticks])) for k in subs},
                "subsystems_p99_ms": {k: float(np.percentile([t[3].get(k, 0.0) for t in ticks], 99)) for k in subs},
            }
        out["slowest_single_brain_update_ms"] = self.brain_max
        gp = np.array(self.gc_pauses or [0.0])
        out["gc_pauses"] = {"count": len(self.gc_pauses), **_stats(gp), "over_4ms": int((gp > 4.0).sum())}
        out["queries"] = {k: {"calls": len(v), **_stats(np.array(v)), "over_4ms": int(sum(1 for x in v if x > 4.0))}
                          for k, v in sorted(self.calls.items()) if v}
        out["spikes"] = {"count": len(self.spikes),
                         "worst": sorted(self.spikes, key=lambda s: -s["ai_ms"])[:25]}
        return out


def _stats(a) -> dict:
    import numpy as np
    return {"mean": float(a.mean()), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)), "max": float(a.max())}


# ------------------------------------------------------------------ behaviour
class MetricsCollector:
    def __init__(self, game, demo, profiler: TickProfiler):
        self.game = game
        self.demo = demo
        d = game.director
        self.d = d
        d.listeners.append(self._event)
        profiler.after_tick.append(self._tick)
        self.tick_n = 0
        self.rounds: list[dict] = []
        self.plans: list[dict] = []
        self.deaths: list[dict] = []
        self.last_seen: dict[tuple[int, int], float] = {}
        self.round_first_seen: dict[tuple[int, int], float] = {}
        self.exposed_t = defaultdict(float)
        self.alive_t = defaultdict(float)
        self.pairs: dict[tuple, list] = {}
        self.stacking: list[dict] = []
        self.clumps: list[dict] = []
        self._clump_since: dict[str, float] = {}
        self.thrown = defaultdict(int)
        self.grenades: list[dict] = []
        self.leaks = defaultdict(int)
        self.team_reports = 0
        self.hold_spots: list[tuple] = []
        self._holds_sampled = False
        self._ctx_grenade = None
        self._in_scan = None
        self._install()

    # -------------------------------------------------------------- hooks
    def _install(self) -> None:
        import ai.bot as bot_mod
        import ai.perception as per_mod
        import ai.tactics as tac_mod
        import weapons.grenades as gren_mod
        col = self

        orig_scan = per_mod.Perception._scan

        def scan(self_, now, enemies):
            col._in_scan = self_.bot
            try:
                orig_scan(self_, now, enemies)
            finally:
                col._in_scan = None
            for c in self_.contacts.values():
                if c.seen:
                    key = (id(self_.bot), id(c.agent))
                    col.last_seen[key] = now
                    col.round_first_seen.setdefault(key, now)
        per_mod.Perception._scan = scan

        orig_damaged = per_mod.Perception.on_damaged

        def on_damaged(self_, attacker, now):
            if attacker is not None and getattr(attacker, "side", None) != self_.bot.side:
                if now - col.last_seen.get((id(self_.bot), id(attacker)), -99.0) > 1.0:
                    col.leaks["exact_attacker_position_when_shot_by_unseen_enemy"] += 1
            return orig_damaged(self_, attacker, now)
        per_mod.Perception.on_damaged = on_damaged

        orig_killed = tac_mod.TeamBrain.on_teammate_killed

        def on_killed(self_, victim, killer, now):
            if killer is not None and killer.side != self_.side and self_.alive_bots():
                if now - col.last_seen.get((id(victim), id(killer)), -99.0) > 1.0:
                    col.leaks["exact_killer_position_broadcast_after_unseen_kill"] += 1
            return orig_killed(self_, victim, killer, now)
        tac_mod.TeamBrain.on_teammate_killed = on_killed

        orig_report = tac_mod.TeamBrain.report

        def report(self_, *a, **kw):
            col.team_reports += 1
            return orig_report(self_, *a, **kw)
        tac_mod.TeamBrain.report = report

        orig_start = tac_mod.TeamBrain.round_start

        def round_start(self_, now):
            orig_start(self_, now)
            if self_.side == "attack":
                col.plans.append({"round": col.d.match.round, "plan": self_.plan, "site": self_.site})
        tac_mod.TeamBrain.round_start = round_start

        orig_throw = bot_mod.BotWeapons.throw

        def throw(self_, key, velocity):
            ok = orig_throw(self_, key, velocity)
            if ok:
                col.thrown[f"{self_.bot.side}:{key}"] += 1
            return ok
        bot_mod.BotWeapons.throw = throw

        orig_det = gren_mod.Grenade.detonate

        def detonate(self_):
            rec = {"key": self_.d.key, "side": getattr(self_.thrower, "side", ""), "t": col.game.loop.time,
                   "pos": tuple(self_.pos()) if callable(getattr(self_, "pos", None)) else None,
                   "enemy_flashed": 0, "team_flashed": 0, "enemy_damaged": 0, "sightings_blocked": 0}
            col.grenades.append(rec)
            col._ctx_grenade = rec
            try:
                return orig_det(self_)
            finally:
                col._ctx_grenade = None
        gren_mod.Grenade.detonate = detonate

        orig_flashed = bot_mod.BotAgent.flashed

        def flashed(self_, duration, strength):
            rec = col._ctx_grenade
            if rec is not None and duration >= 1.0:
                rec["enemy_flashed" if self_.side != rec["side"] else "team_flashed"] += 1
            return orig_flashed(self_, duration, strength)
        bot_mod.BotAgent.flashed = flashed

        fx = self.game.effects
        orig_smoke = fx.smoke_between

        def smoke_between(a, b):
            v = orig_smoke(a, b)
            bot = col._in_scan
            if bot is not None and v >= 0.55:
                col._smoke_block(bot, a, b)
            return v
        fx.smoke_between = smoke_between

        for b in self.d.bots:
            b.damageable.on_damage.append(lambda res, b=b: self._damage(b, res))

    def _smoke_block(self, bot, a, b) -> None:
        now = self.game.loop.time
        for rec in self.grenades:
            if rec["key"] != "smoke" or rec["side"] == bot.side or now - rec["t"] > 20.0 or rec["pos"] is None:
                continue
            if _seg_point_dist(a, b, rec["pos"]) < 4.5:
                rec["sightings_blocked"] += 1

    def _damage(self, victim, res) -> None:
        rec = self._ctx_grenade
        info = res.info
        if rec is not None and info.weapon == "frag":
            attacker = self.d.agent_of(info.attacker)
            if attacker is not None and attacker.side != victim.side:
                rec["enemy_damaged"] += 1

    # -------------------------------------------------------------- events
    def _event(self, kind: str, data: dict) -> None:
        now = self.game.loop.time
        m = self.d.match
        if kind == "round_start":
            self.round_first_seen = {}
            self._holds_sampled = False
            self.pairs = {}
        elif kind == "kill":
            self._kill(data, now)
        elif kind == "round_end":
            r = data["result"]
            self.rounds.append({"round": m.round, "winner": r.winner_side, "reason": r.reason,
                                "length": now - self.demo.round_t0})

    def _kill(self, data: dict, now: float) -> None:
        k, v = data.get("killer"), data["victim"]
        weapon = data.get("weapon", "")
        kind = "bullet" if weapon not in ("frag", "bomb", "world", "") else weapon or "world"
        rec = {"t": now, "round": self.d.match.round, "victim": v.name, "victim_side": v.side,
               "killer": k.name if k is not None else None, "killer_side": k.side if k is not None else None,
               "weapon": weapon, "kind": kind, "headshot": bool(data.get("headshot")),
               "traded": False, "tradeable": False}
        ws = v.weapons.inv.current() if hasattr(v, "weapons") else None
        rec["reloading"] = bool(ws is not None and ws.reloading)
        rec["empty_mag"] = bool(ws is not None and ws.d.magazine > 0 and ws.ammo == 0)
        if k is not None and k.side != v.side:
            seen_t = self.last_seen.get((id(v), id(k)), -99.0)
            rec["victim_saw_killer_5s"] = now - seen_t <= 5.0
            rec["victim_ever_saw_killer_this_round"] = (id(v), id(k)) in self.round_first_seen
            c = k.perception.contacts.get(id(v)) if hasattr(k, "perception") else None
            rec["exposure_before_death"] = (now - c.since) if (c is not None and c.seen) else None
            rec["distance"] = (k.position() - v.position()).length()
            rec["victim_mode"] = v.brain.mode if hasattr(v, "brain") else ""
            rec["killer_mode"] = k.brain.mode if hasattr(k, "brain") else ""
            mates = [b for b in self.d.bots if b.side == v.side and b is not v and b.active and b.alive]
            rec["tradeable"] = any((b.position() - v.position()).length() < 15.0 for b in mates)
        # trades: this victim killed someone of the killer's team in the last 3 s
        if k is not None:
            for d in self.deaths:
                if d["killer"] == v.name and d["victim_side"] == k.side and now - d["t"] <= 3.0 and not d["traded"]:
                    d["traded"] = True
        self.deaths.append(rec)

    # ---------------------------------------------------------------- tick
    def _tick(self, dt: float) -> None:
        self.tick_n += 1
        if self.tick_n % SAMPLE_EVERY:
            return
        d = self.d
        if d.match.phase not in ("live", "planted"):
            return
        now = self.game.loop.time
        sdt = dt * SAMPLE_EVERY
        bots = [b for b in d.bots if b.active and b.alive]
        # exposure: is this bot seen by any enemy bot?
        for b in bots:
            self.alive_t[b.name] += sdt
            if any(e.side != b.side and (c := e.perception.contacts.get(id(b))) is not None and c.seen
                   for e in bots):
                self.exposed_t[b.name] += sdt
        # stacking and clumping
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
                            where = self.game.level.callout_at(pa.x, pa.y)
                            self.stacking.append({"t": now, "round": d.match.round, "pair": key, "where": where,
                                                  "modes": (a.brain.mode, b.brain.mode)})
                    elif rec is not None and dist > 1.2:
                        del self.pairs[key]
            clump = False
            for a in team:
                pa = a.position()
                if sum(1 for b in team if (b.position() - pa).length() < 2.5) >= 3:
                    clump = True
                    break
            if clump:
                since = self._clump_since.get(side)
                if since is None:
                    self._clump_since[side] = now
                elif since >= 0 and now - since >= 2.0:
                    self._clump_since[side] = -1.0
                    a = team[0].position()
                    self.clumps.append({"t": now, "round": d.match.round, "side": side})
            else:
                self._clump_since.pop(side, None)
        # where the defenders hold, 10 s into the round
        if not self._holds_sampled and now - self.demo.round_t0 > 10.0:
            self._holds_sampled = True
            for b in bots:
                t = b.brain.task
                if b.side == "defend" and t.kind == "hold" and t.pos is not None:
                    self.hold_spots.append((round(t.pos.x, 1), round(t.pos.y, 1)))

    # ------------------------------------------------------------- summary
    def summary(self) -> dict:
        demo = self.demo
        rounds = self.rounds
        wins = defaultdict(int)
        reasons = defaultdict(int)
        for r in rounds:
            wins[r["winner"]] += 1
            reasons[f"{r['winner']}/{r['reason']}"] += 1
        pvp = [x for x in self.deaths if x.get("killer_side") and x["killer_side"] != x["victim_side"]]
        gun = [x for x in pvp if x["kind"] == "bullet"]
        tradeable = [x for x in pvp if x["tradeable"]]
        exp = [x["exposure_before_death"] for x in gun if x.get("exposure_before_death") is not None]
        plans = defaultdict(int)
        for p in self.plans:
            plans[p["plan"]] += 1
        plan_sites = defaultdict(int)
        for p in self.plans:
            plan_sites[f"{p['plan']} {p['site']}"] += 1
        holds = defaultdict(int)
        for h in self.hold_spots:
            holds[h] += 1
        util = {}
        for key in ("flash", "smoke", "frag"):
            recs = [g for g in self.grenades if g["key"] == key]
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
        alive = sum(self.alive_t.values())
        exposed = sum(self.exposed_t.values())
        micro = sum(b.brain.follower.stuck_events for b in self.d.bots)
        n_deaths = len(self.deaths)
        fired = sum(b.shots_fired for b in self.d.bots)
        out = {
            "rounds": len(rounds),
            "wins_by_side": dict(wins),
            "attack_win_pct": 100.0 * wins.get("attack", 0) / max(len(rounds), 1),
            "results": dict(reasons),
            "avg_round_s": sum(r["length"] for r in rounds) / max(len(rounds), 1),
            "kills": len(pvp),
            "deaths_total": n_deaths,
            "headshot_pct": 100.0 * sum(1 for x in pvp if x["headshot"]) / max(len(pvp), 1),
            "plants": demo.plants, "defuses": demo.defuses,
            "shots": fired, "bullet_hits": demo.hits, "accuracy_pct": 100.0 * demo.hits / max(fired, 1),
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
            "exposed_s_per_death": exposed / max(n_deaths, 1),
            "stacking_incidents": len(self.stacking),
            "stacking_per_round": len(self.stacking) / max(len(rounds), 1),
            "stacking_where": _count(s["where"] for s in self.stacking),
            "clumps": len(self.clumps),
            "stuck_bots": len(demo.stuck_reports),
            "stuck_reports": list(demo.stuck_reports),
            "micro_stuck_events": micro,
            "grenades_thrown": dict(self.thrown),
            "utility": util,
            "attack_plans": dict(plans),
            "attack_plans_pct": {k: 100.0 * v / max(len(self.plans), 1) for k, v in plans.items()},
            "attack_plan_sites": dict(plan_sites),
            "defender_hold_spots": {"distinct": len(holds), "samples": len(self.hold_spots),
                                    "top": [[list(k), v] for k, v in sorted(holds.items(), key=lambda kv: -kv[1])[:8]]},
            "team_reports": self.team_reports,
            "info_leaks": dict(self.leaks),
            "info_leaks_per_round": {k: v / max(len(rounds), 1) for k, v in self.leaks.items()},
            "deaths": self.deaths,
            "stacking": self.stacking,
            "rounds_detail": rounds,
            "plans_detail": self.plans,
        }
        return out


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


# ------------------------------------------------------------------- report
ROWS = (
    ("rounds", "rounds", "{:.0f}"),
    ("attack_win_pct", "attack round wins %", "{:.0f}"),
    ("avg_round_s", "average round length s", "{:.0f}"),
    ("kills", "kills", "{:.0f}"),
    ("plants", "plants", "{:.0f}"),
    ("defuses", "defuses", "{:.0f}"),
    ("headshot_pct", "headshot kills %", "{:.0f}"),
    ("accuracy_pct", "bullet hits / shots %", "{:.0f}"),
    ("deaths_while_reloading_pct", "deaths while reloading %", "{:.1f}"),
    ("unseen_deaths_pct", "unseen deaths % (killer not seen in last 5 s)", "{:.1f}"),
    ("never_saw_killer_this_round_pct", "killer never seen that round %", "{:.1f}"),
    ("traded_pct", "deaths traded within 3 s %", "{:.1f}"),
    ("traded_of_tradeable_pct", "traded, of deaths with a teammate within 15 m %", "{:.1f}"),
    ("exposure_before_death_s.mean", "killer's sight of victim before the kill s", "{:.2f}"),
    ("exposed_share_pct", "live time seen by an enemy %", "{:.1f}"),
    ("stacking_per_round", "stacking incidents per round", "{:.2f}"),
    ("clumps", "clumps (3+ within 2.5 m for 2 s)", "{:.0f}"),
    ("stuck_bots", "stuck bots (5 s without progress)", "{:.0f}"),
    ("micro_stuck_events", "path-follower micro-stucks", "{:.0f}"),
    ("utility.flash.effective_pct", "flashes that blinded an enemy %", "{:.0f}"),
    ("utility.smoke.effective_pct", "smokes that blocked an enemy sighting %", "{:.0f}"),
    ("utility.frag.effective_pct", "frags that hurt an enemy %", "{:.0f}"),
    ("info_leaks_per_round.exact_attacker_position_when_shot_by_unseen_enemy",
     "leak: exact attacker position when shot unseen, per round", "{:.2f}"),
    ("info_leaks_per_round.exact_killer_position_broadcast_after_unseen_kill",
     "leak: exact killer position broadcast, per round", "{:.2f}"),
)
TIMING_ROWS = (
    ("live.total_ms.mean", "live tick mean ms", "{:.2f}"),
    ("live.total_ms.p95", "live tick p95 ms", "{:.2f}"),
    ("live.total_ms.p99", "live tick p99 ms", "{:.2f}"),
    ("live.ai_ms.mean", "AI mean ms per tick", "{:.2f}"),
    ("live.ai_decisions_ms.mean", "AI decisions mean ms per tick", "{:.2f}"),
    ("live.ai_decisions_ms.max", "AI decisions max ms in a tick", "{:.1f}"),
    ("live.ai_ticks_over_4ms", "ticks with AI > 4 ms", "{:.0f}"),
    ("live.ai_decision_ticks_over_4ms", "ticks with AI decisions > 4 ms", "{:.0f}"),
)


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def compare(files: list[str]) -> str:
    """Markdown table of the key metrics of one or more result files."""
    runs = [json.loads(Path(f).read_text()) for f in files]
    head = "| metric | " + " | ".join(Path(f).stem for f in files) + " |"
    out = [head, "|---|" + "---|" * len(files)]
    for path, label, fmt in ROWS:
        vals = [_get(r["behaviour"], path) for r in runs]
        out.append(f"| {label} | " + " | ".join("-" if v is None else fmt.format(v) for v in vals) + " |")
    for path, label, fmt in TIMING_ROWS:
        vals = [_get(r["timing"], path) for r in runs]
        out.append(f"| {label} | " + " | ".join("-" if v is None else fmt.format(v) for v in vals) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------- runner
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=24)
    ap.add_argument("--difficulty", default="normal")
    ap.add_argument("--out", default="user/bot_metrics.json")
    ap.add_argument("--no-detail", action="store_true", help="time whole ticks only (lowest overhead)")
    ap.add_argument("--full-match", action="store_true",
                    help="play all rounds (no early win at 13) so every run has the same number of rounds")
    ap.add_argument("--res", default="640x360")
    ap.add_argument("--preset", default="low")
    ap.add_argument("--compare", nargs="+", metavar="JSON", help="print a markdown table of result files and exit")
    opts, rest = ap.parse_known_args(argv)
    if opts.compare:
        print(compare(opts.compare))
        return 0
    os.environ["BOT_DEMO_ROUNDS"] = str(opts.rounds)

    import engine.demo as demo_mod
    state = {}
    orig_init = demo_mod.BotDemo.__init__
    orig_summary = demo_mod.BotDemo._summary

    def init(self, game):
        orig_init(self, game)
        if opts.full_match:
            rr = game.director.match.round_rules
            rr["win_score"] = 10 ** 6
            rr["max_rounds"] = max(int(rr["max_rounds"]), opts.rounds)
        prof = TickProfiler(game, detail=not opts.no_detail)
        state["prof"] = prof
        state["col"] = MetricsCollector(game, self, prof)
        state["t0"] = time.time()

    def summary(self):
        orig_summary(self)
        res = {"seed": opts.seed, "difficulty": opts.difficulty, "rounds_requested": opts.rounds,
               "wall_seconds": time.time() - state["t0"], "python": sys.version.split()[0],
               "timing": state["prof"].summary(), "behaviour": state["col"].summary()}
        out = Path(opts.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1, default=str))
        b, t = res["behaviour"], res["timing"].get("live", {})
        log = self.game.log
        log(f"[metrics] {b['rounds']} rounds, attack {b['wins_by_side'].get('attack', 0)} : "
            f"{b['wins_by_side'].get('defend', 0)} defend; kills {b['kills']}, plants {b['plants']}, "
            f"defuses {b['defuses']}, stuck {b['stuck_bots']}")
        if t:
            log(f"[metrics] live tick {t['total_ms']['mean']:.2f} ms mean, p99 {t['total_ms']['p99']:.2f}, "
                f"max {t['total_ms']['max']:.2f}; AI {t['ai_ms']['mean']:.2f} ms mean, max {t['ai_ms']['max']:.2f}")
        log(f"[metrics] written to {out}")

    demo_mod.BotDemo.__init__ = init
    demo_mod.BotDemo._summary = summary
    from engine.app import main as game_main
    return game_main(["--demo", "bots", "--seed", str(opts.seed), "--difficulty", opts.difficulty,
                      "--res", opts.res, "--preset", opts.preset, "--windowed"] + rest)


if __name__ == "__main__":
    sys.exit(main())
