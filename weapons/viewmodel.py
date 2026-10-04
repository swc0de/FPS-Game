"""First-person viewmodel: separate camera/FOV, procedural animation.

Rendering
---------
The weapon and arms hang under the main camera in the scene graph (so they
are lit by exactly the same sun/IBL/local lights/shadows as the player's
position) but are hidden from the world and shadow cameras. A second camera
with its own lens (the *viewmodel FOV*) renders only them into the same HDR
scene buffer after the world, with a depth clear, so the gun never clips
into walls and its perspective is independent of the player's FOV.

Animation layers (all procedural)
---------------------------------
  pose      hip offset <-> aim-down-sights (sight aligned to screen centre)
  draw      weapon rises into view on equip
  sway      lags behind mouse movement (spring) + idle breathing
  bob       figure-eight synced with footsteps, scaled by speed
  kick      per-shot recoil impulse (springs back)
  tracks    keyframed reload / bolt / pump / inspect / melee / throw
  parts     magazine, bolt, slide and pump groups move independently;
            the left hand follows the magazine during reloads
"""
from __future__ import annotations

import math
import random

from panda3d.core import Camera, PerspectiveLens, Point2, Point3, Vec3

from render.renderer import MAIN_CAMERA_MASK, SHADOW_CAMERA_MASK, VIEWMODEL_CAMERA_MASK
from weapons import anim_data
from weapons.models import build_arm, build_weapon_model


class Spring:
    """Critically damped spring toward zero for a 3-vector."""

    def __init__(self, stiffness: float):
        self.k = stiffness
        self.value = Vec3(0, 0, 0)
        self.vel = Vec3(0, 0, 0)

    def impulse(self, v: Vec3) -> None:
        self.vel += v

    def update(self, dt: float, target: Vec3 | None = None) -> Vec3:
        t = target if target is not None else Vec3(0, 0, 0)
        damp = 2.0 * math.sqrt(self.k)
        # sub-step for stability at low frame rates
        steps = max(1, int(dt / 0.008) + 1)
        h = dt / steps
        for _ in range(steps):
            acc = (t - self.value) * self.k - self.vel * damp
            self.vel += acc * h
            self.value += self.vel * h
        return self.value


class VMEntry:
    def __init__(self, model, arm_r, arm_l, meta):
        self.model = model
        self.arm_r = arm_r
        self.arm_l = arm_l
        self.meta = meta
        self.rest = {name: Point3(np_.getPos()) for name, np_ in model.groups.items()}
        self.arm_l_rest = Point3(arm_l.getPos()) if arm_l is not None else None
        self.muzzle = model.root.attachNewNode("muzzle")
        self.muzzle.setPos(model.anchor("muzzle"))
        self.eject = model.root.attachNewNode("eject")
        self.eject.setPos(model.anchor("eject"))


