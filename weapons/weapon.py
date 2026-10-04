"""Runtime weapon state and the gunplay model (pure Python, used by player and bots).

Fire timing, ammo/reload, CS-style inaccuracy and fixed recoil patterns:

* **Inaccuracy** is the half-angle of a random cone:
      base(stand|crouch) + move * speed_fraction + air (if airborne) + accumulated fire
  where speed_fraction ramps from 0 at 34% of max speed (accurate "walk")
  to 1 at full speed, and every shot adds ``fire_add`` that decays with
  time constant ``fire_recovery``. First shots from a still, crouched
  player are therefore near pin-point.
* **Recoil** is a fixed, learnable spray pattern: shot *n* is offset by
  ``pattern[n]`` (degrees, x right / y up) from where the player aims.
  The camera only follows ``view_follow`` of that offset, so the player
  has to pull down/sideways to keep bullets on target. When the trigger is
  released the pattern index recovers (``recovery`` shots per second) and
  the view settles back.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from weapons.defs import WeaponDef

WALK_ACCURATE_FRACTION = 0.34


@dataclass
class AimContext:
    speed: float = 0.0          # horizontal speed (m/s)
    max_speed: float = 5.4      # max ground speed with this weapon
    on_ground: bool = True
    crouched: bool = False
    ads: float = 0.0            # 0..1 aim-down-sights blend
    scoped: bool = False


@dataclass
class WeaponEvent:
    kind: str                   # fire | dry | reload_start | reload_done | shell | cycle | draw
    time: float
    data: dict = field(default_factory=dict)


class WeaponState:
    def __init__(self, wdef: WeaponDef, rng: random.Random | None = None, infinite_reserve: bool = False):
        self.d = wdef
        self.rng = rng or random.Random()
        self.ammo = wdef.magazine
        self.reserve = wdef.reserve
        self.infinite_reserve = infinite_reserve
        self.next_fire = 0.0
        self.last_shot = -10.0
        self.recoil_index = 0.0
        self.fire_inacc = 0.0
        self.reload_end: float | None = None
        self.reload_kind = ""
        self.shell_next: float | None = None
        self.draw_end = 0.0
        self.shot_count = 0
        self.trigger_held = False
        self.events: list[WeaponEvent] = []
        self.zoom_level = 0          # sniper scope level (0 = unscoped)

    # ------------------------------------------------------------- status
    @property
    def reloading(self) -> bool:
        return self.reload_end is not None or self.shell_next is not None

    def busy(self, now: float) -> bool:
        return now < self.draw_end or self.reloading

    def deploy(self, now: float) -> None:
        self.cancel_reload()
        self.draw_end = now + self.d.draw_time
        self.next_fire = max(self.next_fire, self.draw_end)
        self.zoom_level = 0
        self.events.append(WeaponEvent("draw", now))

    def cancel_reload(self) -> None:
        self.reload_end = None
        self.shell_next = None

    # ------------------------------------------------------------- recoil
    def recoil_offset(self, index: float | None = None) -> tuple[float, float]:
        pattern = self.d.recoil.pattern
        i = self.recoil_index if index is None else index
        i = max(0.0, min(i, len(pattern) - 1))
        i0 = int(math.floor(i))
        i1 = min(i0 + 1, len(pattern) - 1)
        t = i - i0
        x = pattern[i0][0] * (1 - t) + pattern[i1][0] * t
        y = pattern[i0][1] * (1 - t) + pattern[i1][1] * t
        return x, y

    def view_offset(self) -> tuple[float, float]:
        x, y = self.recoil_offset()
        f = self.d.recoil.view_follow
        return x * f, y * f

    # -------------------------------------------------------- inaccuracy
    def inaccuracy(self, ctx: AimContext) -> float:
        ia = self.d.inaccuracy
        values = {"stand": ia.stand, "crouch": ia.crouch, "move": ia.move, "air": ia.air}
        if ctx.scoped and ia.scoped:
            values.update(ia.scoped)
        b = values["crouch"] if (ctx.crouched and ctx.on_ground) else values["stand"]
        lo = ctx.max_speed * WALK_ACCURATE_FRACTION
        frac = (ctx.speed - lo) / max(ctx.max_speed - lo, 1e-3)
        frac = min(max(frac, 0.0), 1.0)
        total = b + values["move"] * frac
        if not ctx.on_ground:
            total += values["air"]
        total += self.fire_inacc
        if ctx.ads > 0 and not ctx.scoped:
            total *= 1.0 + (ia.ads - 1.0) * ctx.ads
        return total

    # -------------------------------------------------------------- tick
    def update(self, now: float, dt: float) -> None:
        # recoil recovers only once the player stops spraying
        if now - self.last_shot > self.d.fire_interval * 1.25:
            self.recoil_index = max(0.0, self.recoil_index - self.d.recoil.recovery * dt)
        if self.d.inaccuracy.fire_recovery > 0:
            self.fire_inacc *= math.exp(-dt / self.d.inaccuracy.fire_recovery)
        if self.reload_end is not None and now >= self.reload_end:
            self._finish_reload(now)
        if self.shell_next is not None and now >= self.shell_next:
            self._insert_shell(now)

    def _finish_reload(self, now: float) -> None:
        need = self.d.magazine - self.ammo
        take = need if self.infinite_reserve else min(need, self.reserve)
        self.ammo += take
        if not self.infinite_reserve:
            self.reserve -= take
        self.reload_end = None
        self.events.append(WeaponEvent("reload_done", now))

    def _insert_shell(self, now: float) -> None:
        if self.ammo < self.d.magazine and (self.reserve > 0 or self.infinite_reserve):
            self.ammo += 1
            if not self.infinite_reserve:
                self.reserve -= 1
            self.events.append(WeaponEvent("shell", now))
        if self.ammo >= self.d.magazine or (self.reserve <= 0 and not self.infinite_reserve):
            self.shell_next = None
            self.events.append(WeaponEvent("reload_done", now))
        else:
            self.shell_next = now + float(self.d.raw.get("reload_shell_time", 0.5))

    def can_reload(self) -> bool:
        return (self.d.magazine > 0 and self.ammo < self.d.magazine
                and (self.reserve > 0 or self.infinite_reserve) and not self.reloading)

    def start_reload(self, now: float) -> bool:
        if not self.can_reload() or now < self.draw_end:
            return False
        self.zoom_level = 0
        if self.d.fire_mode == "pump":
            self.shell_next = now + float(self.d.raw.get("reload_start_time", 0.35)) + \
                float(self.d.raw.get("reload_shell_time", 0.5))
            self.reload_kind = "shells"
        else:
            empty = self.ammo == 0
            self.reload_kind = "empty" if empty else "tactical"
            self.reload_end = now + (self.d.reload_empty_time if empty else self.d.reload_time)
        self.events.append(WeaponEvent("reload_start", now, {"kind": self.reload_kind}))
        return True

    def reload_progress(self, now: float) -> float:
        if self.reload_end is None:
            return 0.0
        total = self.d.reload_empty_time if self.reload_kind == "empty" else self.d.reload_time
        return 1.0 - max(self.reload_end - now, 0.0) / max(total, 1e-3)

    # -------------------------------------------------------------- fire
    def wants_shot(self, now: float, trigger_down: bool, pressed: bool) -> bool:
        """Decide whether a shot happens this tick. Handles fire modes,
        empty magazines (dry fire + auto reload) and shotgun reload interrupts."""
        held_before = self.trigger_held
        self.trigger_held = trigger_down
        if not trigger_down and not pressed:
            return False
        if now < self.draw_end:
            return False
        if self.shell_next is not None:
            if self.ammo > 0 and (pressed or not held_before):
                self.shell_next = None          # interrupt shell reload to fire
            else:
                return False
        if self.reload_end is not None:
            return False
        if now < self.next_fire:
            return False
        if self.d.fire_mode in ("semi", "bolt", "pump", "melee") and held_before and not pressed:
            return False
        if self.d.magazine > 0 and self.ammo <= 0:
            if pressed or not held_before:
                self.events.append(WeaponEvent("dry", now))
                self.next_fire = now + 0.25
                if not self.start_reload(now):
                    pass
            return False
        return True

    def shot_offsets(self, ctx: AimContext) -> list[tuple[float, float]]:
        """Angular offsets (deg, x right / y up) of each pellet of the next shot,
        relative to where the player aims: recoil pattern + random spread."""
        rx, ry = self.recoil_offset(math.floor(self.recoil_index))
        inacc = self.inaccuracy(ctx)
        out = []
        theta = self.rng.uniform(0, 2 * math.pi)
        r = inacc * self.rng.random()
        sx, sy = r * math.cos(theta), r * math.sin(theta)
        for _ in range(max(self.d.pellets, 1)):
            px = py = 0.0
            if self.d.pellets > 1:
                pt = self.rng.uniform(0, 2 * math.pi)
                pr = self.d.pellet_spread * math.sqrt(self.rng.random())
                px, py = pr * math.cos(pt), pr * math.sin(pt)
            out.append((rx + sx + px, ry + sy + py))
        return out

    def on_fired(self, now: float) -> None:
        if self.d.magazine > 0:
            self.ammo -= 1
        self.shot_count += 1
        # schedule from the previous due time so the cadence matches the RPM exactly
        due = self.next_fire if now - self.next_fire < self.d.fire_interval else now
        self.next_fire = due + self.d.fire_interval
        self.last_shot = now
        last = len(self.d.recoil.pattern) - 1
        self.recoil_index = min(math.floor(self.recoil_index) + 1, last)
        self.fire_inacc += self.d.inaccuracy.fire_add
        self.events.append(WeaponEvent("fire", now, {"shot": self.shot_count}))
        if self.d.fire_mode in ("bolt", "pump") and (self.ammo > 0 or self.d.fire_mode == "pump"):
            self.events.append(WeaponEvent("cycle", now))
        if self.d.scope and self.d.scope.get("unscope_after_shot"):
            self.zoom_level = 0

    def is_tracer(self) -> bool:
        n = self.d.tracer_every
        return n > 0 and self.shot_count % n == 0

    def pop_events(self) -> list[WeaponEvent]:
        ev, self.events = self.events, []
        return ev


def angles_to_dir(yaw_deg: float, pitch_deg: float):
    """Panda3D heading/pitch (degrees) -> unit forward vector (x, y, z)."""
    h = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    cp = math.cos(p)
    return (-math.sin(h) * cp, math.cos(h) * cp, math.sin(p))


def apply_offset(yaw: float, pitch: float, dx: float, dy: float) -> tuple[float, float]:
    """Offset view angles by (dx right, dy up) degrees."""
    return yaw - dx, max(-89.9, min(89.9, pitch + dy))
