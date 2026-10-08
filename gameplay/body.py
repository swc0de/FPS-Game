"""Third-person soldier body: a mannequin on the game skeleton with
procedural animation and hitboxes that follow the pose.

The skeleton is gameplay/skeleton.py (47 bones, the same for every
appearance). The pose is solved in numpy each tick:

    root (death fall) -> pelvis -> spine_01..03 -> neck -> head
                           |                +-> arms (2-bone IK onto the weapon grips)
                           |                +-> weapon (completes the aim pitch)
                           +-> thigh -> calf (walk cycle, crouch)

and the result goes to two places:
* the skinning palette (one matrix per bone, render/shaders/skinning.glsl),
  solved in numpy, for visible bodies only;
* a NodePath mirror of the 19 bones that carry a hit box or the weapon,
  which gets each bone's local transform (Panda chains them in C++). The
  hit boxes (gameplay/hitboxes.py PARTS: same sizes and hit groups as the
  target dummies) are children of their bone's node, so Panda3D's Bullet
  integration moves the kinematic boxes with the pose in the next physics
  step: what you see is what you hit.

The body mesh is the jointed mannequin of Milestone 5, stored in the bind
pose and rigidly skinned (weight 1 on one bone per part) until the
procedural human replaces it.

The animation is procedural:
* aim pitch is spread over spine_01 (25%), spine_03 (30%) and the weapon
  (45%), the head follows the full pitch;
* walking swings the legs with the distance travelled (stride length per
  gait), forwards/backwards with pitch and sideways with roll, and bobs
  the pelvis;
* crouching lowers the pelvis and folds hips and knees;
* death tips the whole body over around the feet, away from the killing
  shot, while the knees buckle.

``visible=False`` poses the skeleton and hit boxes only (the first-person
player needs hit boxes but no body).
"""
from __future__ import annotations

import math
import zlib

import numpy as np
from panda3d.core import (BoundingSphere, LODNode, LVecBase4f, NodePath, Point3, PTA_LVecBase4f, TransformState,
                          Vec3)

from engine.geometry import MeshBuilder, build_skinned
from engine.physics import EXACT_RAY, segment_capsule
from gameplay import skeleton as sk
from gameplay.lean import Lean
from gameplay.hitboxes import CAPSULES, PARTS, HitboxRig, capsule_length, capsule_mount
from weapons.models import shared_weapon_model

UPPER_ARM = sk.UPPER_ARM
FOREARM = sk.LOWER_ARM + sk.PALM      # elbow to the grip in the palm
SHOULDER = (0.21, 0.0, 0.24)          # in spine_03 space (clavicle + upper arm offsets)
STAND_PELVIS = sk.STAND_PELVIS
CROUCH_PELVIS = 0.50

# where the weapon sits in spine_03 space and how its pitch is shared
HOLDS = {
    "long": {"pos": (0.115, 0.17, 0.17), "hands": 2},
    "pistol": {"pos": (0.04, 0.42, 0.16), "hands": 2},
    "knife": {"pos": (0.21, 0.30, -0.20), "hands": 1},
    "grenade": {"pos": (0.22, 0.20, 0.10), "hands": 1},
    "bomb": {"pos": (0.0, 0.30, -0.16), "hands": 2},
}
CLASS_HOLD = {"rifle": "long", "smg": "long", "sniper": "long", "shotgun": "long", "pistol": "pistol",
              "knife": "knife", "grenade": "grenade", "bomb": "bomb"}

# part -> (bone, offset, local hpr); arm parts lie along +Y of their bone
PART_MOUNT = {
    "pelvis": ("pelvis", (0, 0, -0.03), (0, 0, 0)),
    "stomach": ("spine_01", (0, 0, 0.06), (0, 0, 0)),
    "chest": ("spine_03", (0, 0, 0.13), (0, 0, 0)),
    "neck": ("neck", (0, 0, 0.02), (0, 0, 0)),
    "head": ("head", (0, 0, 0.065), (0, 0, 0)),
    "upper_arm_l": ("upperarm_l", (0, UPPER_ARM * 0.5, 0), (0, -90, 0)),
    "upper_arm_r": ("upperarm_r", (0, UPPER_ARM * 0.5, 0), (0, -90, 0)),
    "forearm_l": ("lowerarm_l", (0, FOREARM * 0.45, 0), (0, -90, 0)),
    "forearm_r": ("lowerarm_r", (0, FOREARM * 0.45, 0), (0, -90, 0)),
    "thigh_l": ("thigh_l", (0, 0, -0.215), (0, 0, 0)),
    "thigh_r": ("thigh_r", (0, 0, -0.215), (0, 0, 0)),
    "calf_l": ("calf_l", (0, 0, -0.21), (0, 0, 0)),
    "calf_r": ("calf_r", (0, 0, -0.21), (0, 0, 0)),
}

