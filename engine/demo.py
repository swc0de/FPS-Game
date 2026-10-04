"""Scripted demos: drive the player with virtual input, take screenshots.

    python main.py --demo weapons      # tour of milestone-2 features

Each script is a list of steps executed on fixed ticks:
  ("pose", x, y, eye_z, heading, pitch)   place the player's eye
  ("aim", x, y, z)                         look at a world point
  ("give", key) / ("select", slot)        loadout changes
  ("hold", action, ticks)                  hold a key for N ticks
  ("press", action)                        tap a key
  ("wait", ticks)
  ("shot", name)                           save user/screenshots/demo_<name>.png
  ("report", label)                        print dummy damage readouts
  ("route", name, x, y, z, heading)        start a walking route (teleport, feet position)
  ("goto", x, y)                           run toward a waypoint (fails if stuck for 2 s)
  ("expect_z", zmin, zmax)                 check the player's feet height
  ("end_route",)                           print PASS/FAIL for the route
  ("call", fn)                             run fn(game)

    python main.py --map compound --demo routes   # walk every lane of the map
    python main.py --mode sandbox --demo m6        # destruction (milestone 6)
"""
from __future__ import annotations

import math

from panda3d.core import Point3, Vec3

from engine import paths

EYE = 1.67 + 0.025


def weapons_script(game) -> list:
    s = []
    rng_y = 27.4
    # --- rifle at the 10 m armoured dummy: taps, then a burst
    s += [("pose", -32, rng_y, EYE, 0, 0), ("select", "primary"), ("wait", 50),
          ("aim", -32, 39.5, 1.63), ("press", "fire"), ("wait", 12), ("report", "rifle headshot 10m armoured"),
          ("wait", 160), ("aim", -32, 39.5, 1.25), ("hold", "fire", 4), ("wait", 1), ("shot", "rifle_fire"),
          ("wait", 30), ("report", "rifle burst 10m armoured")]
    # --- spray pattern on the wall (no compensation) -> decal pattern
    s += [("pose", -9.2, 30.4, EYE, 0, 0), ("press", "reload"), ("wait", 200), ("aim", -9.2, 40.35, 1.3),
          ("hold", "fire", 196), ("wait", 6), ("shot", "spray_mid"), ("wait", 40), ("shot", "spray_pattern")]
    # --- reload & inspect animations
    s += [("press", "reload"), ("wait", 70), ("shot", "reload_anim"), ("wait", 160),
          ("press", "inspect"), ("wait", 60), ("shot", "inspect_anim"), ("wait", 150)]
    # --- penetration panels: one shot through each panel at the dummy behind it
    s += [("pose", -53.4, 31.0, EYE, 0, 0), ("wait", 10)]
    for x in (-56.0, -54.7, -53.4, -52.1, -50.8):
        s += [("aim", x, 37.4, 1.3), ("press", "fire"), ("wait", 16)]
    s += [("wait", 4), ("shot", "penetration"), ("report", "penetration panels")]
    # --- pick up the C9 from the rack with the use key, drop it, walk over it to auto-pickup
    s += [("pose", -45, 28.2, EYE, 0, 0), ("aim", -45, 29.4, 0.96), ("press", "use"), ("wait", 50),
          ("report", "picked up from rack (expect c9)"), ("shot", "pickup_c9"),
          ("pose", -38, 27.0, EYE, 0, -20), ("press", "drop"), ("wait", 60), ("select", "secondary"),
          ("wait", 20), ("hold", "forward", 40), ("wait", 30), ("report", "auto-pickup after walking over it")]
    # --- shotgun at 10 m
    s += [("give", "s12"), ("pose", -44, rng_y, EYE, 0, 0), ("wait", 50), ("aim", -44, 39.5, 1.3), ("press", "fire"),
          ("wait", 2), ("shot", "shotgun_fire"), ("wait", 60), ("report", "shotgun 10m"), ("wait", 20)]
    # --- sniper: scope in and shoot the 30 m dummy
    s += [("give", "sr90"), ("pose", -24, rng_y, EYE, 0, 0), ("wait", 70), ("aim", -22, 58.8, 1.3),
          ("press", "aim"), ("wait", 20), ("shot", "sniper_scope"), ("press", "fire"), ("wait", 10),
          ("report", "sniper 30m chest"), ("wait", 100), ("shot", "sniper_bolt")]
    # --- pistol + knife
    s += [("select", "secondary"), ("wait", 40), ("aim", -32, 39.5, 1.3), ("press", "fire"), ("wait", 2),
          ("shot", "pistol_fire"), ("wait", 30), ("select", "melee"), ("wait", 30), ("press", "fire"), ("wait", 6),
          ("shot", "knife_slash"), ("wait", 30)]
    # --- grenades: frag at the 20 m dummies, smoke, flash
    s += [("select", "grenade"), ("pose", -40, rng_y + 1.5, EYE, 0, 0), ("wait", 40), ("aim", -40, 46, 3.6),
          ("hold", "fire", 24), ("wait", 106), ("shot", "frag_explosion"), ("wait", 30),
          ("report", "frag at 20m dummies"), ("wait", 40),
          ("select", "grenade"), ("wait", 40), ("aim", -28, 41, 3.0), ("hold", "fire", 24), ("wait", 300),
          ("shot", "smoke_cloud"), ("wait", 20),
          ("select", "grenade"), ("wait", 40), ("aim", -36, 37, 2.6), ("hold", "fire", 24), ("wait", 98),
          ("shot", "flashbang"), ("wait", 320)]
    # --- overview of the range after all that
    s += [("pose", -38, 25.0, 3.4, -20, -12), ("wait", 4), ("shot", "range_after")]
    return s


