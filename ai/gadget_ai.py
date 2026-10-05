"""Bots and the Siege layer (Milestone 6): one ``GadgetAI`` per TeamBrain.

Defenders, at the start of the prep phase, queue setup work before walking
to their hold spots: reinforce the walls and hatch of the site they guard
(breach walls first) and put their gadget down - wire across an attack
lane, sensors facing an entrance, the shield and the jammer at their spot.
Dead defenders keep watching the cameras.

Attackers throw a drone during prep and drive it along a lane towards the
target site (the drone spots defenders for the team). In the round they
breach: before the execute one bot blows (or burns, or hammers) a breach
wall of the site open and the team pushes through the hole. Vesper pulses
near the site, Static throws an EMP at known enemy electronics, and
everybody shoots enemy cameras, sensors, jammers and drones they see when
there is no fight going on.
"""
from __future__ import annotations

import math

import numpy as np
from panda3d.core import Point3, Vec3

from ai.aim import angles_to
from ai.brain import Task, solve_throw
from ai.steering import PathFollower
from engine.physics import MASK_SIGHT
from gameplay.gadgets import WallCharge, ThermalLance, emp_grenade_def
from gameplay.observation import view_dir


def _zone_dist(z, p) -> float:
    dx = max(z["min"][0] - p[0], 0.0, p[0] - z["max"][0])
    dy = max(z["min"][1] - p[1], 0.0, p[1] - z["max"][1])
    return math.hypot(dx, dy)