B = sk.INDEX


def _mirrored() -> list:
    """The bones that carry a hit box, the weapon or the charge pack, and all their ancestors,
    parents first."""
    keep = set()
    for name in [m[0] for m in PART_MOUNT.values()] + [c.bone for c in CAPSULES] + ["weapon", "pack"]:
        i = B[name]
        while i >= 0 and i not in keep:
            keep.add(i)
            i = int(sk.PARENT[i])
    return sorted(keep)


MIRRORED = _mirrored()


_CAPSULE_MOUNTS = {c.name: capsule_mount(c) for c in CAPSULES}             # capsule frame on its bone
_CAPSULE_HALF = {c.name: capsule_length(c) / 2 for c in CAPSULES}
HEAD_BONES = np.array([B["neck"], B["head"]], np.int64)     # collapsed for a camera at the eyes
LOD_SWITCH = ((0.0, 14.0), (14.0, 40.0), (40.0, 100000.0))   # metres from the camera, per LOD
PACK_POS, PACK_HPR = (0.0, -0.045, -0.04), (0.0, 90.0, 0.0)   # the charge on the plate carrier's back, keypad out


def _mat(pos=(0, 0, 0), hpr=(0, 0, 0), scale=(1, 1, 1)) -> np.ndarray:
    m = TransformState.makePosHprScale(Point3(*pos), Vec3(*hpr), Vec3(*scale)).getMat()
    return np.array([[m.getCell(r, c) for c in range(4)] for r in range(4)])


COLLAPSE = np.diag([1e-4, 1e-4, 1e-4, 1.0])


_CLIPS = None


def clips() -> dict:
    """data/character_anims.json (procedural clips: reload, switch, throw, knife, plant, defuse)."""
    global _CLIPS
    if _CLIPS is None:
        import json
        from engine import paths
        with open(paths.DATA_DIR / "character_anims.json", "r", encoding="utf-8") as f:
            _CLIPS = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return _CLIPS