def viewmodel_script(game) -> list:
    s = [("pose", -32, 27.4, EYE, 0, 0), ("wait", 4)]
    for key in ("r7", "c9", "mx5", "s12", "sr90"):
        s += [("give", key), ("wait", 70), ("shot", f"vm_{key}_hip"), ("hold", "aim", 30), ("shot", f"vm_{key}_ads")]
        if key == "sr90":
            s += [("press", "aim"), ("wait", 4)]
    s += [("select", "secondary"), ("wait", 50), ("shot", "vm_p9_hip"), ("hold", "aim", 30), ("shot", "vm_p9_ads"),
          ("select", "melee"), ("wait", 40), ("shot", "vm_knife"), ("select", "grenade"), ("wait", 40),
          ("shot", "vm_frag")]
    return s


def impacts_script(game) -> list:
    """Close-up of impacts on different surfaces (decals + particles)."""
    s = [("pose", -9.2, 37.5, EYE, 0, 0), ("wait", 50), ("aim", -9.2, 40.35, 1.4), ("press", "fire"), ("wait", 2),
         ("shot", "impact_t2"), ("wait", 6), ("shot", "impact_t8"), ("wait", 10)]
    for dx in (-1.0, -0.5, 0.0, 0.5, 1.0):
        s += [("aim", -9.2 + dx, 40.35, 1.2 + abs(dx) * 0.3), ("press", "fire"), ("wait", 12)]
    s += [("shot", "impact_decals"), ("pose", -53.4, 33.0, EYE, 0, 0), ("wait", 10)]
    for x in (-56.0, -54.7, -53.4, -52.1, -50.8):
        s += [("aim", x, 35.0, 1.4), ("press", "fire"), ("wait", 12)]
    s += [("shot", "impact_panels")]
    return s


def flash_script(game) -> list:
    """Muzzle flash / tracer timing check for every gun."""
    s = [("pose", -32, 27.4, EYE, 0, 0)]
    for key in ("r7", "mx5", "s12", "sr90"):
        s += [("give", key), ("wait", 70), ("aim", -32, 49.5, 1.3), ("press", "fire"), ("shot", f"flash_{key}"),
              ("wait", 40)]
    return s


def routes_script(game) -> list:
    """Walk the main lanes of the map (waypoints) and report blocked paths."""
    routes = game.level.data.get("test_routes", [])
    s = []
    for r in routes:
        x, y, z, h = r["start"]
        s.append(("route", r["name"], x, y, z, h))
        for wp in r["waypoints"]:
            s.append(("goto", wp[0], wp[1]))
            if len(wp) > 2:
                s.append(("expect_z", wp[2] - 0.35, wp[2] + 0.35))
        s.append(("end_route",))
    return s


