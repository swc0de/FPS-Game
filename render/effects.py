"""Gameplay visual effects: impacts, muzzle flashes, tracers, casings,
explosions, smoke clouds, temporary lights and camera shake."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np
from panda3d.core import NodePath, Point3, Vec3

from engine.geometry import MeshBuilder
from engine.physics import MASK_SIGHT
from render import particles as P
from render.decals import DecalSystem
from render.local_lights import LocalLight

R = random.uniform


def _cone(n: int, axis, spread: float, speed: tuple) -> np.ndarray:
    """n random vectors within a cone around axis (spread in radians)."""
    a = np.asarray(axis, np.float32)
    a = a / (np.linalg.norm(a) + 1e-9)
    up = np.array([0, 0, 1.0]) if abs(a[2]) < 0.9 else np.array([0, 1.0, 0])
    t = np.cross(up, a)
    t /= np.linalg.norm(t)
    b = np.cross(a, t)
    phi = np.random.uniform(0, 2 * math.pi, n)
    th = np.random.uniform(0, spread, n)
    d = (np.cos(th)[:, None] * a + np.sin(th)[:, None] * (np.cos(phi)[:, None] * t + np.sin(phi)[:, None] * b))
    s = np.random.uniform(speed[0], speed[1], n)[:, None]
    return (d * s).astype(np.float32)


def _jitter(n: int, center, radius: float) -> np.ndarray:
    return np.asarray(center, np.float32) + np.random.uniform(-radius, radius, (n, 3)).astype(np.float32)


# surface impact presets: dust colour, debris frame/colour, sparks
IMPACTS = {
    "dust_grey":  {"dust": (0.55, 0.54, 0.52), "chips": (0.42, 0.41, 0.4), "chip_frame": P.F_CHUNK, "n_dust": 5, "n_chips": 8},
    "dust_red":   {"dust": (0.55, 0.38, 0.32), "chips": (0.45, 0.25, 0.2), "chip_frame": P.F_CHUNK, "n_dust": 5, "n_chips": 8},
    "dust_white": {"dust": (0.85, 0.83, 0.8), "chips": (0.8, 0.78, 0.74), "chip_frame": P.F_CHUNK, "n_dust": 6, "n_chips": 6},
    "dust_tan":   {"dust": (0.6, 0.52, 0.4), "chips": (0.5, 0.42, 0.3), "chip_frame": P.F_DUST, "n_dust": 6, "n_chips": 4},
    "dirt":       {"dust": (0.38, 0.31, 0.24), "chips": (0.3, 0.24, 0.18), "chip_frame": P.F_CHUNK, "n_dust": 6, "n_chips": 10},
    "splinters":  {"dust": (0.6, 0.48, 0.34), "chips": (0.7, 0.55, 0.36), "chip_frame": P.F_SPLINTER, "n_dust": 3, "n_chips": 9},
    "sparks":     {"dust": (0.45, 0.45, 0.45), "chips": (0.3, 0.3, 0.3), "chip_frame": P.F_CHUNK, "n_dust": 2, "n_chips": 2, "sparks": 12},
    "glass":      {"dust": (0.8, 0.85, 0.85), "chips": (0.8, 0.9, 0.9), "chip_frame": P.F_SHARD, "n_dust": 1, "n_chips": 10},
    "blood":      {"blood": True},
    "dummy":      {"dust": (0.75, 0.5, 0.25), "chips": (0.7, 0.45, 0.2), "chip_frame": P.F_CHUNK, "n_dust": 3, "n_chips": 6},
}


@dataclass
class SmokeCloud:
    pos: Point3
    radius: float
    age: float
    duration: float
    emitted: int = 0

    @property
    def density(self) -> float:
        """0..1 opacity for gameplay checks (AI vision)."""
        grow = min(self.age / 1.5, 1.0)
        fade = min(max((self.duration - self.age) / 3.0, 0.0), 1.0)
        return grow * fade


class Effects:
    def __init__(self, game):
        self.game = game
        self.atlas = P.make_atlas()
        self.alpha = P.ParticleSystem(game.render, self.atlas, 3000, additive=False, sort=20, name="fx_alpha")
        self.add = P.ParticleSystem(game.render, self.atlas, 2500, additive=True, sort=25, name="fx_add")
        from render.renderer import SHADOW_CAMERA_MASK
        for ps in (self.alpha, self.add):
            ps.np.hide(SHADOW_CAMERA_MASK)
        self.decals = DecalSystem(game, game.renderer.defines)
        self.surfaces = game.ballistics.surfaces if hasattr(game, "ballistics") else {}
        self.temp_lights: list[tuple[LocalLight, float, float, Vec3]] = []
        self.casings = ShellCasings(game)
        self.smokes: list[SmokeCloud] = []
        self.shake = 0.0
        self.vm_flash: P.ParticleSystem | None = None

    # ------------------------------------------------------------ helpers
    def attach_viewmodel(self, vm_root: NodePath) -> None:
        """Muzzle flashes for the viewmodel live in camera space."""
        self.vm_flash = P.ParticleSystem(vm_root, self.atlas, 64, additive=True, sort=30, name="vm_flash")
        self.vm_flash.np.setShaderInput("u_camPos", Vec3(0, 0, 0), 60)

    def light_pulse(self, pos, color, intensity: float, rng: float, duration: float) -> None:
        light = LocalLight(pos=Point3(*pos), color=Vec3(*color) * intensity, range=rng, shadows=False)
        self.game.renderer.lights.add(light)
        self.temp_lights.append((light, duration, duration, Vec3(*color) * intensity))

    def add_shake(self, amount: float) -> None:
        self.shake = min(self.shake + amount, 3.0)

    # ------------------------------------------------------------ impacts
    def impact(self, pos, normal, surface: str, direction, exit_hole: bool = False) -> None:
        sdef = self.surfaces.get(surface) or self.surfaces.get("default", {})
        style = sdef.get("decal", "concrete")
        effect = IMPACTS.get(sdef.get("effect", "dust_grey"), IMPACTS["dust_grey"])
        n = Vec3(*normal)
        if n.lengthSquared() < 1e-6:
            n = -Vec3(*direction)
        n.normalize()
        d = Vec3(*direction)
        d.normalize()
        refl = d - n * (2 * d.dot(n))
        p = Point3(*pos)
        if style != "none":
            if style == "concrete" and random.random() < 0.5:
                style = "concrete2"
            if style == "metal" and random.random() < 0.5:
                style = "metal2"
            self.decals.add(p, n, style)
        if effect.get("blood"):
            self.blood(p, d)
            return
        out = n if not exit_hole else d
        nd = effect.get("n_dust", 4)
        if nd:
            c = effect["dust"]
            self.alpha.emit(nd, _jitter(nd, p + out * 0.03, 0.02), _cone(nd, out, 0.6, (0.4, 1.6)),
                            np.random.uniform(0.8, 1.6, nd), np.random.uniform(0.05, 0.1, nd),
                            np.random.uniform(0.35, 0.7, nd), (c[0], c[1], c[2], 0.75), (c[0], c[1], c[2], 0.0),
                            rotv=np.random.uniform(-1, 1, nd), drag=3.0, grav=-0.15, frame=P.F_SMOKE1, lit=1.0)
        nc = effect.get("n_chips", 6)
        if nc:
            c = effect["chips"]
            axis = (out + refl * 0.5) if not exit_hole else d
            self.alpha.emit(nc, _jitter(nc, p + out * 0.02, 0.01), _cone(nc, axis, 0.9, (2.0, 6.0)),
                            np.random.uniform(0.35, 0.8, nc), np.random.uniform(0.008, 0.02, nc), 0.01,
                            (c[0], c[1], c[2], 1.0), rotv=np.random.uniform(-15, 15, nc), drag=0.6, grav=9.8,
                            frame=effect.get("chip_frame", P.F_CHUNK), lit=1.0)
        ns = effect.get("sparks", 0)
        if ns:
            ns = int(ns * random.uniform(0.6, 1.3))
            self.add.emit(ns, _jitter(ns, p + n * 0.01, 0.005), _cone(ns, refl * 0.6 + n * 0.4, 0.7, (3.0, 9.0)),
                          np.random.uniform(0.12, 0.35, ns), 0.012, 0.004, (9.0, 5.0, 1.8, 1.0), (4.0, 1.2, 0.3, 0.0),
                          drag=1.5, grav=7.0, stretch=0.03, frame=P.F_SPARK)
            self.add.emit(1, p + n * 0.02, (0, 0, 0), 0.06, 0.12, 0.2, (5.0, 3.0, 1.2, 1.0), (2.0, 1.0, 0.3, 0.0),
                          frame=P.F_GLOW)

    def blood(self, p: Point3, d: Vec3) -> None:
        n = 6
        self.alpha.emit(n, _jitter(n, p, 0.03), _cone(n, d, 0.7, (0.4, 2.0)), np.random.uniform(0.25, 0.5, n),
                        np.random.uniform(0.06, 0.12, n), np.random.uniform(0.25, 0.45, n), (0.35, 0.02, 0.02, 0.85),
                        (0.25, 0.01, 0.01, 0.0), drag=4.0, grav=1.0, frame=P.F_MIST, lit=1.0)
        hit = self.game.physics.ray_cast(p, p + d * 2.0, MASK_SIGHT)
        if hit is not None:
            self.decals.add(hit.pos, hit.normal, "blood", size=random.uniform(0.35, 0.7))

    # --------------------------------------------------------- weapon fx
    def muzzle_flash(self, vm_muzzle_cam_space: Point3 | None, world_pos: Point3, direction: Vec3,
                     scale: float = 1.0, light: bool = True) -> None:
        if self.vm_flash is not None and vm_muzzle_cam_space is not None:
            q = vm_muzzle_cam_space
            fwd = Vec3(0, 1, 0)
            s = scale * random.uniform(0.85, 1.15)
            self.vm_flash.emit(1, q + fwd * 0.02, (0, 0, 0), 0.05, 0.07 * s, 0.09 * s, (8, 5.5, 2.5, 1.0),
                               (4, 2, 0.6, 0.0), rot=random.uniform(0, 6.28), frame=P.F_STAR)
            self.vm_flash.emit(2, [q + fwd * 0.06, q + fwd * 0.1], [fwd * 0.6, fwd * 0.9], 0.04,
                               0.035 * s, 0.05 * s, (6, 3.5, 1.4, 0.9), (3, 1.2, 0.3, 0.0), stretch=0.09,
                               frame=P.F_FLASH_SIDE)
            self.vm_flash.emit(1, q, (0, 0, 0), 0.05, 0.12 * s, 0.16 * s, (1.5, 0.9, 0.4, 0.6),
                               (0.5, 0.2, 0.05, 0.0), frame=P.F_GLOW)
        if light:
            self.light_pulse(world_pos + direction * 0.3, (1.0, 0.72, 0.4), 18.0 * scale, 6.0, 0.06)
        n = 2
        self.alpha.emit(n, _jitter(n, world_pos + direction * 0.1, 0.02), _cone(n, direction, 0.4, (0.3, 0.8)),
                        np.random.uniform(0.6, 1.0, n), 0.03, 0.18, (0.7, 0.7, 0.7, 0.25), (0.7, 0.7, 0.7, 0.0),
                        drag=2.0, grav=-0.4, frame=P.F_SMOKE2, lit=1.0)

    def tracer(self, start: Point3, end: Point3, speed: float = 420.0) -> None:
        d = end - start
        dist = d.length()
        if dist < 1.5:
            return
        d /= dist
        life = dist / speed
        self.add.emit(1, start + d * 1.2, d * speed, life, 0.012, 0.012, (14.0, 9.0, 3.5, 1.0),
                      (12.0, 7.0, 2.5, 0.9), stretch=0.0045, frame=P.F_STREAK)

    def eject_shell(self, kind: str, pos: Point3, side: Vec3, inherit: Vec3) -> None:
        self.casings.spawn(kind, pos, side, inherit)

    # --------------------------------------------------------- grenades fx
    def explosion(self, pos: Point3, radius: float) -> None:
        p = Point3(*pos)
        self.light_pulse(p + Vec3(0, 0, 0.6), (1.0, 0.6, 0.3), 260.0, radius * 2.2, 0.4)
        n = 18
        self.add.emit(n, _jitter(n, p + Vec3(0, 0, 0.3), 0.3), _cone(n, (0, 0, 1), 1.4, (1.0, 6.0)),
                      np.random.uniform(0.25, 0.55, n), np.random.uniform(0.4, 0.8, n), np.random.uniform(1.4, 2.6, n),
                      (9.0, 4.0, 1.2, 1.0), (2.0, 0.5, 0.1, 0.0), rotv=np.random.uniform(-2, 2, n), drag=3.0,
                      grav=-1.0, frame=P.F_FIRE)
        n = 26
        self.add.emit(n, _jitter(n, p + Vec3(0, 0, 0.2), 0.1), _cone(n, (0, 0, 1), 1.5, (6.0, 18.0)),
                      np.random.uniform(0.3, 0.9, n), 0.02, 0.01, (12.0, 6.0, 2.0, 1.0), (5.0, 1.5, 0.3, 0.0),
                      drag=1.2, grav=9.0, stretch=0.02, frame=P.F_SPARK)
        n = 24
        self.alpha.emit(n, _jitter(n, p + Vec3(0, 0, 0.5), 0.6), _cone(n, (0, 0, 1), 1.3, (0.5, 3.5)),
                        np.random.uniform(2.5, 5.0, n), np.random.uniform(0.8, 1.4, n), np.random.uniform(3.0, 5.0, n),
                        (0.2, 0.19, 0.18, 0.85), (0.32, 0.3, 0.28, 0.0), rotv=np.random.uniform(-0.5, 0.5, n),
                        drag=1.8, grav=-0.35, frame=P.F_SMOKE3, lit=1.0)
        n = 22
        self.alpha.emit(n, _jitter(n, p + Vec3(0, 0, 0.2), 0.2), _cone(n, (0, 0, 1), 1.2, (4.0, 12.0)),
                        np.random.uniform(0.8, 1.6, n), np.random.uniform(0.02, 0.05, n), 0.02,
                        (0.25, 0.22, 0.2, 1.0), rotv=np.random.uniform(-15, 15, n), drag=0.3, grav=9.8,
                        frame=P.F_CHUNK, lit=1.0)
        hit = self.game.physics.ray_cast(p + Vec3(0, 0, 0.5), p - Vec3(0, 0, 2.0), MASK_SIGHT)
        if hit is not None:
            self.decals.add(hit.pos, hit.normal, "scorch", size=radius * 0.45)
        cam = self.game.camera.getPos(self.game.render)
        dist = (cam - p).length()
        self.add_shake(max(0.0, 2.2 * (1.0 - dist / (radius * 3.0))))

    def flashbang(self, pos: Point3) -> None:
        p = Point3(*pos)
        self.light_pulse(p, (1.0, 0.98, 0.95), 1600.0, 28.0, 0.15)
        self.add.emit(1, p, (0, 0, 0), 0.12, 0.8, 2.5, (60, 60, 55, 1.0), (20, 20, 18, 0.0), frame=P.F_GLOW)
        n = 16
        self.add.emit(n, _jitter(n, p, 0.05), _cone(n, (0, 0, 1), 1.6, (3.0, 10.0)), np.random.uniform(0.2, 0.5, n),
                      0.015, 0.005, (20, 18, 14, 1.0), (6, 5, 3, 0.0), drag=1.0, grav=8.0, stretch=0.02,
                      frame=P.F_SPARK)
        n = 6
        self.alpha.emit(n, _jitter(n, p, 0.2), _cone(n, (0, 0, 1), 1.4, (0.2, 1.0)), np.random.uniform(1.5, 2.5, n),
                        0.3, 1.0, (0.8, 0.8, 0.8, 0.5), (0.8, 0.8, 0.8, 0.0), drag=2.0, grav=-0.2,
                        frame=P.F_SMOKE1, lit=1.0)

    def smoke_grenade(self, pos: Point3, radius: float, duration: float) -> SmokeCloud:
        cloud = SmokeCloud(Point3(*pos), radius, 0.0, duration)
        self.smokes.append(cloud)
        return cloud

    def _update_smokes(self, dt: float) -> None:
        keep = []
        for c in self.smokes:
            c.age += dt
            # emit puffs during the first ~2 s, sized so the cloud fills the radius
            target = int(min(c.age / 2.0, 1.0) * 70)
            new = target - c.emitted
            if new > 0:
                remaining = max(c.duration - c.age, 1.0)
                pos = []
                vel = []
                for _ in range(new):
                    th = random.uniform(0, 2 * math.pi)
                    rr = c.radius * math.sqrt(random.random()) * 0.85
                    h = random.uniform(0.3, 2.6)
                    pos.append((c.pos.x + math.cos(th) * rr * 0.3, c.pos.y + math.sin(th) * rr * 0.3, c.pos.z + 0.2))
                    vel.append((math.cos(th) * rr * 0.9, math.sin(th) * rr * 0.9, h * 0.9))
                life = np.random.uniform(remaining - 1.5, remaining + 1.0, new).clip(2.0, None)
                self.alpha.emit(new, pos, vel, life, np.random.uniform(0.8, 1.2, new),
                                np.random.uniform(2.4, 3.2, new), (0.78, 0.78, 0.76, 0.92), (0.82, 0.82, 0.8, 0.0),
                                rotv=np.random.uniform(-0.15, 0.15, new), drag=1.3, grav=0.0,
                                frame=np.random.choice([P.F_SMOKE1, P.F_SMOKE2, P.F_SMOKE3], new), lit=1.0)
                c.emitted = target
            if c.age < c.duration:
                keep.append(c)
        self.smokes = keep

    def smoke_between(self, a: Point3, b: Point3) -> float:
        """Total smoke opacity along a segment (for AI/flash LOS checks)."""
        total = 0.0
        d = b - a
        L = d.length()
        if L < 1e-3:
            return 0.0
        d /= L
        for c in self.smokes:
            t = max(0.0, min(L, (c.pos - a).dot(d)))
            closest = a + d * t
            if (closest - c.pos).length() < c.radius:
                total += c.density
        return min(total, 1.0)

    # ------------------------------------------------------------- update
    def update(self, dt: float) -> None:
        cam = self.game.camera.getPos(self.game.render)
        self._update_smokes(dt)
        self.alpha.update(dt, cam)
        self.add.update(dt, cam)
        if self.vm_flash is not None:
            self.vm_flash.update(dt, None)
        keep = []
        for light, left, total, col in self.temp_lights:
            left -= dt
            if left <= 0:
                self.game.renderer.lights.remove(light)
                continue
            k = left / total
            light.color = col * (k * k)
            keep.append((light, left, total, col))
        self.temp_lights = keep
        self.casings.update(dt)
        self.shake = max(self.shake - dt * 2.5, 0.0)


# ----------------------------------------------------------------- casings
SHELL_SPECS = {
    "pistol": (0.0045, 0.019, "brass"),
    "rifle": (0.0052, 0.039, "brass"),
    "magnum": (0.0068, 0.06, "brass"),
    "shell12": (0.0105, 0.06, "pbr_red_plastic"),
}


class ShellCasings:
    """Ejected cartridge cases: tiny meshes with cheap ballistic motion that
    bounce on the floor found by one ray cast at spawn."""

    MAX = 28

    def __init__(self, game):
        self.game = game
        self.root = game.render.attachNewNode("casings")
        from render.renderer import SHADOW_CAMERA_MASK
        self.root.hide(SHADOW_CAMERA_MASK)
        self.templates: dict[str, NodePath] = {}
        self.items: list[dict] = []

    def _template(self, kind: str) -> NodePath:
        t = self.templates.get(kind)
        if t is None:
            r, length, mat = SHELL_SPECS.get(kind, SHELL_SPECS["rifle"])
            mb = MeshBuilder()
            mb.add_cylinder((0, 0, 0), r, length, segments=10, hpr=(0, -90, 0))
            t = NodePath(mb.build(f"casing_{kind}"))
            self.game.materials.get(mat).apply(t)
            if kind == "shell12":
                mb2 = MeshBuilder()
                mb2.add_cylinder((0, -length * 0.42, 0), r * 1.05, length * 0.18, segments=10, hpr=(0, -90, 0))
                b = t.attachNewNode(mb2.build("base"))
                self.game.materials.get("brass").apply(b)
            self.templates[kind] = t
        return t

    def spawn(self, kind: str, pos: Point3, side: Vec3, inherit: Vec3) -> None:
        if len(self.items) >= self.MAX:
            old = self.items.pop(0)
            old["np"].removeNode()
        np_ = self._template(kind).instanceTo(self.root)
        np_.setPos(pos)
        np_.setHpr(R(0, 360), R(0, 360), R(0, 360))
        vel = side * R(1.6, 2.6) + Vec3(0, 0, R(0.8, 1.6)) + inherit
        hit = self.game.physics.ray_cast(pos, pos - Vec3(0, 0, 3.0), MASK_SIGHT)
        floor = hit.pos.z if hit is not None else pos.z - 3.0
        self.items.append({"np": np_, "vel": vel, "spin": Vec3(R(-900, 900), R(-900, 900), R(-600, 600)),
                           "floor": floor, "age": 0.0, "bounces": 0})

    def update(self, dt: float) -> None:
        keep = []
        for it in self.items:
            it["age"] += dt
            np_ = it["np"]
            if it["age"] > 6.0:
                np_.removeNode()
                continue
            if it["bounces"] < 3:
                v = it["vel"]
                v.z -= 9.8 * dt
                p = np_.getPos() + v * dt
                if p.z <= it["floor"] + 0.006:
                    p.z = it["floor"] + 0.006
                    if abs(v.z) > 0.6:
                        v.z = -v.z * 0.35
                        v.x *= 0.5
                        v.y *= 0.5
                        it["spin"] *= 0.5
                        it["bounces"] += 1
                    else:
                        it["bounces"] = 3
                        np_.setP(0)
                        np_.setR(90)
                np_.setPos(p)
                if it["bounces"] < 3:
                    np_.setHpr(np_.getHpr() + it["spin"] * dt)
            keep.append(it)
        self.items = keep
