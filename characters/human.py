"""The procedural human: body, head and hands as signed distance fields
(characters/sdf.py) fitted to the game skeleton's bind pose (an A-pose,
gameplay/skeleton.py).

Everything is built in model space around the rest-pose joints, so the
same proportions follow the skeleton whatever the appearance: men and women,
the three builds and every face share the joint positions and the hit boxes
(OVERHAUL_PLAN B-3). Variety is soft volume (a few centimetres of muscle
and fat) and the face.

Shapes, from the inside out:
* ``body``: torso, limbs, neck, as round cones along the bones with
  ellipsoid muscles (pectorals, deltoids, biceps, forearm flexors, glutes,
  quadriceps, calves). Mostly covered by clothing; it is the form the
  clothing shells are grown from.
* ``head``: cranium, face, jaw and chin, cheekbones, brow, nose, lips,
  eye sockets with lids, ears, and the neck down into the collar.
* ``hand``: palm and three finger chains plus the thumb, along the finger
  bones (gloves are an offset of it).

Parameters (``Build`` and ``Face``) are plain numbers drawn per bot by
characters/appearance.py; the defaults are an average man.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from characters import sdf as S
from gameplay import skeleton as sk

B = sk.INDEX
_P = sk.REST_WORLD[:, 3, :3]
_R = sk.REST_WORLD[:, :3, :3]


def P(name: str) -> np.ndarray:
    return _P[B[name]].copy()


def at(name: str, local) -> np.ndarray:
    """A point given in a bone's rest frame, in model space."""
    return _P[B[name]] + np.asarray(local, np.float64) @ _R[B[name]]


def frame(name: str) -> np.ndarray:
    """The bone's rest rotation (rows = its local axes in model space)."""
    return _R[B[name]].copy()


@dataclass
class Build:
    female: bool = False
    mass: float = 0.0        # -1 slim .. +1 heavy (soft volume, about +-2 cm)
    muscle: float = 0.3      # 0 .. 1


@dataclass
class Face:
    width: float = 1.0       # multipliers around 1.0
    jaw: float = 1.0
    chin: float = 1.0
    chin_forward: float = 0.0    # metres
    cheekbones: float = 1.0
    brow: float = 1.0
    nose_length: float = 1.0
    nose_width: float = 1.0
    nose_bridge: float = 1.0
    nose_hook: float = 0.0       # metres, the tip down (+) or up (-)
    lips: float = 1.0
    mouth_width: float = 1.0
    eye_spacing: float = 1.0
    eye_depth: float = 1.0
    ears: float = 1.0
    cranium_length: float = 1.0
    forehead_slope: float = 0.0  # metres the forehead leans back
    neck: float = 1.0
    extra: dict = field(default_factory=dict)


# the hit sphere's centre (gameplay/hitboxes.py): the skull is built around it so the
# visual head and the hit head coincide
HEAD_CENTRE = np.array([0.0, 0.01, 1.635])


