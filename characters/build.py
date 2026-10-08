"""Assemble a soldier: head, eyes, hair, hands, clothing and gear for one
appearance in one team's kit, as one skinned mesh per level of detail.

Every piece is meshed from its distance field (characters/mesher.py),
simplified to its share of the triangle budget (characters/decimate.py),
weighted to the skeleton (characters/weights.py), and tagged with its
palette slot (characters/palette.py). The pieces are concatenated into
plain arrays:

    positions (n, 3)  bind pose, model space
    normals   (n, 3)
    colour    (n, 4)  rgb: paint multiplied over the slot colour (lips, brows,
                      iris, stubble); a: cavity occlusion
    joints    (n, 4), weights (n, 4)
    slot      (n,)    palette slot
    triangles (t, 3)

per LOD, cached as ``assets/cache/characters/<key>.npz``. Building a new
appearance takes a few seconds (several in parallel at startup); loading a
cached one is a file read.

Budgets (OVERHAUL_PLAN 4.4): about 12k triangles at LOD0, 4k at LOD1, 1.2k at
LOD2 (also the shadow caster).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from characters import clothing as C
from characters import gear as G
from characters import human as H
from characters import sdf as S
from characters import weights as W
from characters.appearance import Appearance, TeamLook
from characters.decimate import decimate
from characters.mesher import surface_nets
from characters.palette import SLOT

LOD_BUDGETS = (12000, 4000, 1200)
# share of the budget per piece kind (the rest of the gear shares "gear")
SHARE = {"head": 0.30, "hair": 0.05, "eyes": 0.02, "hands": 0.10, "shirt": 0.16, "trousers": 0.12, "boots": 0.05,
         "gear": 0.20}


@dataclass
class Mesh:
    pos: np.ndarray
    nrm: np.ndarray
    col: np.ndarray
    joints: np.ndarray
    weights: np.ndarray
    slot: np.ndarray
    tris: np.ndarray

    @staticmethod
    def concat(parts: list) -> "Mesh":
        parts = [p for p in parts if p is not None and len(p.tris)]
        base, tris = 0, []
        for p in parts:
            tris.append(p.tris + base)
            base += len(p.pos)
        cat = lambda k: np.concatenate([getattr(p, k) for p in parts])
        return Mesh(cat("pos"), cat("nrm"), cat("col"), cat("joints"), cat("weights"), cat("slot"),
                    np.concatenate(tris) if tris else np.zeros((0, 3), np.int64))


def _normals_from(shape, pts: np.ndarray, eps: float) -> np.ndarray:
    from characters.mesher import gradient
    g = gradient(shape, pts, eps)
    return g / np.maximum(np.linalg.norm(g, axis=1, keepdims=True), 1e-12)


def mesh_lods(shape, cell: float, share: float, slot: str, skin: str, tag_shape=None, paint=None,
              ao: bool = True) -> list:
    """``mesh_piece`` with the LODs chained (each simplified from the previous one), weights from
    ``tag_shape``'s regions (default: the shape itself), and cavity occlusion in the colour's alpha."""
    lo, hi = shape.bounds()
    v, f, _ = surface_nets(shape, lo, hi, cell)
    if len(f) == 0:
        return [None] * len(LOD_BUDGETS)
    tag_shape = tag_shape if tag_shape is not None else shape
    out = []
    for total in LOD_BUDGETS:
        target = max(int(share * total), 16)
        if len(f) > target:
            v, f, _ = decimate(v, f, target)
        n = _normals_from(shape, v, cell * 0.25)
        if skin == "body":
            _, tag = tag_shape.evaluate(v, with_tags=True)
            joints, weights = W.compute(v, tag_shape.tags(), region_of=tag)
        else:
            joints, weights = W.rigid(len(v), skin)
        col = np.ones((len(v), 4), np.float32)
        slots = np.full(len(v), SLOT[slot], np.float32)
        if paint is not None:
            rgb = paint(v, n)
            if rgb is not None:
                col[:, :3] = rgb
        if ao:
            col[:, 3] = cavity(shape, v, n, cell)
        out.append(Mesh(v.copy(), n, col, joints, weights, slots, f.copy()))
    return out


def cavity(shape, v: np.ndarray, n: np.ndarray, cell: float) -> np.ndarray:
    """Ambient occlusion from the distance field: step out along the normal; where the
    field is closer than the step, something is near (Quilez's SDF occlusion)."""
    occ = np.zeros(len(v))
    w = 1.0
    for k, step in enumerate((0.006, 0.014, 0.03)):
        d = shape(v + n * step)
        occ += w * np.maximum(step - d, 0.0) / step
        w *= 0.6
    return np.clip(1.0 - 0.55 * occ, 0.35, 1.0).astype(np.float32)


# ------------------------------------------------------------------- head
def _soft(x: np.ndarray, e0: float, e1: float) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def head_paint(app: Appearance, lm: dict):
    """Paint over the skin colour: lips, brows, beard shadow or stubble, flushed cheeks, nose
    and ears, darker under the eyes, and the scalp for shaved and buzzed heads."""
    skin = app.skin_rgb()
    hair = np.array(HAIR_SRGB[app.hair_colour]) ** 2.2
    c = lm["centre"]
    mz = lm["mouth_z"]
    female = app.build.female

    def paint(v, n):
        r = v - c
        out = np.ones((len(v), 3))
        # lips
        lips = (1 - _soft(np.abs(r[:, 0]), 0.016, 0.024)) * (1 - _soft(np.abs(v[:, 2] - mz), 0.006, 0.011)) * \
            _soft(r[:, 1], 0.07, 0.08)
        out *= 1 - lips[:, None] * (1 - np.array([0.86, 0.62, 0.62] if not female else [0.82, 0.55, 0.57]))
        # flush: cheeks, nose tip, ears
        cheek = sum((1 - _soft(np.linalg.norm(r - np.array([0.042 * sx, 0.06, -0.03]), axis=1), 0.0, 0.03))
                    for sx in (-1, 1))
        nose = 1 - _soft(np.linalg.norm(r - np.array([0, 0.1, -0.035]), axis=1), 0.0, 0.02)
        ears = _soft(np.abs(r[:, 0]), 0.07, 0.08) * (1 - _soft(np.abs(r[:, 2] + 0.008), 0.02, 0.035))
        flush = np.clip(cheek * 0.5 + nose * 0.6 + ears * 0.4, 0, 1) * (0.5 + 0.5 * app.haemoglobin)
        out *= 1 - flush[:, None] * (1 - np.array([1.0, 0.86, 0.86]))
        # under the eyes
        for ec in lm["eyes"]:
            u = 1 - _soft(np.linalg.norm((v - ec) * np.array([1.0, 1.0, 1.8]) - np.array([0, 0.004, -0.03]), axis=1),
                          0.0, 0.016)
            out *= 1 - u[:, None] * 0.12
        # brows: a band above each eye socket, thinner towards the temples
        for sx, ec in zip((-1, 1), lm["eyes"]):
            dx = (v[:, 0] - ec[0]) * sx
            arch = ec[2] + 0.0155 + 0.003 * (1 - (dx / 0.02) ** 2)
            thick = 0.0042 if not female else 0.003
            band = (1 - _soft(np.abs(v[:, 2] - arch), thick * 0.5, thick)) * (1 - _soft(np.abs(dx), 0.018, 0.026)) * \
                _soft(r[:, 1], 0.05, 0.065)
            out = out * (1 - band[:, None]) + band[:, None] * (hair / skin) * 0.9
        # beard shadow or stubble on the jaw, chin and upper lip
        if not female:
            jaw = (1 - _soft(r[:, 2], -0.055, -0.03)) * _soft(r[:, 2], -0.15, -0.13) * _soft(r[:, 1], -0.01, 0.02)
            upper_lip = (1 - _soft(np.abs(v[:, 2] - (mz + 0.014)), 0.004, 0.009)) * (1 - _soft(np.abs(r[:, 0]), 0.02, 0.03)) \
                * _soft(r[:, 1], 0.07, 0.085)
            region = np.clip(jaw + upper_lip, 0, 1) * (1 - lips)
            amount = {"none": 0.18, "stubble": 0.55, "moustache": 0.25}.get(app.facial_hair, 0.4)
            out = out * (1 - region[:, None] * amount) + region[:, None] * amount * (hair / skin) * 0.8
        # scalp: shaved shows a shadow, buzzed shows the hair colour
        hairline = _hairline(r)
        scalp = _soft(r[:, 2] - hairline, -0.004, 0.006)
        amount = {"shaved": 0.2, "buzz": 0.85}.get(app.hair, 0.6)
        out = out * (1 - scalp[:, None] * amount) + scalp[:, None] * amount * (hair / skin)
        return out

    return paint


def _hairline(r: np.ndarray) -> np.ndarray:
    """Height (relative to the head centre) of the hairline above each point's direction."""
    front = _soft(r[:, 1], 0.0, 0.06)          # 1 at the forehead
    back = _soft(-r[:, 1], 0.02, 0.08)         # 1 at the nape
    side = 1 - np.maximum(front, back)
    return front * 0.052 + side * 0.012 + back * -0.075


def hair_shape(app: Appearance, head_shape: S.Shape, covered: bool) -> S.Shape | None:
    """Hair as a shell over the scalp (crew, short, bun); buzzed and shaved heads are paint."""
    if app.hair in ("shaved", "buzz"):
        return None
    c = H.HEAD_CENTRE
    top, sides = {"crew": (0.011, 0.005), "short": (0.016, 0.009), "bun": (0.008, 0.007)}[app.hair]
    base = S.Shape(prims=[p for p in head_shape.prims if p.tag == "head" and not p.subtract])
    extra = []
    if app.hair == "bun":
        extra.append(S.ell(c + (0, -0.098, 0.035), (0.032, 0.03, 0.03), "head"))
    ex = S.Shape(prims=extra)
    seed = app.seed

    def fn(p):
        r = p - c
        up = _soft(r[:, 2], -0.02, 0.07)
        d = base(p) - (sides + (top - sides) * up) - 0.0015 * C.value_noise(p, 0.012, seed + 31)
        cut = _hairline(r) - r[:, 2]
        d = S.smax(d, cut, 0.004)
        if covered:
            d = S.smax(d, r[:, 2] - 0.025, 0.004)
        if extra:
            d = S.smin(d, ex(p), 0.01)
        return d

    lo, hi = base.bounds(0.03)
    return G.Field(fn, lo, hi)


def beard_shape(app: Appearance, head_shape: S.Shape, lm: dict) -> S.Shape | None:
    if app.build.female or app.facial_hair not in ("short_beard", "full_beard", "moustache"):
        return None
    c = H.HEAD_CENTRE
    mz = lm["mouth_z"]
    thick = {"short_beard": 0.004, "full_beard": 0.009, "moustache": 0.004}[app.facial_hair]
    base = S.Shape(prims=[p for p in head_shape.prims if not p.subtract])
    kind = app.facial_hair
    seed = app.seed

    def fn(p):
        r = p - c
        d = base(p) - thick - 0.0012 * C.value_noise(p, 0.008, seed + 41)
        if kind == "moustache":
            region = np.maximum(np.abs(p[:, 2] - (mz + 0.012)) - 0.006, np.abs(r[:, 0]) - 0.026)
            region = np.maximum(region, 0.08 - r[:, 1])
        else:
            region = np.maximum(r[:, 2] + 0.03, -0.155 - r[:, 2])       # below the cheeks, above the throat
            region = np.maximum(region, -0.005 - r[:, 1])               # in front of the ears
            lips = np.maximum(np.abs(p[:, 2] - mz) - 0.007, np.abs(r[:, 0]) - 0.02)
            region = np.maximum(region, -lips)
        return S.smax(d, region, 0.004)

    lo, hi = base.bounds(0.02)
    return G.Field(fn, lo, hi)


def eyes(app: Appearance, lm: dict) -> list:
    """Eyeballs: a white sclera, the iris and the pupil painted from the forward direction."""
    iris = np.array(EYE_SRGB[app.eye_colour]) ** 2.2
    parts = []
    for ec in lm["eyes"]:
        sh = S.Shape(prims=[S.ell(ec, (lm["eye_radius"],) * 3, "head")])

        def paint(v, n, ec=ec):
            d = (v - ec) / np.linalg.norm(v - ec, axis=1, keepdims=True)
            fwd = np.array([0.0, 1.0, 0.0])
            ang = np.degrees(np.arccos(np.clip(d @ fwd, -1, 1)))
            sclera = np.array([0.80, 0.77, 0.72])
            col = np.tile(sclera, (len(v), 1))
            irs = 1 - _soft(ang, 27.0, 31.0)
            col = col * (1 - irs[:, None]) + irs[:, None] * iris
            pupil = 1 - _soft(ang, 9.0, 12.0)
            col = col * (1 - pupil[:, None]) + pupil[:, None] * 0.01
            return col / 0.8                      # the eye slot's colour is 0.8 grey
        parts.append(mesh_lods(sh, 0.0012, SHARE["eyes"] / 2, "eye", "head", paint=paint, ao=False))
    return parts


# ------------------------------------------------------- MakeHuman head
def _top4(dense: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Joints and weights (n, 4) from dense per-bone weights (n, N_BONES)."""
    order = np.argsort(-dense, axis=1, kind="stable")[:, :4]
    w = np.take_along_axis(dense, order, axis=1)
    w = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-12)
    return order.astype(np.float32), w.astype(np.float32)


def mesh_cavity(v: np.ndarray, n: np.ndarray, radius: float = 0.02) -> np.ndarray:
    """Occlusion for an explicit mesh (no distance field): how much of the surface within
    ``radius`` rises above each vertex's tangent plane (eye sockets, nostrils, ears, the
    mouth's corners)."""
    occ = np.zeros(len(v))
    for s in range(0, len(v), 512):
        d = v[None, :, :] - v[s:s + 512, None, :]
        dist = np.linalg.norm(d, axis=2)
        near = np.where((dist > 1e-6) & (dist < radius), 1.0 - dist / radius, 0.0)
        cos = np.einsum("bnk,bk->bn", d, n[s:s + 512]) / np.maximum(dist, 1e-9)
        occ[s:s + 512] = (near * np.clip(cos - 0.1, 0.0, None)).sum(axis=1) / np.maximum(near.sum(axis=1), 1e-9)
    return np.clip(1.0 - 1.1 * occ, 0.35, 1.0).astype(np.float32)


def explicit_lods(v: np.ndarray, f: np.ndarray, dense: np.ndarray, share: float, slot: str, paint=None,
                  ao: np.ndarray | None = None, lods: int = len(LOD_BUDGETS)) -> list:
    """``mesh_lods`` for a mesh that already exists (the MakeHuman head and the layers grown
    from it): simplified in a chain, weights and occlusion carried along; LODs past ``lods``
    are left out."""
    from characters.mesher import vertex_normals
    attrs = {"w": dense, "ao": ao if ao is not None else np.ones(len(v), np.float32)}
    out = []
    for k, total in enumerate(LOD_BUDGETS):
        if k >= lods:
            out.append(None)
            continue
        target = max(int(share * total), 16)
        if len(f) > target:
            v, f, attrs = decimate(v, f, target, attrs=attrs)
        n = vertex_normals(v, f)
        joints, weights = _top4(attrs["w"])
        col = np.ones((len(v), 4), np.float32)
        if paint is not None:
            col[:, :3] = paint(v, n)
        col[:, 3] = attrs["ao"]
        out.append(Mesh(v.copy(), n, col, joints, weights, np.full(len(v), SLOT[slot], np.float32), f.copy()))
    return out


def _sub(v: np.ndarray, f: np.ndarray, keep_tri: np.ndarray):
    """The triangles ``keep_tri`` and their vertices, renumbered; also the old vertex ids."""
    f = f[keep_tri]
    used = np.unique(f)
    remap = np.full(len(v), -1, np.int64)
    remap[used] = np.arange(len(used))
    return used, remap[f]


def mh_hair(app: Appearance, head: dict, covered: bool) -> list:
    """Hair as a layer grown off the MakeHuman scalp: the scalp's triangles above the hairline,
    pushed out along the normals by the style's thickness, thinning to nothing at the hairline
    (where the scalp paint carries the colour) and under a helmet or cap. None at the far LOD."""
    if app.hair in ("shaved", "buzz"):
        return []
    from characters.mesher import vertex_normals
    v, f, dense = head["verts"], head["tris"], head["weights"]
    n = vertex_normals(v, f)
    r = v - head["landmarks"]["centre"]
    above = r[:, 2] - _hairline(r)
    used, tris = _sub(v, f, (above[f] > -0.004).all(axis=1))
    top, sides = {"crew": (0.011, 0.005), "short": (0.016, 0.009), "bun": (0.008, 0.007)}[app.hair]
    ru, au = r[used], above[used]
    up = _soft(ru[:, 2], -0.02, 0.07)
    taper = _soft(au, -0.002, 0.012)
    thick = (sides + (top - sides) * up) * taper
    thick += 0.0015 * C.value_noise(v[used], 0.012, app.seed + 31) * taper
    if covered:
        thick *= 1.0 - 0.9 * _soft(ru[:, 2], 0.015, 0.03)
    pos = v[used] + n[used] * (thick + 0.0006)[:, None]
    pieces = [explicit_lods(pos, tris, dense[used], SHARE["hair"], "hair", lods=2)]
    if app.hair == "bun":
        z = head["landmarks"]["centre"][2] + 0.035
        band = v[np.abs(v[:, 2] - z) < 0.006]
        back = band[:, 1].min() if len(band) else head["landmarks"]["centre"][1] - 0.09
        bun = S.Shape(prims=[S.ell((0.0, back - 0.012, z), (0.032, 0.03, 0.03), "head")])
        pieces.append(mesh_lods(bun, 0.004, SHARE["hair"] * 0.3, "hair", "head"))
    return pieces


def mh_beard(app: Appearance, head: dict) -> list:
    """Beard or moustache as a layer grown off the MakeHuman jaw, chin and upper lip."""
    if app.build.female or app.facial_hair not in ("short_beard", "full_beard", "moustache"):
        return []
    from characters.mesher import vertex_normals
    v, f, dense = head["verts"], head["tris"], head["weights"]
    lm = head["landmarks"]
    n = vertex_normals(v, f)
    r = v - lm["centre"]
    mz = lm["mouth_z"]
    if app.facial_hair == "moustache":
        region = np.maximum(np.abs(v[:, 2] - (mz + 0.012)) - 0.006, np.abs(r[:, 0]) - 0.026)
        region = np.maximum(region, 0.08 - r[:, 1])
    else:
        region = np.maximum(r[:, 2] + 0.03, -0.155 - r[:, 2])
        region = np.maximum(region, -0.005 - r[:, 1])
        lips = np.maximum(np.abs(v[:, 2] - mz) - 0.007, np.abs(r[:, 0]) - 0.02)
        region = np.maximum(region, -lips)
    used, tris = _sub(v, f, (region[f] < 0.004).all(axis=1))
    if not len(tris):
        return []
    thick = {"short_beard": 0.004, "full_beard": 0.009, "moustache": 0.004}[app.facial_hair]
    t = thick * (1.0 - _soft(region[used], -0.008, 0.002)) + 0.0012 * C.value_noise(v[used], 0.008, app.seed + 41)
    pos = v[used] + n[used] * (np.maximum(t, 0.0) + 0.0005)[:, None]
    return [explicit_lods(pos, tris, dense[used], SHARE["hair"] * 0.6, "hair", lods=2)]


def mh_head(app: Appearance, head: dict) -> list:
    """The MakeHuman skin: painted like the procedural head, with mesh occlusion."""
    from characters.mesher import vertex_normals
    v, f = head["verts"], head["tris"]
    ao = mesh_cavity(v, vertex_normals(v, f))
    return explicit_lods(v, f, head["weights"], SHARE["head"], "skin", paint=head_paint(app, head["landmarks"]), ao=ao)


HAIR_SRGB = None
EYE_SRGB = None


def _colours():
    global HAIR_SRGB, EYE_SRGB
    from characters.appearance import EYE_COLOURS, HAIR_COLOURS
    HAIR_SRGB, EYE_SRGB = HAIR_COLOURS, EYE_COLOURS


_colours()


# ---------------------------------------------------------------- assemble
def assemble(app: Appearance, style: str) -> list[Mesh]:
    """Every piece of one soldier in one team's kit, per LOD."""
    look: TeamLook = app.team[style]
    body = H.body(app.build)
    head_shape, lm = H.head(app.face, app.build)
    head_shape.add(*H.eyelids(lm))
    pieces = []        # lists of per-LOD meshes
    covered = look.headgear in ("helmet", "cap")
    mh = None
    if look.headgear != "balaclava" and app.makehuman:
        from characters import makehuman
        mh = makehuman.head_region(makehuman.data_dir(), app)
    if mh is not None:
        lm = mh["landmarks"]
        pieces.append(mh_head(app, mh))
        pieces.extend(mh_hair(app, mh, covered))
        pieces.extend(mh_beard(app, mh))
    elif look.headgear != "balaclava":
        pieces.append(mesh_lods(head_shape, 0.0035, SHARE["head"], "skin", "body", paint=head_paint(app, lm)))
        hs = hair_shape(app, head_shape, covered)
        if hs is not None:
            pieces.append(mesh_lods(hs, 0.004, SHARE["hair"], "hair", "head"))
        bs = beard_shape(app, head_shape, lm)
        if bs is not None:
            pieces.append(mesh_lods(bs, 0.003, SHARE["hair"] * 0.6, "hair", "head"))
    else:
        # only the eyes' surroundings show: a coarser head under the knit
        pieces.append(mesh_lods(head_shape, 0.004, SHARE["head"] * 0.5, "skin", "body", paint=head_paint(app, lm)))
    pieces.extend(eyes(app, lm))
    # hands
    hands = {s: H.hand(s, app.build) for s in ("l", "r")}
    if look.gloves:
        for s, g in C.gloves(hands, app.seed).items():
            pieces.append(mesh_lods(g, 0.0028, SHARE["hands"] / 2, "gloves", "body", tag_shape=hands[s]))
    else:
        for s, hsh in hands.items():
            pieces.append(mesh_lods(hsh, 0.0028, SHARE["hands"] / 2, "skin", "body"))
    # clothing
    pieces.append(mesh_lods(C.shirt(body, app.seed, look.sleeves), 0.009, SHARE["shirt"], "shirt", "body",
                            tag_shape=body))
    pieces.append(mesh_lods(C.trousers(body, app.seed), 0.009, SHARE["trousers"], "trousers", "body",
                            tag_shape=body))
    pieces.append(mesh_lods(C.boots(body, app.seed), 0.006, SHARE["boots"], "boots", "body", tag_shape=body))
    # gear
    gear = G.plate_carrier(body, style, look.layout) + G.belt(look.layout) + G.knee_pads()
    if look.headgear == "helmet":
        gear += G.helmet(style, look.cover, look.ear_pro, look.goggles)
    elif look.headgear == "cap":
        gear += G.cap()
        if look.ear_pro:
            gear += [p for p in G.helmet(style, False, True, False) if p.name == "ear_pro"]
    else:
        gear += G.balaclava(head_shape, lm)
    if look.glasses:
        gear += G.glasses(lm)
    # the gear's budget by size (bounding-box area, softened so small pieces stay recognisable)
    size = []
    for piece in gear:
        lo, hi = piece.shape.bounds()
        e = np.asarray(hi) - np.asarray(lo)
        size.append((2.0 * (e[0] * e[1] + e[1] * e[2] + e[2] * e[0])) ** 0.75)
    size = np.asarray(size)
    for piece, sz in zip(gear, size):
        pieces.append(mesh_lods(piece.shape, piece.cell, SHARE["gear"] * sz / size.sum(), piece.slot, piece.skin,
                                tag_shape=body if piece.skin == "body" else None))
    return [Mesh.concat([p[k] for p in pieces]) for k in range(len(LOD_BUDGETS))]


# ------------------------------------------------------------- GPU / cache
_FORMAT = None


def vertex_format():
    """position, normal, colour (paint + cavity), 4 bone indices, 4 weights, palette slot."""
    global _FORMAT
    if _FORMAT is None:
        from panda3d.core import Geom, GeomVertexArrayFormat, GeomVertexFormat, InternalName
        arr = GeomVertexArrayFormat()
        arr.addColumn(InternalName.getVertex(), 3, Geom.NTFloat32, Geom.CPoint)
        arr.addColumn(InternalName.getNormal(), 3, Geom.NTFloat32, Geom.CNormal)
        arr.addColumn(InternalName.getColor(), 4, Geom.NTFloat32, Geom.CColor)
        arr.addColumn(InternalName.make("skin_joints"), 4, Geom.NTFloat32, Geom.COther)
        arr.addColumn(InternalName.make("skin_weights"), 4, Geom.NTFloat32, Geom.COther)
        arr.addColumn(InternalName.make("char_slot"), 1, Geom.NTFloat32, Geom.COther)
        _FORMAT = GeomVertexFormat.registerFormat(GeomVertexFormat(arr))
    return _FORMAT


def geom_node(mesh: Mesh, name: str = "character"):
    """One Geom (one draw call) for the whole mesh."""
    from panda3d.core import Geom, GeomNode, GeomTriangles, GeomVertexData
    n = len(mesh.pos)
    data = np.zeros((n, 19), np.float32)
    data[:, 0:3] = mesh.pos
    data[:, 3:6] = mesh.nrm
    data[:, 6:10] = mesh.col
    data[:, 10:14] = mesh.joints
    w = mesh.weights / np.maximum(mesh.weights.sum(axis=1, keepdims=True), 1e-9)
    data[:, 14:18] = w
    data[:, 18] = mesh.slot
    vdata = GeomVertexData(name, vertex_format(), Geom.UHStatic)
    vdata.uncleanSetNumRows(n)
    memoryview(vdata.modifyArray(0)).cast("B")[:] = data.tobytes()
    prim = GeomTriangles(Geom.UHStatic)
    prim.setIndexType(Geom.NTUint32)
    h = prim.modifyVertices()
    h.uncleanSetNumRows(mesh.tris.size)
    memoryview(h).cast("B")[:] = np.ascontiguousarray(mesh.tris, np.uint32).tobytes()
    geom = Geom(vdata)
    geom.addPrimitive(prim)
    node = GeomNode(name)
    node.addGeom(geom)
    return node


def save(path, lods: list) -> None:
    arrays = {}
    for k, m in enumerate(lods):
        for field in ("pos", "nrm", "col", "joints", "weights", "slot", "tris"):
            a = getattr(m, field)
            arrays[f"{k}_{field}"] = a.astype(np.int32 if field == "tris" else np.float32)
    np.savez_compressed(path, **arrays)


def load(path) -> list:
    with np.load(path) as z:
        n = len({k.split("_")[0] for k in z.files})
        return [Mesh(*(z[f"{k}_{f}"] for f in ("pos", "nrm", "col", "joints", "weights", "slot")),
                     z[f"{k}_tris"].astype(np.int64)) for k in range(n)]
