"""Death ragdolls (OVERHAUL_PLAN 4.5): about a dozen Bullet capsules with joint limits,
started from the pose the soldier died in.

Visual only:
* the bodies are in the debris collision group, which collides with the
  static world (and other debris) but is never in a movement, sight or
  bullet mask, so a ragdoll cannot block anyone, stop a bullet or change a
  line of sight (dead bodies never did), and gameplay stays reproducible
  with ``--seed``;
* the skeleton follows the capsules: each capsule drives one bone, the bones
  between them keep their last local pose;
* once everything has been slow for a second the ragdoll freezes: its bodies
  leave the simulation and the pose stays until the round resets.

Joints: cone-twist at the neck, spine, shoulders and hips (swing and twist
limits like a human's), hinges at the elbows and knees (one way only).
"""
from __future__ import annotations

import math

import numpy as np
from panda3d.bullet import BulletCapsuleShape, BulletConeTwistConstraint, BulletHingeConstraint, BulletRigidBodyNode
from panda3d.core import BitMask32, LMatrix4f, Point3, TransformState, Vec3

from engine.physics import GROUP_DEBRIS
from gameplay import skeleton as sk

B = sk.INDEX

# capsule per driving bone: (bone, child landmark bone or None + length, radius, mass)
PARTS = (
    ("pelvis", "spine_02", 0.11, 12.0),
    ("spine_02", "neck", 0.13, 14.0),
    ("head", None, 0.09, 4.5),
    ("upperarm_l", "lowerarm_l", 0.05, 2.5),
    ("upperarm_r", "lowerarm_r", 0.05, 2.5),
    ("lowerarm_l", "hand_l", 0.042, 1.8),
    ("lowerarm_r", "hand_r", 0.042, 1.8),
    ("thigh_l", "calf_l", 0.07, 8.0),
    ("thigh_r", "calf_r", 0.07, 8.0),
    ("calf_l", "foot_l", 0.055, 4.5),
    ("calf_r", "foot_r", 0.055, 4.5),
)
HEAD_LENGTH = 0.18

# (parent part, child part, kind, limits): cone-twist (swing1, swing2, twist) or hinge (low, high) in degrees
JOINTS = (
    ("pelvis", "spine_02", "cone", (25, 25, 15)),
    ("spine_02", "head", "cone", (40, 40, 45)),
    ("spine_02", "upperarm_l", "cone", (80, 80, 50)),
    ("spine_02", "upperarm_r", "cone", (80, 80, 50)),
    ("upperarm_l", "lowerarm_l", "hinge", (0, 140)),
    ("upperarm_r", "lowerarm_r", "hinge", (0, 140)),
    ("pelvis", "thigh_l", "cone", (70, 35, 20)),
    ("pelvis", "thigh_r", "cone", (70, 35, 20)),
    ("thigh_l", "calf_l", "hinge", (-140, 0)),
    ("thigh_r", "calf_r", "hinge", (-140, 0)),
)


def _mat_to_np(m: LMatrix4f) -> np.ndarray:
    return np.array([[m.getCell(r, c) for c in range(4)] for r in range(4)])


def _np_to_mat(a: np.ndarray) -> LMatrix4f:
    return LMatrix4f(*a.ravel().tolist())