# ------------------------------------------------------------------- body
def body(build: Build = Build()) -> S.Shape:
    m = 1.0 + 0.06 * build.mass               # soft volume
    mu = build.muscle
    fem = build.female
    sh = S.Shape()
    # --- torso
    chest_w = (0.150 if not fem else 0.138) * m
    sh.add(S.ell((0, 0.0, 1.30), (chest_w, 0.105 * m, 0.17), "torso", blend=0.0))       # ribcage
    sh.add(S.ell((0, -0.025, 1.39), (0.150 if not fem else 0.13, 0.075, 0.07), "torso", blend=0.05))  # upper back
    for sx in (-1, 1):                                                                   # pectorals / bust
        if fem:
            sh.add(S.ell((0.068 * sx, 0.075, 1.29), (0.062, 0.05, 0.058), "torso", blend=0.03))
        else:
            sh.add(S.ell((0.07 * sx, 0.066, 1.335), (0.075, 0.04 + 0.008 * mu, 0.058), "torso", blend=0.035))
        # trapezius slope to the neck
        sh.add(S.cone((0.03 * sx, -0.012, 1.46), (0.17 * sx, -0.008, 1.42), 0.045, 0.04, "torso", blend=0.04))
    sh.add(S.ell((0, 0.008, 1.10), ((0.128 if not fem else 0.118) * m, 0.095 * m + 0.012 * build.mass, 0.13),
                 "torso", blend=0.06))                                                  # abdomen
    hip_w = (0.158 if not fem else 0.172) * m
    sh.add(S.ell((0, -0.006, 0.93), (hip_w, 0.105 * m, 0.11), "pelvis", blend=0.06))      # pelvis
    for sx in (-1, 1):
        sh.add(S.ell((0.068 * sx, -0.055, 0.86), (0.075 * m, 0.068 * m, 0.088), "pelvis", blend=0.04))  # glutes
    # --- neck (the head shape repeats it at higher resolution)
    sh.add(S.cone((0, -0.008, 1.40), (0, 0.0, 1.56), 0.058 * (0.92 if fem else 1.0), 0.052, "neck", blend=0.04))
    # --- arms
    for side, sx in (("l", -1), ("r", 1)):
        s, e, w = P(f"upperarm_{side}"), P(f"lowerarm_{side}"), P(f"hand_{side}")
        delt = (0.058 if not fem else 0.05) * (1 + 0.15 * mu) * m
        sh.add(S.ell(s + np.array([0.012 * sx, 0.0, -0.025]), (delt, delt * 1.05, delt * 1.2), f"arm_{side}",
                     blend=0.045))
        sh.add(S.cone(s + (e - s) * 0.05, e, 0.047 * m, 0.037 * m, f"arm_{side}", blend=0.035))
        fr = frame(f"upperarm_{side}")
        bic = s + (e - s) * 0.5 + fr[2] * 0.0 + np.array([0, 0.022, 0])
        sh.add(S.ell(bic, (0.03 * (1 + 0.3 * mu), 0.032 * (1 + 0.3 * mu), 0.075), f"arm_{side}", blend=0.03,
                     rot=_ell_rot(e - s)))
        sh.add(S.cone(e, w, 0.039 * m, 0.027, f"arm_{side}", blend=0.03))
        flex = e + (w - e) * 0.28
        sh.add(S.ell(flex, (0.036 * (1 + 0.2 * mu), 0.034, 0.075), f"arm_{side}", blend=0.03, rot=_ell_rot(w - e)))
    # --- legs
    for side, sx in (("l", -1), ("r", 1)):
        h, k, a = P(f"thigh_{side}"), P(f"calf_{side}"), P(f"foot_{side}")
        sh.add(S.cone(h + np.array([0.008 * sx, 0, 0.02]), k, 0.088 * m, 0.054, f"leg_{side}", blend=0.05))
        sh.add(S.ell(h + (k - h) * 0.45 + np.array([0.004 * sx, 0.03, 0]), (0.065 * m, 0.06 * m, 0.17),
                     f"leg_{side}", blend=0.04))                                         # quadriceps
        sh.add(S.ell(k + np.array([0, 0.012, 0.01]), (0.05, 0.05, 0.05), f"leg_{side}", blend=0.03))   # knee
        sh.add(S.cone(k, a + np.array([0, -0.01, 0.0]), 0.05, 0.034, f"leg_{side}", blend=0.03))
        sh.add(S.ell(k + np.array([0, -0.03, -0.11]), (0.048 * (1 + 0.15 * mu), 0.05, 0.1), f"leg_{side}",
                     blend=0.03))                                                       # calf
        # foot (boots are grown from it)
        sh.add(S.box(a + np.array([0, 0.055, -0.035]), (0.042, 0.115, 0.04), 0.03, f"foot_{side}", blend=0.03))
    return sh


def _ell_rot(direction) -> np.ndarray:
    """Rotation turning world offsets into a frame whose z runs along ``direction``."""
    z = np.asarray(direction, np.float64)
    z = z / np.linalg.norm(z)
    x = np.cross([0.0, 1.0, 0.0], z)
    if np.linalg.norm(x) < 1e-6:
        x = np.array([1.0, 0.0, 0.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z])


