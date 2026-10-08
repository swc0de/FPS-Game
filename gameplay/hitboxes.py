"""Per-body-part hitboxes and a matching mannequin body.

Characters (target dummies now, bots in milestone 5) carry a rig of
kinematic Bullet shapes, one per body part, in the GROUP_HITBOX collision
group. Bullets ray-test against them; every part is tagged with its hit
group (head/chest/stomach/arm/leg) and a python tag pointing at its owner.

Each part has a standing and a crouching transform (relative to the feet,
character facing +Y); the rig blends between them so hitboxes always match
the visible body.
"""
from __future__ import annotations

from dataclasses import dataclass

from panda3d.bullet import BulletBoxShape, BulletCapsuleShape, BulletRigidBodyNode, BulletSphereShape, ZUp
from panda3d.core import NodePath, Point3, TransformState, Vec3

from engine.geometry import MeshBuilder
from engine.physics import GROUP_HITBOX, PhysicsWorld


@dataclass(frozen=True)
class Part:
    name: str
    hitgroup: str
    shape: str               # "box" | "sphere"
    size: tuple              # full extents (box) or (radius,) (sphere)
    stand: tuple             # (x, y, z, h, p, r)
    crouch: tuple


# Pose: neutral standing (arms slightly forward) and a crouch.
PARTS: tuple[Part, ...] = (
    Part("head", "head", "sphere", (0.112,), (0, 0.01, 1.635, 0, 0, 0), (0, 0.13, 1.08, 0, 0, 0)),
    Part("neck", "chest", "box", (0.11, 0.11, 0.12), (0, 0, 1.50, 0, 0, 0), (0, 0.10, 0.95, 0, -15, 0)),
    Part("chest", "chest", "box", (0.40, 0.24, 0.32), (0, 0, 1.31, 0, 0, 0), (0, 0.08, 0.78, 0, -18, 0)),
    Part("stomach", "stomach", "box", (0.34, 0.21, 0.22), (0, 0, 1.04, 0, 0, 0), (0, 0.03, 0.56, 0, -10, 0)),
    Part("pelvis", "stomach", "box", (0.36, 0.22, 0.16), (0, 0, 0.87, 0, 0, 0), (0, -0.03, 0.42, 0, 0, 0)),
    Part("upper_arm_l", "arm", "box", (0.11, 0.11, 0.30), (-0.265, 0.02, 1.29, 0, 8, -5),
         (-0.265, 0.13, 0.75, 0, 25, -5)),
    Part("upper_arm_r", "arm", "box", (0.11, 0.11, 0.30), (0.265, 0.02, 1.29, 0, 8, 5),
         (0.265, 0.13, 0.75, 0, 25, 5)),
    Part("forearm_l", "arm", "box", (0.095, 0.095, 0.30), (-0.285, 0.07, 1.00, 0, 18, -3),
         (-0.27, 0.30, 0.62, 0, 70, -3)),
    Part("forearm_r", "arm", "box", (0.095, 0.095, 0.30), (0.285, 0.07, 1.00, 0, 18, 3),
         (0.27, 0.30, 0.62, 0, 70, 3)),
    Part("thigh_l", "leg", "box", (0.16, 0.17, 0.43), (-0.105, 0, 0.62, 0, 0, 0),
         (-0.11, 0.20, 0.44, 0, -78, 0)),
    Part("thigh_r", "leg", "box", (0.16, 0.17, 0.43), (0.105, 0, 0.62, 0, 0, 0),
         (0.11, 0.20, 0.44, 0, -78, 0)),
    Part("calf_l", "leg", "box", (0.13, 0.14, 0.42), (-0.105, 0, 0.22, 0, 0, 0),
         (-0.11, 0.36, 0.22, 0, 8, 0)),
    Part("calf_r", "leg", "box", (0.13, 0.14, 0.42), (0.105, 0, 0.22, 0, 0, 0),
         (0.11, 0.36, 0.22, 0, 8, 0)),
)