def round_script(game) -> list:
    """Milestone 4: pistol round won by elimination, a bomb round won by
    detonation, then a defuse as a defender. Prints money after each step."""
    s = [("wait_phase", "freeze", 200), ("wait", 10), ("open_buy",), ("wait", 6), ("shot", "m4_buy_menu"),
         ("buy", "kevlar"), ("close_buy",), ("report_match", "after buying kevlar (expect $150)"),
         ("wait_phase", "live", 2000)]
    for i in range(5):
        s += [("shoot_standin", i)]
        if i == 1:
            s += [("wait", 2), ("shot", "m4_killfeed")]
    s += [("wait", 4), ("shot", "m4_round_won"), ("report_match", "pistol round won (expect $4900)"),
          ("wait_phase", "freeze", 2000), ("wait", 4), ("buy", "r7"), ("buy", "kevlar_helmet"),
          ("report_match", "bought rifle + helmet upgrade (expect $1850: kept armour, helmet $350)"), ("wait_phase", "live", 2000),
          ("teleport", -40.0, 19.5, 0.02, 180.0), ("select", "bomb"), ("wait", 20), ("hold_async", "fire", 260),
          ("wait", 120), ("shot", "m4_planting"), ("wait", 150), ("report_match", "after plant (expect planted, +$300 = $2150)"),
          ("wait", 6), ("shot", "m4_planted"), ("scoreboard", True), ("wait", 10), ("shot", "m4_scoreboard"),
          ("wait", 10), ("scoreboard", False), ("teleport", -47.0, -4.0, 0.02, 0.0), ("aim", -40.0, 19.5, 1.5),
          ("wait_phase", "round_end", 6000), ("shot", "m4_explosion"), ("wait", 6), ("shot", "m4_explosion2"),
          ("report_match", "bomb round (expect detonation win, $5650)"),
          ("console", "team defend"), ("wait_phase", "live", 2000), ("console", "plant B"),
          ("teleport", 40.0, 33.0, 0.02, 0.0), ("aim_bomb",), ("wait", 4), ("hold_async", "use", 700),
          ("wait", 320), ("shot", "m4_defusing"), ("wait", 400), ("shot", "m4_defused"),
          ("report_match", "defuse round (expect defused win, $4600)")]
    return s


def _panel_report(game, label: str) -> None:
    lines = [f"[demo] --- {label}"]
    for p in game.destruction.panels:
        f = p.destroyed_fraction()
        if f > 0 or p.reinforced:
            lines.append(f"[demo]   panel {p.spec.name or p.index:16s} #{p.index:3d} destroyed {f * 100:5.1f}%"
                         + (" reinforced" if p.reinforced else ""))
    game.log("\n".join(lines))


def _frag_at(game, x, y, z, fuse=0.05):
    from weapons.grenades import Grenade
    g = Grenade(game, game.weapon_db.grenades["frag"], Point3(x, y, z), Vec3(0, 0, 0), game.player)
    g.age = g.d.fuse - fuse
    game.spawn_grenade(g)


def _panel_named(game, name: str, near=None):
    best, bd = None, 1e9
    for p in game.destruction.panels:
        if p.spec.name != name:
            continue
        d = 0.0 if near is None else float(sum((p.center[i] - near[i]) ** 2 for i in range(len(near))))
        if d < bd:
            best, bd = p, d
    return best