# ------------------------------------------------------------------- head
def head(face: Face = Face(), build: Build = Build()) -> tuple[S.Shape, dict]:
    """The head and neck, and landmarks (eye centres and radius, the hairline) for the
    eyes, hair and paint."""
    f = face
    fem = build.female
    c = HEAD_CENTRE
    sh = S.Shape()
    w = f.width * (0.95 if fem else 1.0)
    # cranium
    sh.add(S.ell(c + (0, -0.012 * f.cranium_length, 0.018), (0.073 * w, 0.096 * f.cranium_length, 0.09), "head",
                 blend=0.0))
    sh.add(S.ell(c + (0, 0.035 - f.forehead_slope, 0.04), (0.066 * w, 0.06, 0.06), "head", blend=0.03))   # forehead
    # face mass and jaw
    jaw = f.jaw * (0.9 if fem else 1.0)
    sh.add(S.ell(c + (0, 0.035, -0.045), (0.06 * w, 0.06, 0.07), "head", blend=0.03))
    for sx in (-1, 1):
        sh.add(S.ell(c + (0.048 * sx * jaw * w, 0.012, -0.072), (0.02, 0.036, 0.026), "head", blend=0.025))   # angle
        sh.add(S.cone(c + (0.05 * sx * jaw * w, 0.02, -0.08), c + (0.016 * sx, 0.07 + f.chin_forward, -0.112),
                      0.016 * jaw, 0.012, "head", blend=0.02))                               # jawline
        cb = f.cheekbones
        sh.add(S.ell(c + (0.047 * sx * w, 0.055, -0.012), (0.022 * cb, 0.016 * cb, 0.014 * cb), "head", blend=0.02))
    chin = f.chin * (0.85 if fem else 1.0)
    sh.add(S.ell(c + (0, 0.076 + f.chin_forward, -0.112), (0.022 * chin, 0.017 * chin, 0.02 * chin), "head",
                 blend=0.018))
    # brow ridge
    br = f.brow * (0.6 if fem else 1.0)
    sh.add(S.ell(c + (0, 0.08 - f.forehead_slope * 0.3, 0.02), (0.054 * w, 0.012 + 0.006 * br, 0.012), "head",
                 blend=0.02))
    # eye sockets, carved
    es = 0.032 * f.eye_spacing
    eye_c = []
    for sx in (-1, 1):
        ec = c + (es * sx, 0.069 - 0.004 * (f.eye_depth - 1.0), 0.008)
        eye_c.append(ec)
        sh.add(S.ell(ec + (0, 0.012, 0.0), (0.0185, 0.012, 0.0115), "head", blend=0.007, subtract=True))
    # nose
    nl, nw, nb = f.nose_length, f.nose_width, f.nose_bridge
    root = c + (0, 0.084, 0.006)
    tip = c + (0, 0.103 + 0.006 * nb, -0.04 * nl - f.nose_hook)
    sh.add(S.cone(root, tip, 0.0075 * nb, 0.0105 * nw, "head", blend=0.012))
    for sx in (-1, 1):
        sh.add(S.ell(c + (0.0125 * sx * nw, 0.09, -0.045 * nl), (0.0105 * nw, 0.0085, 0.0075), "head", blend=0.008))
    sh.add(S.ell(c + (0, 0.094, -0.049 * nl), (0.007, 0.009, 0.004), "head", blend=0.006))  # columella
    # lips and mouth
    lp = f.lips * (1.15 if fem else 1.0)
    mw = f.mouth_width
    mouth_z = -0.066 - 0.012 * (nl - 1.0)
    sh.add(S.ell(c + (0, 0.088, mouth_z + 0.007), (0.022 * mw, 0.009 * lp, 0.0065 * lp), "head", blend=0.007))
    sh.add(S.ell(c + (0, 0.085, mouth_z - 0.007), (0.020 * mw, 0.0095 * lp, 0.0072 * lp), "head", blend=0.007))
    sh.add(S.ell(c + (0, 0.096, mouth_z), (0.021 * mw, 0.008, 0.0011), "head", blend=0.002, subtract=True))
    # ears
    for sx in (-1, 1):
        ear_rot = _ell_rot((0.25 * sx, -0.15, 1.0))
        ea = c + (0.074 * sx * w, -0.006, -0.008)
        sh.add(S.ell(ea, (0.011 * f.ears, 0.021 * f.ears, 0.031 * f.ears), "head", blend=0.008, rot=ear_rot))
        sh.add(S.ell(ea + (0.008 * sx, 0.002, 0.0), np.array([0.006, 0.013, 0.02]) * f.ears, "head", blend=0.004,
                     rot=ear_rot, subtract=True))
    # neck, into the collar
    nk = f.neck * (0.88 if fem else 1.0)
    sh.add(S.cone((0, -0.008, 1.40), c + (0, -0.012, -0.075), 0.058 * nk, 0.05 * nk, "neck", blend=0.03))
    if not fem:
        sh.add(S.ell(c + (0, 0.045, -0.135), (0.011, 0.01, 0.015), "neck", blend=0.012))   # larynx
    landmarks = {"eyes": eye_c, "eye_radius": 0.0118, "centre": c, "mouth_z": c[2] + mouth_z}
    return sh, landmarks