@dataclass(frozen=True)
class Capsule:
    """A hit capsule on a bone of the game skeleton (gameplay/skeleton.py): its axis from ``a``
    to ``b`` (model space, rest pose; ``b`` None = a sphere at ``a``), ``radius`` around it."""
    name: str
    hitgroup: str
    bone: str
    a: tuple
    b: tuple | None
    radius: float

    @property
    def shape(self) -> str:
        return "sphere" if self.b is None else "capsule"


def _capsules() -> tuple:
    """The soldiers' hit boxes (Milestone 9, OVERHAUL_PLAN 4.1 and B5): capsules along the
    bones, a sphere for the head. One set for every appearance. Sizes fitted to the visible
    soldier (tools/soldier_sheet.py --masks, tools/fit_hitboxes.py): the torso as three
    wide capsules across the body (chest, stomach, pelvis), the limbs along their bones; no hit
    boxes on the hands and feet (as before)."""
    from gameplay import skeleton as sk
    P = lambda n: tuple(float(x) for x in sk.REST_WORLD[sk.INDEX[n], 3, :3])
    out = [
        Capsule("head", "head", "head", (0.0, 0.01, 1.635), None, 0.112),
        Capsule("neck", "chest", "neck", P("neck"), (0.0, 0.005, P("neck")[2] + 0.085), 0.06),
        Capsule("chest", "chest", "spine_03", (-0.045, 0.0, 1.33), (0.045, 0.0, 1.33), 0.17),
        Capsule("stomach", "stomach", "spine_01", (-0.025, 0.0, 1.06), (0.025, 0.0, 1.06), 0.125),
        Capsule("pelvis", "stomach", "pelvis", (-0.09, 0.0, 0.89), (0.09, 0.0, 0.89), 0.13),
    ]
    for s in ("l", "r"):
        out += [
            Capsule(f"upper_arm_{s}", "arm", f"upperarm_{s}", P(f"upperarm_{s}"), P(f"lowerarm_{s}"), 0.055),
            Capsule(f"forearm_{s}", "arm", f"lowerarm_{s}", P(f"lowerarm_{s}"), P(f"hand_{s}"), 0.045),
            Capsule(f"thigh_{s}", "leg", f"thigh_{s}", P(f"thigh_{s}"), P(f"calf_{s}"), 0.085),
            Capsule(f"calf_{s}", "leg", f"calf_{s}", P(f"calf_{s}"), P(f"foot_{s}"), 0.065),
        ]
    return tuple(out)


CAPSULES: tuple = _capsules()


def capsule_mount(c: Capsule):
    """The capsule's frame relative to its bone (an LMatrix4f): centred on the axis, local z
    along it (Bullet's capsules run along z)."""
    import numpy as np
    from panda3d.core import LMatrix4f
    from gameplay import skeleton as sk
    inv = np.linalg.inv(sk.REST_WORLD[sk.INDEX[c.bone]])
    a = (np.append(c.a, 1.0) @ inv)[:3]
    b = (np.append(c.b, 1.0) @ inv)[:3] if c.b is not None else a + np.array([0.0, 0.0, 1e-3])
    z = (b - a) / max(np.linalg.norm(b - a), 1e-9)
    hint = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    y = np.cross(z, hint)
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    c0 = (a + b) * 0.5 if c.b is not None else a
    return LMatrix4f(*x, 0.0, *y, 0.0, *z, 0.0, *c0, 1.0)


def capsule_length(c: Capsule) -> float:
    import numpy as np
    return 0.0 if c.b is None else float(np.linalg.norm(np.subtract(c.b, c.a)))


def part_transform(part: Part, crouch: float) -> TransformState:
    a, b = part.stand, part.crouch
    v = [a[i] + (b[i] - a[i]) * crouch for i in range(6)]
    return TransformState.makePosHpr(Point3(v[0], v[1], v[2]), Vec3(v[3], v[4], v[5]))