class Viewmodel:
    def __init__(self, game):
        self.game = game
        self.base_fov = float(game.settings.video.get("viewmodel_fov", 54.0))
        lens = PerspectiveLens()
        lens.setNearFar(0.01, 20.0)
        self.lens = lens
        cam = Camera("viewmodel_cam", lens)
        cam.setCameraMask(VIEWMODEL_CAMERA_MASK)
        self.cam_np = game.camera.attachNewNode(cam)
        buffer = game.renderer.post.manager.buffers[0]
        dr = buffer.makeDisplayRegion()
        dr.setSort(10)
        dr.setClearDepthActive(True)
        dr.setClearDepth(1.0)
        dr.setCamera(self.cam_np)
        self.region = dr
        # the viewmodel camera sees nothing but the viewmodel
        game.render.hide(VIEWMODEL_CAMERA_MASK)
        self.root = game.camera.attachNewNode("viewmodel")
        self.root.showThrough(VIEWMODEL_CAMERA_MASK)
        self.root.hide(MAIN_CAMERA_MASK | SHADOW_CAMERA_MASK)
        self.pivot = self.root.attachNewNode("pivot")
        self.entries: dict[str, VMEntry] = {}
        self.current: VMEntry | None = None
        self.cls = ""

        self.kick_pos = Spring(260.0)
        self.kick_rot = Spring(220.0)
        self.sway = Spring(90.0)
        self.idle_t = 0.0
        self.ads = 0.0
        self.draw_t = 1.0
        self.draw_dur = 0.5
        self.track = None
        self.track_t = 0.0
        self.track_dur = 1.0
        self.track_driver = None      # callable returning 0..1 (reload progress)
        self.track_hold = False       # keep the last pose (shotgun loading)
        self.part_track = None        # cycling actions run beside the main track
        self.part_t = 0.0
        self.part_dur = 1.0
        self.slide_back = 0.0
        self.slide_locked = False
        self.hidden = False
        self.apply_fov(0.0)

    # ------------------------------------------------------------- entries
    def entry(self, model_key: str) -> VMEntry:
        e = self.entries.get(model_key)
        if e is None:
            mats = self.game.materials
            model = build_weapon_model(mats, model_key, self.pivot, f"vm:{model_key}")
            vm = model.meta.get("viewmodel", {})
            hip = Point3(*vm.get("offset", (0.13, 0.3, -0.15)))
            to_gun = lambda p: Point3(*p) - hip
            arm_r = build_arm(mats, model.root, model.anchor("grip_r"), to_gun(vm.get("elbow_r", (0.28, -0.12, -0.38))),
                              1, "arm_r")
            arm_l = None
            if not vm.get("one_handed"):
                arm_l = build_arm(mats, model.root, model.anchor("grip_l"),
                                  to_gun(vm.get("elbow_l", (-0.22, 0.02, -0.4))), -1, "arm_l")
            model.root.hide()
            e = VMEntry(model, arm_r, arm_l, vm)
            self.entries[model_key] = e
        return e

    def equip(self, model_key: str, cls: str, draw_time: float) -> None:
        if self.current is not None:
            self.current.model.root.hide()
        self.current = self.entry(model_key)
        self.current.model.root.show()
        self.cls = cls
        self.draw_t = 0.0
        self.draw_dur = max(draw_time, 0.05)
        self.track = None
        self.part_track = None
        self.slide_locked = False
        self.kick_pos.value = Vec3(0, 0, 0)
        self.kick_rot.value = Vec3(0, 0, 0)

    def holster(self) -> None:
        if self.current is not None:
            self.current.model.root.hide()
        self.current = None

    # -------------------------------------------------------------- events
    def play(self, name: str, duration: float, driver=None, hold: bool = False) -> None:
        self.track = anim_data.TRACKS.get(name)
        self.track_t = 0.0
        self.track_dur = max(duration, 0.01)
        self.track_driver = driver
        self.track_hold = hold

    def stop_track(self) -> None:
        self.track = None
        self.track_driver = None

    def play_part(self, name: str, duration: float) -> None:
        self.part_track = anim_data.TRACKS.get(name)
        self.part_t = 0.0
        self.part_dur = max(duration, 0.01)

    def on_fire(self, punch: float, empty_after: bool = False) -> None:
        r = random.uniform
        s = 0.6 + 0.4 * min(punch, 3.0)
        aim = 0.45 if self.ads > 0.5 else 1.0
        self.kick_pos.impulse(Vec3(r(-0.05, 0.05), -0.9 * s, 0.12 * s) * aim)
        self.kick_rot.impulse(Vec3(r(-6, 6) * s, 55.0 * s, r(-12, 12) * s) * aim)
        self.slide_back = 1.0
        if empty_after and self.cls == "pistol":
            self.slide_locked = True

    def apply_fov(self, zoom_blend: float) -> None:
        fov = self.base_fov * (1.0 - 0.22 * zoom_blend)
        aspect = self.game.getAspectRatio() if self.game.win else 16 / 9
        hfov = math.degrees(2 * math.atan(math.tan(math.radians(fov) / 2) * aspect))
        self.lens.setFov(hfov, fov)

    def set_hidden(self, hidden: bool) -> None:
        if hidden != self.hidden:
            self.hidden = hidden
            self.root.hide() if hidden else self.root.show()

    # -------------------------------------------------------------- update
    def update(self, dt: float, ctx: dict) -> None:
        e = self.current
        if e is None:
            return
        self.idle_t += dt
        ads_target = ctx.get("ads", 0.0)
        ads_time = max(ctx.get("ads_time", 0.2), 0.05)
        step = dt / ads_time
        self.ads += max(min(ads_target - self.ads, step), -step)
        ads = self.ads * self.ads * (3 - 2 * self.ads)
        self.apply_fov(ads)

        meta = e.meta
        hip = Vec3(*meta.get("offset", (0.13, 0.3, -0.15)))
        sight = e.model.anchor("sight")
        ads_pos = Vec3(-sight.x, meta.get("ads_distance", 0.2) - sight.y, -sight.z)
        pos = hip * (1 - ads) + ads_pos * ads
        base_hpr = Vec3(*meta.get("hpr", (0, 0, 0))) * (1 - ads)

        # sway: lag opposite to mouse motion, plus idle breathing
        dx, dy = ctx.get("mouse", (0.0, 0.0))
        lag = 1.0 - 0.75 * ads
        self.sway.impulse(Vec3(dx * 0.9, -dy * 0.9, dx * 0.6) * lag)
        sway = self.sway.update(dt)
        sway = Vec3(max(min(sway.x, 6), -6), max(min(sway.y, 6), -6), max(min(sway.z, 6), -6))
        breathe = Vec3(math.sin(self.idle_t * 1.1) * 0.0012, 0, math.sin(self.idle_t * 1.7) * 0.0015) * (1 - 0.8 * ads)

        # movement bob (figure eight)
        ph = ctx.get("bob_phase", 0.0)
        w = ctx.get("bob_weight", 0.0) * (1 - 0.85 * ads)
        bob = Vec3(math.sin(ph) * 0.009, 0, -abs(math.sin(ph)) * 0.010 + 0.004) * w
        bob_rot = Vec3(math.sin(ph) * 0.8, 0, math.sin(ph) * 1.6) * w

        crouch = ctx.get("crouch", 0.0) * (1 - ads)
        land = ctx.get("land", 0.0)

        # draw: rise from below
        if self.draw_t < 1.0:
            self.draw_t = min(self.draw_t + dt / self.draw_dur, 1.0)
        d = 1.0 - (1.0 - self.draw_t) ** 3
        draw_pos = Vec3(0, 0, -0.22) * (1 - d)
        draw_rot = Vec3(0, -55, 0) * (1 - d)

        kp = self.kick_pos.update(dt) * 0.01
        kr = self.kick_rot.update(dt) * 0.05

        final_pos = pos + sway_to_pos(sway) * 0.002 + breathe + bob + draw_pos + kp + \
            Vec3(0, 0, -0.012 * crouch + land * 0.25)
        final_hpr = base_hpr + Vec3(sway.x * 0.6, sway.y * 0.5, sway.z * 0.9) + bob_rot + draw_rot + \
            Vec3(kr.x, kr.y, kr.z) + Vec3(0, 0, -4.0 * crouch)
        self.pivot.setPos(final_pos)
        self.pivot.setHpr(final_hpr)

        # keyframed tracks
        gun_pos = [0.0, 0.0, 0.0]
        gun_rot = [0.0, 0.0, 0.0]
        parts = {"mag_pos": [0, 0, 0], "bolt_pos": [0, 0, 0], "pump_pos": [0, 0, 0], "slide_pos": None}
        mag_vis = 1
        hand_l = 0.0
        if self.track is not None:
            if self.track_driver is not None:
                t = self.track_driver()
            else:
                self.track_t += dt / self.track_dur
                t = self.track_t
            if t >= 1.0 and not self.track_hold:
                self.track = None
            else:
                t = min(t, 1.0)
                tr = self.track
                gun_pos = anim_data.sample(tr, "gun_pos", t, gun_pos)
                gun_rot = anim_data.sample(tr, "gun_rot", t, gun_rot)
                for ch in ("mag_pos", "bolt_pos", "pump_pos"):
                    parts[ch] = anim_data.sample(tr, ch, t, parts[ch])
                if "slide_pos" in tr:
                    parts["slide_pos"] = anim_data.sample(tr, "slide_pos", t, [0, 0, 0])
                mag_vis = anim_data.sample(tr, "mag_vis", t, [1])[0]
                hand_l = anim_data.sample(tr, "hand_l", t, [0])[0]
        if self.part_track is not None:
            self.part_t += dt / self.part_dur
            if self.part_t >= 1.0:
                self.part_track = None
            else:
                tr = self.part_track
                for ch in ("bolt_pos", "pump_pos"):
                    v = anim_data.sample(tr, ch, self.part_t, None)
                    if v is not None:
                        parts[ch] = v
                gp = anim_data.sample(tr, "gun_pos", self.part_t, [0, 0, 0])
                gr = anim_data.sample(tr, "gun_rot", self.part_t, [0, 0, 0])
                gun_pos = [a + b for a, b in zip(gun_pos, gp)]
                gun_rot = [a + b for a, b in zip(gun_rot, gr)]
        root = e.model.root
        root.setPos(*gun_pos)
        root.setHpr(*gun_rot)

        groups = e.model.groups
        if "mag" in groups:
            groups["mag"].setPos(e.rest["mag"] + Vec3(*parts["mag_pos"]))
            groups["mag"].show() if mag_vis >= 0.5 else groups["mag"].hide()
        if "bolt" in groups:
            groups["bolt"].setPos(e.rest["bolt"] + Vec3(*parts["bolt_pos"]))
        if "pump" in groups:
            groups["pump"].setPos(e.rest["pump"] + Vec3(*parts["pump_pos"]))
        if "slide" in groups:
            self.slide_back = max(self.slide_back - dt / 0.07, 0.0)
            sp = parts["slide_pos"]
            if sp is not None:
                off = Vec3(*sp)
                if self.track is None:
                    self.slide_locked = False
            else:
                back = 1.0 if self.slide_locked else math.sin(self.slide_back * math.pi * 0.5)
                off = Vec3(0, -0.028 * back, 0)
            groups["slide"].setPos(e.rest["slide"] + off)
        if e.arm_l is not None:
            if "pump" in groups:
                base = e.arm_l_rest + Vec3(*parts["pump_pos"])
            else:
                base = e.arm_l_rest
            if hand_l > 0:
                target = e.model.anchor("mag") + Vec3(*parts["mag_pos"]) + Vec3(-0.01, 0, -0.05)
                e.arm_l.setPos(base + (target - base) * hand_l)
            else:
                e.arm_l.setPos(base)

    # -------------------------------------------------------- projections
    def muzzle_world_point(self, depth: float = 0.6) -> Point3:
        """World-space point where the viewmodel muzzle *appears* to be, at
        ``depth`` metres along the main camera ray (tracers/flash start here)."""
        cam = self.game.camera
        if self.current is None:
            return cam.getPos(self.game.render)
        p = self.current.muzzle.getPos(cam)
        film = Point2()
        if not self.lens.project(p, film):
            film = Point2(0.3, -0.3)
        near, far = Point3(), Point3()
        self.game.camLens.extrude(film, near, far)
        ray = far - near
        ray.normalize()
        pt = ray * (depth / max(ray.y, 1e-3))
        return self.game.render.getRelativePoint(cam, pt)

    def eject_world(self) -> tuple[Point3, Vec3]:
        cam = self.game.camera
        if self.current is None:
            return cam.getPos(self.game.render), Vec3(1, 0, 0)
        p = self.current.eject.getPos(cam)
        film = Point2()
        if not self.lens.project(p, film):
            film = Point2(0.2, -0.2)
        near, far = Point3(), Point3()
        self.game.camLens.extrude(film, near, far)
        ray = far - near
        ray.normalize()
        pt = ray * (0.45 / max(ray.y, 1e-3))
        right = self.game.render.getRelativeVector(cam, Vec3(1, 0.15, 0.6))
        right.normalize()
        return self.game.render.getRelativePoint(cam, pt), right


def sway_to_pos(s: Vec3) -> Vec3:
    return Vec3(-s.x, 0, -s.y)
