"""Gear for the procedural soldiers: plate carrier and pouches, belt, knee
pads, helmets with covers, ear protection and mounts, caps, balaclavas,
goggles and glasses.

All of it is hard-surface modelling with distance fields (rounded boxes,
cut ellipsoid shells, cones), meshed like the body (characters/mesher.py)
so edges are bevelled and nothing has to line up with UVs. No insignia,
flags, patches, logos or camouflage patterns: plain colours from the team
palette, with wear and grime in the shader.

The two teams differ in silhouette and value, not only hue (OVERHAUL_PLAN
4.2): Vanguard wears soft-covered helmets with goggles on the front and a
slim carrier; Bastion a high-cut helmet with ear protection and a bulkier
carrier with side plates.

Each piece says how it is skinned: ``"body"`` takes the weights of the body
region under it (the carrier bends with the spine), a bone name rides that
bone rigidly (a helmet on the head).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from characters import sdf as S
from characters.human import HEAD_CENTRE, P, frame
from characters.clothing import value_noise


@dataclass
class Piece:
    name: str
    shape: object          # callable distance field with .bounds()
    slot: str              # palette slot (characters/palette.py)
    cell: float            # meshing resolution at LOD0, metres
    skin: str = "body"     # "body" or a bone name


class Field:
    """A callable distance field from a function and its bounds."""

    def __init__(self, fn, lo, hi):
        self.fn, self.lo, self.hi = fn, np.asarray(lo, np.float64), np.asarray(hi, np.float64)

    def __call__(self, p):
        return self.fn(p)

    def bounds(self):
        return self.lo, self.hi


def _shape(*prims, offset=0.0) -> S.Shape:
    return S.Shape(prims=list(prims), offset=offset)


# ------------------------------------------------------------------ torso
def plate_carrier(body: S.Shape, style: str, layout: dict) -> list[Piece]:
    """Front and back plate bags on a cummerbund, shoulder straps, magazine pouches,
    an admin pouch and a radio pouch on ``layout["radio"]`` ("l"/"r")."""
    bulky = style == "bastion"
    th = 0.034 if bulky else 0.026
    torso = S.Shape(prims=[p for p in body.prims if p.tag in ("torso",) and not p.subtract])
    pieces = []
    # plates: boxes pressed onto the chest and back, rounded
    front = _shape(S.box((0, 0.112, 1.235), (0.135, th, 0.165), 0.02, "torso"),
                   S.box((0, 0.12, 1.085), (0.15, th * 0.8, 0.045), 0.018, "torso"))     # kangaroo pouch
    back = _shape(S.box((0, -0.125, 1.24), (0.14, th, 0.17), 0.02, "torso"))
    # cummerbund: the torso grown into a band
    band_shell = S.Shape(prims=torso.prims, offset=0.016)

    def band(p):
        d = band_shell(p)
        return S.smax(d, np.maximum(1.03 - p[:, 2], p[:, 2] - 1.19), 0.006)

    straps = []
    for sx in (-1, 1):
        straps.append(S.cone((0.085 * sx, 0.1, 1.38), (0.095 * sx, -0.01, 1.485), 0.024, 0.022, "torso"))
        straps.append(S.cone((0.095 * sx, -0.01, 1.485), (0.085 * sx, -0.112, 1.37), 0.022, 0.024, "torso"))
    carrier = S.Shape(prims=front.prims + back.prims + straps)
    lo, hi = np.array([-0.2, -0.17, 1.0]), np.array([0.2, 0.17, 1.52])

    def carrier_fn(p):
        return S.smin(carrier(p) - 0.0012 * value_noise(p, 0.02, 3), band(p), 0.01)

    pieces.append(Piece("carrier", Field(carrier_fn, lo, hi), "carrier", 0.008))
    if bulky:
        side = _shape(*[S.box((0.165 * sx, 0.0, 1.13), (0.02, 0.07, 0.08), 0.012, "torso") for sx in (-1, 1)])
        pieces.append(Piece("side_plates", side, "carrier", 0.006))
    # magazine pouches on the kangaroo/front flap, shingled
    n_mags = layout.get("mags", 3)
    mags = []
    for k in range(n_mags):
        x = (k - (n_mags - 1) / 2) * 0.072
        mags.append(S.box((x, 0.15 + th * 0.4, 1.10), (0.031, 0.022, 0.06), 0.008, "torso"))
        mags.append(S.box((x, 0.165 + th * 0.4, 1.155), (0.033, 0.012, 0.016), 0.005, "torso"))   # flap
    pieces.append(Piece("mag_pouches", _shape(*mags), "pouch", 0.005))
    # admin pouch high on the chest
    if layout.get("admin", True):
        pieces.append(Piece("admin", _shape(S.box((0, 0.142 + th * 0.4, 1.31), (0.075, 0.016, 0.045), 0.01, "torso")),
                            "pouch", 0.005))
    # radio on one side of the back, antenna up past the shoulder
    rs = -1.0 if layout.get("radio", "l") == "l" else 1.0
    radio = _shape(S.box((0.12 * rs, -0.135, 1.2), (0.035, 0.03, 0.075), 0.01, "torso"),
                   S.cone((0.135 * rs, -0.14, 1.27), (0.15 * rs, -0.15, 1.58), 0.0045, 0.003, "torso"))
    pieces.append(Piece("radio", radio, "hard", 0.004))
    return pieces


def belt(layout: dict) -> list[Piece]:
    ring = S.Shape(prims=[S.ell((0, -0.006, 0.975), (0.178, 0.125, 0.026), "pelvis")])

    def belt_fn(p):
        inner = S.ell((0, -0.006, 0.975), (0.158, 0.106, 0.06), "pelvis").distance(p)
        return S.smax(ring(p), -inner, 0.004)

    pieces = [Piece("belt", Field(belt_fn, (-0.2, -0.15, 0.94), (0.2, 0.15, 1.01)), "webbing", 0.005)]
    pouches = [S.box((0.17, -0.02, 0.94), (0.028, 0.045, 0.055), 0.01, "pelvis")]          # dump / utility
    if layout.get("holster", True):
        pouches.append(S.box((0.185, 0.01, 0.88), (0.022, 0.06, 0.085), 0.012, "pelvis"))
    pouches.append(S.box((-0.17, -0.03, 0.945), (0.026, 0.04, 0.05), 0.01, "pelvis"))
    pieces.append(Piece("belt_pouches", _shape(*pouches), "pouch", 0.005))
    return pieces


def knee_pads() -> list[Piece]:
    pads = []
    for side in ("l", "r"):
        k = P(f"calf_{side}")
        pads.append(S.ell(k + np.array([0.0, 0.07, -0.02]), (0.052, 0.03, 0.075), f"leg_{side}"))
    return [Piece("knee_pads", _shape(*pads), "pouch", 0.005)]


# ------------------------------------------------------------------- head
def _cranium(grow: float) -> S.Shape:
    c = HEAD_CENTRE
    return _shape(S.ell(c + (0, -0.012, 0.022), (0.08 + grow, 0.103 + grow, 0.094 + grow), "head"))


def _blend(x: np.ndarray, x0: float, x1: float, a: float, b: float) -> np.ndarray:
    """a below x0, b above x1, smoothstep in between."""
    t = np.clip((x - x0) / (x1 - x0), 0.0, 1.0)
    return a + (b - a) * t * t * (3.0 - 2.0 * t)


def helmet(style: str, cover: bool, ear_pro: bool, goggles: bool) -> list[Piece]:
    c = HEAD_CENTRE
    pieces = []
    shell = _cranium(0.024)
    high_cut = style == "bastion"

    def shell_fn(p):
        d = shell(p)
        # brim line: front above the brow, sides above (high cut) or over the ears
        rel = p - c
        # brim line, blended (a step would not be a distance field: the mesh would crack)
        brim_z = _blend(rel[:, 1], 0.015, 0.045, 0.03 if high_cut else -0.01, 0.035)
        brim_z = brim_z + _blend(rel[:, 1], -0.07, -0.03, -0.03, 0.0)
        d = S.smax(d, brim_z - rel[:, 2], 0.01)
        return d

    lo, hi = c + np.array([-0.13, -0.15, -0.06]), c + np.array([0.13, 0.15, 0.15])
    pieces.append(Piece("helmet", Field(shell_fn, lo, hi), "helmet", 0.005, skin="head"))
    if cover:
        cov = _cranium(0.029)

        def cover_fn(p):
            rel = p - c
            d = cov(p) - 0.0025 * value_noise(p, 0.03, 7) - 0.0012 * value_noise(p, 0.01, 8)
            brim_z = _blend(rel[:, 1], 0.015, 0.045, -0.012, 0.03) + _blend(rel[:, 1], -0.07, -0.03, -0.03, 0.0)
            return S.smax(d, brim_z - rel[:, 2], 0.01)

        pieces.append(Piece("helmet_cover", Field(cover_fn, lo - 0.01, hi + 0.01), "helmet_cover", 0.005, skin="head"))
    hw = []
    # NVG shroud on the front, side rails
    hw.append(S.box(c + (0, 0.1, 0.075), (0.022, 0.012, 0.018), 0.004, "head"))
    for sx in (-1, 1):
        hw.append(S.box(c + (0.1 * sx, 0.0, 0.035), (0.01, 0.055, 0.011), 0.003, "head"))
    pieces.append(Piece("helmet_hardware", _shape(*hw), "hard", 0.003, skin="head"))
    if ear_pro:
        cups = []
        for sx in (-1, 1):
            ea = c + (0.088 * sx, -0.006, -0.008)
            cups.append(S.cone(ea, ea + np.array([0.028 * sx, 0, 0]), 0.034, 0.03, "head"))
        cups.append(S.cone(c + (-0.1, -0.01, 0.03), c + (0.1, -0.01, 0.03), 0.009, 0.009, "head"))   # band under the shell
        pieces.append(Piece("ear_pro", _shape(*cups), "hard", 0.004, skin="head"))
    if goggles:
        gg = [S.box(c + (0, 0.112, 0.062), (0.075, 0.016, 0.022), 0.01, "head")]
        pieces.append(Piece("goggles", _shape(*gg), "lens", 0.003, skin="head"))
        # a band round the helmet: an elliptic ring 9 mm thick (two mesh cells, so the mesh stays
        # manifold and simplifies) and 24 mm tall
        outer = S.ell(c + (0, -0.012, 0.06), (0.108, 0.13, 0.5), "head")
        inner = S.ell(c + (0, -0.012, 0.06), (0.099, 0.121, 0.5), "head")

        def strap_fn(p):
            ring = S.smax(outer.distance(p), -inner.distance(p), 0.002)
            return S.smax(ring, np.abs(p[:, 2] - (c[2] + 0.06)) - 0.012, 0.002)

        pieces.append(Piece("goggle_strap", Field(strap_fn, c - 0.15, c + 0.15), "webbing", 0.004, skin="head"))
    return pieces


def cap() -> list[Piece]:
    c = HEAD_CENTRE
    crown = _cranium(0.006)

    def crown_fn(p):
        rel = p - c
        d = crown(p) - 0.001 * value_noise(p, 0.02, 9)
        return S.smax(d, 0.012 - rel[:, 2], 0.006)

    brim = _shape(S.ell(c + (0, 0.1, 0.016), (0.07, 0.06, 0.005), "head"))
    lo, hi = c - 0.13, c + 0.16
    return [Piece("cap", Field(crown_fn, lo, hi), "cap", 0.004, skin="head"),
            Piece("cap_brim", brim, "cap", 0.003, skin="head")]


def balaclava(head_shape: S.Shape, landmarks: dict) -> list[Piece]:
    c = HEAD_CENTRE
    shell = S.Shape(prims=[p for p in head_shape.prims if not p.subtract], offset=0.004)
    eyes = landmarks["eyes"]

    def fn(p):
        d = shell(p) - 0.0012 * value_noise(p, 0.015, 13)
        slit = S.box((eyes[0] + eyes[1]) / 2 + np.array([0, 0.02, 0.0]), (0.055, 0.05, 0.016), 0.008, "head").distance(p)
        d = S.smax(d, -slit, 0.004)
        return S.smax(d, 1.43 - p[:, 2], 0.006)

    return [Piece("balaclava", Field(fn, c - np.array([0.13, 0.13, 0.25]), c + 0.14), "balaclava", 0.004,
                  skin="body")]


def glasses(landmarks: dict) -> list[Piece]:
    eyes = landmarks["eyes"]
    mid = (eyes[0] + eyes[1]) / 2
    parts = [S.box(mid + np.array([0, 0.03, 0.002]), (0.068, 0.004, 0.017), 0.006, "head")]
    for sx in (-1, 1):
        parts.append(S.cone(mid + np.array([0.068 * sx, 0.028, 0.006]), mid + np.array([0.074 * sx, -0.06, 0.0]),
                            0.0022, 0.0022, "head"))
    return [Piece("glasses", _shape(*parts), "lens", 0.002, skin="head")]
