"""Third-person soldier body: a jointed mannequin with procedural animation
and hitboxes that follow the pose.

Skeleton (character faces +Y, origin between the feet)::

    root (yaw) -> tilt (death fall) -> pelvis -> spine -> chest -> neck -> head
                                         |                 +-> arms (2-bone IK onto the weapon grips)
                                         |                 +-> weapon (completes the aim pitch)
                                         +-> hip -> knee (walk cycle, crouch)

Every body part is a separate node, so the pose is a handful of joint
rotations per tick. The hit boxes (gameplay/hitboxes.py PARTS: same sizes
and hit groups as the target dummies) are children of their body part, so
Panda3D's Bullet integration moves the kinematic boxes with the pose in the
next physics step: what you see is what you hit.

The animation is procedural:
* aim pitch is spread over spine (25%), chest (30%) and the weapon (45%),
  the head follows the full pitch;
* walking swings the legs with the distance travelled (stride length per
  gait), forwards/backwards with pitch and sideways with roll, and bobs
  the pelvis;
* crouching lowers the pelvis and folds hips and knees;
* death tips the whole body over around the feet, away from the killing
  shot, while the knees buckle.

``visible=False`` builds the skeleton and hit boxes only (the first-person
player needs hit boxes but no body).
"""
from __future__ import annotations

import math

from panda3d.core import (BoundingSphere, LMatrix4f, LVecBase4f, NodePath, Point3, PTA_LMatrix4f, TransformState,
                          Vec3)

from engine.geometry import MeshBuilder, build_skinned
from gameplay.lean import Lean
from gameplay.hitboxes import PARTS, HitboxRig
from weapons.models import segment_hpr, shared_weapon_model

UPPER_ARM = 0.30
FOREARM = 0.32
SHOULDER = (0.21, 0.0, 0.24)          # in chest-joint space
STAND_PELVIS = 0.90
CROUCH_PELVIS = 0.50

# where the weapon sits in chest space and how its pitch is shared
HOLDS = {
    "long": {"pos": (0.115, 0.17, 0.17), "hands": 2},
    "pistol": {"pos": (0.04, 0.42, 0.16), "hands": 2},
    "knife": {"pos": (0.21, 0.30, -0.20), "hands": 1},
    "grenade": {"pos": (0.22, 0.20, 0.10), "hands": 1},
    "bomb": {"pos": (0.0, 0.30, -0.16), "hands": 2},
}
CLASS_HOLD = {"rifle": "long", "smg": "long", "sniper": "long", "shotgun": "long", "pistol": "pistol",
              "knife": "knife", "grenade": "grenade", "bomb": "bomb"}

# part -> (parent joint, offset, local hpr); arm parts lie along +Y of their joint
PART_MOUNT = {
    "pelvis": ("pelvis", (0, 0, -0.03), (0, 0, 0)),
    "stomach": ("spine", (0, 0, 0.06), (0, 0, 0)),
    "chest": ("chest", (0, 0, 0.13), (0, 0, 0)),
    "neck": ("neck", (0, 0, 0.02), (0, 0, 0)),
    "head": ("neck", (0, 0.01, 0.155), (0, 0, 0)),
    "upper_arm_l": ("shoulder_l", (0, UPPER_ARM * 0.5, 0), (0, -90, 0)),
    "upper_arm_r": ("shoulder_r", (0, UPPER_ARM * 0.5, 0), (0, -90, 0)),
    "forearm_l": ("elbow_l", (0, FOREARM * 0.45, 0), (0, -90, 0)),
    "forearm_r": ("elbow_r", (0, FOREARM * 0.45, 0), (0, -90, 0)),
    "thigh_l": ("hip_l", (0, 0, -0.215), (0, 0, 0)),
    "thigh_r": ("hip_r", (0, 0, -0.215), (0, 0, 0)),
    "calf_l": ("knee_l", (0, 0, -0.21), (0, 0, 0)),
    "calf_r": ("knee_r", (0, 0, -0.21), (0, 0, 0)),
}


BONES = [p.name for p in PARTS]          # skinning bone index = part order
MAX_BONES = 24                            # render/shaders/skinning.glsl


