"""Procedural weapon meshes from data/weapon_models.json.

Parts are bevelled boxes, cylinders and spheres batched per (animation
group, material), so a whole gun is a handful of draw calls while the
magazine, bolt, slide or pump stay on separate nodes for animation.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from panda3d.core import LVecBase4f, NodePath, Point3

from engine import paths
from engine.geometry import MeshBuilder

AXIS_HPR = {"x": (0, 0, 90), "y": (0, -90, 0), "z": (0, 0, 0)}
_DEFS = None


def model_defs() -> dict:
    global _DEFS
    if _DEFS is None:
        with open(paths.DATA_DIR / "weapon_models.json", "r", encoding="utf-8") as f:
            _DEFS = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return _DEFS


@dataclass
class WeaponModel:
    key: str
    root: NodePath
    groups: dict[str, NodePath] = field(default_factory=dict)
    anchors: dict[str, Point3] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def anchor(self, name: str) -> Point3:
        return Point3(self.anchors.get(name, Point3(0, 0, 0)))


def build_weapon_model(materials, key: str, parent: NodePath | None = None, name: str | None = None) -> WeaponModel:
    d = model_defs()[key]
    root = NodePath(name or f"weapon:{key}") if parent is None else parent.attachNewNode(name or f"weapon:{key}")
    builders: dict[tuple[str, str], MeshBuilder] = {}
    emissive = []
    for part in d["parts"]:
        group = part.get("group", "body")
        mat = part.get("mat", "gun_metal")
        mb = MeshBuilder()
        if "emit" in part:
            emissive.append((part, mb))
        else:
            mb = builders.setdefault((group, mat), MeshBuilder())
        uv = materials.get(mat).uv_scale
        shape = part["shape"]
        pos = part["pos"]
        if shape == "box":
            mb.add_chamfer_box(pos, part["size"], bevel=part.get("bevel", 0.004), hpr=part.get("hpr", (0, 0, 0)),
                               uv_scale=uv)
        elif shape == "cyl":
            hpr = AXIS_HPR[part.get("axis", "y")]
            r = part["radius"]
            segs = max(10, min(28, int(r * 900)))
            mb.add_cylinder(pos, r, part["length"], segments=segs, uv_scale=uv, hpr=hpr)
        elif shape == "sphere":
            r = part["radius"]
            mb.add_sphere(pos, r, rings=10 if r < 0.02 else 16, segments=16 if r < 0.02 else 28, uv_scale=uv)
    groups = {"body": root.attachNewNode("body")}
    for (group, mat), mb in builders.items():
        gnode = groups.get(group)
        if gnode is None:
            gnode = groups[group] = root.attachNewNode(group)
        np_ = gnode.attachNewNode(mb.build(f"{key}:{group}:{mat}"))
        materials.get(mat).apply(np_)
    for part, mb in emissive:
        gnode = groups.setdefault(part.get("group", "body"), root.attachNewNode(part.get("group", "body")))
        np_ = gnode.attachNewNode(mb.build(f"{key}:emit"))
        materials.get(part.get("mat", "pbr_white_rough")).apply(np_)
        e = part["emit"]
        np_.setShaderInput("u_emission", LVecBase4f(e[0], e[1], e[2], 0))
    anchors = {k: Point3(*v) for k, v in d.get("anchors", {}).items()}
    return WeaponModel(key, root, groups, anchors, d)


_PROTOTYPES: dict[tuple, WeaponModel] = {}


def shared_weapon_model(materials, key: str, parent: NodePath, name: str | None = None,
                        flatten: str = "all") -> WeaponModel:
    """A copy of a model that is built only once per key.

    Building a model bevels dozens of boxes in numpy: 50-300 ms for a rifle or
    a gadget. Bots switch weapons, drop their gun when they die and deploy
    gadgets in the middle of a round, and each of those used to rebuild the
    model inside the 64 Hz tick (docs/baseline). The prototype is built and
    flattened the first time; ``copyTo`` then shares its Geoms copy-on-write,
    so a copy costs microseconds and renders exactly the same.

    ``flatten``: "all" merges everything per material (third-person guns,
    pickups); "body" merges only the static body group and keeps the other
    groups (an LED) as separate nodes; "none" keeps the hierarchy."""
    pkey = (id(materials), key, flatten)
    proto = _PROTOTYPES.get(pkey)
    if proto is None:
        proto = build_weapon_model(materials, key, None, f"proto:{key}")
        if flatten == "all":
            proto.root.flattenStrong()
            proto.groups = {"body": proto.root}
        elif flatten == "body" and proto.groups.get("body") is not None:
            proto.groups["body"].flattenStrong()
        _PROTOTYPES[pkey] = proto
    root = proto.root.copyTo(parent)
    root.setName(name or f"weapon:{key}")
    if flatten == "all":
        groups = {"body": root}
    else:
        groups = {g: root.find(g) for g in proto.groups}
        groups = {g: np_ for g, np_ in groups.items() if not np_.isEmpty()}
    return WeaponModel(key, root, groups, dict(proto.anchors), proto.meta)


def segment_hpr(a: Point3, b: Point3) -> tuple[float, float]:
    """Heading/pitch that aim local +Y from a to b."""
    dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
    h = math.degrees(math.atan2(-dx, dy))
    p = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return h, p


def build_arm(materials, parent: NodePath, hand: Point3, elbow: Point3, side: int, name: str) -> NodePath:
    """Gloved hand + forearm with a camo sleeve, running from the hand back to
    the elbow target (all in the parent's space). Returns the arm node, whose
    origin sits at the hand so it can be animated independently."""
    arm = parent.attachNewNode(name)
    arm.setPos(hand)
    h, p = segment_hpr(hand, elbow)
    length = (elbow - hand).length()
    glove = MeshBuilder()
    # palm wrapped around the grip, fingers and thumb
    glove.add_chamfer_box((0, 0, 0), (0.05, 0.085, 0.045), bevel=0.014)
    glove.add_chamfer_box((side * -0.03, 0.025, 0.0), (0.02, 0.075, 0.035), bevel=0.008)
    glove.add_chamfer_box((side * 0.022, 0.012, 0.022), (0.018, 0.05, 0.018), bevel=0.007)
    g = arm.attachNewNode(glove.build(f"{name}:glove"))
    g.setHpr(h, p, 0)
    materials.get("glove").apply(g)
    fore = MeshBuilder()
    fore.add_chamfer_box((0, length * 0.18, 0), (0.06, length * 0.36, 0.055), bevel=0.018)
    f = arm.attachNewNode(fore.build(f"{name}:wrist"))
    f.setHpr(h, p, 0)
    f.setPos(0, 0, 0)
    f.setY(f, 0.04)
    materials.get("glove").apply(f)
    sleeve = MeshBuilder()
    sleeve.add_chamfer_box((0, length * 0.62, 0), (0.095, length * 0.66, 0.088), bevel=0.03)
    s = arm.attachNewNode(sleeve.build(f"{name}:sleeve"))
    s.setHpr(h, p, 0)
    materials.get("sleeve_camo").apply(s)
    return arm
