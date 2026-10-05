"""The Siege layer of a match (Milestone 6), owned by the MatchDirector.

Per round every participant gets a *kit* from its specialist
(data/specialists.json): one unique gadget plus the common equipment -
drones and wall charges for attackers, reinforcements for defenders. This
module owns everything placed in the world during a round (gadgets,
drones, the map cameras), the team pings, and the human's controls:

    X        specialist gadget (place / throw / swing / scan)
    C        wall charge (attackers)
    6        drones (attackers) or cameras (defenders); in a view:
             mouse looks, WASD drives a drone, LMB pings, Q/E switch, 6 leaves
    F (hold) reinforce the wall you look at (defenders, 2 per round)
    MMB      ping what you look at for your team

Bots use the same entry points (``deploy``, ``reinforce``, ``throw_drone``,
``pulse``...) through ai/gadget_ai.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from panda3d.core import Point3, Vec3

from engine.physics import MASK_BULLETS, MASK_SIGHT
from gameplay.gadgets import (DEPLOYABLES, Deployable, DeployShield, MotionSensor, RazorWire, SignalJammer,
                              ThermalLance, WallCharge, emp_grenade_def, load_specialists, side_of)
from gameplay.observation import Drone, MapCamera, view_dir

SIDES = ("attack", "defend")


@dataclass
class Kit:
    specialist: str = "recruit"
    gadget: str = ""
    gadget_left: int = 0
    charges: int = 0
    bought_charges: int = 0
    drones: int = 0
    reinforcements: int = 0
    ready_t: float = 0.0              # gadget cooldown (pulse, hammer)
    swing_t: float | None = None      # hammer impact time
    pulse_until: float = 0.0


@dataclass
class Ping:
    side: str
    pos: Point3
    kind: str                         # spot | mark | sensor | pulse | drone | camera
    until: float
    agent: object = None
    by: object = None
    born: float = 0.0
    extra: dict = field(default_factory=dict)

    def where(self) -> Point3:
        a = self.agent
        if a is not None and getattr(a, "alive", False) and hasattr(a, "position"):
            return a.position() + Vec3(0, 0, 1.0)
        return self.pos


class Tactical:
    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.cfg = load_specialists()
        self.gcfg = self.cfg["gadgets"]
        self.human_choice = {s: self.cfg[s][0]["key"] for s in SIDES}
        self.kits: dict[int, Kit] = {}
        self.deployables: list = []
        self.drones: list[Drone] = []
        self.cameras: list[MapCamera] = []
        self.pings: list[Ping] = []
        self._slow: dict[int, float] = {}
        self.view = None                  # Drone | MapCamera the human looks through
        self.view_lost_t = 0.0
        self.action = None                # human hold action: {"kind", "t", "total", "panel"}
        self.message = ""
        self._message_t = 0.0
        self.listeners: list = []         # callback(kind, data) -> HUD
        self.verbose = False              # log gadget use (bot match demo)
        self.stats: dict[str, int] = {}

    def log(self, key: str, text: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1
        if self.verbose:
            self.game.log(f"[tac] t={self.now:6.1f} {text}")

    # ------------------------------------------------------------ specialists
    def specialists(self, side: str) -> list[dict]:
        return list(self.cfg[side])

    def specialist(self, key: str) -> dict:
        for s in SIDES:
            for sp in self.cfg[s]:
                if sp["key"] == key:
                    return sp
        return self.cfg["recruit"]

    def kit(self, agent) -> Kit:
        k = self.kits.get(id(agent))
        if k is None:
            k = self.kits[id(agent)] = Kit()
        return k

    def choose(self, agent, key: str) -> bool:
        """Human picks a specialist (buy menu). Applies at once during freeze/prep
        if the old gadget is untouched, otherwise from the next round."""
        side = agent.side
        if side not in SIDES or key not in [s["key"] for s in self.cfg[side]] + ["recruit"]:
            return False
        self.human_choice[side] = key
        m = self.director.match
        kit = self.kit(agent)
        old = self.specialist(kit.specialist)
        unused = kit.gadget_left == int(self.gcfg.get(old.get("gadget", ""), {}).get("count", 0))
        if m.phase in ("freeze", "prep") and unused:
            before = {id(a): self.kit(a).specialist for a in m.participants if a.side == side}
            self._assign(side)
            for a in m.participants:
                if a.side == side and (a is agent or self.kit(a).specialist != before.get(id(a))):
                    self._fill_kit(a)
        return True

    def _assign(self, side: str) -> None:
        d = self.director
        members = [a for a in d.match.participants if a.side == side]
        keys = [s["key"] for s in self.cfg[side]]
        taken = set()
        human = d.player_agent
        if human in members and not d.spectate_only:
            hk = self.human_choice.get(side, keys[0])
            self.kit(human).specialist = hk
            taken.add(hk)
        bots = sorted((a for a in members if a is not human), key=lambda a: a.name)
        free = [k for k in keys if k not in taken]
        for i, b in enumerate(bots):
            self.kit(b).specialist = free[i] if i < len(free) else "recruit"

    def _fill_kit(self, agent) -> None:
        kit = self.kit(agent)
        sp = self.specialist(kit.specialist)
        kit.gadget = sp.get("gadget", "")
        kit.gadget_left = int(self.gcfg.get(kit.gadget, {}).get("count", 0)) if kit.gadget else 0
        common = self.cfg["common"].get(agent.side, {})
        recruit = kit.specialist == "recruit"
        kit.charges = kit.bought_charges = 0
        kit.drones = kit.reinforcements = 0
        if agent.side == "attack":
            kit.charges = int(common.get("free_wall_charges", 1)) + (int(common.get("recruit_bonus_charges", 1))
                                                                      if recruit else 0)
            kit.drones = int(common.get("drones", 2))
        else:
            kit.reinforcements = int(common.get("reinforcements", 2)) + (
                int(common.get("recruit_bonus_reinforcements", 1)) if recruit else 0)
        kit.ready_t = 0.0
        kit.swing_t = None

    def can_buy_charge(self, agent) -> tuple[bool, str]:
        if agent.side != "attack":
            return False, "not available to your team"
        if self.kit(agent).bought_charges >= int(self.gcfg["wall_charge"].get("max", 2)):
            return False, "carrying the maximum"
        return True, ""

    def buy_charge(self, agent) -> None:
        kit = self.kit(agent)
        kit.charges += 1
        kit.bought_charges += 1

    # ------------------------------------------------------------------ round
    def round_reset(self) -> None:
        self.exit_view()
        for d in self.deployables + self.drones + self.cameras:
            d.destroy(effect=False)
        self.deployables, self.drones, self.cameras, self.pings = [], [], [], []
        self.action = None
        self._slow.clear()
        for i, spec in enumerate(self.game.level.cameras):
            self.cameras.append(MapCamera(self, spec, i))
        for side in SIDES:
            self._assign(side)
        for a in self.director.match.participants:
            if a.side in SIDES:
                self._fill_kit(a)

    def clear(self) -> None:
        self.exit_view()
        for d in self.deployables + self.drones + self.cameras:
            d.destroy(effect=False)
        self.deployables, self.drones, self.cameras, self.pings = [], [], [], []

    # ---------------------------------------------------------------- queries
    @property
    def now(self) -> float:
        return self.game.loop.time

    def say(self, text: str, secs: float = 2.0) -> None:
        self.message = text
        self._message_t = secs

    def characters(self):
        """(agent, kinematic character) of everyone alive (razor wire)."""
        d = self.director
        out = []
        human = d.player_agent
        if not d.spectate_only and human.alive and not self.game.player.noclip:
            out.append((human, self.game.player.char))
        for b in d.bots:
            if b.active and b.alive:
                out.append((b, b.char))
        return out

    def slow(self, agent, factor: float) -> None:
        k = id(agent)
        self._slow[k] = min(self._slow.get(k, 1.0), factor)

    def enemies_of(self, side: str) -> list:
        return [a for a in self.director.match.participants
                if a.side in SIDES and a.side != side and a.alive and getattr(a, "active", True)]

    def jammed(self, pos, side: str) -> bool:
        """An active enemy jammer covers pos."""
        for d in self.deployables:
            if isinstance(d, SignalJammer) and d.side != side and d.active:
                if (d.pos - Point3(*pos)).length() <= d.radius:
                    return True
        return False

    def views(self, side: str) -> list:
        if side == "attack":
            return [d for d in self.drones if d.alive and d.side == side]
        return [c for c in self.cameras if c.alive]

    def team_pings(self, side: str) -> list[Ping]:
        return [p for p in self.pings if p.side == side]

    # ------------------------------------------------------------------ pings
    def ping(self, side: str, pos, kind: str, duration: float, agent=None, by=None) -> None:
        now = self.now
        for p in self.pings:
            if p.side == side and agent is not None and p.agent is agent:
                p.until = max(p.until, now + duration)
                p.pos = Point3(*pos)
                p.kind = kind if kind != "mark" else p.kind
                return
        self.pings.append(Ping(side, Point3(*pos), kind, now + duration, agent, by, now))
        if agent is not None:
            tb = self.director.brain_of(side)
            if tb is not None:
                tb.intel(agent, Point3(*pos), now)
        d = self.director
        if side == d.player_agent.side or d.spectate_only:
            for cb in list(self.listeners):
                cb("ping", {"side": side, "kind": kind, "agent": agent})

    def spot_from(self, side: str, eye: Point3, direction: Vec3, by=None, max_range: float = 60.0) -> bool:
        """Ping along a view ray: an enemy (or their gadget) if one is hit, else the spot."""
        end = eye + direction * max_range
        hits = self.game.physics.ray_cast_all(eye, end, MASK_BULLETS)
        pt = None
        for h in hits:
            owner = h.node.getPythonTag("owner")
            if owner is not None and owner is not by:
                gadget = isinstance(owner, Deployable)
                agent = None if gadget else self.director.agent_of(owner)
                if agent is not None and agent.side != side and agent.alive:
                    self.ping(side, agent.position(), "spot", float(self.gcfg["ping"].get("time", 4.0)), agent=agent,
                              by=by)
                    return True
                if gadget and side_of(owner) != side:
                    self.ping(side, owner.center(), "gadget", float(self.gcfg["ping"].get("time", 4.0)), by=by)
                    return True
                continue
            if h.node.getPythonTag("owner") is None:
                pt = h.pos
                break
        if pt is not None:
            self.ping(side, pt, "mark", 3.0, by=by)
        return False

    # ----------------------------------------------------------- world events
    def on_explosion(self, pos, radius: float, source=None) -> None:
        """Explosions wreck gadgets and drones nearby."""
        p = Point3(*pos)
        for d in list(self.deployables) + list(self.drones):
            if d is source or not d.alive:
                continue
            r = radius + (d.blocks_explosions if d.blocks_explosions else 0.4)
            if (d.center() - p).length() <= r:
                d.destroy()

    def emp(self, pos, radius: float, duration: float, side: str) -> int:
        """Disable enemy electronics (gadgets, drones, cameras) in range."""
        p = Point3(*pos)
        n = 0
        until = self.now + duration
        for d in self.deployables + self.drones + self.cameras:
            if d.alive and d.side != side and d.electronic and (d.center() - p).length() <= radius:
                d.emp(until)
                n += 1
        self.log("emp", f"EMP disabled {n} device(s)")
        return n

    def pulse(self, agent) -> int:
        """Vesper's pulse: reveal enemies within the radius to the team."""
        cfg = self.gcfg["pulse_scanner"]
        r = float(cfg.get("radius", 15.0))
        pos = agent.position()
        n = 0
        for e in self.enemies_of(agent.side):
            if (e.position() - pos).length() <= r:
                self.ping(agent.side, e.position(), "pulse", float(cfg.get("reveal_time", 3.5)), agent=e, by=agent)
                n += 1
        self.game.audio.play_at("pulse", pos + Vec3(0, 0, 1.2), 0.8)
        self.kit(agent).pulse_until = self.now + 0.6
        self.log("pulse", f"{agent.name} pulsed: {n} contact(s)")
        return n

    # ---------------------------------------------------------- placing things
    def aim_ray(self, eye: Point3, direction: Vec3, max_range: float, mask=MASK_SIGHT):
        return self.game.physics.ray_cast(eye, eye + direction * max_range, mask)

    def breach_spot(self, eye: Point3, direction: Vec3, kind: str):
        """(panel, position, normal) where a charge/lance would go, or a reason string."""
        cfg = self.gcfg[kind]
        hit = self.aim_ray(eye, direction, float(cfg.get("place_range", 1.6)))
        if hit is None:
            return "move closer to a wall"
        panel = hit.node.getPythonTag("panel")
        if panel is None:
            return "only destructible walls and hatches"
        if panel.reinforced and kind != "thermal_lance":
            return "the wall is reinforced - use a thermal lance"
        n = Vec3(hit.normal)
        pos = Point3(hit.pos)
        if panel.spec.kind != "floor":
            a, b = panel.ax
            vert = b if abs(panel.R[b][2]) > 0.9 else a
            bottom = float(panel.center[2]) - panel.size[vert] / 2
            pos.z = bottom + min(1.0, panel.size[vert] / 2)
            n.z = 0.0
            n.normalize()
        for d in self.deployables:
            if isinstance(d, (WallCharge, ThermalLance)) and d.alive and (d.pos - pos).length() < 0.9:
                return "already a charge there"
        return panel, pos + n * 0.012, n

    def floor_spot(self, eye: Point3, yaw: float, pitch: float, max_range: float):
        """Point on the floor in front of the eye (for wire, shield, jammer)."""
        d = view_dir(yaw, pitch)
        hit = self.aim_ray(eye, d, max_range)
        if hit is not None and hit.normal.z > 0.7:
            return Point3(hit.pos)
        flat = view_dir(yaw, 0.0)
        reach = max_range if hit is None else min(max_range, max((hit.pos - eye).length() - 0.4, 0.4))
        p = eye + flat * reach
        down = self.game.physics.ray_cast(p, p - Vec3(0, 0, 2.6), MASK_SIGHT)
        if down is None or down.normal.z < 0.7:
            return None
        return Point3(down.pos)

    def deploy(self, agent, kind: str, eye: Point3, yaw: float, pitch: float) -> tuple[bool, str]:
        """Place a gadget the way a player would (aim from ``eye``). Used by the
        human and the bots."""
        kit = self.kit(agent)
        side = agent.side
        if kind == "wall_charge":
            if kit.charges <= 0:
                return False, "no wall charges"
        elif kit.gadget != kind or kit.gadget_left <= 0:
            return False, "nothing left"
        cfg = self.gcfg[kind]
        direction = view_dir(yaw, pitch)
        obj = None
        if kind in ("wall_charge", "thermal_lance"):
            spot = self.breach_spot(eye, direction, kind)
            if isinstance(spot, str):
                return False, spot
            panel, pos, n = spot
            cls = WallCharge if kind == "wall_charge" else ThermalLance
            obj = cls(self, agent, side, panel, pos, n)
            self.game.audio.play_at("charge_place", pos, 0.7)
        elif kind == "motion_sensor":
            hit = self.aim_ray(eye, direction, float(cfg.get("place_range", 2.2)))
            if hit is None or hit.node.getPythonTag("owner") is not None:
                return False, "aim at a wall or the floor"
            obj = MotionSensor(self, agent, side, hit.pos + hit.normal * 0.005, hit.normal)
            self.game.audio.play_at("charge_place", hit.pos, 0.5)
        elif kind in ("razor_wire", "deploy_shield", "signal_jammer"):
            pos = self.floor_spot(eye, yaw, pitch, float(cfg.get("place_range", 1.6)))
            if pos is None:
                return False, "no floor there"
            cls = {"razor_wire": RazorWire, "deploy_shield": DeployShield, "signal_jammer": SignalJammer}[kind]
            obj = cls(self, agent, side, pos, yaw)
            self.game.audio.play_at("shield_deploy" if kind == "deploy_shield" else "wire_place", pos, 0.7)
        else:
            return False, "not a placeable gadget"
        self.deployables.append(obj)
        if kind == "wall_charge":
            kit.charges -= 1
        else:
            kit.gadget_left -= 1
        self.log(kind, f"{agent.name} placed {cfg.get('name', kind).lower()} at "
                       f"({obj.pos.x:.1f}, {obj.pos.y:.1f}, {obj.pos.z:.1f})")
        return True, cfg.get("name", kind)

    def use_gadget(self, agent, eye: Point3, yaw: float, pitch: float, inherit: Vec3 | None = None) -> tuple[bool, str]:
        """The specialist key (X): place, throw, swing or scan."""
        kit = self.kit(agent)
        g = kit.gadget
        if not g:
            return False, "recruits have no gadget"
        if kit.gadget_left <= 0:
            return False, f"no {self.gcfg[g].get('name', g).lower()} left"
        now = self.now
        if now < kit.ready_t:
            return False, ""
        cfg = self.gcfg[g]
        if g in DEPLOYABLES:
            return self.deploy(agent, g, eye, yaw, pitch)
        if g == "pulse_scanner":
            n = self.pulse(agent)
            kit.gadget_left -= 1
            kit.ready_t = now + float(cfg.get("cooldown", 8.0))
            return True, f"pulse: {n} contact{'s' if n != 1 else ''}"
        if g == "breach_hammer":
            kit.ready_t = now + float(cfg.get("cooldown", 1.1))
            kit.swing_t = now + float(cfg.get("swing_time", 0.45))
            self.game.audio.play_at("knife_swing", eye, 0.6)
            return True, ""
        if g == "emp_grenade":
            from weapons.grenades import Grenade, throw_velocity
            gd = emp_grenade_def()
            vel = throw_velocity(yaw, pitch, gd.throw_speed, inherit or Vec3(0, 0, 0))
            start = eye + view_dir(yaw, 0) * 0.3 - Vec3(0, 0, 0.1)
            self.game.spawn_grenade(Grenade(self.game, gd, start, vel, agent))
            self.game.audio.play_at("throw", start, 0.5)
            kit.gadget_left -= 1
            kit.ready_t = now + 0.8
            return True, ""
        return False, ""

    def hammer_hit(self, agent, eye: Point3, yaw: float, pitch: float) -> None:
        cfg = self.gcfg["breach_hammer"]
        d = view_dir(yaw, pitch)
        hits = self.game.physics.ray_cast_all(eye, eye + d * float(cfg.get("range", 2.0)), MASK_BULLETS)
        hit = next((h for h in hits if h.node.getPythonTag("owner") is not agent), None)
        if hit is None:
            # razor wire has no collision: check what lies on the floor in front
            for dep in self.deployables:
                if isinstance(dep, RazorWire) and dep.alive and dep.contains(eye + d * 1.5 - Vec3(0, 0, 1.4)):
                    dep.hammer()
            return
        g = self.game
        dep = hit.node.getPythonTag("deployable")
        owner = hit.node.getPythonTag("owner")
        panel = hit.node.getPythonTag("panel")
        if dep is not None:
            dep.destroy()
        elif panel is not None:
            if panel.reinforced:
                g.effects.sparks(hit.pos, hit.normal, 8)
                g.audio.play_at("impact_metal", hit.pos, 0.9)
                return
            panel.damage(hit.pos, float(cfg.get("radius", 0.42)), float(cfg.get("damage", 400)), "melee", d)
        elif owner is not None and hasattr(owner, "damageable") and owner is not agent:
            from gameplay.damage import DamageInfo
            info = DamageInfo(float(cfg.get("player_damage", 80)), 0.85, hit.node.getTag("hitgroup") or "chest",
                              "melee", agent, "breach_hammer", tuple(hit.pos), tuple(d))
            res = owner.damageable.take_damage(info)
            if res is not None and hasattr(owner, "on_hit"):
                owner.on_hit(res, hit.pos, d)
        for w in self.deployables:
            if isinstance(w, RazorWire) and w.alive and w.contains(hit.pos):
                w.hammer()
        g.audio.play_at("hammer_hit", hit.pos, 1.0)
        g.effects.impact(hit.pos, hit.normal, "concrete", d)
        g.notify_noise(hit.pos, 1.0, 35.0, source=agent)

    # ------------------------------------------------------------- reinforce
    def reinforce_target(self, eye: Point3, direction: Vec3):
        """The reinforceable wall (within reach) or hatch (overhead) you look at."""
        cfg = self.gcfg["reinforce"]
        reach = float(cfg.get("range", 1.8))
        hit = self.aim_ray(eye, direction, reach + 1.4)
        if hit is None:
            return None
        panel = hit.node.getPythonTag("panel")
        if panel is None or not panel.spec.reinforceable:
            return None
        if panel.spec.kind != "floor" and (hit.pos - eye).length() > reach:
            return None
        return panel

    def finish_reinforce(self, agent, panel) -> bool:
        kit = self.kit(agent)
        if kit.reinforcements <= 0 or not panel.can_reinforce():
            return False
        panel.reinforce()
        kit.reinforcements -= 1
        self.log("reinforce", f"{agent.name} reinforced {panel.spec.name or panel.index}")
        self.game.audio.play_at("reinforce", Point3(*panel.center), 0.9)
        return True

    # ----------------------------------------------------------------- drones
    def throw_drone(self, agent, pos: Point3, yaw: float) -> Drone | None:
        kit = self.kit(agent)
        if kit.drones <= 0:
            return None
        kit.drones -= 1
        fwd = view_dir(yaw, 0.0)
        vel = fwd * 3.5 + Vec3(0, 0, 2.2)
        n = sum(1 for d in self.drones if d.owner is agent) + 1
        drone = Drone(self, agent, agent.side, pos + fwd * 0.45 + Vec3(0, 0, 0.6), yaw, vel,
                      label=f"{agent.name} drone {n}")
        self.drones.append(drone)
        self.game.audio.play_at("throw", pos + Vec3(0, 0, 1.0), 0.5)
        self.log("drone", f"{agent.name} threw a drone")
        return drone

    # ------------------------------------------------------------ human view
    @property
    def observing(self) -> bool:
        return self.view is not None

    def enter_view(self, view) -> None:
        g = self.game
        if self.view is not None and self.view is not view and isinstance(self.view, Drone):
            self.view.controller = None
            self.view.drive(Vec3(0, 0, 0))
            if self.view.alive:
                self.view.root.show()
        self.view = view
        if isinstance(view, Drone):
            view.controller = "human"
            view.root.hide()              # the lens would fill the screen
        g.input.clear_presses()
        g.player.external_view = self._view_mouse
        g.weapons.force_hide_vm = True
        g.audio.play_ui("camera_switch", 0.5)

    def exit_view(self) -> None:
        if self.view is None:
            return
        g = self.game
        if isinstance(self.view, Drone):
            self.view.controller = None
            self.view.drive(Vec3(0, 0, 0))
            if self.view.alive:
                self.view.root.show()
        self.view = None
        g.player.external_view = None
        g.weapons.force_hide_vm = False
        g.input.clear_presses()

    def cycle_view(self, step: int) -> None:
        human = self.director.player_agent
        views = self.views(human.side)
        if not views:
            self.exit_view()
            return
        i = views.index(self.view) if self.view in views else -1
        self.enter_view(views[(i + step) % len(views)])

    def _view_mouse(self, dx: float, dy: float) -> None:
        v = self.view
        if v is None:
            return
        if isinstance(v, MapCamera):
            v.look(-dx, -dy)
        else:
            v.yaw = (v.yaw - dx) % 360.0
            v.pitch = max(-70.0, min(70.0, v.pitch - dy))

    def _toggle_view(self) -> None:
        if self.view is not None:
            self.exit_view()
            return
        d = self.director
        human = d.player_agent
        g = self.game
        if human.side == "attack":
            own = [x for x in self.drones if x.alive and x.owner is human]
            if not own and self.kit(human).drones > 0:
                drone = self.throw_drone(human, g.player.char.pos, g.player.yaw)
                if drone is not None:
                    self.enter_view(drone)
                    return
            views = own or self.views("attack")
            if not views:
                self.say("no drones left")
                return
            self.enter_view(views[-1])
        else:
            views = self.views("defend")
            if not views:
                self.say("every camera is destroyed")
                return
            self.enter_view(views[0])

    def _drive_view(self, dt: float) -> None:
        inp = self.game.input
        v = self.view
        if inp.consume("observe"):
            self.exit_view()
            return
        if inp.consume("lean_right") or inp.consume("wheel_down"):
            self.cycle_view(1)
            v = self.view
        elif inp.consume("lean_left") or inp.consume("wheel_up"):
            self.cycle_view(-1)
            v = self.view
        if v is None:
            return
        if inp.consume("fire") and not v.jammed:
            self.spot_from(self.director.player_agent.side, v.eye(), view_dir(v.yaw, v.pitch), by=v)
            self.game.audio.play_ui("ping", 0.4)
        if isinstance(v, Drone) and v.controller == "human":
            f = float(inp.is_down("forward")) - float(inp.is_down("back"))
            s = float(inp.is_down("right")) - float(inp.is_down("left"))
            h = math.radians(v.yaw)
            wish = Vec3(-math.sin(h), math.cos(h), 0) * f + Vec3(math.cos(h), math.sin(h), 0) * s
            if wish.lengthSquared() > 1e-6:
                wish.normalize()
            v.drive(wish, inp.consume("jump"))

    # ---------------------------------------------------------- human input
    def _human_update(self, dt: float) -> None:
        d = self.director
        g = self.game
        human = d.player_agent
        inp = g.input
        m = d.match
        if d.spectate_only or human.side not in SIDES:
            return
        if not human.alive or m.phase in ("round_end", "halftime", "match_end", "waiting"):
            self.exit_view()
            self.action = None
            return
        if self.view is not None:
            if not self.view.alive:
                self.view_lost_t = 1.0
                self.exit_view()
            else:
                self._drive_view(dt)
                return
        if inp.consume("observe"):
            self._toggle_view()
            return
        if m.phase == "prep" and not d.hint:
            d.hint = ("Preparation: scout with a drone [6]" if human.side == "attack" else
                      "Preparation: reinforce walls [F], set up your gadget [X], cameras [6]")
        player = g.player
        eye = player.eye()
        direction = view_dir(player.yaw, player.pitch)
        kit = self.kit(human)
        locked = d.combat_locked(human)
        if inp.consume("ping"):
            self.spot_from(human.side, eye, direction, by=player)
            g.audio.play_ui("ping", 0.4)
        # hammer impact
        if kit.swing_t is not None and self.now >= kit.swing_t:
            kit.swing_t = None
            self.hammer_hit(player, eye, player.yaw, player.pitch)
            g.weapons.punch.impulse(Vec3(0.0, -6.0, 3.0))
        if inp.consume("gadget"):
            if locked and kit.gadget not in ("razor_wire", "deploy_shield", "signal_jammer", "motion_sensor"):
                self.say("wait for the round to start")
            else:
                ok, msg = self.use_gadget(human, eye, player.yaw, player.pitch, player.char.vel)
                if msg:
                    self.say(msg)
        if inp.consume("charge") and human.side == "attack":
            if locked:
                self.say("wait for the round to start")
            else:
                ok, msg = self.deploy(human, "wall_charge", eye, player.yaw, player.pitch)
                self.say(msg if ok or msg else "")
        # hold F: reinforce
        if human.side == "defend":
            panel = self.reinforce_target(eye, direction)
            holding = inp.is_down("use")
            if self.action is not None:
                if not holding or panel is not self.action["panel"]:
                    self.action = None
                else:
                    self.action["t"] += dt
                    d.progress = ("reinforce", min(self.action["t"] / self.action["total"], 1.0))
                    if self.action["t"] >= self.action["total"]:
                        if self.finish_reinforce(human, panel):
                            self.say(f"reinforced ({kit.reinforcements} left)")
                        self.action = None
            elif panel is not None:
                if kit.reinforcements <= 0:
                    d.hint = "No reinforcements left"
                elif not panel.can_reinforce():
                    d.hint = "Already reinforced" if panel.reinforced else "Too damaged to reinforce"
                else:
                    d.hint = f"Hold [F] to reinforce ({kit.reinforcements} left)"
                    if holding:
                        self.action = {"kind": "reinforce", "panel": panel, "t": 0.0,
                                       "total": float(self.gcfg["reinforce"].get("time", 2.5))}
                        g.audio.play_at("hammer_hit", Point3(*panel.center), 0.4)

    @property
    def human_busy(self) -> bool:
        """Locks the human's movement (reinforcing or looking through a view)."""
        return self.view is not None or self.action is not None

    # ------------------------------------------------------------------ tick
    def fixed_update(self, dt: float) -> None:
        now = self.now
        self._human_update(dt)
        # last tick's wire contacts slow movement this tick
        slow, self._slow = self._slow, {}
        g = self.game
        g.player.slow = slow.get(id(self.director.player_agent), 1.0)
        for b in self.director.bots:
            b.slow = slow.get(id(b), 1.0)
        for kit_owner in self.director.match.participants:
            kit = self.kits.get(id(kit_owner))
            if kit is not None and kit.swing_t is not None and now >= kit.swing_t and kit_owner is not \
                    self.director.player_agent and kit_owner.alive:
                kit.swing_t = None
                self.hammer_hit(kit_owner, kit_owner.eye(), kit_owner.aim.yaw, kit_owner.aim.pitch)
        for dep in self.deployables:
            if dep.alive:
                dep.update(dt, now)
        for dr in self.drones:
            if dr.alive:
                dr.update(dt, now)
        for cam in self.cameras:
            if cam.alive:
                cam.update(dt, now)
        self.deployables = [x for x in self.deployables if x.alive]
        self.drones = [x for x in self.drones if x.alive]
        self.pings = [p for p in self.pings if p.until > now]
        self._spot_from_views(now)
        if self._message_t > 0:
            self._message_t -= dt
            if self._message_t <= 0:
                self.message = ""
        if self.view_lost_t > 0:
            self.view_lost_t -= dt

    def _spot_from_views(self, now: float) -> None:
        """Drones driven by bots spot enemies automatically twice a second (the
        human pings by hand; dead defender bots watch cameras: ai/gadget_ai.py)."""
        if int(now * 2) == int((now - 1 / 64) * 2):
            return
        for v in self.drones:
            if v.alive and v.controller is not None and v.controller != "human" and v is not self.view:
                self.scan_view(v, v.side, by=v.controller)

    def scan_view(self, v, side: str, by=None) -> int:
        """Ping every enemy a drone or camera can see right now."""
        if not v.alive or v.jammed:
            return 0
        phys = self.game.physics
        eye = v.eye()
        fwd = view_dir(v.yaw, 0.0)
        cos_half = math.cos(math.radians(float(v.cfg.get("fov", 85.0)) * 0.6))
        rng = float(v.cfg.get("view_range", 30.0))
        kind = "drone" if isinstance(v, Drone) else "camera"
        n = 0
        for e in self.enemies_of(side):
            c = e.center_of_mass()
            dv = c - eye
            dist = dv.length()
            if dist > rng or dv.dot(fwd) / max(dist, 1e-3) < cos_half:
                continue
            if phys.ray_cast(eye, c, MASK_SIGHT) is None:
                self.ping(side, e.position(), kind, 3.0, agent=e, by=by)
                n += 1
        return n

    def frame_update(self, dt: float, alpha: float) -> None:
        for dr in self.drones:
            dr.frame_update(alpha)
        v = self.view
        if v is not None and v.alive:
            cam = self.game.camera
            if isinstance(v, Drone):
                p = v.char.interpolated_pos(alpha)
                cam.setPos(p.x, p.y, p.z + 0.11)
            else:
                cam.setPos(v.eye())
            cam.setHpr(v.yaw, v.pitch, 0.0)
            self.game.player.camera_pos = cam.getPos(self.game.render)