def _bind(pos, scale) -> list:
    """Row-vector 4x4 matrix placing a mesh inside its bone (for build_skinned)."""
    m = TransformState.makePosHprScale(Point3(*pos), Vec3(0, 0, 0), Vec3(*scale)).getMat()
    return [[m.getCell(r, c) for c in range(4)] for r in range(4)]


def two_bone_elbow(s: Vec3, h: Vec3, l1: float, l2: float, pole: Vec3) -> Vec3:
    """Elbow position for a shoulder s reaching for a hand target h."""
    d = h - s
    dist = d.length()
    if dist < 1e-4:
        return s + pole.normalized() * l1
    dist_c = min(max(dist, abs(l1 - l2) + 1e-3), l1 + l2 - 1e-3)
    axis = d / dist
    a = (l1 * l1 - l2 * l2 + dist_c * dist_c) / (2 * dist_c)
    hh = math.sqrt(max(l1 * l1 - a * a, 0.0))
    perp = pole - axis * pole.dot(axis)
    if perp.lengthSquared() < 1e-8:
        perp = Vec3(0, 0, -1) - axis * (-axis.z)
    perp.normalize()
    return s + axis * a + perp * hh


class CharacterBody:
    def __init__(self, game, owner, uniform: str = "uniform_tan", helmet_mat: str = "metal_olive",
                 visible: bool = True, name: str = "body"):
        self.game = game
        self.owner = owner
        self.visible = visible
        self.first_person = False
        self.root = game.render.attachNewNode(name)
        if not visible:
            self.root.hide()
        self.tilt = self.root.attachNewNode("tilt")
        j = {}
        j["pelvis"] = self.tilt.attachNewNode("pelvis")
        j["spine"] = j["pelvis"].attachNewNode("spine")
        j["spine"].setPos(0, 0, 0.08)
        j["chest"] = j["spine"].attachNewNode("chest")
        j["chest"].setPos(0, 0, 0.20)
        j["neck"] = j["chest"].attachNewNode("neck")
        j["neck"].setPos(0, 0, 0.30)
        for side, sx in (("l", -1), ("r", 1)):
            j[f"shoulder_{side}"] = j["chest"].attachNewNode(f"shoulder_{side}")
            j[f"shoulder_{side}"].setPos(SHOULDER[0] * sx, SHOULDER[1], SHOULDER[2])
            j[f"elbow_{side}"] = j["chest"].attachNewNode(f"elbow_{side}")
            j[f"hip_{side}"] = j["pelvis"].attachNewNode(f"hip_{side}")
            j[f"hip_{side}"].setPos(0.105 * sx, 0, -0.06)
            j[f"knee_{side}"] = j[f"hip_{side}"].attachNewNode(f"knee_{side}")
            j[f"knee_{side}"].setPos(0, 0, -0.43)
        j["gun"] = j["chest"].attachNewNode("gun")
        self.joints = j
        self.parts: dict[str, NodePath] = {}
        self._bones = None
        for part in PARTS:
            parent, off, hpr = PART_MOUNT[part.name]
            np_ = j[parent].attachNewNode(f"part:{part.name}")
            np_.setPos(*off)
            np_.setHpr(*hpr)
            self.parts[part.name] = np_
        if visible:
            self._build_meshes(uniform, helmet_mat)
        self.rig = HitboxRig(game.physics, owner, surface="flesh", parents=self.parts)
        self.weapon_model = None
        self.weapon_key = ""
        self.hold = "long"
        self.muzzle: NodePath | None = None
        self._grips = (Point3(0, 0, 0), Point3(0, 0, 0))
        # animation state
        self.phase = 0.0
        self.move_weight = 0.0
        self.crouch = 0.0
        self.aim_pitch = 0.0
        self.kick = 0.0
        self.dead_t = -1.0
        self.fall_dir = 1.0
        self.flash = 0.0
        self.alive = True
        self._sig = None

    # ------------------------------------------------------------ meshes
    def _build_meshes(self, uniform: str, helmet_mat: str) -> None:
        """All parts merged into one GPU-skinned mesh per material (a handful of
        draw calls per soldier instead of one per part; render/shaders/skinning.glsl)."""
        mats = self.game.materials
        body = mats.get(uniform)
        groups: dict[str, list] = {}

        def add(mat: str, mb: MeshBuilder, part: str, bind=None) -> None:
            groups.setdefault(mat, []).append((mb, BONES.index(part), bind))

        for part in PARTS:
            mb = MeshBuilder()
            if part.shape == "sphere":
                r = part.size[0]
                mb.add_sphere((0, 0, 0), r * 0.97, rings=12, segments=20, uv_scale=body.uv_scale)
                m = "glove"                                   # balaclava
            else:
                sx, sy, sz = part.size
                mb.add_chamfer_box((0, 0, 0), (sx * 0.96, sy * 0.96, sz * 0.97), bevel=min(sx, sy) * 0.3,
                                   uv_scale=body.uv_scale)
                m = "glove" if part.name == "neck" else uniform
            add(m, mb, part.name)
        # helmet, goggles, vest, gloves, boots
        h = MeshBuilder()
        h.add_sphere((0, 0, 0), 0.128, rings=10, segments=20)
        add(helmet_mat, h, "head", _bind((0, -0.008, 0.035), (1.0, 1.08, 0.82)))
        g = MeshBuilder()
        g.add_chamfer_box((0, 0.095, 0.02), (0.17, 0.04, 0.05), bevel=0.012)
        add("glass_dark", g, "head")
        v = MeshBuilder()
        v.add_chamfer_box((0, 0, 0), (0.44, 0.29, 0.34), bevel=0.03)
        v.add_chamfer_box((0, 0.155, -0.06), (0.32, 0.04, 0.14), bevel=0.012)
        v.add_chamfer_box((-0.1, 0.16, 0.06), (0.09, 0.03, 0.1), bevel=0.01)
        add("canvas", v, "chest")
        for side in ("l", "r"):
            hand = MeshBuilder()
            hand.add_chamfer_box((0, 0, FOREARM * 0.5), (0.085, 0.07, 0.1), bevel=0.02)
            add("glove", hand, f"forearm_{side}")
            boot = MeshBuilder()
            boot.add_chamfer_box((0, 0.04, -0.17), (0.14, 0.27, 0.12), bevel=0.03)
            add("rubber", boot, f"calf_{side}")
        self.skin = self.root.attachNewNode("skin")
        # the vertices sit at their bones' origins in the mesh, so the bounds must be given
        self.skin.node().setBounds(BoundingSphere(Point3(0, 0, 0.9), 1.7))
        self.skin.node().setFinal(True)
        for mat, pieces in groups.items():
            node = build_skinned(pieces, f"body:{mat}")
            np_ = self.skin.attachNewNode(node)
            mats.get(mat).apply(np_)
        self._bones = PTA_LMatrix4f.emptyArray(MAX_BONES)
        for k in range(MAX_BONES):
            self._bones[k] = LMatrix4f.identMat()
        self.skin.setShaderInput("u_bones", self._bones)
        self.skin.setShaderInput("u_skinned", 1.0)
        self._update_bones()
        self.root.hide()

    HEAD_BONES = ("head", "neck")

    def set_first_person(self, on: bool) -> None:
        """Collapse the head (and helmet, goggles) for a camera at this body's eyes."""
        if on != self.first_person:
            self.first_person = on
            self._update_bones()

    def _update_bones(self) -> None:
        if not self.visible or self._bones is None:
            return
        root = self.root
        for k, name in enumerate(BONES):
            m = self.parts[name].getMat(root)
            if self.first_person and name in self.HEAD_BONES:
                m = LMatrix4f.scaleMat(1e-4) * m
            self._bones[k] = m

    # ------------------------------------------------------------ weapon
    def set_weapon(self, model_key: str | None, cls: str = "rifle") -> None:
        """Show a weapon model in the hands (None = empty hands)."""
        if model_key == self.weapon_key and self.weapon_model is not None:
            return
        if self.weapon_model is not None:
            self.weapon_model.root.removeNode()
            self.weapon_model = None
        self.weapon_key = model_key or ""
        self.hold = CLASS_HOLD.get(cls, "long")
        gun = self.joints["gun"]
        gun.setPos(*HOLDS[self.hold]["pos"])
        if model_key is None:
            self._grips = (Point3(0, 0.05, -0.05), Point3(0, 0.05, -0.05))
            self.muzzle = None
            return
        if self.visible:
            # third-person guns never animate their parts: one merged copy of a shared prototype
            self.weapon_model = shared_weapon_model(self.game.materials, model_key, gun)
            anchors = self.weapon_model.anchors
        else:
            from weapons.models import model_defs
            anchors = {k: Point3(*v) for k, v in model_defs()[model_key].get("anchors", {}).items()}
        self._grips = (Point3(anchors.get("grip_r", Point3())), Point3(anchors.get("grip_l", Point3())))
        if self.muzzle is None or self.muzzle.isEmpty():
            self.muzzle = gun.attachNewNode("muzzle")
        self.muzzle.setPos(anchors.get("muzzle", Point3(0, 0.5, 0)))

    def muzzle_pos(self) -> Point3:
        if self.muzzle is not None and not self.muzzle.isEmpty():
            return self.muzzle.getPos(self.game.render)
        return self.joints["gun"].getPos(self.game.render)

    def on_fire(self, strength: float = 1.0) -> None:
        self.kick = min(self.kick + 0.6 * strength, 1.0)

    # ------------------------------------------------------------- state
    def place(self, pos, yaw: float) -> None:
        self.root.setPos(Point3(*pos))
        self.root.setH(yaw)

    def reset(self) -> None:
        self._sig = None
        self.dead_t = -1.0
        self.alive = True
        self.tilt.setHpr(0, 0, 0)
        self.tilt.setPos(0, 0, 0)
        self.flash = 0.0
        self._update_bones()
        if self.visible:
            self.root.show()
            self.root.setShaderInput("u_emission", LVecBase4f(0, 0, 0, 0))
        if self.weapon_model is not None:
            self.weapon_model.root.show()
        self.rig.set_enabled(True)

    def hide(self) -> None:
        if self.visible:
            self.root.hide()
        self.rig.set_enabled(False)

    def die(self, direction: Vec3 | None = None) -> None:
        """Start the death fall away from the killing shot."""
        self.alive = False
        self.dead_t = 0.0
        self.rig.set_enabled(False)
        fwd = self.game.render.getRelativeVector(self.root, Vec3(0, 1, 0))
        self.fall_dir = 1.0
        if direction is not None and Vec3(direction).dot(fwd) > 0:
            self.fall_dir = -1.0           # shot in the back: fall forwards
        if self.weapon_model is not None:
            self.weapon_model.root.hide()

    def on_hit(self) -> None:
        self.flash = 0.1

    # ---------------------------------------------------------- animation
    def animate(self, dt: float, pos, yaw: float, pitch: float, crouch: float, vel: Vec3, on_ground: bool,
                walking: bool = False, lean: float = 0.0) -> None:
        """Pose for this tick and move the hit boxes along. ``pitch`` is the aim pitch,
        ``lean`` -1..1 rolls the upper body sideways (gameplay/lean.py)."""
        self.root.setPos(Point3(*pos))
        self.root.setH(yaw)
        if self.dead_t >= 0.0:
            if self.dead_t < 1.0:
                self._animate_death(dt)
                self._update_bones()
            return
        # nothing changed (standing still, same view): the pose and hit boxes are current
        speed = math.hypot(vel.x, vel.y)
        sig = (round(pos[0], 3), round(pos[1], 3), round(pos[2], 3), round(yaw, 1), round(pitch, 1),
               round(crouch, 2), round(lean, 2), self.hold, self.weapon_key)
        if sig == self._sig and speed < 0.05 and self.move_weight < 0.01 and self.kick <= 0.0 and self.flash <= 0:
            return
        self._sig = sig
        self.crouch = crouch
        self.aim_pitch = max(-80.0, min(80.0, pitch))
        moving = min(speed / 2.0, 1.0) if on_ground else 0.0
        self.move_weight += (moving - self.move_weight) * min(dt * 10.0, 1.0)
        lean_amount = lean
        stride = 1.0 if crouch > 0.5 else (1.15 if walking else 1.6)
        self.phase = (self.phase + speed * dt / stride * math.pi) % (2 * math.pi)
        self.kick = max(self.kick - dt * 8.0, 0.0)
        j = self.joints
        # direction of travel relative to facing
        h = math.radians(yaw)
        fwd = (-math.sin(h), math.cos(h))
        right = (math.cos(h), math.sin(h))
        if speed > 0.1:
            vf = (vel.x * fwd[0] + vel.y * fwd[1]) / speed
            vs = (vel.x * right[0] + vel.y * right[1]) / speed
        else:
            vf, vs = 1.0, 0.0
        w = self.move_weight
        amp = 16.0 if crouch > 0.5 else (22.0 if walking else 32.0)
        s = math.sin(self.phase)
        bob = abs(math.cos(self.phase)) * 0.03 * w
        j["pelvis"].setZ(STAND_PELVIS + (CROUCH_PELVIS - STAND_PELVIS) * crouch - bob)
        j["pelvis"].setR(vs * s * 4.0 * w)
        lean = -12.0 * crouch - 4.0 * w * vf
        a = self.aim_pitch
        roll = Lean.body_roll(lean_amount)
        j["spine"].setHpr(0, lean + a * 0.25, roll)
        j["chest"].setP(a * 0.30 - self.kick * 3.0)
        j["neck"].setHpr(0, a * 0.45 - lean, -roll * 0.35)
        j["gun"].setP(a * 0.45 + self.kick * 4.0)
        j["gun"].setY(HOLDS[self.hold]["pos"][1] - self.kick * 0.03)
        hip_base = 80.0 * crouch
        knee_base = -95.0 * crouch
        for side, sign in (("l", 1.0), ("r", -1.0)):
            ss = s * sign
            swing = ss * amp * w
            j[f"hip_{side}"].setHpr(0, hip_base + swing * vf, -swing * vs * 0.6)
            lift = max(0.0, math.sin(self.phase * 1.0 + (0 if sign > 0 else math.pi) + 1.2))
            j[f"knee_{side}"].setP(knee_base - lift * (40.0 if crouch < 0.5 else 18.0) * w)
        self._pose_arms()
        self._update_bones()
        if self.flash > 0:
            self.flash -= dt
            k = max(self.flash, 0.0) / 0.1
            if self.visible:
                self.root.setShaderInput("u_emission", LVecBase4f(0.45 * k, 0.04 * k, 0.02 * k, 0))

    def _pose_arms(self) -> None:
        j = self.joints
        gun = j["gun"]
        g = gun.getPos()
        p = math.radians(gun.getP())
        cp, sp = math.cos(p), math.sin(p)

        def to_chest(a: Point3) -> Vec3:
            return Vec3(g.x + a.x, g.y + a.y * cp - a.z * sp, g.z + a.y * sp + a.z * cp)

        hand_r = to_chest(self._grips[0])
        if HOLDS[self.hold]["hands"] == 2:
            hand_l = to_chest(self._grips[1])
        else:
            hand_l = Vec3(-0.22, 0.12, -0.32)
        for side, sx, hand in (("l", -1.0, hand_l), ("r", 1.0, hand_r)):
            s = Vec3(SHOULDER[0] * sx, SHOULDER[1], SHOULDER[2])
            elbow = two_bone_elbow(s, hand, UPPER_ARM, FOREARM, Vec3(sx * 0.8, -0.3, -1.0))
            hh, pp = segment_hpr(Point3(s), Point3(elbow))
            j[f"shoulder_{side}"].setHpr(hh, pp, 0)
            j[f"elbow_{side}"].setPos(elbow)
            hh, pp = segment_hpr(Point3(elbow), Point3(hand))
            j[f"elbow_{side}"].setHpr(hh, pp, 0)

    def _animate_death(self, dt: float) -> None:
        if self.dead_t >= 1.0:
            return
        self.dead_t = min(self.dead_t + dt * 1.8, 1.0)
        t = self.dead_t * self.dead_t
        j = self.joints
        self.tilt.setP(86.0 * t * self.fall_dir)
        self.tilt.setZ(0.13 * t)
        j["pelvis"].setZ(STAND_PELVIS + (CROUCH_PELVIS - STAND_PELVIS) * min(self.crouch + t * 0.3, 1.0))
        for side in ("l", "r"):
            j[f"hip_{side}"].setP(40.0 * t * (1 if self.fall_dir < 0 else 0.4))
            j[f"knee_{side}"].setP(-60.0 * t)
        j["chest"].setP(-10.0 * t * self.fall_dir)

    def destroy(self) -> None:
        self.rig.destroy()
        self.root.removeNode()
