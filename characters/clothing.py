"""Clothing for the procedural soldiers: combat shirt, trousers, gloves and
boots as shells grown from the body (characters/human.py).

Each garment is a distance field: the body primitives it covers, grown by
the cloth's thickness and ease (looser on the legs), intersected with the
region it covers (the shirt ends at the waist and the cuffs, the trousers
at the boots), plus its own parts - sleeve and cargo pockets with flaps,
elbow pads, a collar, the boot sole and the glove cuff.

Folds are a displacement of the shell, not a texture: rings where a limb
bends (inside of the elbows, behind the knees), stacked folds above the
boots and the cuffs, drape where the shirt tucks in, and a little low
frequency noise so no surface is perfectly smooth. Weave and wear are the
fabric shader's job (render/shaders/character.frag).

Weights come from the body's regions at each clothing vertex
(characters/weights.py), so cloth follows the skin it sits on.
"""
from __future__ import annotations

import numpy as np

from characters import sdf as S
from characters.human import P, at, frame
from gameplay import skeleton as sk


# ------------------------------------------------------------------ noise
def _hash3(i: np.ndarray, seed: int) -> np.ndarray:
    h = (i[..., 0] * 73856093) ^ (i[..., 1] * 19349663) ^ (i[..., 2] * 83492791) ^ (seed * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFF).astype(np.float64) / 65535.0


def value_noise(p: np.ndarray, scale: float, seed: int = 0) -> np.ndarray:
    """Smooth 3D value noise in [-1, 1] with features about ``scale`` metres apart."""
    q = p / scale
    i = np.floor(q).astype(np.int64)
    t = q - i
    t = t * t * (3.0 - 2.0 * t)
    out = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = (t[:, 0] if dx else 1 - t[:, 0]) * (t[:, 1] if dy else 1 - t[:, 1]) * (t[:, 2] if dz else 1 - t[:, 2])
                out = out + w * _hash3(i + np.array([dx, dy, dz]), seed)
    return out * 2.0 - 1.0


