"""The MakeHuman base human (MPFB2, CC0 1.0) for the soldiers' faces.

``tools/download_assets.py --only characters`` fetches a pinned set of files
into assets/characters/mpfb2/ after checking the licence (OVERHAUL_PLAN 6).
This module reads them (plain text: OBJ, gzipped ``.target`` offset lists,
JSON rig and weights; no new dependency) and turns one appearance into a
head-and-neck mesh on the game skeleton (the body, hands and clothing stay
procedural, characters/human.py: they are under clothing, gloves and gear):

1. **Shape**: the base mesh plus weighted targets - ethnicity, gender and
   build ("macrodetails"), then face proportions (head shape, nose, mouth,
   chin, cheekbones, brows, forehead, ears, eyes, neck) from the appearance's
   face numbers.
2. **Joints**: every rig joint is the centre of a named vertex group of the
   shaped mesh, so joints follow the shape.
3. **Placement**: the head and neck move onto the game skeleton's joints and
   the head's middle onto the procedural head's (``place_head``), so every
   face shares one skeleton, one set of hit boxes and the helmet (B-3).
4. **Weights**: MakeHuman's game-engine weights renamed to the game's bones.

Without the files everything falls back to characters/human.py; nothing
downloaded is ever required to play.

MakeHuman space: decimetres, Y up, facing +Z, the character's left at +X.
Game space: metres, Z up, facing +Y, the character's left at -X:
``game = (-x, z, y) / 10``.
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT_REL = Path("characters") / "mpfb2"
DATA_REL = Path("src") / "mpfb" / "data"

# MakeHuman game-engine bone -> game bone
BONE_MAP = {
    "Root": "root", "pelvis": "pelvis", "spine_01": "spine_01", "spine_02": "spine_02", "spine_03": "spine_03",
    "neck_01": "neck", "head": "head",
}
for _s in ("l", "r"):
    BONE_MAP.update({
        f"clavicle_{_s}": f"clavicle_{_s}", f"upperarm_{_s}": f"upperarm_{_s}", f"lowerarm_{_s}": f"lowerarm_{_s}",
        f"hand_{_s}": f"hand_{_s}", f"thigh_{_s}": f"thigh_{_s}", f"calf_{_s}": f"calf_{_s}",
        f"foot_{_s}": f"foot_{_s}", f"ball_{_s}": f"toe_{_s}",
    })
    for _k in (1, 2, 3):
        BONE_MAP[f"thumb_0{_k}_{_s}"] = f"thumb_0{_k}_{_s}"
        BONE_MAP[f"index_0{_k}_{_s}"] = f"index_0{_k}_{_s}"
        for _f in ("middle", "ring", "pinky"):
            BONE_MAP[f"{_f}_0{_k}_{_s}"] = f"fingers_0{_k}_{_s}"


def data_dir() -> Path | None:
    """assets/characters/mpfb2/src/mpfb/data if the fetched files are complete, else None."""
    import os
    from engine import paths
    if os.environ.get("COLD_SECTOR_OFFLINE", "") == "1":
        return None                                # the offline check: downloaded characters ignored
    root = Path(paths.ASSETS_DIR) / ROOT_REL
    manifest = root / "manifest.json"
    if not manifest.exists():
        return None
    try:
        m = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    if not all((root / f).exists() for f in m.get("files", [])):
        return None
    return root / DATA_REL


def to_game(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float64)
    return np.stack([-v[..., 0], v[..., 2], v[..., 1]], axis=-1) * 0.1


class Base:
    """The parsed base mesh: vertices, quads split into triangles per group, vertex groups."""

    def __init__(self, data: Path):
        verts, faces, group_faces = [], [], {}
        cur = ""
        with open(data / "3dobjs" / "base.obj", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("v "):
                    verts.append([float(x) for x in line.split()[1:4]])
                elif line.startswith("g "):
                    cur = line[2:].strip()
                elif line.startswith("f "):
                    idx = [int(t.split("/")[0]) - 1 for t in line.split()[1:]]
                    group_faces.setdefault(cur, []).append(len(faces))
                    faces.append(idx)
        self.verts = np.asarray(verts, np.float64)
        self.faces = faces
        self.group_faces = {k: np.asarray(v, np.int64) for k, v in group_faces.items()}

    def group_vertices(self, name: str) -> np.ndarray:
        fs = self.group_faces.get(name)
        if fs is None:
            return np.zeros(0, np.int64)
        return np.unique(np.concatenate([np.asarray(self.faces[i]) for i in fs]))

    def triangles(self, groups=("body",)) -> np.ndarray:
        tris = []
        for g in groups:
            for i in self.group_faces.get(g, []):
                f = self.faces[i]
                for k in range(1, len(f) - 1):
                    tris.append((f[0], f[k], f[k + 1]))
        return np.asarray(tris, np.int64)


def read_target(path: Path, n: int) -> tuple[np.ndarray, np.ndarray]:
    """A sparse target: (vertex indices, offsets in MakeHuman units)."""
    opener = gzip.open if path.suffix == ".gz" else open
    idx, off = [], []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) != 4 or parts[0].startswith("#"):
                continue
            i = int(parts[0])
            if 0 <= i < n:
                idx.append(i)
                off.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return np.asarray(idx, np.int64), np.asarray(off, np.float64).reshape(-1, 3)


@lru_cache(maxsize=1)
def load(data_str: str):
    """The base mesh, the rig and the weights (cached per process)."""
    data = Path(data_str)
    base = Base(data)
    rig = json.loads((data / "rigs" / "standard" / "rig.game_engine.json").read_text())
    weights = json.loads((data / "rigs" / "standard" / "weights.game_engine.json").read_text())["weights"]
    return base, rig, weights


@lru_cache(maxsize=256)
def target(data_str: str, rel: str):
    base, _, _ = load(data_str)
    return read_target(Path(data_str) / "targets" / rel, len(base.verts))


def shaped(data: Path, mix: dict[str, float]) -> np.ndarray:
    """Base vertices plus ``sum(weight * target)`` (mix: target path relative to targets/ -> weight)."""
    base, _, _ = load(str(data))
    v = base.verts.copy()
    for rel, w in mix.items():
        if abs(w) < 1e-4:
            continue
        idx, off = target(str(data), rel)
        np.add.at(v, idx, off * w)
    return v


def joints(data: Path, verts: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Each rig bone's (head, tail) in game space, from the centres of its joint vertex groups."""
    base, rig, _ = load(str(data))
    centre = {}

    def at(spec) -> np.ndarray:
        name = spec.get("cube_name")
        if name not in centre:
            vi = base.group_vertices(name)
            centre[name] = verts[vi].mean(axis=0) if len(vi) else None
        c = centre[name]
        if c is None:
            # no such group: the rig's own default (Blender space, metres: x, -z, y -> game)
            d = np.asarray(spec["default_position"], np.float64)
            return np.array([-d[0], -d[1], d[2]])
        return to_game(c)

    return {bone: (at(b["head"]), at(b["tail"])) for bone, b in rig.items() if "head" in b and "tail" in b}


