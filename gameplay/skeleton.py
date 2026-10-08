"""The game skeleton shared by every soldier appearance (Milestone 9).

47 bones (docs/OVERHAUL_PLAN.md 4.1)::

    root (death fall pivot at the feet)
     +-> pelvis -> spine_01 -> spine_02 -> spine_03 -> neck -> head
     |                                       +-> clavicle -> upperarm -> lowerarm -> hand -> 3 finger chains
     |                                       |                +-> twist     +-> twist        (thumb, index,
     |                                       |                                                 the other three)
     |                                       +-> weapon, pack
     +-> thigh -> calf -> foot -> toe            (both legs hang off the pelvis)

Conventions, as everywhere in the game: the character faces +Y, Z is up and
the origin is between the feet. Matrices are Panda3D's row-vector 4x4 (a
point moves as ``p @ M``, the translation is the last row, and a child's
model matrix is ``local @ parent``), so they convert to ``LMatrix4f`` as is.

Bone axes: the spine, neck and head point up (+Z); the arms, hands and
fingers run along +Y of their frame, the legs down (-Z), the feet forward
(+Y). In the rest pose (an A-pose, the bind pose of every body mesh) the
back of each hand faces outwards (+Z) and its thumb is on +X of the left
hand and -X of the right, so a finger curls towards the palm with a negative
pitch on both sides.

A ``Pose`` holds every bone's local heading/pitch/roll and translation.
Two things consume it:
* the skinning palette: ``solve()`` turns all 47 into matrices at once and
  runs forward kinematics by pointer jumping (four batched matmuls), and
  ``palette_rows()`` gives the matrices for render/shaders/skinning.glsl.
  Reading 47 nested NodePaths back instead costs about 0.5 ms per soldier;
* the hit boxes and the weapon: gameplay/body.py mirrors the few bones they
  hang on as NodePaths and copies their local transforms over, so Panda
  does that forward kinematics in C++ and invisible bodies never solve.
Both come from the same numbers, so what you see is what you hit (tested).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Bone:
    name: str
    parent: str            # "" for the root
    pos: tuple             # rest translation in the parent's frame
    hpr: tuple = (0.0, 0.0, 0.0)   # rest heading, pitch, roll in the parent's frame


UPPER_ARM = 0.30           # shoulder to elbow
LOWER_ARM = 0.25           # elbow to wrist
PALM = 0.07                # wrist to the grip point in the palm
STAND_PELVIS = 0.90
A_POSE = 40.0              # rest angle of the arms from vertical


def _seg_hpr(d) -> tuple:
    """Heading and pitch that aim local +Y along d (as weapons.models.segment_hpr)."""
    dx, dy, dz = d
    return math.degrees(math.atan2(-dx, dy)), math.degrees(math.atan2(dz, math.hypot(dx, dy))), 0.0


def _arm(side: str, sx: float) -> list:
    """Clavicle, arm, twist bones, hand and three finger chains. ``sx`` is -1 left, +1 right;
    ``t`` puts the thumb side of the hand forwards on both sides (see the module docstring)."""
    t = -sx
    a = math.radians(A_POSE)
    arm = _seg_hpr((math.sin(a) * sx, 0.05, -math.cos(a)))
    bones = [
        Bone(f"clavicle_{side}", "spine_03", (0.03 * sx, 0.0, 0.22)),
        Bone(f"upperarm_{side}", f"clavicle_{side}", (0.18 * sx, 0.0, 0.02), arm),
        Bone(f"upperarm_twist_{side}", f"upperarm_{side}", (0.0, UPPER_ARM * 0.5, 0.0)),
        Bone(f"lowerarm_{side}", f"upperarm_{side}", (0.0, UPPER_ARM, 0.0)),
        Bone(f"lowerarm_twist_{side}", f"lowerarm_{side}", (0.0, LOWER_ARM * 0.5, 0.0)),
        Bone(f"hand_{side}", f"lowerarm_{side}", (0.0, LOWER_ARM, 0.0)),
    ]
    chains = (("thumb", (0.024 * t, 0.025, -0.012), (-28.0 * t, 0.0, 0.0), (0.040, 0.032)),
              ("index", (0.022 * t, 0.088, 0.0), (0.0, 0.0, 0.0), (0.045, 0.026)),
              ("fingers", (-0.012 * t, 0.090, 0.0), (0.0, 0.0, 0.0), (0.046, 0.029)))
    for name, root, hpr, (l1, l2) in chains:
        bones.append(Bone(f"{name}_01_{side}", f"hand_{side}", root, hpr))
        bones.append(Bone(f"{name}_02_{side}", f"{name}_01_{side}", (0.0, l1, 0.0)))
        bones.append(Bone(f"{name}_03_{side}", f"{name}_02_{side}", (0.0, l2, 0.0)))
    return bones


def _leg(side: str, sx: float) -> list:
    return [
        Bone(f"thigh_{side}", "pelvis", (0.105 * sx, 0.0, -0.06)),
        Bone(f"calf_{side}", f"thigh_{side}", (0.0, 0.0, -0.43)),
        Bone(f"foot_{side}", f"calf_{side}", (0.0, 0.0, -0.33)),
        Bone(f"toe_{side}", f"foot_{side}", (0.0, 0.14, -0.06)),
    ]


BONES: tuple[Bone, ...] = tuple([
    Bone("root", "", (0.0, 0.0, 0.0)),
    Bone("pelvis", "root", (0.0, 0.0, STAND_PELVIS)),
    Bone("spine_01", "pelvis", (0.0, 0.0, 0.08)),
    Bone("spine_02", "spine_01", (0.0, 0.0, 0.10)),
    Bone("spine_03", "spine_02", (0.0, 0.0, 0.10)),
    Bone("neck", "spine_03", (0.0, 0.0, 0.30)),
    Bone("head", "neck", (0.0, 0.01, 0.09)),
    *_arm("l", -1.0), *_arm("r", 1.0),
    *_leg("l", -1.0), *_leg("r", 1.0),
    Bone("weapon", "spine_03", (0.115, 0.17, 0.17)),
    Bone("pack", "spine_03", (0.0, -0.17, 0.10)),
])

N_BONES = len(BONES)
MAX_BONES = 48             # render/shaders/skinning.glsl: 3 vec4 rows per bone
INDEX = {b.name: i for i, b in enumerate(BONES)}
PARENT = np.array([INDEX[b.parent] if b.parent else -1 for b in BONES], np.int64)


def _levels() -> list:
    depth = [0] * N_BONES
    for i, p in enumerate(PARENT):
        assert p < i, "bones must come after their parent"
        depth[i] = 0 if p < 0 else depth[p] + 1
    return [np.array([i for i in range(N_BONES) if depth[i] == d], np.int64) for d in range(max(depth) + 1)]


LEVELS = _levels()


# ----------------------------------------------------------------- rotations
def hpr_matrix(h: float, p: float, r: float = 0.0) -> np.ndarray:
    """3x3 rotation for a Panda heading/pitch/roll (row vectors: roll, then pitch, then heading)."""
    ch, sh = math.cos(math.radians(h)), math.sin(math.radians(h))
    cp, sp = math.cos(math.radians(p)), math.sin(math.radians(p))
    cr, sr = math.cos(math.radians(r)), math.sin(math.radians(r))
    return np.array([[cr * ch - sr * sp * sh, cr * sh + sr * sp * ch, -sr * cp],
                     [-cp * sh, cp * ch, sp],
                     [sr * ch + cr * sp * sh, sr * sh - cr * sp * ch, cr * cp]])


def hpr_matrices(hpr: np.ndarray) -> np.ndarray:
    """``hpr_matrix`` for an (n, 3) array of degrees."""
    a = np.radians(np.asarray(hpr, np.float64))
    ch, sh = np.cos(a[:, 0]), np.sin(a[:, 0])
    cp, sp = np.cos(a[:, 1]), np.sin(a[:, 1])
    cr, sr = np.cos(a[:, 2]), np.sin(a[:, 2])
    m = np.empty((len(a), 3, 3))
    m[:, 0, 0] = cr * ch - sr * sp * sh
    m[:, 0, 1] = cr * sh + sr * sp * ch
    m[:, 0, 2] = -sr * cp
    m[:, 1, 0] = -cp * sh
    m[:, 1, 1] = cp * ch
    m[:, 1, 2] = sp
    m[:, 2, 0] = sr * ch + cr * sp * sh
    m[:, 2, 1] = sr * sh - cr * sp * ch
    m[:, 2, 2] = cr * cp
    return m


def aim_hpr(d) -> tuple:
    """Heading, pitch and roll that aim local +Y along d, with no roll (as ``segment_hpr``)."""
    return _seg_hpr(d)


def _rot(h: float, p: float, r: float) -> tuple:
    """``hpr_matrix`` as nested tuples (cheaper than numpy for one matrix)."""
    ch, sh = math.cos(math.radians(h)), math.sin(math.radians(h))
    cp, sp = math.cos(math.radians(p)), math.sin(math.radians(p))
    cr, sr = math.cos(math.radians(r)), math.sin(math.radians(r))
    return ((cr * ch - sr * sp * sh, cr * sh + sr * sp * ch, -sr * cp),
            (-cp * sh, cp * ch, sp),
            (sr * ch + cr * sp * sh, sr * sh - cr * sp * ch, cr * cp))


def matrix_hpr(m) -> tuple:
    """Heading, pitch and roll of a rotation matrix (the inverse of ``hpr_matrix``)."""
    p = math.asin(max(-1.0, min(1.0, m[1][2])))
    h = math.atan2(-m[1][0], m[1][1])
    r = math.atan2(-m[0][2], m[2][2])
    return math.degrees(h), math.degrees(p), math.degrees(r)


def relative_hpr(parent: tuple, child: tuple) -> tuple:
    """The local heading/pitch/roll of a bone whose rotation is ``child`` in some frame,
    under a parent whose rotation is ``parent`` in the same frame (child @ parent^T;
    only the three entries matrix_hpr reads are computed)."""
    (a0, a1, a2), (b0, b1, b2), (c0, c1, c2) = _rot(*child)
    (d0, d1, d2), (e0, e1, e2), (f0, f1, f2) = _rot(*parent)
    m02 = a0 * f0 + a1 * f1 + a2 * f2
    m10 = b0 * d0 + b1 * d1 + b2 * d2
    m11 = b0 * e0 + b1 * e1 + b2 * e2
    m12 = b0 * f0 + b1 * f1 + b2 * f2
    m22 = c0 * f0 + c1 * f1 + c2 * f2
    return matrix_hpr(((0.0, 0.0, m02), (m10, m11, m12), (0.0, 0.0, m22)))


def local_rotations(hpr: np.ndarray) -> np.ndarray:
    """(N_BONES, 3) heading/pitch/roll -> local rotation matrices."""
    return hpr_matrices(hpr)


REST_HPR = np.array([b.hpr for b in BONES], np.float64)
REST_POS = np.array([b.pos for b in BONES], np.float64)


def affine(rot: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """(n, 3, 3) rotations and (n, 3) translations -> (n, 4, 4) row-vector matrices."""
    m = np.zeros((len(rot), 4, 4))
    m[:, :3, :3] = rot
    m[:, 3, :3] = pos
    m[:, 3, 3] = 1.0
    return m


def _jumps() -> list:
    """Ancestor tables for pointer jumping: step k maps every bone to its 2^k-th ancestor
    (the extra index N_BONES is an identity "above the root")."""
    a = np.append(np.where(PARENT < 0, N_BONES, PARENT), N_BONES)
    out = [a]
    while 2 ** len(out) < len(LEVELS):
        out.append(out[-1][out[-1]])
    return out


JUMPS = _jumps()


def solve(local: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """Forward kinematics: (N_BONES, 4, 4) local matrices -> model-space ones.

    Pointer jumping: after step k every bone holds the product of its chain up to
    its 2^k-th ancestor, so ceil(log2(depth)) = 4 batched matmuls solve the whole
    skeleton (a loop over its 12 levels costs six times as much in numpy calls)."""
    m = np.empty((N_BONES + 1, 4, 4)) if out is None else out
    m[:N_BONES] = local
    m[N_BONES] = np.eye(4)
    for a in JUMPS:
        m = np.matmul(m, m[a])
    return m[:N_BONES]


REST_WORLD = solve(affine(local_rotations(REST_HPR), REST_POS))          # the bind pose
INV_BIND = np.linalg.inv(REST_WORLD)


def palette_rows(world: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """Skinning rows for the shader: for each bone the first three rows of the column-vector
    form of ``inv(bind) @ world`` (= the first three columns of the row-vector matrix),
    shape (N_BONES, 3, 4)."""
    m = np.matmul(INV_BIND, world)
    rows = np.ascontiguousarray(np.transpose(m[:, :, :3], (0, 2, 1)), np.float32)
    if out is not None:
        out[:] = rows
        return out
    return rows


def skin_points(points: np.ndarray, joints: np.ndarray, weights: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """CPU reference of render/shaders/skinning.glsl: blend the rows by weight and move the
    bind-pose points (n, 3). Used by the tests and the alignment checks."""
    r = np.einsum("nk,nkij->nij", weights, rows[joints.astype(np.int64)])
    ph = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    return np.einsum("nij,nj->ni", r, ph)


class Pose:
    """Every bone's local heading/pitch/roll and translation, posed in place, and their
    model-space solution."""

    __slots__ = ("hpr", "pos", "world", "dirty", "_buf")

    def __init__(self):
        self.hpr = REST_HPR.copy()
        self.pos = REST_POS.copy()
        self.world = REST_WORLD.copy()          # (N_BONES, 4, 4) after the last solve()
        self.dirty = set(range(N_BONES))        # bones set since the consumer last looked
        self._buf = np.empty((N_BONES + 1, 4, 4))

    def reset(self) -> None:
        self.hpr[:] = REST_HPR
        self.pos[:] = REST_POS
        self.dirty.update(range(N_BONES))

    def set_hpr(self, bone: int, h: float, p: float, r: float = 0.0) -> None:
        self.hpr[bone] = (h, p, r)
        self.dirty.add(bone)

    def set_pos(self, bone: int, x: float, y: float, z: float) -> None:
        self.pos[bone] = (x, y, z)
        self.dirty.add(bone)

    def solve(self) -> np.ndarray:
        self.world = solve(affine(local_rotations(self.hpr), self.pos), self._buf)
        return self.world