def _along(p: np.ndarray, a, b) -> tuple[np.ndarray, np.ndarray]:
    """Distance along the segment ab (metres from a) and from its line."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    ab = b - a
    L = np.linalg.norm(ab)
    u = ab / L
    s = (p - a) @ u
    r = np.linalg.norm((p - a) - s[:, None] * u, axis=1)
    return s, r


def _rings(p, joint, axis_from, axis_to, wavelength, amp, reach) -> np.ndarray:
    """Ring folds around a bending joint: a sine along the limb that fades ``reach`` away."""
    s, _ = _along(p, axis_from, axis_to)
    js, _ = _along(joint[None, :], axis_from, axis_to)
    dist = np.abs(s - js[0])
    fade = np.clip(1.0 - dist / reach, 0.0, 1.0) ** 2
    return amp * fade * np.sin(2 * np.pi * s / wavelength)


# ------------------------------------------------------------------ garments
def _cut(d: np.ndarray, plane_d: np.ndarray, k: float = 0.004) -> np.ndarray:
    """Intersect a shell with the half-space plane_d <= 0."""
    return S.smax(d, plane_d, k)


class Garment:
    """A clothing piece: distance = shell, cut by its region, minus folds."""

    def __init__(self, name: str, base: S.Shape, extra: list, cuts, folds, slot: str):
        self.name = name
        self.base = base
        self.extra = S.Shape(prims=list(extra))
        self.cuts = cuts           # f(p) -> plane distance (<= 0 inside the garment's region)
        self.folds = folds         # f(p) -> outward offset in metres
        self.slot = slot

    def __call__(self, p: np.ndarray) -> np.ndarray:
        d = self.base(p) - self.folds(p)
        d = _cut(d, self.cuts(p))
        if self.extra.prims:
            d = S.smin(d, self.extra(p), 0.006)
        return d

    def bounds(self):
        lo, hi = self.base.bounds(0.03)
        if self.extra.prims:
            l2, h2 = self.extra.bounds(0.01)
            lo, hi = np.minimum(lo, l2), np.maximum(hi, h2)
        return lo, hi


def _subset(body: S.Shape, tags: tuple, offset: float) -> S.Shape:
    return S.Shape(prims=[p for p in body.prims if p.tag in tags and not p.subtract], offset=offset)


def shirt(body: S.Shape, seed: int = 0, sleeves: str = "full") -> Garment:
    """Combat shirt: torso and sleeves, tucked in at the waist, cuffs at the wrists
    (``sleeves="rolled"``: to mid-forearm)."""
    base = _subset(body, ("torso", "pelvis", "arm_l", "arm_r"), 0.011)
    waist = 0.885
    wrist_t = 0.92 if sleeves == "full" else 0.45
    cuffs = []
    for side in ("l", "r"):
        e, w = P(f"lowerarm_{side}"), P(f"hand_{side}")
        cuffs.append((e + (w - e) * wrist_t, (w - e) / np.linalg.norm(w - e)))

    def cuts(p):
        d = waist - p[:, 2]
        for c, n in cuffs:
            d = np.maximum(d, (p - c) @ n)
        return d

    elbows = [(P(f"lowerarm_{s}"), P(f"upperarm_{s}"), P(f"hand_{s}")) for s in ("l", "r")]

    def folds(p):
        out = 0.0025 * value_noise(p, 0.05, seed) + 0.0012 * value_noise(p, 0.018, seed + 1)
        for e, s, w in elbows:
            out = out + _rings(p, e, s, w, 0.03, 0.0035, 0.09)
        # drape where the shirt tucks in
        tuck = np.clip(1.0 - (p[:, 2] - waist) / 0.08, 0.0, 1.0)
        ang = np.arctan2(p[:, 1], p[:, 0])
        out = out + 0.004 * tuck * (0.5 + 0.5 * np.sin(ang * 9.0 + 3.0 * value_noise(p, 0.1, seed + 2)))
        # sleeve cuffs gather
        for c, n in cuffs:
            near = np.clip(1.0 - np.abs((p - c) @ n) / 0.05, 0.0, 1.0)
            out = out + 0.003 * near * np.sin(((p - c) @ n) * 2 * np.pi / 0.016)
        return out

    extra = []
    for side, sx in (("l", -1), ("r", 1)):
        s, e = P(f"upperarm_{side}"), P(f"lowerarm_{side}")
        fr = frame(f"upperarm_{side}")
        # sleeve pocket on the outside of the upper arm, with its flap
        out_dir = np.array([sx, 0.0, 0.0])
        mid = s + (e - s) * 0.42 + out_dir * 0.05
        rot = np.stack([np.cross(fr[1], out_dir) / np.linalg.norm(np.cross(fr[1], out_dir)), fr[1], out_dir])
        extra.append(S.box(mid, (0.04, 0.055, 0.012), 0.008, f"arm_{side}", rot=rot))
        extra.append(S.box(mid - fr[1] * 0.045 + out_dir * 0.006, (0.043, 0.016, 0.01), 0.006, f"arm_{side}", rot=rot))
        # elbow pad
        el = P(f"lowerarm_{side}") + np.array([0.0, -0.035, 0.0])
        extra.append(S.ell(el, (0.035, 0.022, 0.04), f"arm_{side}", rot=None))
    # collar: a short stand-up ring round the neck base
    extra.append(S.cone((0, -0.012, 1.425), (0, -0.004, 1.475), 0.07, 0.064, "torso"))
    g = Garment("shirt", base, extra, cuts, folds, "shirt")
    g.collar_hole = (np.array([0, -0.006, 1.45]), 0.058)
    return g


def trousers(body: S.Shape, seed: int = 0) -> Garment:
    base = _subset(body, ("pelvis", "leg_l", "leg_r"), 0.017)
    top, cuff = 1.0, 0.19

    def cuts(p):
        return np.maximum(p[:, 2] - top, cuff - p[:, 2])

    knees = [(P(f"calf_{s}"), P(f"thigh_{s}"), P(f"foot_{s}")) for s in ("l", "r")]

    def folds(p):
        out = 0.003 * value_noise(p, 0.06, seed + 5) + 0.0012 * value_noise(p, 0.02, seed + 6)
        for k, h, a in knees:
            out = out + _rings(p, k + np.array([0, -0.03, 0]), h, a, 0.035, 0.004, 0.1)
            # stacked folds above the boots
            stack = np.clip(1.0 - (p[:, 2] - cuff) / 0.09, 0.0, 1.0)
            out = out + 0.005 * stack * np.sin(p[:, 2] * 2 * np.pi / 0.022 + 2.0 * value_noise(p, 0.04, seed + 7))
        return out

    extra = []
    for side, sx in (("l", -1), ("r", 1)):
        h, k = P(f"thigh_{side}"), P(f"calf_{side}")
        side_pt = h + (k - h) * 0.42 + np.array([0.083 * sx, 0.012, 0.0])
        extra.append(S.box(side_pt, (0.022, 0.07, 0.085), 0.015, f"leg_{side}"))            # cargo pocket
        extra.append(S.box(side_pt + np.array([0.006 * sx, 0.0, 0.07]), (0.02, 0.074, 0.022), 0.008, f"leg_{side}"))
    # waistband
    extra.append(S.ell((0, -0.006, 0.985), (0.17, 0.118, 0.022), "pelvis"))
    return Garment("trousers", base, extra, cuts, folds, "trousers")


def gloves(hand_shapes: dict, seed: int = 0) -> dict:
    """Glove shells over each hand, with a cuff over the sleeve end."""
    out = {}
    for side, hs in hand_shapes.items():
        e, w = P(f"lowerarm_{side}"), P(f"hand_{side}")
        n = (w - e) / np.linalg.norm(w - e)
        base = S.Shape(prims=list(hs.prims), offset=0.0018)
        cuff = S.cone(w - n * 0.045, w + n * 0.01, 0.036, 0.032, f"hand_{side}")

        def cuts(p, w=w, n=n):
            return (w - n * 0.05 - p) @ n

        def folds(p, s=seed):
            return 0.0006 * value_noise(p, 0.012, s + 11)

        out[side] = Garment(f"glove_{side}", base, [cuff], cuts, folds, "gloves")
    return out


def boots(body: S.Shape, seed: int = 0) -> Garment:
    """Boots: the foot grown, an ankle shaft up to the trouser cuff, a thick sole."""
    feet = _subset(body, ("foot_l", "foot_r"), 0.014)
    extra = []
    for side in ("l", "r"):
        a, k = P(f"foot_{side}"), P(f"calf_{side}")
        extra.append(S.cone(a + np.array([0, -0.005, 0.0]), a + (k - a) * 0.42, 0.05, 0.052, f"foot_{side}"))
        extra.append(S.box(a + np.array([0, 0.055, -0.068]), (0.05, 0.135, 0.014), 0.008, f"foot_{side}"))   # sole
        extra.append(S.box(a + np.array([0, -0.045, -0.055]), (0.045, 0.04, 0.02), 0.012, f"foot_{side}"))   # heel

    def cuts(p):
        return p[:, 2] - 0.26

    def folds(p):
        return 0.0015 * value_noise(p, 0.03, seed + 21)

    return Garment("boots", feet, extra, cuts, folds, "boots")