# ---------------------------------------------------------------- placement
def weight_matrix(data: Path, n: int) -> np.ndarray:
    """MakeHuman's game-engine weights as a dense (n, bones) matrix over ``list(BONE_MAP)``,
    rows summing to 1 (unweighted vertices go to the pelvis)."""
    _, _, weights = load(str(data))
    names = list(BONE_MAP)
    W = np.zeros((n, len(names)))
    for k, name in enumerate(names):
        for vi, w in weights.get(name, []):
            W[int(vi), k] = float(w)
    W[W.sum(axis=1) <= 1e-9, names.index("pelvis")] = 1.0
    return W / W.sum(axis=1, keepdims=True)


# the procedural head's vertical centre (characters/human.py: crown 1.743, chin 1.503), which the
# head hit sphere and the helmet are built around
HEAD_MID_Z = 1.623


def place_head(data: Path, verts_mh: np.ndarray, W: np.ndarray) -> np.ndarray:
    """Shaped MakeHuman vertices -> game space, head and neck only.

    Both rest poses hold the head upright, so the head and the neck only move: each onto its
    game joint, blended by the head's weight (the neck stretches or shortens in between). The
    head is then lifted or lowered so its middle (crown to chin) sits where the procedural
    head's does: the hit sphere and the helmet fit every face. Turning the bones onto the
    game's axes instead would pitch the face."""
    from gameplay import skeleton as sk
    names = list(BONE_MAP)
    j = joints(data, verts_mh)
    g = to_game(verts_mh)
    a = W[:, names.index("head")][:, None]
    t_head = sk.REST_WORLD[sk.INDEX["head"], 3, :3] - j["head"][0]
    t_neck = sk.REST_WORLD[sk.INDEX["neck"], 3, :3] - j["neck_01"][0]
    v = g + a * t_head + (1.0 - a) * t_neck
    base, _, _ = load(str(data))
    body = base.group_vertices("body")
    head = body[W[body, names.index("head")] > 0.95]
    mid = 0.5 * (v[head, 2].max() + v[head, 2].min())
    v[:, 2] += a[:, 0] * (HEAD_MID_Z - mid)
    return v


