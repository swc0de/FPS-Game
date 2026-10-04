"""Kinematic character controller built on Bullet convex sweeps.

Panda3D's stock ``BulletCharacterControllerNode`` is known to jitter on
stairs and cannot crouch cleanly, so characters (player and bots) use a
custom *collide-and-slide* controller instead:

* The body is a capsule. ``pos`` is the point between the feet.
* Each tick the desired motion is swept through the Bullet world with
  ``sweepTestClosest``. On a hit we move up to the contact (minus a small
  skin), clip the velocity against the contact plane and continue with the
  remaining time (max 4 bumps; creases between two planes slide along
  their intersection line).
* Stairs: if horizontal motion is blocked while grounded, the move is
  retried from ``step_height`` higher and the result that travels farthest
  horizontally wins (Quake-style step-slide).
* Grounding: a short downward sweep finds walkable ground (normal within
  ``max_slope``); while grounded the probe extends to ``step_height`` so
  walking down stairs/slopes snaps instead of "skipping".
* Crouching shrinks the capsule. Standing back up sweeps upward to check
  headroom. Crouching in the air tucks the legs up (crouch-jump).
* Ground movement uses Source-engine style friction/acceleration so it
  feels familiar to CS players (counter-strafing, walk/crouch accuracy).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Callable

from panda3d.bullet import BulletBoxShape, BulletCapsuleShape, BulletGhostNode, ZUp
from panda3d.core import NodePath, Point3, Vec3

from engine import paths
from engine.physics import GRAVITY, MASK_MOVEMENT, PhysicsWorld, surface_of

SKIN = 0.012          # gap kept between the capsule and surfaces
MAX_BUMPS = 4
MAX_DEPEN_STEP = 0.1  # metres per depenetration iteration
# A capsule resting on an edge only counts as supported if the contact is
# reasonably far under it (normal.z > this), otherwise we would "climb"
# tall ledges by hanging on the side of the capsule.
EDGE_SUPPORT_NZ = 0.35


def load_movement_config() -> dict:
    with open(paths.DATA_DIR / "movement.json", "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class MoveInput:
    """Per-tick intent. ``wish_dir`` is a world-space horizontal unit vector."""
    wish_dir: Vec3 = field(default_factory=Vec3)
    walk: bool = False
    crouch: bool = False
    jump: bool = False
    speed_scale: float = 1.0   # weapon weight / gadget modifiers


@dataclass
class StepEvent:
    kind: str            # "footstep" | "land" | "jump"
    pos: Point3
    surface: str
    loudness: float      # 0 = silent, 1 = full volume
    speed: float = 0.0


class KinematicCharacter:
    def __init__(self, physics: PhysicsWorld, config: dict | None = None,
                 mask=MASK_MOVEMENT):
        self.physics = physics
        self.world = physics.world
        self.cfg = config or load_movement_config()
        c = self.cfg
        self.radius = c["radius"]
        self.hull = c.get("hull", "box")
        self.stand_height = c["stand_height"]
        self.crouch_height = c["crouch_height"]
        self.step_height = c["step_height"]
        self.min_walk_normal = math.cos(math.radians(c["max_slope_deg"]))
        self.mask = mask

        self.pos = Point3(0, 0, 0)
        self.vel = Vec3(0, 0, 0)
        self.height = self.stand_height
        self.prev_pos = Point3(self.pos)
        self.prev_height = self.height

        self.on_ground = False
        self.ground_normal = Vec3(0, 0, 1)
        self.ground_surface = "default"
        self.crouched = False          # true while the capsule is below standing height
        self.walking = False
        self.jump_cooldown = 0.0
        self.landing_timer = 0.0
        self.air_time = 0.0
        self.last_fall_speed = 0.0
        self._stride = 0.0
        self.on_event: Callable[[StepEvent], None] | None = None

        self._last_hit_pos = Point3()
        self.depen_interval = 1                # bots: check for overlaps every N ticks only
        self._depen_k = 0
        self._shapes: dict[int, object] = {}
        self._ghosts: dict[int, NodePath] = {}

    # ------------------------------------------------------------ shapes
    def _key(self, height: float) -> int:
        return int(round(height * 100))  # centimetre buckets

    def _shape(self, height: float):
        key = self._key(height)
        shape = self._shapes.get(key)
        if shape is None:
            if self.hull == "capsule":
                cyl = max(key / 100.0 - 2 * self.radius, 0.01)
                shape = BulletCapsuleShape(self.radius, cyl, ZUp)
            else:
                # World-aligned box hull (never rotates with view yaw), like
                # Source/CS: flat bottom gives crisp stairs and ledge standing.
                shape = BulletBoxShape(Vec3(self.radius, self.radius, key / 200.0))
            self._shapes[key] = shape
        return shape

    def _ghost(self, height: float) -> NodePath:
        key = self._key(height)
        g = self._ghosts.get(key)
        if g is None:
            node = BulletGhostNode("char_probe")
            node.addShape(self._shape(height))
            g = NodePath(node)
            self._ghosts[key] = g
        return g

    def _center(self, feet: Point3, height: float | None = None) -> Point3:
        h = self.height if height is None else height
        return Point3(feet.x, feet.y, feet.z + self._key(h) / 200.0)

    @property
    def eye_height(self) -> float:
        return self.height - self.cfg["eye_offset"]

    def teleport(self, pos) -> None:
        self.pos = Point3(*pos)
        self.prev_pos = Point3(self.pos)
        self.vel = Vec3(0, 0, 0)
        self.on_ground = False
        self._depenetrate()
        self._ground_check(0.5)

    # ------------------------------------------------------------ traces
    def _trace(self, start: Point3, end: Point3, height: float | None = None):
        """Sweep the capsule from feet position ``start`` to ``end``.

        Returns ``(fraction, normal, node)`` or None if the path is clear.
        """
        if (end - start).lengthSquared() < 1e-12:
            return None
        h = self.height if height is None else height
        res = self.physics.sweep(self._shape(h), self._center(start, h), self._center(end, h), self.mask)
        if not res.hasHit():
            return None
        n = Vec3(res.getHitNormal())
        if n.lengthSquared() < 1e-8:
            n = -(end - start).normalized()
        else:
            n.normalize()
        self._last_hit_pos = Point3(res.getHitPos())
        return res.getHitFraction(), n, res.getNode()

    def _surface_normal_below(self, hit_pos: Point3, feet: Point3):
        """Capsule sweeps report rounded normals on edges/corners; ray-cast the
        actual face under the contact point to decide if it is walkable."""
        # step slightly past the contact, away from the capsule axis: that is
        # where the supporting face is (step top / ledge we are hanging over)
        away = Vec3(hit_pos.x - feet.x, hit_pos.y - feet.y, 0)
        if away.lengthSquared() > 1e-8:
            away.normalize()
        probe = Point3(hit_pos.x, hit_pos.y, hit_pos.z) + away * 0.03
        res = self.world.rayTestClosest(probe + Vec3(0, 0, 0.08), probe - Vec3(0, 0, 0.12), self.mask)
        if res.hasHit():
            n = Vec3(res.getHitNormal())
            if n.lengthSquared() > 1e-8:
                n.normalize()
                return n, res.getNode()
        return None

    def _depenetrate(self, iterations: int = 3) -> bool:
        """Push the capsule out of any geometry it overlaps (spawns, crouch bugs).

        A box-box contact reports up to four manifold points with the same
        depth, so only the deepest point per object counts (summing them
        would push four times too far), and each step is capped so a deep
        overlap can never shove the character through a thin floor."""
        moved = False
        for _ in range(iterations):
            ghost = self._ghost(self.height)
            ghost.setPos(self._center(self.pos))
            result = self.world.contactTest(ghost.node(), False)
            deepest: dict[int, tuple[float, Vec3]] = {}
            for contact in result.getContacts():
                other = contact.getNode1()
                if other is ghost.node():
                    other = contact.getNode0()
                    sign = -1.0
                else:
                    sign = 1.0
                if not (other.getIntoCollideMask() & self.mask).getWord():
                    continue
                mp = contact.getManifoldPoint()
                dist = mp.getDistance()
                if dist < -1e-4:
                    key = other.this
                    if key not in deepest or dist < deepest[key][0]:
                        deepest[key] = (dist, Vec3(mp.getNormalWorldOnB()) * sign)
            push = Vec3(0, 0, 0)
            for dist, n in deepest.values():
                push += n * (-dist + SKIN * 0.5)
            if self.on_ground and push.z < 0.0:
                push.z = 0.0          # head in a ceiling: never push a grounded character into the floor
            if push.lengthSquared() < 1e-10:
                break
            if push.length() > MAX_DEPEN_STEP:
                push *= MAX_DEPEN_STEP / push.length()
            self.pos += push
            moved = True
        return moved

    # ----------------------------------------------------------- physics
    def _friction(self, dt: float) -> None:
        speed = math.hypot(self.vel.x, self.vel.y)
        if speed < 1e-4:
            self.vel.x = self.vel.y = 0.0
            return
        control = max(speed, self.cfg["stop_speed"])
        drop = control * self.cfg["ground_friction"] * dt
        scale = max(speed - drop, 0.0) / speed
        self.vel.x *= scale
        self.vel.y *= scale

    def _accelerate(self, wish_dir: Vec3, wish_speed: float, accel: float, dt: float,
                    cap: float | None = None) -> None:
        if wish_speed <= 0:
            return
        current = self.vel.dot(wish_dir)
        limit = wish_speed if cap is None else min(wish_speed, cap)
        add = limit - current
        if add <= 0:
            return
        accel_speed = min(accel * dt * wish_speed, add)
        self.vel += wish_dir * accel_speed

    @staticmethod
    def _clip(vel: Vec3, normal: Vec3, overbounce: float = 1.001) -> Vec3:
        backoff = vel.dot(normal)
        if backoff < 0:
            backoff *= overbounce
        else:
            backoff /= overbounce
        return vel - normal * backoff

    def _slide_move(self, pos: Point3, vel: Vec3, dt: float):
        """Collide-and-slide. Returns (new_pos, new_vel, blocked_by_wall)."""
        pos = Point3(pos)
        vel = Vec3(vel)
        primal = Vec3(vel)
        planes: list[Vec3] = []
        time_left = dt
        blocked = False
        for _ in range(MAX_BUMPS):
            delta = vel * time_left
            dist = delta.length()
            if dist < 1e-6:
                break
            hit = self._trace(pos, pos + delta)
            if hit is None:
                pos += delta
                break
            frac, n, _node = hit
            # move to the contact, then back off SKIN along the contact normal
            # (backing off along the motion direction stalls grazing slides)
            pos += delta * frac + n * SKIN
            time_left *= (1.0 - frac)
            direction = delta / dist

            if n.z < self.min_walk_normal:
                blocked = True
                if self.on_ground and n.z > -0.3:
                    # steep surfaces act as vertical walls while walking
                    n = Vec3(n.x, n.y, 0)
                    if n.lengthSquared() < 1e-6:
                        n = -direction
                    n.normalize()
            planes.append(n)
            vel = self._clip(vel, n)
            for other in planes[:-1]:
                if vel.dot(other) < -1e-5:
                    crease = other.cross(n)
                    if crease.lengthSquared() < 1e-8:
                        vel = Vec3(0, 0, 0)
                    else:
                        crease.normalize()
                        vel = crease * crease.dot(vel)
                    break
            if vel.dot(primal) <= 0:
                vel = Vec3(0, 0, 0)
                break
        return pos, vel, blocked

    def _step_slide_move(self, dt: float) -> None:
        start_pos = Point3(self.pos)
        start_vel = Vec3(self.vel)
        down_pos, down_vel, blocked = self._slide_move(start_pos, start_vel, dt)
        if not (blocked and self.on_ground):
            self.pos, self.vel = down_pos, down_vel
            return
        # Try the move again from step_height above.
        up_target = start_pos + Vec3(0, 0, self.step_height)
        hit = self._trace(start_pos, up_target)
        up_pos = Point3(start_pos)
        if hit is None:
            up_pos = up_target
        else:
            up_pos.z += max(self.step_height * hit[0] - SKIN, 0.0)
        if up_pos.z - start_pos.z < 0.05:
            self.pos, self.vel = down_pos, down_vel
            return
        step_pos, step_vel, _ = self._slide_move(up_pos, start_vel, dt)
        # Push back down onto the step.
        drop = (up_pos.z - start_pos.z) + 0.05
        hit = self._trace(step_pos, step_pos - Vec3(0, 0, drop))
        if hit is not None:
            if hit[1].z < self.min_walk_normal:
                real = None
                if hit[1].z > EDGE_SUPPORT_NZ:
                    real = self._surface_normal_below(self._last_hit_pos, step_pos)
                if real is None or real[0].z < self.min_walk_normal:
                    self.pos, self.vel = down_pos, down_vel
                    return
            step_pos.z -= max(drop * hit[0] - SKIN, 0.0)
        else:
            step_pos.z -= drop
        d_down = (down_pos.xy - start_pos.xy).lengthSquared()
        d_step = (step_pos.xy - start_pos.xy).lengthSquared()
        if d_step > d_down + 1e-6:
            self.pos = step_pos
            self.vel = Vec3(step_vel.x, step_vel.y, 0.0)
        else:
            self.pos, self.vel = down_pos, down_vel

    def _ground_check(self, probe: float, jumped: bool = False) -> None:
        was_on_ground = self.on_ground
        if jumped or (not was_on_ground and self.vel.z > 1.0):
            self.on_ground = False
            return
        hit = self._trace(self.pos, self.pos - Vec3(0, 0, probe))
        if hit is not None and EDGE_SUPPORT_NZ < hit[1].z < 0.999:
            # Rounded capsule normals on edges are not the floor's normal;
            # ray-cast the face we are actually standing on.
            real = self._surface_normal_below(self._last_hit_pos, self.pos)
            if real is not None and real[0].z >= self.min_walk_normal:
                hit = (hit[0], real[0], real[1])
        if hit is not None and hit[1].z >= self.min_walk_normal:
            frac, n, node = hit
            self.pos.z -= max(probe * frac - SKIN, 0.0)
            self.on_ground = True
            self.ground_normal = n
            self.ground_surface = surface_of(node)
            self.vel.z = 0.0
        else:
            self.on_ground = False
        if self.on_ground and not was_on_ground:
            self._on_land()

    def _on_land(self) -> None:
        fall_speed = self.last_fall_speed
        if self.air_time > 0.12 and fall_speed > 2.5:
            self.landing_timer = self.cfg["landing_recover_time"] * min(fall_speed / 7.0, 1.4)
            loud = self.cfg["footsteps"]["land_loudness"] * min(fall_speed / 6.0, 1.0)
            if self.walking or self.crouched:
                loud *= 0.5
            self._emit("land", loud, fall_speed)
        self.air_time = 0.0

    def _update_crouch(self, dt: float, want_crouch: bool) -> None:
        target = self.crouch_height if want_crouch else self.stand_height
        if abs(self.height - target) < 1e-4:
            self.height = target
            self.crouched = self.height < self.stand_height - 1e-3
            return
        rate = (self.stand_height - self.crouch_height) / max(self.cfg["crouch_time"], 1e-3)
        step = rate * dt
        if target < self.height:
            new_h = max(self.height - step, target)
            shrink = self.height - new_h
            if not self.on_ground and self.cfg.get("crouch_jump_tuck", True):
                self.pos.z += shrink  # keep the head still: tuck legs up
            self.height = new_h
        else:
            grow = min(step, target - self.height)
            if self.on_ground:
                hit = self._trace(self.pos, self.pos + Vec3(0, 0, grow))
                allowed = grow if hit is None else max(grow * hit[0] - SKIN, 0.0)
                self.height += allowed
            else:
                # in the air: extend legs downward first
                hit = self._trace(self.pos, self.pos - Vec3(0, 0, grow))
                down = grow if hit is None else max(grow * hit[0] - SKIN, 0.0)
                self.pos.z -= down
                self.height += down
                rest = grow - down
                if rest > 1e-4:
                    hit = self._trace(self.pos, self.pos + Vec3(0, 0, rest))
                    up = rest if hit is None else max(rest * hit[0] - SKIN, 0.0)
                    self.height += up
        self.crouched = self.height < self.stand_height - 1e-3

    def _emit(self, kind: str, loudness: float, speed: float = 0.0) -> None:
        if self.on_event:
            self.on_event(StepEvent(kind, Point3(self.pos), self.ground_surface, loudness, speed))

    def _footsteps(self, dt: float) -> None:
        if not self.on_ground:
            return
        speed = math.hypot(self.vel.x, self.vel.y)
        if speed < 0.5:
            self._stride = min(self._stride, 0.35)
            return
        fs = self.cfg["footsteps"]
        if self.crouched:
            stride, loud = fs["crouch_stride"], fs["crouch_loudness"]
        elif self.walking:
            stride, loud = fs["walk_stride"], fs["walk_loudness"]
        else:
            stride, loud = fs["run_stride"], fs["run_loudness"]
        # running slowly (e.g. counter-strafing) is quieter
        loud *= min(speed / (self.cfg["run_speed"] * 0.8), 1.0) if loud > 0 else 0.0
        self._stride += speed * dt
        if self._stride >= stride:
            self._stride -= stride
            self._emit("footstep", loud, speed)

    # --------------------------------------------------------------- tick
    def max_speed(self, inp: MoveInput) -> float:
        c = self.cfg
        if self.crouched and self.on_ground:
            base = c["crouch_speed"]
        elif inp.walk:
            base = c["walk_speed"]
        else:
            base = c["run_speed"]
        if self.landing_timer > 0:
            t = self.landing_timer / c["landing_recover_time"]
            base *= 1.0 - (1.0 - c["landing_slowdown"]) * min(t, 1.0)
        return base * inp.speed_scale

    def step(self, dt: float, inp: MoveInput) -> None:
        self.prev_pos = Point3(self.pos)
        self.prev_height = self.height
        c = self.cfg
        self.walking = inp.walk
        self.jump_cooldown = max(self.jump_cooldown - dt, 0.0)
        self.landing_timer = max(self.landing_timer - dt, 0.0)

        self._depen_k += 1
        if self._depen_k >= self.depen_interval:
            self._depen_k = 0
            self._depenetrate(2)
        self._update_crouch(dt, inp.crouch)

        jumped = False
        if inp.jump and self.on_ground and self.jump_cooldown <= 0.0:
            self.vel.z = c["jump_speed"]
            self.on_ground = False
            self.jump_cooldown = c["jump_cooldown"] + 0.3
            jumped = True
            loud = 0.0 if (inp.walk or self.crouched) else c["footsteps"]["run_loudness"] * 0.6
            self._emit("jump", loud)

        wish_speed = self.max_speed(inp)
        if self.on_ground:
            self.vel.z = 0.0
            self._friction(dt)
            self._accelerate(inp.wish_dir, wish_speed, c["ground_accel"], dt)
            # hard cap on ground speed (no bunny-hop speed gain)
            horiz = math.hypot(self.vel.x, self.vel.y)
            limit = c["run_speed"] * inp.speed_scale
            if horiz > limit:
                s = limit / horiz
                self.vel.x *= s
                self.vel.y *= s
            # Move along the ground plane, preserving horizontal speed so
            # slopes do not bleed velocity every tick.
            n = self.ground_normal
            if n.z > 0.01:
                self.vel.z = -(n.x * self.vel.x + n.y * self.vel.y) / n.z
            self._step_slide_move(dt)
        else:
            self._accelerate(inp.wish_dir, wish_speed, c["air_accel"], dt, cap=c["air_wish_cap"])
            self.vel.z -= GRAVITY * dt * 0.5
            self.pos, self.vel, _ = self._slide_move(self.pos, self.vel, dt)
            self.vel.z -= GRAVITY * dt * 0.5
            self.air_time += dt
            self.last_fall_speed = max(-self.vel.z, 0.0)

        probe = self.step_height if (self.on_ground and not jumped) else 0.04
        self._ground_check(probe, jumped)
        if self.on_ground:
            self.air_time = 0.0
        self._footsteps(dt)

    # ---------------------------------------------------------- helpers
    def interpolated_pos(self, alpha: float) -> Point3:
        return self.prev_pos + (self.pos - self.prev_pos) * alpha

    def interpolated_height(self, alpha: float) -> float:
        return self.prev_height + (self.height - self.prev_height) * alpha

    @property
    def horizontal_speed(self) -> float:
        return math.hypot(self.vel.x, self.vel.y)