def _smooth(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _channel(keys: list, name: str, t: float):
    """A channel's value at normalised time t: smoothstep between the keys that set it.
    Returns (before, after, w) for hand targets (resolved by the caller) or a number."""
    prev = nxt = None
    for k in keys:
        if name not in k:
            continue
        if k["t"] <= t:
            prev = k
        elif nxt is None:
            nxt = k
    if prev is None and nxt is None:
        return None
    if prev is None:
        prev = nxt
    if nxt is None:
        nxt = prev
    span = nxt["t"] - prev["t"]
    w = _smooth((t - prev["t"]) / span) if span > 1e-9 else 0.0
    a, b = prev[name], nxt[name]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * w
    return (a, b, w)


def _bind(pos, scale) -> np.ndarray:
    """Row-vector 4x4 matrix placing a mesh inside its part."""
    return _mat(pos, (0, 0, 0), scale)


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
                 visible: bool = True, name: str = "body", character: tuple | None = None):
        """``character``: (bot name, appearance seed, side) for a procedural soldier
        (characters/); without it the body is the jointed mannequin."""
        self.game = game
        self.owner = owner
        self.visible = visible
        self.first_person = False
        self.root = game.render.attachNewNode(name)
        if not visible:
            self.root.hide()
        self.pose = sk.Pose()
        # the bones that carry the hit box parts and the weapon, as nodes
        self.nodes: dict[int, NodePath] = {}
        for i in MIRRORED:
            parent = self.root if sk.PARENT[i] < 0 else self.nodes[int(sk.PARENT[i])]
            self.nodes[i] = parent.attachNewNode(sk.BONES[i].name)
        self.parts: dict[str, NodePath] = {}
        for part in PARTS:
            bone, off, hpr = PART_MOUNT[part.name]
            np_ = self.nodes[B[bone]].attachNewNode(f"part:{part.name}")
            np_.setPos(*off)
            np_.setHpr(*hpr)
            self.parts[part.name] = np_
        self.gun = self.nodes[B["weapon"]]
        self._lod_n = zlib.crc32(name.encode()) & 3          # staggers the animation LOD ticks
        self._bones = None
        self._rows = None
        self._palette_stale = False
        if visible:
            if character is not None:
                self._build_character(*character)
            else:
                self._build_meshes(uniform, helmet_mat)
        # hit capsules on the bones (gameplay/hitboxes.py CAPSULES): the same for every body
        self.hit_mounts: dict[str, NodePath] = {}
        for c in CAPSULES:
            np_ = self.nodes[B[c.bone]].attachNewNode(f"hit:{c.name}")
            np_.setMat(capsule_mount(c))
            self.hit_mounts[c.name] = np_
        self.rig = HitboxRig(game.physics, owner, surface="flesh", parents=self.hit_mounts, parts=CAPSULES)
        # the exact capsule test (engine/physics.py EXACT_RAY) in the pose Bullet last synced
        self._node_hpr, self._node_pos = self.pose.hpr.copy(), self.pose.pos.copy()
        self._step_pose = None
        self._step_lists = None
        self._step_bones: dict = {}
        self._step_ends: dict = {}
        for part, np_ in self.rig.parts:
            if part.b is not None:
                np_.node().setPythonTag(EXACT_RAY, lambda a, b, c=part: self._exact_capsule(c, a, b))
        self._hooks = getattr(game.physics, "before_step", None)
        if self._hooks is not None:
            self._hooks.append(self._snapshot)
        self.weapon_model = None
        self.weapon_key = ""
        self.pack_model = None
        self._pack_on = False
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
        self.ragdoll = None            # gameplay/ragdoll.py, soldiers only
        self._vel = Vec3(0, 0, 0)
        self.fall_dir = 1.0
        self.flash = 0.0
        self.alive = True
        self._sig = None
        self.active_clips: dict[str, dict] = {}      # name -> {t, dur, loop, stop}
        self.flinch = (0.0, 0.0, 0.0)                # pitch, roll, time left
        self._apply(force=True)
        if visible:
            self.root.hide()

    # ------------------------------------------------------------ meshes
    def _build_meshes(self, uniform: str, helmet_mat: str) -> None:
        """All parts merged into one skinned mesh per material (a handful of draw calls
        per soldier instead of one per part), stored in the skeleton's bind pose with
        each part rigidly on its bone (render/shaders/skinning.glsl)."""
        mats = self.game.materials
        body = mats.get(uniform)
        groups: dict[str, list] = {}

        def add(mat: str, mb: MeshBuilder, part: str, local=None, bone: str | None = None) -> None:
            """``local`` places the mesh in the part; ``bone`` overrides the part's bone."""
            name, off, hpr = PART_MOUNT[part]
            place = _mat(off, hpr) @ sk.REST_WORLD[B[name]]
            if local is not None:
                place = local @ place
            groups.setdefault(mat, []).append((mb, B[bone or name], place))

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
            add("glove", hand, f"forearm_{side}", bone=f"hand_{side}")
            boot = MeshBuilder()
            boot.add_chamfer_box((0, 0.04, -0.17), (0.14, 0.27, 0.12), bevel=0.03)
            add("rubber", boot, f"calf_{side}", bone=f"foot_{side}")
        self.skin = self.root.attachNewNode("skin")
        # posed vertices leave the bind-pose bounds (crouch, death fall), so the bounds are given
        self.skin.node().setBounds(BoundingSphere(Point3(0, 0, 0.9), 1.7))
        self.skin.node().setFinal(True)
        for mat, pieces in groups.items():
            node = build_skinned(pieces, f"body:{mat}")
            np_ = self.skin.attachNewNode(node)
            mats.get(mat).apply(np_)
        self._bones = PTA_LVecBase4f.emptyArray(sk.MAX_BONES * 3)
        self._rows = np.zeros((sk.MAX_BONES, 3, 4), np.float32)
        self._rows[:, 0, 0] = self._rows[:, 1, 1] = self._rows[:, 2, 2] = 1.0
        self.skin.setShaderInput("u_bones", self._bones)
        self.skin.setShaderInput("u_skinned", 1.0)

    def _build_character(self, bot_name: str, seed: int, side: str) -> None:
        """The procedural soldier: one skinned mesh, one draw call, its colours a palette table
        (characters/palette.py, render/shaders/character.frag)."""
        from characters import build, detail, library, palette
        from characters.appearance import HAIR_COLOURS, appearance, style_for_side
        style = style_for_side(side)
        app = appearance(bot_name, seed)
        lods = library.meshes(bot_name, seed, style)
        self.skin = self.root.attachNewNode("skin")
        self.skin.node().setBounds(BoundingSphere(Point3(0, 0, 0.9), 1.7))
        self.skin.node().setFinal(True)
        # levels of detail by distance (OVERHAUL_PLAN 4.4); the shadow cameras are far away
        # along the sun, so shadow passes pick the coarsest one by themselves
        lod = LODNode("lod")
        lod.setCenter(Point3(0, 0, 1.0))
        lod_np = self.skin.attachNewNode(lod)
        for k, (near, far) in enumerate(LOD_SWITCH[:len(lods)]):
            lod.addSwitch(far, near)
            lod_np.attachNewNode(build.geom_node(lods[k], f"character:{bot_name}:lod{k}"))
        self.game.renderer.use_character_shader(self.skin)
        hair = np.array(HAIR_COLOURS[app.hair_colour]) ** 2.2
        col, par = palette.table(style, app.skin_rgb(), hair, np.array([0.8, 0.8, 0.8]))
        self._palette = PTA_LVecBase4f.emptyArray(palette.N_SLOTS)
        self._slot_params = PTA_LVecBase4f.emptyArray(palette.N_SLOTS)
        memoryview(self._palette).cast("B")[:] = np.ascontiguousarray(col, np.float32).tobytes()
        memoryview(self._slot_params).cast("B")[:] = np.ascontiguousarray(par, np.float32).tobytes()
        self.skin.setShaderInput("u_palette", self._palette)
        self.skin.setShaderInput("u_slotParams", self._slot_params)
        self.skin.setShaderInput("u_detail", detail.shared_texture())
        self.triangles = [len(m.tris) for m in lods]
        self.mesh_lod0 = lods[0]          # bind-pose arrays, for tools (hit box fitting, areas)
        self._bones = PTA_LVecBase4f.emptyArray(sk.MAX_BONES * 3)
        self._rows = np.zeros((sk.MAX_BONES, 3, 4), np.float32)
        self._rows[:, 0, 0] = self._rows[:, 1, 1] = self._rows[:, 2, 2] = 1.0
        self.skin.setShaderInput("u_bones", self._bones)
        self.skin.setShaderInput("u_skinned", 1.0)

    def show_hitboxes(self, on: bool) -> None:
        """Draw the hit capsules over the body (console ``hitboxes``), coloured by hit group."""
        old = getattr(self, "_hit_debug", None)
        if old is not None:
            for np_ in old:
                np_.removeNode()
            self._hit_debug = None
        if not on:
            return
        from gameplay.hitboxes import capsule_length
        colours = {"head": (1.0, 0.25, 0.2, 0.45), "chest": (1.0, 0.75, 0.2, 0.4), "stomach": (0.3, 0.8, 1.0, 0.4),
                   "arm": (0.4, 1.0, 0.4, 0.4), "leg": (0.7, 0.5, 1.0, 0.4)}
        self._hit_debug = []
        for c in CAPSULES:
            mb = MeshBuilder()
            length = capsule_length(c)
            if length > 0.0:
                mb.add_cylinder((0, 0, 0), c.radius, length, segments=16, caps=False)
                for z in (-length * 0.5, length * 0.5):
                    mb.add_sphere((0, 0, z), c.radius, rings=8, segments=16)
            else:
                mb.add_sphere((0, 0, 0), c.radius, rings=10, segments=20)
            np_ = self.hit_mounts[c.name].attachNewNode(mb.build(f"hitdbg:{c.name}"))
            np_.setShaderOff(1000)
            np_.setLightOff(1000)
            np_.setColor(*colours.get(c.hitgroup, (1, 1, 1, 0.4)), 1000)
            np_.setTransparency(True, 1000)
            np_.setDepthWrite(False, 1000)
            np_.setDepthTest(False, 1000)
            np_.setBin("fixed", 60, 1000)
            self._hit_debug.append(np_)

    def set_first_person(self, on: bool) -> None:
        """Collapse the head (and helmet, goggles) for a camera at this body's eyes."""
        if on != self.first_person:
            self.first_person = on
            self._apply(force=True)

    def _apply(self, force: bool = False) -> None:
        """Copy the pose to the hit box and weapon nodes (every tick: gameplay), and solve the
        skinning palette when it is due (``_palette_due``: drawing only)."""
        P = self.pose
        if P.dirty:
            hpr, pos = P.hpr.tolist(), P.pos.tolist()
            nodes = self.nodes
            for i in P.dirty:
                node = nodes.get(i)
                if node is not None:
                    node.setPosHpr(*pos[i], *hpr[i])
            idx = list(P.dirty)
            self._node_hpr[idx] = P.hpr[idx]           # what the nodes hold (the exact capsule test)
            self._node_pos[idx] = P.pos[idx]
            P.dirty.clear()
        if self._bones is None:
            return
        if not force and not self._palette_due():
            self._palette_stale = True
            return
        self._solve_palette()

    # ------------------------------------------------------------ exact hit capsules
    def _snapshot(self) -> None:
        """Just before the physics step, where Bullet syncs the kinematic hit capsules from the
        scene graph: keep that pose, so the exact test checks the capsules shots see (shots test
        the pose of the last step)."""
        self._step_pose = (self.root.getMat(), self._node_hpr.copy(), self._node_pos.copy())
        self._step_lists = None
        self._step_bones = {}
        self._step_ends = {}

    def _exact_capsule(self, c, start: Point3, end: Point3):
        if self._step_pose is None:
            self._snapshot()                       # created since the last step: Bullet has its pose now
        ends = self._step_ends.get(c.name)
        if ends is None:
            m = _CAPSULE_MOUNTS[c.name] * self._step_bone(B[c.bone])
            half = _CAPSULE_HALF[c.name]
            a, b = m.xformPoint(Point3(0, 0, -half)), m.xformPoint(Point3(0, 0, half))
            ends = (a[0], a[1], a[2]), (b[0], b[1], b[2])
            self._step_ends[c.name] = ends
        return segment_capsule(start, end, ends[0], ends[1], c.radius)

    def _step_bone(self, i: int):
        """Bone ``i`` in world space at the last step, composed the way the nodes are (Panda's
        own transforms), only along the chain a capsule needs."""
        m = self._step_bones.get(i)
        if m is None:
            if self._step_lists is None:
                self._step_lists = (self._step_pose[1].tolist(), self._step_pose[2].tolist())
            hpr, pos = self._step_lists
            p = int(sk.PARENT[i])
            parent = self._step_bone(p) if p >= 0 else self._step_pose[0]
            m = TransformState.makePosHpr(Point3(*pos[i]), Vec3(*hpr[i])).getMat() * parent
            self._step_bones[i] = m
        return m

    def _solve_palette(self) -> None:
        world = self.pose.solve()
        if self.first_person:
            world = world.copy()
            world[HEAD_BONES] = np.matmul(COLLAPSE, world[HEAD_BONES])
        sk.palette_rows(world, self._rows[:sk.N_BONES])
        memoryview(self._bones).cast("B")[:] = self._rows.tobytes()
        self._palette_stale = False

    def _palette_due(self) -> bool:
        """Animation LOD (OVERHAUL_PLAN 4.4): the skinning palette is only for drawing, so it is
        solved every tick within 14 m of the camera, every second tick to 40 m and every fourth
        beyond or behind the camera (where only its shadow can show). The hit box nodes are posed
        every tick regardless, so gameplay never depends on the view."""
        self._lod_n += 1
        cam = getattr(self.game, "camera", None)
        if cam is None or self.first_person:
            return True
        rel = self.root.getPos(cam)                 # camera space: +y is ahead
        d2 = rel.lengthSquared()
        if d2 > 9.0 and rel.y < -1.5:
            return self._lod_n % 4 == 0             # behind the camera: only its shadow can show
        if d2 < 14.0 * 14.0:
            return True
        return self._lod_n % (2 if d2 < 40.0 * 40.0 else 4) == 0

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
        self.pose.set_pos(B["weapon"], *HOLDS[self.hold]["pos"])
        gun = self.gun
        if model_key is None:
            self._grips = (Point3(0, 0.05, -0.05), Point3(0, 0.05, -0.05))
            self.muzzle = None
            return
        if self.visible:
            # third-person guns never animate their parts: one merged copy of a shared prototype,
            # and beyond 40 m (as the body's coarsest LOD) one Geom in one material
            lod = LODNode("weapon_lod")
            lod_np = gun.attachNewNode(lod)
            near = shared_weapon_model(self.game.materials, model_key, lod_np)
            lod.addSwitch(LOD_SWITCH[2][0], 0.0)
            shared_weapon_model(self.game.materials, model_key, lod_np, flatten="far")
            lod.addSwitch(LOD_SWITCH[2][1], LOD_SWITCH[2][0])
            near.root = lod_np                    # removing the model removes both levels
            self.weapon_model = near
            anchors = self.weapon_model.anchors
        else:
            from weapons.models import model_defs
            anchors = {k: Point3(*v) for k, v in model_defs()[model_key].get("anchors", {}).items()}
        self._grips = (Point3(anchors.get("grip_r", Point3())), Point3(anchors.get("grip_l", Point3())))
        if self.muzzle is None or self.muzzle.isEmpty():
            self.muzzle = gun.attachNewNode("muzzle")
        self.muzzle.setPos(anchors.get("muzzle", Point3(0, 0.5, 0)))

    def set_pack(self, on: bool) -> None:
        """The carried charge on the back (OVERHAUL_PLAN B-8), while ``on`` (the director's rule:
        attackers and omniscient spectators only) and the charge is not in the hands. Drawing
        only, one Geom: the carrier stays within the draw-call budget."""
        on = on and self.visible and self.alive and self.weapon_key != "bomb_charge"
        if on == self._pack_on:
            return
        self._pack_on = on
        if on and self.pack_model is None:
            self.pack_model = shared_weapon_model(self.game.materials, "bomb_charge", self.nodes[B["pack"]],
                                                  "charge_pack", flatten="far")
            self.pack_model.root.setPosHpr(*PACK_POS, *PACK_HPR)
        if self.pack_model is not None:
            if on:
                self.pack_model.root.show()
            else:
                self.pack_model.root.hide()

    def muzzle_pos(self) -> Point3:
        if self.muzzle is not None and not self.muzzle.isEmpty():
            return self.muzzle.getPos(self.game.render)
        return self.gun.getPos(self.game.render)

    # ------------------------------------------------------------ events
    def event(self, name: str, duration: float | None = None, direction: Vec3 | None = None) -> None:
        """Animation events: fire, reload, switch, throw, knife, plant, defuse (looping until
        ``stop``), hit (a flinch away from ``direction``, world space)."""
        if name == "fire":
            self.on_fire()
            return
        if name == "hit":
            self._flinch(direction)
            return
        if name == "reload" and self.hold == "pistol":
            name = "reload_pistol"
        d = clips().get(name)
        if d is None:
            return
        if name in ("switch", "throw", "knife", "plant", "defuse"):
            # the hands are needed: an unfinished reload winds back quickly
            for other in ("reload", "reload_pistol"):
                c = self.active_clips.get(other)
                if c is not None and not c["stop"]:
                    c["stop"] = True
                    c["dur"] *= 0.25
        self.active_clips[name] = {"t": 0.0, "dur": max(float(duration or d["duration"]), 0.05),
                                   "loop": bool(d.get("loop", False)), "stop": False}
        self._sig = None

    def stop(self, name: str) -> None:
        """End a looping clip (it plays back to the start, then ends)."""
        c = self.active_clips.get(name)
        if c is not None:
            c["stop"] = True

    def _flinch(self, direction: Vec3 | None) -> None:
        """A short additive flinch, the same at any health."""
        if direction is None:
            self.flinch = (-6.0, 0.0, 0.3)
            return
        # the torso gives way along the shot: a shot from behind bends it forward (negative pitch)
        local = self.root.getRelativeVector(self.game.render, Vec3(direction))
        self.flinch = (-6.0 * (1.0 if local.y > 0 else -1.0) * min(abs(local.y) + 0.3, 1.0),
                       6.0 * local.x, 0.3)
        self._sig = None

    def _clip_channels(self, dt: float) -> dict:
        """Advance the active clips and sum their channels (hands: the last clip that sets one)."""
        out: dict = {}
        done = []
        for name, c in self.active_clips.items():
            keys = clips()[name]["keys"]
            if c["stop"]:
                c["t"] -= dt / c["dur"]
                if c["t"] <= 0.0:
                    done.append(name)
                    continue
            else:
                c["t"] += dt / c["dur"]
                if c["t"] >= 1.0:
                    if c["loop"]:
                        c["t"] = 1.0
                    else:
                        done.append(name)
                        continue
            for ch in ("weapon_p", "weapon_r", "weapon_h", "weapon_x", "weapon_y", "weapon_z", "spine_p", "neck_p",
                       "crouch"):
                v = _channel(keys, ch, c["t"])
                if v is not None:
                    out[ch] = max(out.get(ch, 0.0), v) if ch == "crouch" else out.get(ch, 0.0) + v
            for ch in ("hand_l", "hand_r"):
                v = _channel(keys, ch, c["t"])
                if v is not None:
                    out[ch] = v
        for name in done:
            del self.active_clips[name]
        return out

    def on_fire(self, strength: float = 1.0) -> None:
        self.kick = min(self.kick + 0.6 * strength, 1.0)

    # ------------------------------------------------------------- state
    def place(self, pos, yaw: float) -> None:
        self.root.setPos(Point3(*pos))
        self.root.setH(yaw)

    def reset(self) -> None:
        self._sig = None
        self.active_clips = {}
        self.flinch = (0.0, 0.0, 0.0)
        self.dead_t = -1.0
        self._drop_ragdoll()
        self.alive = True
        self.pose.set_hpr(B["root"], 0, 0, 0)
        self.pose.set_pos(B["root"], 0, 0, 0)
        self.flash = 0.0
        self._apply(force=True)
        if self.visible:
            self.root.show()
            self.root.setShaderInput("u_emission", LVecBase4f(0, 0, 0, 0))
        if self.weapon_model is not None:
            self.weapon_model.root.show()
        self.rig.set_enabled(True)
        self._snapshot()

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
        if self._bones is not None and getattr(self.game, "physics", None) is not None:
            from gameplay.ragdoll import Ragdoll
            world = self.pose.solve().copy()
            self._death_world = world
            # each bone's matrix relative to its parent at death: the bones no capsule drives
            # keep it and ride their parent
            par = np.maximum(sk.PARENT, 0)
            self._death_local = np.matmul(world, np.linalg.inv(world[par]))
            impulse = None
            if direction is not None and Vec3(direction).length() > 1e-6:
                impulse = Vec3(direction).normalized() * 9.0
            self.ragdoll = Ragdoll(self.game.physics, self.root, world, Vec3(self._vel), impulse)

    def _drop_ragdoll(self) -> None:
        if self.ragdoll is not None:
            self.ragdoll.remove()
            self.ragdoll = None

    def _ragdoll_pose(self) -> None:
        """The skinning palette from the ragdoll's capsules."""
        rd = self.ragdoll
        drive = rd._last if rd.frozen else rd.bone_worlds()
        world = self._death_world.copy()
        local = self._death_local
        for i, b in enumerate(sk.BONES):
            m = drive.get(b.name)
            if m is not None:
                world[i] = m
            elif i > 0:
                world[i] = local[i] @ world[sk.PARENT[i]]
        sk.palette_rows(world, self._rows[:sk.N_BONES])
        memoryview(self._bones).cast("B")[:] = self._rows.tobytes()

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
            if self.ragdoll is not None:
                if not self.ragdoll.frozen:
                    self.ragdoll.update(dt)
                    self._ragdoll_pose()
            elif self.dead_t < 1.0:
                self._animate_death(dt)
                self._apply()
            return
        self._vel = vel
        # nothing changed (standing still, same view): the pose and hit boxes are current
        speed = math.hypot(vel.x, vel.y)
        sig = (round(pos[0], 3), round(pos[1], 3), round(pos[2], 3), round(yaw, 1), round(pitch, 1),
               round(crouch, 2), round(lean, 2), self.hold, self.weapon_key)
        if sig == self._sig and speed < 0.05 and self.move_weight < 0.01 and self.kick <= 0.0 and self.flash <= 0 \
                and not self.active_clips and self.flinch[2] <= 0.0:
            if self._palette_stale and self._palette_due():
                self._solve_palette()            # the animation LOD skipped the last change
            return
        self._sig = sig
        ch = self._clip_channels(dt) if self.active_clips else {}
        crouch = max(crouch, ch.get("crouch", 0.0))
        fp, fr, ft = self.flinch
        if ft > 0.0:
            k = ft / 0.3
            self.flinch = (fp, fr, max(ft - dt, 0.0))
            fp, fr = fp * k, fr * k
        else:
            fp = fr = 0.0
        self.crouch = crouch
        self.aim_pitch = max(-80.0, min(80.0, pitch))
        moving = min(speed / 2.0, 1.0) if on_ground else 0.0
        self.move_weight += (moving - self.move_weight) * min(dt * 10.0, 1.0)
        lean_amount = lean
        stride = 1.0 if crouch > 0.5 else (1.15 if walking else 1.6)
        self.phase = (self.phase + speed * dt / stride * math.pi) % (2 * math.pi)
        self.kick = max(self.kick - dt * 8.0, 0.0)
        P = self.pose
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
        P.set_pos(B["pelvis"], 0.0, 0.0, STAND_PELVIS + (CROUCH_PELVIS - STAND_PELVIS) * crouch - bob)
        P.set_hpr(B["pelvis"], 0.0, 0.0, vs * s * 4.0 * w)
        lean = -12.0 * crouch - 4.0 * w * vf
        a = self.aim_pitch
        roll = Lean.body_roll(lean_amount)
        P.set_hpr(B["spine_01"], 0.0, lean + a * 0.25 + fp, roll + fr)
        P.set_hpr(B["spine_03"], 0.0, a * 0.30 - self.kick * 3.0 + ch.get("spine_p", 0.0))
        P.set_hpr(B["neck"], 0.0, a * 0.45 - lean + ch.get("neck_p", 0.0), -roll * 0.35)
        hold = HOLDS[self.hold]["pos"]
        P.set_pos(B["weapon"], hold[0] + ch.get("weapon_x", 0.0), hold[1] - self.kick * 0.03 + ch.get("weapon_y", 0.0),
                  hold[2] + ch.get("weapon_z", 0.0))
        gun_p = a * 0.45 + self.kick * 4.0 + ch.get("weapon_p", 0.0)
        gun_h, gun_r = ch.get("weapon_h", 0.0), ch.get("weapon_r", 0.0)
        P.set_hpr(B["weapon"], gun_h, gun_p, gun_r)
        hip_base = 80.0 * crouch
        knee_base = -95.0 * crouch
        for side, sign in (("l", 1.0), ("r", -1.0)):
            ss = s * sign
            swing = ss * amp * w
            P.set_hpr(B[f"thigh_{side}"], 0.0, hip_base + swing * vf, -swing * vs * 0.6)
            lift = max(0.0, math.sin(self.phase * 1.0 + (0 if sign > 0 else math.pi) + 1.2))
            P.set_hpr(B[f"calf_{side}"], 0.0, knee_base - lift * (40.0 if crouch < 0.5 else 18.0) * w)
        self._pose_arms(gun_h, gun_p, gun_r, ch.get("hand_l"), ch.get("hand_r"))
        self._apply()
        if self.flash > 0:
            self.flash -= dt
            k = max(self.flash, 0.0) / 0.1
            if self.visible:
                self.root.setShaderInput("u_emission", LVecBase4f(0.45 * k, 0.04 * k, 0.02 * k, 0))

    def _pose_arms(self, gun_h: float, gun_p: float, gun_r: float, clip_l=None, clip_r=None) -> None:
        """Two-bone IK from the shoulders onto the weapon grips (or a clip's targets), all in
        spine_03 space."""
        P = self.pose
        g = P.pos[B["weapon"]]
        rot = sk.hpr_matrix(gun_h, gun_p, gun_r)

        def gun_point(a) -> Vec3:
            v = np.asarray((a[0], a[1], a[2]), np.float64) @ rot
            return Vec3(g[0] + v[0], g[1] + v[1], g[2] + v[2])

        def resolve(target, default: Vec3) -> Vec3:
            if target is None:
                return default
            space, x, y, z = target
            return gun_point((x, y, z)) if space == "gun" else Vec3(x, y, z)

        def blend(clip, default: Vec3) -> Vec3:
            if clip is None:
                return default
            a, b, w = clip
            pa, pb = resolve(a, default), resolve(b, default)
            return pa + (pb - pa) * w

        hand_r = gun_point(self._grips[0])
        if HOLDS[self.hold]["hands"] == 2:
            hand_l = gun_point(self._grips[1])
        else:
            hand_l = Vec3(-0.22, 0.12, -0.32)
        hand_l, hand_r = blend(clip_l, hand_l), blend(clip_r, hand_r)
        for side, sx, hand in (("l", -1.0, hand_l), ("r", 1.0, hand_r)):
            s = Vec3(SHOULDER[0] * sx, SHOULDER[1], SHOULDER[2])
            elbow = two_bone_elbow(s, hand, UPPER_ARM, FOREARM, Vec3(sx * 0.8, -0.3, -1.0))
            # the clavicle is not rotated, so the upper arm aims in spine_03 space
            upper = sk.aim_hpr(elbow - s)
            P.set_hpr(B[f"upperarm_{side}"], *upper)
            P.set_hpr(B[f"lowerarm_{side}"], *sk.relative_hpr(upper, sk.aim_hpr(hand - elbow)))

    def _animate_death(self, dt: float) -> None:
        if self.dead_t >= 1.0:
            return
        self.dead_t = min(self.dead_t + dt * 1.8, 1.0)
        t = self.dead_t * self.dead_t
        P = self.pose
        P.set_hpr(B["root"], 0.0, 86.0 * t * self.fall_dir)
        P.set_pos(B["root"], 0.0, 0.0, 0.13 * t)
        P.set_pos(B["pelvis"], 0.0, 0.0,
                  STAND_PELVIS + (CROUCH_PELVIS - STAND_PELVIS) * min(self.crouch + t * 0.3, 1.0))
        for side in ("l", "r"):
            P.set_hpr(B[f"thigh_{side}"], 0.0, 40.0 * t * (1 if self.fall_dir < 0 else 0.4))
            P.set_hpr(B[f"calf_{side}"], 0.0, -60.0 * t)
        P.set_hpr(B["spine_03"], 0.0, -10.0 * t * self.fall_dir)

    def destroy(self) -> None:
        self._drop_ragdoll()
        if self._hooks is not None and self._snapshot in self._hooks:
            self._hooks.remove(self._snapshot)
        for part, np_ in self.rig.parts:
            np_.node().clearPythonTag(EXACT_RAY)
        self.rig.destroy()
        self.root.removeNode()
