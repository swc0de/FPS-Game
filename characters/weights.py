"""Skin weights for the procedural soldiers: up to four bones per vertex.

Each vertex belongs to a body region (the tag of the nearest primitive of the
body's distance field, characters/sdf.py), and only the bones of that region
may move it, so a thigh never follows the other thigh and an arm never pulls
the ribs. Within the region the weights fall off with the distance to each
bone's segment (inverse distance to the 4th power: about 50/50 at a joint,
fading over a limb radius either side), the four largest are kept and
normalised.

Twist bones (OVERHAUL_PLAN 4.1) get their share along the limb instead of
by distance: the forearm's weight moves from ``lowerarm`` at the elbow to
``lowerarm_twist`` towards the wrist, the upper arm's from ``upperarm`` at the
shoulder to ``upperarm_twist`` towards the elbow. When the animation rolls a
twist bone part of the way, the roll spreads along the limb instead of
wringing the skin at one ring (the "candy wrapper").
"""
from __future__ import annotations

import numpy as np

from gameplay import skeleton as sk

B = sk.INDEX
_P = sk.REST_WORLD[:, 3, :3]


def _pt(name: str) -> np.ndarray:
    return _P[B[name]]


def _axis(name: str, length: float, axis=(0.0, 1.0, 0.0)) -> np.ndarray:
    """A point ``length`` along a local axis of a bone in the rest pose."""
    return _pt(name) + np.asarray(axis) @ sk.REST_WORLD[B[name], :3, :3] * length


def _segments() -> dict:
    s = {
        "pelvis": (_pt("pelvis") + (0, 0, -0.10), _pt("spine_01")),
        "spine_01": (_pt("spine_01"), _pt("spine_02")),
        "spine_02": (_pt("spine_02"), _pt("spine_03")),
        "spine_03": (_pt("spine_03"), _pt("neck")),
        "neck": (_pt("neck"), _pt("head")),
        "head": (_pt("head"), _pt("head") + (0, 0.02, 0.17)),
    }
    for side in ("l", "r"):
        s[f"clavicle_{side}"] = (_pt(f"clavicle_{side}"), _pt(f"upperarm_{side}"))
        s[f"upperarm_{side}"] = (_pt(f"upperarm_{side}"), _pt(f"lowerarm_{side}"))
        s[f"lowerarm_{side}"] = (_pt(f"lowerarm_{side}"), _pt(f"hand_{side}"))
        s[f"hand_{side}"] = (_pt(f"hand_{side}"), _axis(f"hand_{side}", 0.085))
        for chain in ("thumb", "index", "fingers"):
            for k, nxt in ((1, 2), (2, 3)):
                s[f"{chain}_0{k}_{side}"] = (_pt(f"{chain}_0{k}_{side}"), _pt(f"{chain}_0{nxt}_{side}"))
            s[f"{chain}_03_{side}"] = (_pt(f"{chain}_03_{side}"), _axis(f"{chain}_03_{side}", 0.022))
        s[f"thigh_{side}"] = (_pt(f"thigh_{side}"), _pt(f"calf_{side}"))
        s[f"calf_{side}"] = (_pt(f"calf_{side}"), _pt(f"foot_{side}"))
        s[f"foot_{side}"] = (_pt(f"foot_{side}"), _pt(f"toe_{side}"))
        s[f"toe_{side}"] = (_pt(f"toe_{side}"), _axis(f"toe_{side}", 0.06))
    return {k: (np.asarray(a, np.float64), np.asarray(b, np.float64)) for k, (a, b) in s.items()}


SEGMENTS = _segments()

FINGERS = [f"{c}_0{k}" for c in ("thumb", "index", "fingers") for k in (1, 2, 3)]

# region tag -> bones that may move it
REGIONS = {
    "head": ["neck", "head"],
    "neck": ["spine_03", "neck", "head"],
    "torso": ["pelvis", "spine_01", "spine_02", "spine_03", "neck", "clavicle_l", "clavicle_r",
              "upperarm_l", "upperarm_r", "thigh_l", "thigh_r"],
    "pelvis": ["pelvis", "spine_01", "thigh_l", "thigh_r"],
}
for _s in ("l", "r"):
    REGIONS[f"arm_{_s}"] = ["spine_03", f"clavicle_{_s}", f"upperarm_{_s}", f"lowerarm_{_s}", f"hand_{_s}"]
    REGIONS[f"hand_{_s}"] = [f"lowerarm_{_s}", f"hand_{_s}"] + [f"{f}_{_s}" for f in FINGERS]
    REGIONS[f"leg_{_s}"] = ["pelvis", f"thigh_{_s}", f"calf_{_s}", f"foot_{_s}"]
    REGIONS[f"foot_{_s}"] = [f"calf_{_s}", f"foot_{_s}", f"toe_{_s}"]


def segment_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance from points to the segment ab, and the parameter (0 at a, 1 at b) of the closest point."""
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(p - (a + t[:, None] * ab), axis=1), t


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def compute(points: np.ndarray, regions: list, power: float = 4.0, eps: float = 0.012,
            region_of: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Joints (n, 4) and weights (n, 4) for bind-pose points.

    ``regions``: the region tag of each tag index, ``region_of``: (n,) tag index
    per point (characters.sdf.Shape.evaluate(..., with_tags=True))."""
    p = np.asarray(points, np.float64)
    n = len(p)
    region_of = np.zeros(n, np.int64) if region_of is None else np.asarray(region_of, np.int64)
    joints = np.zeros((n, 4), np.float32)
    weights = np.zeros((n, 4), np.float32)
    for ri, tag in enumerate(regions):
        sel = np.nonzero(region_of == ri)[0]
        if len(sel) == 0:
            continue
        bones = REGIONS.get(tag)
        if bones is None:
            raise KeyError(f"no bones for region {tag!r}")
        q = p[sel]
        cols, ts = [], {}
        for name in bones:
            d, t = segment_distance(q, *SEGMENTS[name])
            cols.append(1.0 / (d + eps) ** power)
            ts[name] = t
        w = np.stack(cols, axis=1)
        names = list(bones)
        # twist shares along the limb
        for side in ("l", "r"):
            for bone, twist, e0, e1 in ((f"lowerarm_{side}", f"lowerarm_twist_{side}", 0.15, 0.85),
                                        (f"upperarm_{side}", f"upperarm_twist_{side}", 0.25, 0.95)):
                if bone in names:
                    k = names.index(bone)
                    share = _smoothstep(e0, e1, ts[bone]) * w[:, k]
                    w[:, k] -= share
                    w = np.concatenate([w, share[:, None]], axis=1)
                    names.append(twist)
        if w.shape[1] < 4:                            # regions with fewer than four bones
            pad = 4 - w.shape[1]
            w = np.concatenate([w, np.zeros((len(w), pad))], axis=1)
            names += [names[0]] * pad
        idx = np.argsort(-w, axis=1)[:, :4]
        top = np.take_along_axis(w, idx, axis=1)
        top = top / np.maximum(top.sum(axis=1, keepdims=True), 1e-30)
        bone_ids = np.array([B[nm] for nm in names], np.int64)
        joints[sel] = bone_ids[idx]
        weights[sel] = top
    return joints, weights


def rigid(n: int, bone: str) -> tuple[np.ndarray, np.ndarray]:
    """Weights for a prop that rides one bone (gear, eyes)."""
    j = np.zeros((n, 4), np.float32)
    j[:, 0] = B[bone]
    w = np.zeros((n, 4), np.float32)
    w[:, 0] = 1.0
    return j, w