class Ragdoll:
    SETTLE_SPEED = 0.25        # m/s and rad/s
    SETTLE_TIME = 1.0
    MAX_TIME = 6.0

    def __init__(self, physics, root, world: np.ndarray, velocity: Vec3, impulse: Vec3 | None = None):
        """``root``: the body's root NodePath (the capsules live in render space, the result is read
        back relative to it); ``world``: the bones' model matrices at death (Pose.world)."""
        self.physics = physics
        self.root = root
        self.render = root.getTop()
        self.bodies: dict[str, object] = {}
        self.offsets: dict[str, np.ndarray] = {}     # capsule frame relative to its bone
        self.constraints = []
        self.time = 0.0
        self.calm = 0.0
        self.frozen = False
        root_mat = _mat_to_np(root.getMat(self.render))
        for bone, child, radius, mass in PARTS:
            bw = world[B[bone]]
            a = bw[3, :3]
            if child is not None:
                b = world[B[child]][3, :3]
            else:
                b = a + bw[2, :3] * HEAD_LENGTH          # the head points up its local z
            axis = b - a
            length = max(float(np.linalg.norm(axis)), 2 * radius + 0.01)
            centre = (a + b) * 0.5
            # capsule frame: z along the segment, centred on it (Bullet capsules run along z)
            z = axis / max(np.linalg.norm(axis), 1e-6)
            x = np.cross(bw[1, :3] if abs(np.dot(bw[1, :3], z)) < 0.9 else bw[0, :3], z)
            x /= np.linalg.norm(x)
            y = np.cross(z, x)
            cap = np.eye(4)
            cap[0, :3], cap[1, :3], cap[2, :3], cap[3, :3] = x, y, z, centre
            self.offsets[bone] = cap @ np.linalg.inv(bw)          # bone -> capsule (both model space)
            node = BulletRigidBodyNode(f"ragdoll:{bone}")
            node.addShape(BulletCapsuleShape(radius, max(length - 2 * radius, 0.01), 2))
            node.setMass(mass)
            node.setIntoCollideMask(GROUP_DEBRIS)
            node.setFriction(0.9)
            node.setRestitution(0.05)
            node.setLinearDamping(0.05)
            node.setAngularDamping(0.4)
            np_ = self.render.attachNewNode(node)
            np_.setMat(self.render, _np_to_mat(cap @ root_mat))
            node.setLinearVelocity(velocity)
            if impulse is not None and bone in ("spine_02", "head", "pelvis"):
                node.applyCentralImpulse(impulse * (mass / 30.0))
            node.setDeactivationEnabled(False)
            physics.world.attachRigidBody(node)
            self.bodies[bone] = np_
        for pa, ch, kind, lim in JOINTS:
            self._joint(pa, ch, kind, lim, world)

    def _joint(self, pa: str, ch: str, kind: str, lim: tuple, world: np.ndarray) -> None:
        a, b = self.bodies[pa], self.bodies[ch]
        pivot = Point3(*world[B[ch]][3, :3])                      # at the child's joint, model space
        pivot_w = self.render.getRelativePoint(self.root, pivot)
        fa = TransformState.makePos(a.getRelativePoint(self.render, pivot_w))
        fb = TransformState.makePos(b.getRelativePoint(self.render, pivot_w))
        if kind == "cone":
            c = BulletConeTwistConstraint(a.node(), b.node(), fa, fb)
            c.setLimit(*lim, softness=0.9, bias=0.3, relaxation=1.0)
        else:
            c = BulletHingeConstraint(a.node(), b.node(), fa, fb)
            c.setLimit(*lim, softness=0.9, bias=0.3, relaxation=1.0)
        c.setDebugDrawSize(0.0)
        self.physics.world.attachConstraint(c, True)
        self.constraints.append(c)

    def update(self, dt: float) -> bool:
        """Advance the settle timer; True while the pose is still changing."""
        if self.frozen:
            return False
        self.time += dt
        fast = any(np_.node().getLinearVelocity().length() > self.SETTLE_SPEED
                   or np_.node().getAngularVelocity().length() > self.SETTLE_SPEED * 4
                   for np_ in self.bodies.values())
        self.calm = 0.0 if fast else self.calm + dt
        if self.calm > self.SETTLE_TIME or self.time > self.MAX_TIME:
            self.freeze()
        return True

    def bone_worlds(self) -> dict[str, np.ndarray]:
        """Model-space matrices (relative to the body root) of the driving bones."""
        inv_root = np.linalg.inv(_mat_to_np(self.root.getMat(self.render)))
        out = {}
        for bone, np_ in self.bodies.items():
            cap = _mat_to_np(np_.getMat(self.render)) @ inv_root
            out[bone] = np.linalg.inv(self.offsets[bone]) @ cap
        return out

    def freeze(self) -> None:
        if self.frozen:
            return
        self.frozen = True
        self._last = self.bone_worlds()
        self.remove()

    def remove(self) -> None:
        for c in self.constraints:
            self.physics.world.removeConstraint(c)
        self.constraints = []
        for np_ in self.bodies.values():
            self.physics.world.removeRigidBody(np_.node())
            np_.removeNode()
        self.bodies = {}