class HitboxRig:
    """``parents`` (part name -> NodePath): attach each hit box under an
    animated body part instead of posing it from ``update``; Panda3D's Bullet
    integration then moves the kinematic boxes with the scene graph during
    the physics step (no per-tick Python work)."""

    def __init__(self, physics: PhysicsWorld, owner, surface: str = "flesh", parents: dict | None = None,
                 parts: tuple | None = None):
        self.physics = physics
        self.owner = owner
        self.parts: list[tuple[Part, NodePath]] = []
        self.enabled = False
        self.parented = parents is not None
        for part in parts or PARTS:
            node = BulletRigidBodyNode(f"hitbox:{part.name}")
            if isinstance(part, Capsule):
                if part.b is None:
                    node.addShape(BulletSphereShape(part.radius))
                else:
                    node.addShape(BulletCapsuleShape(part.radius, capsule_length(part), ZUp))
            elif part.shape == "sphere":
                node.addShape(BulletSphereShape(part.size[0]))
            else:
                node.addShape(BulletBoxShape(Vec3(*part.size) * 0.5))
            node.setKinematic(True)
            node.setIntoCollideMask(GROUP_HITBOX)
            node.setTag("hitgroup", part.hitgroup)
            node.setTag("part", part.name)
            node.setTag("surface", surface)
            node.setPythonTag("owner", owner)
            np_ = (parents[part.name] if parents is not None else physics.root).attachNewNode(node)
            self.parts.append((part, np_))
        self.set_enabled(True)

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self.enabled:
            return
        for _, np_ in self.parts:
            if enabled:
                self.physics.world.attachRigidBody(np_.node())
            else:
                self.physics.world.removeRigidBody(np_.node())
        self.enabled = enabled

    def update(self, root_transform: TransformState, crouch: float = 0.0) -> None:
        for part, np_ in self.parts:
            np_.setTransform(root_transform.compose(part_transform(part, crouch)))

    def destroy(self) -> None:
        self.set_enabled(False)
        for _, np_ in self.parts:
            np_.removeNode()
        self.parts = []


def build_mannequin(materials, parent: NodePath, body_mat: str = "dummy_polymer",
                    joint_mat: str = "steel", helmet: bool = False, vest: bool = False) -> dict[str, NodePath]:
    """Visual body matching PARTS: one node per part so the pose can blend."""
    nodes = {}
    body = materials.get(body_mat)
    for part in PARTS:
        mb = MeshBuilder()
        if part.shape == "sphere":
            r = part.size[0]
            mb.add_sphere((0, 0, 0), r * 0.98, rings=14, segments=24, uv_scale=body.uv_scale)
        else:
            sx, sy, sz = part.size
            mb.add_chamfer_box((0, 0, 0), (sx * 0.96, sy * 0.96, sz * 0.97), bevel=min(sx, sy) * 0.3,
                               uv_scale=body.uv_scale)
        np_ = parent.attachNewNode(mb.build(f"body:{part.name}"))
        body.apply(np_)
        nodes[part.name] = np_
    if helmet:
        mb = MeshBuilder()
        mb.add_sphere((0, 0, 0.02), 0.135, rings=10, segments=20)
        mb2 = MeshBuilder()
        mb2.add_chamfer_box((0, 0.0, -0.01), (0.27, 0.27, 0.04), bevel=0.015)
        hm = materials.get("metal_olive")
        for m in (mb, mb2):
            h = nodes["head"].attachNewNode(m.build("helmet"))
            h.setScale(1, 1, 0.75) if m is mb else None
            hm.apply(h)
    if vest:
        mb = MeshBuilder()
        mb.add_chamfer_box((0, 0, 0), (0.43, 0.29, 0.34), bevel=0.03)
        mb.add_chamfer_box((0, 0.155, -0.05), (0.32, 0.04, 0.16), bevel=0.012)
        v = nodes["chest"].attachNewNode(mb.build("vest"))
        materials.get("canvas").apply(v)
    return nodes


def pose_mannequin(nodes: dict[str, NodePath], crouch: float) -> None:
    for part in PARTS:
        np_ = nodes.get(part.name)
        if np_ is not None:
            np_.setTransform(part_transform(part, crouch))
