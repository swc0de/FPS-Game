"""The MakeHuman base human (MPFB2, CC0 1.0) for the soldiers' faces and hands.

``tools/download_assets.py --only characters`` fetches a pinned set of files
into assets/characters/mpfb2/ after checking the licence (OVERHAUL_PLAN 6).
This module reads them (plain text: OBJ, gzipped ``.target`` offset lists,
JSON rig and weights; no new dependency) and turns one appearance into a
head-and-neck mesh and hands on the game skeleton:

1. **Shape**: the base mesh plus weighted targets - ethnicity, gender and
   build ("macrodetails"), then face proportions (head shape, nose, mouth,
   chin, cheekbones, brows, forehead, ears, eyes, neck) from the appearance's
   face numbers.
2. **Joints**: every rig joint is the centre of a named vertex group of the
   shaped mesh, so joints follow the shape.
3. **Retarget**: each MakeHuman bone gets the similarity transform (turn,
   uniform scale, move) that puts its head and tail on the game skeleton's
   bind pose; vertices follow by linear blend with MakeHuman's own weights,
   so the mesh lands on the shared skeleton (B-3: one skeleton, one set of
   hit boxes for every appearance).
4. **Weights**: MakeHuman's game-engine weights renamed to the game's bones
   (middle, ring and pinky become the one "fingers" chain, ball becomes toe),
   with the twist shares of characters/weights.py.

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


# ------------------------------------------------------------------- retarget
def _rotation_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The smallest rotation (3x3, column vectors) taking direction a onto direction b."""
    a = a / max(np.linalg.norm(a), 1e-12)
    b = b / max(np.linalg.norm(b), 1e-12)
    v = np.cross(a, b)
    c = float(a @ b)
    if c < -0.999999:
        axis = np.cross(a, [1.0, 0.0, 0.0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0.0, 1.0, 0.0])
        axis /= np.linalg.norm(axis)
        return 2.0 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1.0 + c)


def _game_segment(bone: str):
    from characters.weights import SEGMENTS
    from gameplay import skeleton as sk
    if bone in SEGMENTS:
        return SEGMENTS[bone]
    p = sk.REST_WORLD[sk.INDEX[bone], 3, :3]
    return p, p + np.array([0.0, 0.0, 0.1])


FINGER_PREFIXES = ("thumb", "index", "middle", "ring", "pinky")


def bone_transforms(data: Path, j: dict) -> dict[str, tuple[np.ndarray, float, np.ndarray, np.ndarray]]:
    """Per MakeHuman bone: (R, s, mh_head, game_head) with x' = game_head + s R (x - mh_head).
    Fingers move rigidly with their hand (MakeHuman's finger shapes are kept)."""
    out = {}
    for mh, game in BONE_MAP.items():
        if mh not in j or mh.split("_")[0] in FINGER_PREFIXES or mh == "Root":
            continue
        h, t = j[mh]
        gh, gt = _game_segment(game)
        R = _rotation_between(t - h, gt - gh)
        s = float(np.linalg.norm(gt - gh) / max(np.linalg.norm(t - h), 1e-9))
        if mh.startswith("hand_") or mh in ("head", "neck_01"):
            s = 1.0              # short or arbitrary segments: keep the natural size, only turn and move
        out[mh] = (R, s, h, gh)
    for mh in BONE_MAP:
        if mh.split("_")[0] in FINGER_PREFIXES and mh in j:
            out[mh] = out[f"hand_{mh[-1]}"]
    out["Root"] = out["pelvis"]
    return out


def retarget(data: Path, verts_mh: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shaped MakeHuman vertices -> game bind pose. Returns (game vertices, per-vertex
    MakeHuman weights as a (n, bones) dense matrix over ``list(BONE_MAP)``)."""
    _, _, weights = load(str(data))
    j = joints(data, verts_mh)
    tf = bone_transforms(data, j)
    v = to_game(verts_mh)
    names = list(BONE_MAP)
    W = np.zeros((len(v), len(names)))
    for k, name in enumerate(names):
        for vi, w in weights.get(name, []):
            W[int(vi), k] = float(w)
    sums = W.sum(axis=1)
    W[sums <= 1e-9, names.index("pelvis")] = 1.0
    W /= W.sum(axis=1, keepdims=True)
    out = np.zeros_like(v)
    for k, name in enumerate(names):
        sel = W[:, k] > 0
        if not sel.any():
            continue
        R, s, h, gh = tf[name]
        moved = gh + s * (v[sel] - h) @ R.T
        out[sel] += W[sel, k:k + 1] * moved
    return out, W