# ----------------------------------------------------------------- appearance
def _pair(mix: dict, decr: str, incr: str, x: float) -> None:
    """A signed amount x (-1..1) on a decrease/increase target pair."""
    x = max(-1.0, min(1.0, x))
    if x >= 0:
        mix[incr] = mix.get(incr, 0.0) + x
    else:
        mix[decr] = mix.get(decr, 0.0) - x


def _levels(u: float) -> dict[str, float]:
    """0..1 over MakeHuman's min / average / max levels (piecewise linear)."""
    u = max(0.0, min(1.0, u))
    if u < 0.5:
        return {"min": 1.0 - 2.0 * u, "average": 2.0 * u}
    return {"average": 2.0 - 2.0 * u, "max": 2.0 * u - 1.0}


def mix_for(app) -> dict[str, float]:
    """Target weights for one appearance (characters/appearance.py)."""
    import random
    f = app.face
    g = "female" if app.build.female else "male"
    mix: dict[str, float] = {}
    # ancestry: three fractions from the bot's name, leaning with the skin tone
    r = random.Random(f"ancestry:{app.name}:{app.seed}")
    raw = {"african": r.random() * (0.25 + 1.5 * app.melanin), "asian": r.random() * 0.8,
           "caucasian": r.random() * (0.25 + 1.5 * (1.0 - app.melanin))}
    total = sum(raw.values())
    for e, w in raw.items():
        mix[f"macrodetails/{e}-{g}-young.target.gz"] = w / total
    # build: muscle and weight levels (bilinear over MakeHuman's universal targets)
    muscle = _levels(app.build.muscle)
    weight = _levels(0.5 + 0.35 * app.build.mass)
    for m, wm in muscle.items():
        for w, ww in weight.items():
            mix[f"macrodetails/universal-{g}-young-{m}muscle-{w}weight.target.gz"] = wm * ww
    # face: each proportion a signed amount on its target pair
    _pair(mix, "head/head-scale-horiz-decr.target.gz", "head/head-scale-horiz-incr.target.gz", (f.width - 1) / 0.08)
    _pair(mix, "head/head-fat-decr.target.gz", "head/head-fat-incr.target.gz", 0.4 * app.build.mass)
    shape = r.choice(("oval", "round", "square", "triangular"))
    mix[f"head/head-{shape}.target.gz"] = r.uniform(0.2, 0.6)
    _pair(mix, "chin/chin-width-decr.target.gz", "chin/chin-width-incr.target.gz", (f.jaw - 1) / 0.16)
    _pair(mix, "chin/chin-prominent-decr.target.gz", "chin/chin-prominent-incr.target.gz", (f.chin - 1) / 0.2)
    _pair(mix, "chin/chin-height-decr.target.gz", "chin/chin-height-incr.target.gz", f.chin_forward / 0.008)
    _pair(mix, "nose/nose-scale-vert-decr.target.gz", "nose/nose-scale-vert-incr.target.gz", (f.nose_length - 1) / 0.16)
    _pair(mix, "nose/nose-scale-horiz-decr.target.gz", "nose/nose-scale-horiz-incr.target.gz", (f.nose_width - 1) / 0.2)
    _pair(mix, "nose/nose-hump-decr.target.gz", "nose/nose-hump-incr.target.gz", (f.nose_bridge - 1) / 0.24)
    _pair(mix, "nose/nose-trans-up.target.gz", "nose/nose-trans-down.target.gz", f.nose_hook / 0.004)
    _pair(mix, "mouth/mouth-scale-horiz-decr.target.gz", "mouth/mouth-scale-horiz-incr.target.gz",
          (f.mouth_width - 1) / 0.12)
    lips = (f.lips - 1) / 0.24 + (0.3 if app.build.female else 0.0)
    _pair(mix, "mouth/mouth-upperlip-volume-decr.target.gz", "mouth/mouth-upperlip-volume-incr.target.gz", lips)
    _pair(mix, "mouth/mouth-lowerlip-volume-decr.target.gz", "mouth/mouth-lowerlip-volume-incr.target.gz", lips)
    for s in ("l", "r"):
        _pair(mix, f"cheek/{s}-cheek-bones-decr.target.gz", f"cheek/{s}-cheek-bones-incr.target.gz",
              (f.cheekbones - 1) / 0.24)
        _pair(mix, f"ears/{s}-ear-scale-decr.target.gz", f"ears/{s}-ear-scale-incr.target.gz", (f.ears - 1) / 0.16)
        _pair(mix, f"eyes/{s}-eye-scale-decr.target.gz", f"eyes/{s}-eye-scale-incr.target.gz", r.uniform(-0.4, 0.4))
    _pair(mix, "eyebrows/eyebrows-trans-down.target.gz", "eyebrows/eyebrows-trans-up.target.gz", (1 - f.brow) / 0.3)
    _pair(mix, "forehead/forehead-trans-forward.target.gz", "forehead/forehead-trans-backward.target.gz",
          f.forehead_slope / 0.012)
    _pair(mix, "forehead/forehead-scale-vert-decr.target.gz", "forehead/forehead-scale-vert-incr.target.gz",
          (f.cranium_length - 1) / 0.08)
    _pair(mix, "neck/neck-scale-horiz-decr.target.gz", "neck/neck-scale-horiz-incr.target.gz",
          (f.neck - 1) / 0.12 + 0.3 * app.build.mass)
    return mix