def m6_script(game) -> list:
    """Milestone 6: soft walls (rifle, shotgun, frag, melee), the roof hatch,
    reinforcement and the round reset."""
    s = [("call", lambda g: setattr(g.player.damageable, "damage_filter", lambda d, info: False)),
         ("pose", -39.5, -14.0, EYE, 180, 0), ("select", "primary"), ("wait", 40)]
    # rifle: a tight group chips a murder hole through the barracks hallway wall
    for k in range(9):
        s += [("aim", -39.5 + (k % 3) * 0.1, -15.5, 1.5 + (k // 3) * 0.1), ("press", "fire"), ("wait", 14)]
    s += [("wait", 4), ("shot", "m6_rifle_hole"), ("call", lambda g: _panel_report(g, "after 9 rifle rounds"))]
    # shotgun at 2 m: one blast opens a hole
    s += [("give", "s12"), ("pose", -35.0, -13.6, EYE, 180, 0), ("wait", 50), ("aim", -35.0, -15.5, 1.2),
          ("press", "fire"), ("wait", 3), ("shot", "m6_shotgun_burst"), ("wait", 50), ("aim", -35.0, -15.5, 1.0),
          ("press", "fire"), ("wait", 40), ("aim", -35.0, -15.5, 1.3), ("shot", "m6_shotgun_hole"),
          ("call", lambda g: _panel_report(g, "after 2 shotgun blasts"))]
    # frag against the wall: a big hole, debris flying
    s += [("pose", -41.0, -13.0, EYE, 220, -5), ("call", lambda g: _frag_at(g, -43.0, -15.3, 0.9)), ("wait", 6),
          ("shot", "m6_frag_blast"), ("wait", 30), ("shot", "m6_frag_debris"), ("wait", 120),
          ("aim", -43.0, -15.5, 1.1), ("shot", "m6_frag_hole"),
          ("call", lambda g: _panel_report(g, "after a frag against the wall"))]
    # walk through the hole into the room behind
    s += [("route", "frag_hole", -43.0, -13.8, 0.02, 180), ("goto", -43.0, -18.0), ("expect_z", -0.1, 0.2),
          ("end_route",), ("aim", -43.0, -12.0, 1.4), ("wait", 4), ("shot", "m6_through_hole")]
    # knife a hole
    s += [("select", "melee"), ("pose", -33.5, -13.4, EYE, 180, 0), ("wait", 40), ("aim", -33.5, -15.5, 1.3)]
    for _ in range(6):
        s += [("press", "aim"), ("wait", 70)]
    s += [("shot", "m6_knife"), ("call", lambda g: _panel_report(g, "after 6 heavy knife hits"))]
    # roof hatch over site A: blow it open from the roof and look down, then up from inside
    s += [("pose", -43.5, 27.5, 4.5 + EYE, 90, -40), ("wait", 20), ("shot", "m6_hatch_closed"),
          ("call", lambda g: _frag_at(g, -41.5, 27.5, 4.6)), ("wait", 150), ("aim", -41.5, 27.5, 4.4),
          ("shot", "m6_hatch_open"), ("pose", -41.5, 24.5, EYE, 0, 45), ("wait", 10), ("shot", "m6_hatch_below"),
          ("call", lambda g: _panel_report(g, "after a frag on the hatch"))]
    # reinforcement: the armory office wall takes no bullet or frag damage
    s += [("call", lambda g: _panel_named(g, "armory_office", (-47.0, 32.0)).reinforce()), ("select", "primary"),
          ("pose", -47.0, 29.0, EYE, 0, 0), ("wait", 40), ("aim", -47.0, 32.0, 1.5), ("hold", "fire", 30),
          ("call", lambda g: _frag_at(g, -47.0, 31.7, 0.6)), ("wait", 150), ("aim", -47.0, 32.0, 1.5),
          ("shot", "m6_reinforced"), ("call", lambda g: _panel_report(g, "reinforced office wall after 30 shots + frag"))]
    # round reset rebuilds everything
    s += [("call", lambda g: g.destruction.reset()), ("pose", -38.0, -14.0, EYE, 180, 0), ("wait", 10),
          ("shot", "m6_reset"), ("call", lambda g: _panel_report(g, "after reset (expect nothing listed)"))]
    return s


SCRIPTS = {"weapons": weapons_script, "viewmodels": viewmodel_script, "impacts": impacts_script,
           "flash": flash_script, "routes": routes_script, "round": round_script, "m6": m6_script, "bots": None}


def make_demo(game, name: str):
    if name == "bots":
        return BotDemo(game)
    return DemoRunner(game, name)


class BotDemo:
    """Milestone 5: watch a 5v5 bot match at high speed and report how it went.

    Runs ``BOT_DEMO_ROUNDS`` rounds (env, default 8) with 32 fixed ticks per
    rendered frame (``BOT_DEMO_DT``; ``BOT_DEMO_TRACE=1`` logs every bot's
    state every 5 s), follows the most interesting bot (fighting, planting,
    defusing, carrying the charge) and saves user/screenshots/demo_bots_*.png
    around kills, plants and defuses. Prints every round's events, then a
    summary: round results, kills by weapon, headshot rate, accuracy, plants,
    defuses and any bot that got stuck (a move task without progress)."""

    def __init__(self, game):
        import os
        self.game = game
        self.name = "bots"
        self.rounds = int(os.environ.get("BOT_DEMO_ROUNDS", "8"))
        self.trace = os.environ.get("BOT_DEMO_TRACE", "") == "1"
        self.frame_dt = float(os.environ.get("BOT_DEMO_DT", "0.5"))
        self.done = False
        self.ticks = 0
        self.results = []
        self.kills = []
        self.events = []
        self.shot_queue: list[tuple[int, str]] = []
        self.shots_this_round = 0
        self.stuck: dict[int, list] = {}
        self.stuck_reports = []
        self.hits = 0
        self.plants = 0
        self.defuses = 0
        self.round_t0 = 0.0
        game.debug_hud.toggle()
        d = game.director
        d.listeners.append(self._event)
        for b in d.bots:
            b.damageable.on_damage.append(lambda res, b=b: self._hit(res))
        game.log(f"[demo] bot match: {len(d.bots)} bots, difficulty {d.difficulty}, {self.rounds} rounds")

    def _hit(self, res) -> None:
        if res.info.kind == "bullet":
            self.hits += 1

    def _event(self, kind: str, data: dict) -> None:
        g = self.game
        d = g.director
        now = g.loop.time
        t = now - self.round_t0
        if kind == "round_start":
            self.shots_this_round = 0
            self.stuck = {}
        elif kind == "live":
            self.round_t0 = now
        elif kind == "kill":
            k, v = data.get("killer"), data["victim"]
            self.kills.append((k.side if k else "", data.get("weapon", ""), bool(data.get("headshot"))))
            ctx = ""
            if k is not None and hasattr(k, "brain") and hasattr(v, "brain"):
                c = v.perception.contacts.get(id(k))
                saw = "saw" if c is not None and (c.seen or now - c.time < 1.0) else "blind"
                dist = (k.position() - v.position()).length()
                ctx = (f"  [{dist:.0f}m, killer {k.brain.mode}/{k.brain.task.tag or k.brain.task.kind} "
                       f"{k.char.horizontal_speed:.1f}m/s, victim {v.brain.mode}/{v.brain.task.tag or v.brain.task.kind} "
                       f"{saw}]")
            g.log(f"[bots] {t:5.1f}s  {k.name if k else '-'} ({k.side if k else ''}) killed {v.name} "
                  f"with {data.get('weapon')}{' (HS)' if data.get('headshot') else ''} at "
                  f"{g.level.callout_at(v.position().x, v.position().y)}{ctx}")
            if k is not None and hasattr(k, "brain") and k.alive and self.shots_this_round < 3:
                d.spectator.target = k
                self.shot_queue.append((3, f"bots_r{d.match.round}_kill{self.shots_this_round}"))
                self.shots_this_round += 1
        elif kind == "bomb_planted":
            self.plants += 1
            g.log(f"[bots] {t:5.1f}s  charge planted at {d.bomb.site} by {data['planter'].name}")
            self.shot_queue.append((2, f"bots_r{d.match.round}_planted"))
        elif kind == "bomb_defused":
            self.defuses += 1
            g.log(f"[bots] {t:5.1f}s  charge defused by {data['defuser'].name}")
            self.shot_queue.append((2, f"bots_r{d.match.round}_defused"))
        elif kind == "round_end":
            r = data["result"]
            m = d.match
            self.results.append((r.winner_side, r.reason, t))
            a, b = m.scoreline()
            g.log(f"[bots] round {m.round}: {r.winner_side} win ({r.reason}) after {t:.0f}s  -  "
                  f"attack {a} : {b} defend")

    def tick(self, dt: float) -> None:
        self.ticks += 1
        d = self.game.director
        if self.ticks % 16:
            return
        now = self.game.loop.time
        if self.trace and self.ticks % (64 * 5) == 0 and d.match.phase in ("live", "planted"):
            for b in d.bots:
                if b.active and b.alive:
                    p = b.position()
                    self.game.log(f"[trace] {now - self.round_t0:5.1f}s {b.describe()} @ "
                                  f"{self.game.level.callout_at(p.x, p.y)} ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")
        # stuck detection: a bot with a move task that has not moved 0.6 m in 5 s
        if d.match.phase in ("live", "planted"):
            for b in d.bots:
                if not (b.active and b.alive):
                    continue
                br = b.brain
                moving = (br.follower.active and br.mode in ("task", "alert", "seek", "retreat")
                          and b.intent.wish.lengthSquared() > 0.25)
                rec = self.stuck.get(id(b))
                p = b.position()
                if not moving or rec is None:
                    self.stuck[id(b)] = [p, now, False]
                    continue
                if (p - rec[0]).length() > 0.6:
                    self.stuck[id(b)] = [p, now, False]
                elif now - rec[1] > 5.0 and not rec[2]:
                    rec[2] = True
                    msg = (f"{b.name} stuck at ({p.x:.1f}, {p.y:.1f}, {p.z:.2f}) "
                           f"{self.game.level.callout_at(p.x, p.y)} - {br.describe()}")
                    self.stuck_reports.append(msg)
                    self.game.log(f"[bots] STUCK {msg}")
        # follow the action
        sp = d.spectator
        if sp.active and not sp.free:
            busy = [b for b in d.bots if b.active and b.alive and b.brain.mode == "engage"]
            planter = [b for b in d.bots if b.active and b.alive and (b.brain.plant_t > 0 or b.brain.defuse_t > 0)]
            pick = (planter or busy or [None])[0]
            if pick is not None and (sp.target is None or sp.target.brain.mode != "engage"):
                sp.target = pick

    def frame(self) -> bool:
        g = self.game
        d = g.director
        if self.shot_queue:
            n, name = self.shot_queue[0]
            if n <= 0:
                self.shot_queue.pop(0)
                g.screenshot(str(paths.SCREENSHOT_DIR / f"demo_{name}.png"))
            else:
                self.shot_queue[0] = (n - 1, name)
        m = d.match
        if len(self.results) >= self.rounds or m.phase == "match_end":
            if not self.done:
                self.done = True
                self._summary()
            return True
        return False

    def _summary(self) -> None:
        g = self.game
        log = g.log
        d = g.director
        log("[bots] ===== summary =====")
        by_reason = {}
        for side, reason, t in self.results:
            by_reason[(side, reason)] = by_reason.get((side, reason), 0) + 1
        for (side, reason), n in sorted(by_reason.items()):
            log(f"[bots] {side:7s} {reason:15s} x{n}")
        if self.results:
            log(f"[bots] average round length {sum(t for *_, t in self.results) / len(self.results):.0f}s")
        weapons = {}
        for _, w, _hs in self.kills:
            weapons[w] = weapons.get(w, 0) + 1
        hs = sum(1 for *_, h in self.kills if h)
        log(f"[bots] kills {len(self.kills)} ({hs} headshots), by weapon: "
            + ", ".join(f"{w} {n}" for w, n in sorted(weapons.items(), key=lambda x: -x[1])))
        fired = sum(b.shots_fired for b in d.bots)
        log(f"[bots] plants {self.plants}, defuses {self.defuses}, shots {fired}, bullet hits {self.hits} "
            f"({100.0 * self.hits / max(fired, 1):.0f}%)")
        for b in sorted(d.bots, key=lambda b: -b.stats.kills):
            st = b.stats
            log(f"[bots]   {b.name:9s} {b.side:7s} K {st.kills:2d}  D {st.deaths:2d}  A {st.assists:2d}  ${b.money}")
        log(f"[bots] stuck reports: {len(self.stuck_reports)}")
        for msg in self.stuck_reports[:20]:
            log(f"[bots]   {msg}")


class DemoRunner:
    def __init__(self, game, name: str):
        if name not in SCRIPTS:
            raise SystemExit(f"unknown demo {name!r}; available: {', '.join(SCRIPTS)}")
        self.game = game
        self.name = name
        self.steps = SCRIPTS[name](game)
        self.i = 0
        self.wait = 0
        self.holds: dict[str, int] = {}
        self.shots: list[str] = []
        self.done = False
        # routes fast-forward: 8 fixed ticks per rendered frame
        self.frame_dt = 0.125 if name in ("routes", "round") else 1.0 / 30.0
        self.wait_for = None        # (phase, ticks left)
        self.shooting = None        # (stand-in, ticks, shots)
        self.goto = None            # (x, y, ticks, best_dist, best_tick)
        self.route = None           # (name, start_tick, ok)
        self.route_results: list[tuple[str, bool, float]] = []
        self.ticks = 0
        game.input.virtual_mode = True
        game.input.captured = True
        game.debug_hud.toggle()
        game.log(f"[demo] running '{name}' ({len(self.steps)} steps)")

    def _eye(self) -> Point3:
        c = self.game.player.char
        return Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height)

    def _steer(self) -> bool:
        """Drive toward the current waypoint; True while still walking."""
        g = self.game
        x, y, best, best_tick = self.goto
        c = g.player.char.pos
        dx, dy = x - c.x, y - c.y
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            g.input.virtual.discard("forward")
            self.goto = None
            return False
        if dist < best - 0.25:
            best, best_tick = dist, self.ticks
        elif self.ticks - best_tick > 128:          # no progress for 2 s
            g.input.virtual.discard("forward")
            g.log(f"[demo]   STUCK at ({c.x:.1f}, {c.y:.1f}, {c.z:.2f}) heading for ({x}, {y})")
            self.goto = None
            if self.route is not None:
                self.route = (self.route[0], self.route[1], False)
            # skip the rest of this route
            while self.i < len(self.steps) and self.steps[self.i][0] != "end_route":
                self.i += 1
            return False
        g.player.yaw = math.degrees(math.atan2(-dx, dy))
        g.player.pitch = 0.0
        g.input.virtual.add("forward")
        self.goto = (x, y, best, best_tick)
        return True

    def _shoot(self) -> bool:
        """Aim at the target stand-in's head and tap fire until it dies."""
        g = self.game
        a, ticks, shots = self.shooting
        if not a.alive or shots > 30:
            if a.alive:
                g.log(f"[demo]   could not kill {a.name}")
            self.shooting = None
            return False
        head = a.position() + Vec3(0, 0, 1.08 if a.crouch > 0.5 else 1.63)
        eye = self._eye()
        d = head - eye
        g.player.yaw = math.degrees(math.atan2(-d.x, d.y))
        g.player.pitch = math.degrees(math.atan2(d.z, math.hypot(d.x, d.y)))
        if ticks % 14 == 6:
            g.input.press("fire")
            shots += 1
        self.shooting = (a, ticks + 1, shots)
        return True

    def _approach(self, a) -> None:
        """Teleport to a spot with line of sight 6 m in front of a stand-in."""
        from engine.physics import MASK_SIGHT
        g = self.game
        base = a.position()
        head = base + Vec3(0, 0, 1.08 if a.crouch > 0.5 else 1.63)
        fwd = a.forward()
        h0 = math.degrees(math.atan2(-fwd.x, fwd.y))
        for k in range(16):
            ang = math.radians(h0 + (k // 2) * 22.5 * (1 if k % 2 == 0 else -1))
            for dist in (6.0, 4.0, 8.0, 2.5, 1.6):
                p = base + Vec3(-math.sin(ang), math.cos(ang), 0) * dist
                down = g.physics.ray_cast(p + Vec3(0, 0, 1.0), p - Vec3(0, 0, 6.0), MASK_SIGHT)
                if down is None:
                    continue
                feet = down.pos
                eye = feet + Vec3(0, 0, 1.69)
                block = g.physics.ray_cast(eye, head, MASK_SIGHT)
                # the whole hull footprint must be free, or the controller pushes us out of the spot
                # (cast downward: rays that start inside a box report no hit)
                clear = all(g.physics.ray_cast(feet + Vec3(ox, oy, 2.2), feet + Vec3(ox, oy, 0.15), MASK_SIGHT) is None
                            for ox in (-0.45, 0.0, 0.45) for oy in (-0.45, 0.0, 0.45))
                if block is None and clear:
                    g.player.set_pose((feet.x, feet.y, eye.z), (0, 0))
                    g.log(f"[demo]   engaging {a.name} at ({base.x:.1f}, {base.y:.1f}, {base.z:.2f}) "
                          f"from ({feet.x:.1f}, {feet.y:.1f}, {feet.z:.2f})")
                    return
        g.log(f"[demo]   no clear spot near {a.name}")

    def tick(self, dt: float) -> None:
        g = self.game
        self.ticks += 1
        if self.goto is not None and self._steer():
            return
        if self.shooting is not None and self._shoot():
            return
        if self.wait_for is not None:
            phase, left = self.wait_for
            m = g.director.match
            if m.phase == phase or left <= 0:
                if left <= 0:
                    g.log(f"[demo]   timed out waiting for phase {phase} (now {m.phase})")
                self.wait_for = None
            else:
                self.wait_for = (phase, left - 1)
                return
        for action in list(self.holds):
            self.holds[action] -= 1
            if self.holds[action] <= 0:
                del self.holds[action]
                g.input.virtual.discard(action)
        if self.wait > 0:
            self.wait -= 1
            return
        while self.i < len(self.steps) and self.wait <= 0:
            st = self.steps[self.i]
            self.i += 1
            kind = st[0]
            if kind == "pose":
                _, x, y, z, h, p = st
                g.player.set_pose((x, y, z), (h, p))
            elif kind == "aim":
                eye = self._eye()
                dx, dy, dz = st[1] - eye.x, st[2] - eye.y, st[3] - eye.z
                g.player.yaw = math.degrees(math.atan2(-dx, dy))
                g.player.pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
            elif kind == "give":
                w = g.weapons
                w.inv.give_weapon(st[1])
                w.select(w.db.weapons[st[1]].slot, force=True)
            elif kind == "select":
                g.weapons.select(st[1], force=True)
            elif kind == "hold":
                g.input.virtual.add(st[1])
                g.input.press(st[1])
                self.holds[st[1]] = st[2]
                self.wait = st[2]
            elif kind == "hold_async":
                g.input.virtual.add(st[1])
                g.input.press(st[1])
                self.holds[st[1]] = st[2]
            elif kind == "press":
                g.input.press(st[1])
                self.wait = 1
            elif kind == "wait":
                self.wait = st[1]
            elif kind == "shot":
                self.shots.append(st[1])
                self.wait = 1
            elif kind == "report":
                self.report(st[1])
            elif kind == "call":
                st[1](g)
            elif kind == "route":
                _, name, x, y, z, h = st
                g.player.set_pose((x, y, z + g.player.char.eye_height), (h, 0))
                self.route = (name, self.ticks, True)
                g.log(f"[demo] route {name}")
            elif kind == "goto":
                c = g.player.char.pos
                self.goto = (st[1], st[2], math.hypot(st[1] - c.x, st[2] - c.y), self.ticks)
                if self._steer():
                    return
            elif kind == "expect_z":
                z = g.player.char.pos.z
                if not st[1] <= z <= st[2]:
                    g.log(f"[demo]   WRONG HEIGHT z={z:.2f}, expected {st[1] + 0.35:.2f}")
                    self.route = (self.route[0], self.route[1], False)
            elif kind == "wait_phase":
                self.wait_for = (st[1], st[2])
                return
            elif kind == "buy":
                from gameplay.shop import find_item
                d = g.director
                it = find_item(g.weapon_db, d.rules, d.player_agent.side, st[1])
                ok, msg = d.shop.buy(d.player_agent, it)
                g.log(f"[demo]   buy {st[1]}: {msg}")
            elif kind == "open_buy":
                g.buy_menu.open()
            elif kind == "close_buy":
                g.buy_menu.close()
            elif kind == "console":
                g.log(f"[demo]   console> {st[1]}: {g.console.run(st[1])}")
            elif kind == "shoot_standin":
                d = g.director
                enemies = [a for a in d.standins if a.side != d.player_agent.side and a.alive]
                if enemies:
                    a = enemies[0]
                    self._approach(a)
                    self.shooting = (a, 0, 0)
                    return
            elif kind == "teleport":
                _, x, y, z, h = st
                g.player.set_pose((x, y, z + 1.69), (h, 0))
            elif kind == "aim_bomb":
                b = g.director.bomb.pos
                eye = self._eye()
                d = b - eye
                g.player.yaw = math.degrees(math.atan2(-d.x, d.y))
                g.player.pitch = math.degrees(math.atan2(d.z, math.hypot(d.x, d.y)))
            elif kind == "scoreboard":
                g._scoreboard(st[1])
            elif kind == "report_match":
                d = g.director
                m = d.match
                a, b = m.scoreline()
                last = m.history[-1] if m.history else None
                g.log(f"[demo] --- {st[1]}: phase={m.phase} round={m.round} score A{a}-D{b} "
                      f"money=${d.player_agent.money} side={d.player_agent.side} bomb={d.bomb.state}"
                      + (f" last={last.winner_side}/{last.reason}" if last else ""))
            elif kind == "end_route":
                name, t0, ok = self.route
                secs = (self.ticks - t0) / 64.0
                self.route_results.append((name, ok, secs))
                g.log(f"[demo] route {name}: {'PASS' if ok else 'FAIL'} ({secs:.1f} s)")
                self.route = None
        if self.i >= len(self.steps) and self.wait <= 0 and not self.shots and self.goto is None:
            if not self.done and self.route_results:
                ok = sum(1 for r in self.route_results if r[1])
                g.log(f"[demo] routes: {ok}/{len(self.route_results)} passed")
            self.done = True

    def report(self, label: str) -> None:
        g = self.game
        lines = [f"[demo] --- {label}"]
        for d in g.dummies:
            txt = d.label_np.node().getText().replace("\n", " | ")
            if txt:
                lines.append(f"[demo]   {d.damageable.name:16s} hp={d.damageable.health:5.1f} "
                             f"armor={d.damageable.armor:5.1f}  {txt}")
        ws = g.weapons.inv.current()
        if ws is not None:
            lines.append(f"[demo]   weapon {ws.d.key} ammo {ws.ammo}/{ws.reserve}")
        g.log("\n".join(lines))

    def frame(self) -> bool:
        """Called after each rendered frame; returns True when finished."""
        while self.shots:
            name = self.shots.pop(0)
            self.game.screenshot(str(paths.SCREENSHOT_DIR / f"demo_{name}.png"))
        return self.done