def eyelids(landmarks: dict, open_: float = 1.0) -> list:
    """Lids over the eyeballs (unioned into the head after the sockets are carved)."""
    out = []
    r = landmarks["eye_radius"]
    for ec in landmarks["eyes"]:
        out.append(S.ell(ec + (0, 0.0015, 0.0042), (r * 1.15, r * 1.02, r * 0.78), "head", blend=0.003))
        out.append(S.ell(ec + (0, 0.001, -0.0055), (r * 1.1, r * 0.98, r * 0.6), "head", blend=0.003))
    return out


# ------------------------------------------------------------------- hands
def hand(side: str, build: Build = Build(), grow: float = 0.0) -> S.Shape:
    """Palm and fingers along the finger bones (gloves: ``grow`` > 0)."""
    sc = 0.93 if build.female else 1.0
    sh = S.Shape(offset=grow)
    fr = frame(f"hand_{side}")
    t = 1.0 if side == "l" else -1.0
    palm_c = at(f"hand_{side}", (-0.004 * t, 0.048, 0.0))
    sh.add(S.box(palm_c, (0.040 * sc, 0.048 * sc, 0.0135), 0.011, f"hand_{side}", blend=0.0, rot=fr))
    sh.add(S.cone(P(f"hand_{side}") - fr[1] * 0.02, palm_c - fr[1] * 0.03, 0.028, 0.03, f"hand_{side}", blend=0.015))
    radii = {"thumb": (0.0115, 0.0105, 0.0095), "index": (0.0092, 0.0085, 0.0078), "fingers": (0.0098, 0.009, 0.0082)}
    for chain in ("thumb", "index", "fingers"):
        pts = [P(f"{chain}_0{k}_{side}") for k in (1, 2, 3)]
        tip = at(f"{chain}_03_{side}", (0, 0.02, 0))
        pts.append(tip)
        r = radii[chain]
        width = 1.0 if chain != "fingers" else 1.9          # the other three fingers as one flat mitten
        for k in range(3):
            if width == 1.0:
                sh.add(S.cone(pts[k], pts[k + 1], r[k] * sc, (r[k + 1] if k < 2 else r[2] * 0.9) * sc,
                              f"hand_{side}", blend=0.006))
            else:
                mid = (pts[k] + pts[k + 1]) * 0.5
                half = np.linalg.norm(pts[k + 1] - pts[k]) * 0.5 + r[k] * 0.6
                rot = frame(f"{chain}_0{k + 1}_{side}")
                sh.add(S.box(mid, (0.022 * sc, half, r[k] * 0.95), r[k] * 0.9, f"hand_{side}", blend=0.006, rot=rot))
    return sh