class GadgetAI:
    def __init__(self, team):
        self.team = team
        self.director = team.director
        self.game = team.game
        self.nav = team.nav
        self.rng = team.rng
        self.reset()

    def reset(self) -> None:
        self.prep_done = False
        self.live_done = False
        self.claimed: set[int] = set()          # panels being reinforced/breached
        self.drivers: dict[int, tuple] = {}     # drone id -> (drone, follower)
        self.breach: dict | None = None
        self.emp_done = False
        self._watch: dict[int, tuple] = {}      # dead bot id -> (view, until)
        self._scan_t = 0.0
        self._breach_site = None

    @property
    def tac(self):
        return self.director.tactical

    # ---------------------------------------------------------------- tick
    def update(self, now: float) -> None:
        m = self.director.match
        if m.phase == "freeze":
            self.prep_done = self.live_done = False
            return
        if m.phase == "prep" and not self.prep_done:
            self.prep_done = True
            if self.team.side == "defend":
                self._defend_setup(now)
            else:
                self._launch_drones(now)
        if m.phase in ("live", "planted") and not self.live_done:
            self.live_done = True
            self._park_drones()
        self._drive_drones(now)
        if m.phase in ("live", "planted"):
            self._live(now)

    # ------------------------------------------------------------ defence
    def _site_of(self, bot) -> str:
        area = self.team.area_of.get(id(bot), "mid")
        if area in self.team.sites:
            return area
        pos = bot.position()
        return min(self.team.sites, key=lambda n: (self.team.site_centers[n] - pos).length())

    def _stand_for(self, panel, site: str, inside: bool) -> Point3 | None:
        """Where to stand to work on a panel: on the site side (reinforce) or outside (breach)."""
        c = Point3(*panel.center)
        z = self.team.sites[site]
        if panel.spec.kind == "floor":
            below = Point3(c.x, c.y, float(panel.center[2]) - panel.size[panel.thin] / 2 - 0.2)
            hit = self.game.physics.ray_cast(below, below - Vec3(0, 0, 8), MASK_SIGHT)
            if hit is None or not inside:
                return None
            p = self.nav.snap((c.x, c.y, hit.pos.z), search=4)
            return Point3(*p) if p else None
        n = panel.normal
        n = Vec3(n.x, n.y, 0)
        if n.lengthSquared() < 1e-6:
            return None
        n.normalize()
        a, b = panel.ax
        vert = b if abs(panel.R[b][2]) > 0.9 else a
        floor_z = float(panel.center[2]) - panel.size[vert] / 2
        off = panel.size[panel.thin] / 2 + 0.75
        cands = [Point3(c.x + n.x * off, c.y + n.y * off, floor_z), Point3(c.x - n.x * off, c.y - n.y * off, floor_z)]
        cands.sort(key=lambda p: _zone_dist(z, p) + 0.01 * (p - self.team.site_centers[site]).length())
        p = cands[0] if inside else cands[1]
        q = self.nav.snap((p.x, p.y, p.z), search=4)
        if q is None or abs(q[2] - floor_z) > 0.6 or math.hypot(q[0] - p.x, q[1] - p.y) > 1.0:
            return None
        return Point3(*q)

    @staticmethod
    def door_sized(panel) -> bool:
        """A wall panel that reaches the floor and is big enough to walk through
        once opened (not the strip under a window or above a door), or a hatch."""
        if panel.spec.kind == "floor":
            return True
        a, b = panel.ax
        vert = b if abs(panel.R[b][2]) > 0.9 else a
        horiz = a if vert == b else b
        bottom = float(panel.center[2]) - panel.size[vert] / 2
        return panel.size[vert] >= 2.0 and panel.size[horiz] >= 1.2 and bottom < 0.3

    def _panels_for(self, site: str, reinforceable: bool = True, breach: bool = False) -> list:
        z = self.team.sites[site]
        out = []
        for p in self.game.destruction.panels:
            if reinforceable and not p.spec.reinforceable:
                continue
            if breach and not p.spec.breach:
                continue
            if _zone_dist(z, p.center) > 1.5 or not self.door_sized(p):
                continue
            out.append(p)
        return out

    def _defend_setup(self, now: float) -> None:
        tac = self.tac
        for b in self.team.alive_bots():
            hold = b.brain.task
            kit = tac.kit(b)
            site = self._site_of(b)
            chain: list[Task] = []
            # reinforcements: breach walls first, then the hatch, then the rest
            panels = [p for p in self._panels_for(site) if p.index not in self.claimed and p.can_reinforce()]
            panels.sort(key=lambda p: (not p.spec.breach, p.spec.kind != "floor",
                                       (Point3(*p.center) - b.position()).length()))
            for p in panels:
                if len(chain) >= kit.reinforcements:
                    break
                stand = self._stand_for(p, site, inside=True)
                if stand is None:
                    continue
                self.claimed.add(p.index)
                chain.append(Task("use", stand, look=self._aim_point(p, stand), action=self._reinforce_action(p),
                                  action_time=float(tac.gcfg["reinforce"].get("time", 2.5)), tag="reinforce",
                                  timeout=25.0))
            g = self._gadget_task(b, kit, site, hold)
            if g is not None:
                chain.append(g)
            if not chain:
                continue
            for i, t in enumerate(chain):
                t.then = chain[i + 1] if i + 1 < len(chain) else hold
            b.brain.set_task(chain[0])

    def _aim_point(self, panel, stand: Point3) -> Point3:
        c = Point3(*panel.center)
        if panel.spec.kind == "floor":
            return Point3(c.x, c.y, float(panel.center[2]) - panel.size[panel.thin] / 2)
        # the point of the wall right in front of where we stand, at chest height
        lp = panel.to_local(stand)
        a, b = panel.ax
        vert = b if abs(panel.R[b][2]) > 0.9 else a
        horiz = a if vert == b else b
        loc = np.zeros(3)
        loc[horiz] = max(-panel.size[horiz] / 2 + 0.3, min(panel.size[horiz] / 2 - 0.3, lp[horiz]))
        loc[vert] = (-panel.size[vert] / 2 + min(1.2, panel.size[vert] / 2)) * (1 if panel.R[vert][2] > 0 else -1)
        return panel.to_world(loc)

    def _reinforce_action(self, panel):
        def act(bot) -> bool:
            if self.tac.reinforce_target(bot.eye(), view_dir(bot.aim.yaw, bot.aim.pitch)) is panel:
                self.tac.finish_reinforce(bot, panel)
            return True
        return act

    def _gadget_task(self, bot, kit, site: str, hold: Task) -> Task | None:
        g = kit.gadget
        if kit.gadget_left <= 0 or g not in ("razor_wire", "motion_sensor", "deploy_shield", "signal_jammer"):
            return None
        if g in ("deploy_shield", "signal_jammer"):
            if hold.pos is None:
                return None
            yaw = hold.yaw if hold.yaw is not None else 0.0
            fwd = view_dir(yaw, 0.0)
            spot = Point3(hold.pos) + fwd * 0.9
            return Task("use", Point3(hold.pos), look=Point3(spot.x, spot.y, hold.pos.z + 0.05),
                        action=self._deploy_action(g), action_time=0.6, tag="gadget", timeout=20.0)
        entries = self._entries(site)
        if not entries:
            return None
        if g == "razor_wire":
            first = None
            prev = None
            for outside, inward in entries[:kit.gadget_left]:
                t = Task("use", outside - inward * 1.3, look=outside + Vec3(0, 0, 0.05),
                         action=self._deploy_action(g), action_time=0.6, tag="gadget", timeout=20.0)
                if prev is not None:
                    prev.then = t
                first = first or t
                prev = t
            return first
        # motion sensor: on a wall behind the entrance, facing it
        outside, inward = entries[0]
        inner = outside + inward * 2.5
        eye = inner + Vec3(0, 0, 1.6)
        hit = self.game.physics.ray_cast(eye, eye + inward * 6.0, MASK_SIGHT)
        if hit is None or abs(hit.normal.z) > 0.5:
            return None
        mount = Point3(hit.pos.x, hit.pos.y, hit.pos.z + 0.6)
        stand = Point3(*(self.nav.snap((hit.pos.x - inward.x * 1.4, hit.pos.y - inward.y * 1.4, outside.z), search=4)
                         or (inner.x, inner.y, inner.z)))
        return Task("use", stand, look=mount, action=self._deploy_action(g), action_time=0.6, tag="gadget",
                    timeout=20.0)

    def _entries(self, site: str) -> list[tuple[Point3, Vec3]]:
        """Points just outside the site where the attack lanes come in, with the inward direction."""
        z = self.team.sites[site]
        out = []
        for lane in self.team.lanes.get(site, []):
            pts = lane["points"]
            for i in range(1, len(pts)):
                if _zone_dist(z, pts[i]) == 0.0:
                    a, b = Point3(*pts[i - 1]), Point3(*pts[i])
                    d = b - a
                    d.z = 0
                    if d.lengthSquared() < 1e-6:
                        break
                    d.normalize()
                    # walk back from the boundary crossing until 1.2 m outside
                    p = Point3(b)
                    for _ in range(60):
                        if _zone_dist(z, p) >= 1.2:
                            break
                        p = p - d * 0.2
                    q = self.nav.snap((p.x, p.y, p.z), search=3)
                    if q is not None:
                        out.append((Point3(*q), d))
                    break
        self.rng.shuffle(out)
        return out

    def _deploy_action(self, kind: str):
        def act(bot) -> bool:
            self.tac.deploy(bot, kind, bot.eye(), bot.aim.yaw, bot.aim.pitch)
            return True
        return act

    # ------------------------------------------------------------- drones
    def _launch_drones(self, now: float) -> None:
        tac = self.tac
        site = self.team.site or (next(iter(self.team.sites)) if self.team.sites else None)
        if site is None:
            return
        for b in self.team.alive_bots():
            if self.rng.random() < 0.25:
                continue
            drone = tac.throw_drone(b, b.char.pos, b.aim.yaw)
            if drone is None:
                continue
            drone.controller = b
            lanes = self.team.lanes.get(site) or []
            goal = self.team.site_centers[site]
            if lanes:
                lane = self.rng.choice(lanes)
                pts = lane["points"]
                goal = Point3(*pts[min(len(pts) - 1, lane["stage"] + 2)]) if pts else goal
            f = PathFollower(self.nav)
            self.drivers[id(drone)] = (drone, f, goal, now + 0.8)

    def _drive_drones(self, now: float) -> None:
        dt = 0.25
        for key, (drone, f, goal, start) in list(self.drivers.items()):
            if not drone.alive or drone.controller is None or drone.controller == "human":
                del self.drivers[key]
                continue
            if now < start or not drone.char.on_ground:
                continue
            p = drone.char.pos
            if not f.active and not f.failed and not f.arrived:
                f.go_to((p.x, p.y, p.z), goal)
                if f.failed:
                    del self.drivers[key]
                    continue
            if f.arrived or f.failed:
                drone.drive(Vec3(0, 0, 0))
                drone.yaw = (drone.yaw + 35.0) % 360.0      # look around
                continue
            wish, _, jump = f.update(dt, p, drone.char.horizontal_speed)
            drone.drive(wish, jump)
            if wish.lengthSquared() > 1e-6:
                drone.yaw = math.degrees(math.atan2(-wish.x, wish.y))

    def _park_drones(self) -> None:
        for drone, *_ in self.drivers.values():
            drone.drive(Vec3(0, 0, 0))
            drone.controller = None
        self.drivers.clear()

    # --------------------------------------------------------------- live
    def _live(self, now: float) -> None:
        if now < self._scan_t:
            return
        self._scan_t = now + 0.5
        tac = self.tac
        side = self.team.side
        bots = self.team.alive_bots()
        for b in bots:
            kit = tac.kit(b)
            # Vesper: pulse when close to the target site
            if kit.gadget == "pulse_scanner" and kit.gadget_left > 0 and now >= kit.ready_t and side == "attack":
                site = self.team.site
                if site and _zone_dist(self.team.sites[site], b.position()) < 14.0:
                    tac.use_gadget(b, b.eye(), b.aim.yaw, b.aim.pitch)
                    self.team.radio(b, "Pulse out.", key="pulse", every=8.0)
            # Maul's hammer swing in progress is handled by tactical; shoot enemy gadgets in view
            if b.brain.mode == "task" and b.brain.gadget_target is None:
                tgt = self._visible_gadget(b)
                if tgt is not None:
                    b.brain.gadget_target = tgt
        if side == "attack":
            self._breach_update(bots, now)
            self._emp(bots, now)
        else:
            self._watch_cameras(now)

    def _visible_gadget(self, bot):
        tac = self.tac
        eye = bot.eye()
        best, bd = None, 22.0
        cands = [d for d in tac.deployables if d.side != bot.side and d.damageable is not None and d.alive]
        cands += [d for d in tac.drones if d.side != bot.side and d.alive]
        if bot.side == "attack":
            cands += [c for c in tac.cameras if c.alive]
        fwd = view_dir(bot.aim.yaw, 0.0)
        for d in cands:
            c = d.center()
            v = c - eye
            dist = v.length()
            if dist > bd or dist < 0.5 or v.dot(fwd) / dist < 0.2:
                continue
            hit = self.game.physics.ray_cast(eye, c, MASK_SIGHT)
            if hit is not None and (hit.pos - c).length() > 0.25:
                continue
            best, bd = d, dist
        return best

    def _watch_cameras(self, now: float) -> None:
        """Dead defenders sit on the cameras and call out what they see."""
        tac = self.tac
        cams = [c for c in tac.cameras if c.alive and not c.jammed]
        dead = [b for b in self.team.bots() if not b.alive]
        if not cams or not dead:
            return
        for b in dead:
            view, until = self._watch.get(id(b), (None, 0.0))
            if view is None or not view.alive or now >= until:
                view = self.rng.choice(cams)
                self._watch[id(b)] = (view, now + 5.0)
            tac.scan_view(view, "defend", by=b)

    # ------------------------------------------------------------- breach
    def breach_busy(self) -> bool:
        """A teammate is still opening the wall: the execute waits for it."""
        br = self.breach
        if br is None:
            return False
        b = br["bot"]
        return b.alive and b.brain.task.tag in ("breach", "breach_wait") and b.now < br["deadline"]

    def _breach_update(self, bots, now: float) -> None:
        team = self.team
        if team.phase != "stage" or not team.site:
            return
        br = self.breach
        if br is not None:
            if br["bot"] not in bots or now > br["deadline"]:
                self.breach = None
            return
        if self._breach_site == team.site:
            return
        # only from the group gathered outside the site, not on the way there
        staged = [b for b in bots if id(b) in team.stage_arrived]
        if not staged:
            return
        self._breach_site = team.site
        bots = staged
        tac = self.tac
        panels = [p for p in self._panels_for(team.site, reinforceable=False, breach=True)
                  if p.destroyed_fraction() < 0.5 and p.spec.kind == "wall"]
        if not panels:
            return
        carrier = self.director.bomb.carrier
        options = []
        for b in bots:
            kit = tac.kit(b)
            for p in panels:
                if p.reinforced:
                    ok = kit.gadget == "thermal_lance" and kit.gadget_left > 0
                    tool = "thermal_lance"
                elif kit.gadget == "breach_hammer" and kit.gadget_left > 0:
                    ok, tool = True, "breach_hammer"
                else:
                    ok, tool = kit.charges > 0, "wall_charge"
                if not ok:
                    continue
                stand = self._stand_for(p, team.site, inside=False)
                if stand is None:
                    continue
                dist = (stand - b.position()).length()
                if dist > 32.0:
                    continue
                cost = dist + (25.0 if b is carrier else 0.0)
                options.append((cost, b, p, tool, stand))
        if not options:
            tac.log("breach_none", f"no breach at {team.site}: {len(panels)} wall(s), "
                                   f"{sum(p.reinforced for p in panels)} reinforced, no tool or no way round")
            return
        options.sort(key=lambda o: o[0])
        _, b, p, tool, stand = options[0]
        look = self._aim_point(p, stand)
        n = Vec3(stand.x - look.x, stand.y - look.y, 0)
        if n.lengthSquared() < 1e-6:
            return
        n.normalize()
        back = self.nav.snap((stand.x + n.x * 2.6, stand.y + n.y * 2.6, stand.z), search=4)
        back = Point3(*back) if back else stand
        prev_task = b.brain.task

        def resume(bot):
            # through the new hole into the site
            spot = team.site_centers[team.site]
            return Task("move", Point3(spot), look=spot + Vec3(0, 0, 1.2), wait=True, tag="clear") \
                if prev_task.kind != "plant" else prev_task

        def charge_gone(bot) -> bool:
            return not any(isinstance(d, (WallCharge, ThermalLance)) and d.alive and d.owner is bot
                           for d in tac.deployables)

        if tool == "breach_hammer":
            swings = {"n": 0}

            def swing(bot) -> bool:
                tac.use_gadget(bot, bot.eye(), bot.aim.yaw, bot.aim.pitch)
                swings["n"] += 1
                return p.destroyed_fraction() > 0.35 or swings["n"] >= 8

            task = Task("use", stand, look=look, action=swing, action_time=1.2, tag="breach", timeout=25.0, then=resume)
        else:
            def place(bot) -> bool:
                if tool == "wall_charge":
                    ok, msg = tac.deploy(bot, "wall_charge", bot.eye(), bot.aim.yaw, bot.aim.pitch)
                else:
                    ok, msg = tac.use_gadget(bot, bot.eye(), bot.aim.yaw, bot.aim.pitch)
                if not ok:
                    tac.log("breach_failed", f"{bot.name} could not breach: {msg}")
                return True
            wait = Task("hold", back, look=look, wait=True, until=charge_gone, tag="breach_wait", timeout=12.0,
                        then=resume)
            task = Task("use", stand, look=look, action=place, action_time=0.7, tag="breach", timeout=25.0, then=wait)
        b.brain.deferred = None
        b.brain.set_task(task, force=True)
        tac.log("breach_plan", f"{b.name} will breach {p.spec.name or p.index} with the {tool.replace('_', ' ')}")
        self.breach = {"bot": b, "panel": p, "deadline": now + 30.0}
        team.radio(b, {"wall_charge": "Placing a breach charge.", "thermal_lance": "Burning through the wall.",
                       "breach_hammer": "Opening the wall up."}[tool], key="breach", every=10.0)

    def _emp(self, bots, now: float) -> None:
        if self.emp_done or self.team.phase != "exec" or not self.team.site:
            return
        tac = self.tac
        site_c = self.team.site_centers[self.team.site]
        targets = [d for d in tac.deployables + tac.cameras
                   if d.side != "attack" and d.alive and d.electronic and (d.center() - site_c).length() < 16.0]
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
        gd = emp_grenade_def()
        self.game.spawn_grenade(Grenade(self.game, gd, start + view_dir(yaw, 0) * 0.3, vel, thrower))
        tac.kit(thrower).gadget_left -= 1
        self.emp_done = True
        self.team.radio(thrower, "EMP out!", key="emp", every=6.0)

