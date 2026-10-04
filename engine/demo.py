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

    python main.py --map compound --demo routes   # walk every lane of the map
"""
from __future__ import annotations

import math

from panda3d.core import Point3

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


SCRIPTS = {"weapons": weapons_script, "viewmodels": viewmodel_script, "impacts": impacts_script,
           "flash": flash_script, "routes": routes_script}


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
        self.frame_dt = 0.125 if name == "routes" else 1.0 / 30.0
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

    def tick(self, dt: float) -> None:
        g = self.game
        self.ticks += 1
        if self.goto is not None and self._steer():
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
