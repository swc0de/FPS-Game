#!/usr/bin/env python3
"""Measure a seeded bot match: game-logic time per tick and behaviour metrics.

This is the Phase-0 baseline harness of the bot overhaul (docs/OVERHAUL_PLAN.md).
It runs the normal ``--demo bots`` spectated match and observes it from the
outside: it wraps a few methods on the classes and instances that already
exist, and never changes a decision. The same numbers are measured again after
the overhaul, on the same machine and seeds, so the two can be compared.

    python tools/bot_metrics.py --seed 1 --rounds 24 --full-match --ai legacy --out docs/baseline/legacy_normal_seed1.json
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

Behaviour: the demo's own collector (ai/metrics.py, where the metric
    definitions are). With ``--ai team0=v2,team1=legacy`` the match is a
    head-to-head and every metric is also reported per AI, with the round-win
    rate and its Wilson 95 % confidence interval. ``--audit`` adds the
    fairness audit (ai/audit.py).
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
        ("ai.v2.brain.BrainV2", "update", "brains"),
        ("ai.perception.Perception", "update", "perception"),
        ("ai.tactics.TeamBrain", "update", "team"),
        ("ai.v2.strategy.TeamStrategy", "update", "team"),
        ("ai.gadget_ai.GadgetAI", "update", "gadget_ai"),
        ("ai.v2.strategy.GadgetAIV2", "update", "gadget_ai"),
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
        # the v2 brain's expensive parts
        ("ai.v2.brain.BrainV2", "think", "q:v2_think"),
        ("ai.v2.brain.BrainV2", "find_cover", "q:v2_find_cover"),
        ("ai.v2.brain.BrainV2", "preaim", "q:v2_preaim"),
        ("ai.v2.brain.BrainV2", "_choose_lean", "q:v2_lean"),
        ("ai.v2.brain.BrainV2", "do_throw", "q:v2_throw"),
        ("ai.v2.strategy.TeamStrategy", "tick", "q:v2_team_tick"),
        ("ai.v2.strategy.TeamStrategy", "_post_plant", "q:v2_post_plant"),
        ("ai.v2.pathing.PathService", "update", "q:v2_paths"),
        ("ai.v2.belief.PossibilityField", "step", "q:v2_field_step"),
        ("ai.v2.belief.PossibilityField", "observe", "q:v2_field_observe"),
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
        self.key_depth: dict[str, int] = {}
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
        # the demo's own per-tick work (metrics sampling) is not game logic: timed and left out
        self._wrap_class("engine.demo", "BotDemo", "tick", "demo_tick")
        orig = game._fixed_update

        def timed(dt):
            self.current = defaultdict(float)
            self._who = {}
            t0 = PERF()
            orig(dt)
            total = (PERF() - t0) * 1000.0 - self.current.get("demo_tick", 0.0)
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
            depth = prof.key_depth.get(key, 0)
            prof.key_depth[key] = depth + 1
            try:
                return orig(self_, *a, **kw)
            finally:
                if is_ai:
                    prof.ai_depth -= 1
                prof.key_depth[key] = depth
                ms = (PERF() - t0) * 1000.0
                # a call nested in one with the same key (a subclass's super()) is already counted
                if not depth and prof.current is not None:
                    prof.current[key] += ms
                    if key.startswith("q:"):
                        prof.calls[key].append(ms)
                    elif key == "brains" and ms > prof._who.get("ms", 0.0):
                        prof._who = {"ms": ms, "bot": self_.bot.describe()[:120]}
                if not depth and key == "brains" and ms > prof.brain_max:
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
        state["t0"] = time.time()

    def summary(self):
        orig_summary(self)
        d = self.game.director
        res = {"seed": opts.seed, "difficulty": opts.difficulty, "rounds_requested": opts.rounds,
               "ai": d.describe_ai(), "team_ai": d.team_ai,
               "wall_seconds": time.time() - state["t0"], "python": sys.version.split()[0],
               "timing": state["prof"].summary(), "behaviour": self.metrics.summary()}
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