def group_centre(data: Path, verts_game: np.ndarray, name: str) -> np.ndarray | None:
    base, _, _ = load(str(data))
    vi = base.group_vertices(name)
    return verts_game[vi].mean(axis=0) if len(vi) else None


def head_region(data: Path, app) -> dict | None:
    """The shaped head and neck on the game skeleton: vertices, triangles, weights over the
    game's bones (n, N_BONES), and landmarks in characters/human.py's form (eye centres and
    radius, the paint centre, the mouth line). None without the fetched files."""
    if data is None:
        return None
    from gameplay import skeleton as sk
    base, _, _ = load(str(data))
    v_mh = shaped(data, mix_for(app))
    W = weight_matrix(data, len(v_mh))
    v = place_head(data, v_mh, W)
    names = list(BONE_MAP)
    head_w = W[:, names.index("head")] + W[:, names.index("neck_01")]
    tris = base.triangles(("body",))
    # down to well inside the shirt collar (its top is about 1.50 m)
    keep = (head_w[tris] >= 0.2).all(axis=1) & (v[tris][:, :, 2] > 1.38).all(axis=1)
    tris = tris[keep]
    used = np.unique(tris)
    remap = np.full(len(v), -1, np.int64)
    remap[used] = np.arange(len(used))
    gw = np.zeros((len(used), sk.N_BONES))
    for k, mh in enumerate(names):
        col = W[used, k]
        if col.any():
            gw[:, sk.INDEX[BONE_MAP[mh]]] += col
    eyes = sorted((group_centre(data, v, "joint-l-eye"), group_centre(data, v, "joint-r-eye")), key=lambda e: e[0])
    # the mouth line: the corners are the vertices the mouth-width target moves most
    # ("joint-mouth" is the jaw's pivot, above the nose)
    idx, off = target(str(data), "mouth/mouth-scale-horiz-incr.target.gz")
    corners = idx[np.argsort(-np.abs(off[:, 0]))[:6]]
    mouth_z = float(v[corners, 2].mean())
    centre = 0.5 * (eyes[0] + eyes[1]) - np.array([0.0, 0.069, 0.008])     # where human.head's eyes sit
    lm = {"eyes": eyes, "eye_radius": 0.0118, "centre": centre, "mouth_z": mouth_z}
    return {"verts": v[used], "tris": remap[tris], "weights": gw, "landmarks": lm}
