"""Level loading: JSON layout -> batched geometry, colliders, lights, bakes.

Map files live in maps/data/*.json:

    {
      "name": "...",
      "environment": {...sky / sun / fog settings...},
      "bounds": [[xmin, ymin, zmin], [xmax, ymax, zmax]],
      "pieces": [ {"type": "<prefab>", ...}, ... ],
      "camera_shots": [ {"name": ..., "pos": [...], "hpr": [...]}, ... ]
    }

Geometry is batched per material (one GeomNode per material) for few draw
calls; collision uses simple Bullet boxes; occluders feed the sky
visibility bake.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from panda3d.core import LVecBase4f, Point3, Vec3

from engine import paths
from engine.geometry import MeshBuilder
from maps.prefabs import PREFABS
from render import sky_visibility
from render.local_lights import LocalLight, kelvin_to_rgb


class LevelContext:
    def __init__(self, materials):
        self.materials = materials
        self.builders: dict[str, MeshBuilder] = {}
        self.colliders: list[tuple] = []     # (center, size, hpr, surface)
        self.occluders: list[sky_visibility.Occluder] = []
        self.lights: list[dict] = []
        self.spawns: list[dict] = []
        self.fixtures: list[tuple] = []      # (center, size, emission rgb)
        self.props: list[tuple[str, dict]] = []  # game objects created after geometry
        self.zones: list[dict] = []          # bomb sites etc. {"name", "kind", "min", "max"}
        self.callouts: list[dict] = []       # named areas (radio callouts, bot navigation)
        self._stack = [((0.0, 0.0, 0.0), 0.0)]

    # ---------------------------------------------------------- transforms
    def push_transform(self, offset, heading: float) -> None:
        (ox, oy, oz), h = self._stack[-1]
        a = math.radians(h)
        wx = ox + offset[0] * math.cos(a) - offset[1] * math.sin(a)
        wy = oy + offset[0] * math.sin(a) + offset[1] * math.cos(a)
        self._stack.append(((wx, wy, oz + offset[2]), h + heading))

    def pop_transform(self) -> None:
        self._stack.pop()

    def _xf(self, p, hpr=(0, 0, 0)):
        (ox, oy, oz), h = self._stack[-1]
        if h == 0 and ox == 0 and oy == 0 and oz == 0:
            return tuple(p), tuple(hpr)
        a = math.radians(h)
        x = ox + p[0] * math.cos(a) - p[1] * math.sin(a)
        y = oy + p[0] * math.sin(a) + p[1] * math.cos(a)
        return (x, y, oz + p[2]), (hpr[0] + h, hpr[1], hpr[2])

    def _xf_dir(self, d):
        h = self._stack[-1][1]
        a = math.radians(h)
        return (d[0] * math.cos(a) - d[1] * math.sin(a), d[0] * math.sin(a) + d[1] * math.cos(a), d[2])

    # ------------------------------------------------------------ emitters
    def builder(self, mat: str) -> MeshBuilder:
        b = self.builders.get(mat)
        if b is None:
            b = self.builders[mat] = MeshBuilder()
        return b

    def uv_scale(self, mat: str) -> float:
        return self.materials.get(mat).uv_scale

    def surface(self, mat: str) -> str:
        return self.materials.get(mat).surface

    def box(self, center, size, mat, hpr=(0, 0, 0), uv="world", collide=True, occlude=True,
            skip_faces=(), surface=None):
        c, r = self._xf(center, hpr)
        self.builder(mat).add_box(c, size, r, uv_scale=self.uv_scale(mat), skip_faces=skip_faces,
                                  uv_mode=uv)
        if collide:
            self.colliders.append((c, tuple(size), r, surface or self.surface(mat)))
        if occlude:
            self.occluders.append(sky_visibility.Occluder(c, size, r))

    def wedge(self, base, width, length, height, heading, mat):
        b, r = self._xf(base, (heading, 0, 0))
        self.builder(mat).add_wedge(b, width, length, height, r[0], uv_scale=self.uv_scale(mat))

    def cylinder(self, center, radius, height, mat, segments=20, caps=True, hpr=(0, 0, 0)):
        c, r = self._xf(center, hpr)
        self.builder(mat).add_cylinder(c, radius, height, segments=segments, uv_scale=self.uv_scale(mat), caps=caps,
                                       hpr=r)

    def sphere(self, center, radius, mat):
        c, _ = self._xf(center)
        self.builder(mat).add_sphere(c, radius, uv_scale=self.uv_scale(mat))

    def collider(self, center, size, hpr=(0, 0, 0), surface="concrete"):
        c, r = self._xf(center, hpr)
        self.colliders.append((c, tuple(size), r, surface))

    def occluder(self, center, size, hpr=(0, 0, 0)):
        c, r = self._xf(center, hpr)
        self.occluders.append(sky_visibility.Occluder(c, size, r))

    def light(self, e: dict):
        pos, _ = self._xf(e["pos"])
        e = dict(e)
        e["pos"] = pos
        if "dir" in e:
            e["dir"] = self._xf_dir(e["dir"])
        self.lights.append(e)

    def prop(self, kind: str, e: dict):
        e = dict(e)
        for key in ("pos", "base"):
            if key in e:
                e[key], hpr = self._xf(e[key], (e.get("heading", 0.0), 0, 0))
                e["heading"] = hpr[0]
        self.props.append((kind, e))

    def build_piece(self, e: dict):
        kind = e.get("type")
        fn = PREFABS.get(kind)
        if fn is None:
            print(f"[level] unknown piece type {kind!r}; skipped")
            return
        fn(self, e)


def _collect_materials(obj, known: set, out: set):
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_materials(v, known, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_materials(v, known, out)
    elif isinstance(obj, str) and obj in known:
        out.add(obj)


class Level:
    def __init__(self, app, renderer, physics, materials, name: str, log=print):
        self.app = app
        self.renderer = renderer
        self.physics = physics
        self.materials = materials
        self.log = log
        self.path = Path(name) if name.endswith(".json") else paths.MAPS_DIR / f"{name}.json"
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.name = self.data.get("name", self.path.stem)
        self.root = app.render.attachNewNode(f"level:{self.name}")
        self.spawns: list[dict] = []
        self.zones: list[dict] = []
        self.callouts: list[dict] = []
        self.camera_shots = self.data.get("camera_shots", [])
        self.lights: list[LocalLight] = []
        self.props: list[tuple[str, dict]] = []

    def build(self) -> None:
        t0 = time.time()
        used: set[str] = set()
        _collect_materials(self.data.get("pieces", []), set(self.materials.definitions), used)
        used.add("concrete_wall")
        self.materials.preload(sorted(used))
        ctx = LevelContext(self.materials)
        for piece in self.data.get("pieces", []):
            ctx.build_piece(piece)

        # geometry batches
        tris = 0
        for mat_name, mb in ctx.builders.items():
            node = mb.build(f"geo:{mat_name}")
            if node is None:
                continue
            np_ = self.root.attachNewNode(node)
            self.materials.get(mat_name).apply(np_)
            tris += sum(g.getPrimitive(0).getNumFaces() for g in node.getGeoms())
        # collision
        for (c, size, hpr, surface) in ctx.colliders:
            self.physics.add_static_box(c, [s * 0.5 for s in size], hpr, surface=surface)
        # lights + emissive fixtures
        for e in ctx.lights:
            self._add_light(e)
        self.renderer.lights.finalize()
        self.spawns = ctx.spawns
        self.zones = ctx.zones
        self.callouts = ctx.callouts
        self.props = ctx.props
        for kind, e in ctx.props:
            if kind == "weapon_model":
                self._weapon_model(e)
        self.log(f"[level] {self.name}: {len(ctx.builders)} material batches, {tris} triangles, "
                 f"{len(ctx.colliders)} colliders, {len(self.lights)} lights, {len(self.zones)} zones "
                 f"(textures: {self.materials.summary()}) in {time.time() - t0:.1f}s")

        # sky visibility bake
        b = self.data.get("bounds")
        if b and ctx.occluders:
            vol = sky_visibility.bake_cached(ctx.occluders, b[0], b[1], key_extra=self.name, log=self.log,
                                             spacing=tuple(self.data.get("skyvis_spacing", (1.5, 1.5, 1.0))))
            self.renderer.set_sky_visibility(vol)

    def _add_light(self, e: dict) -> None:
        if "kelvin" in e:
            rgb = kelvin_to_rgb(float(e["kelvin"]))
        else:
            rgb = e.get("color", (1.0, 1.0, 1.0))
        intensity = float(e.get("intensity", 10.0))
        color = Vec3(*rgb) * intensity
        kind = e.get("kind", "point")
        d = Vec3(*e.get("dir", (0, 0, -1))).normalized()
        light = LocalLight(pos=Point3(*e["pos"]), color=color, range=float(e.get("range", 10.0)), kind=kind,
                           direction=d, inner_deg=float(e.get("inner", 25.0)), outer_deg=float(e.get("outer", 40.0)),
                           shadows=bool(e.get("shadows", True)))
        self.renderer.lights.add(light)
        self.lights.append(light)
        fixture = e.get("fixture", "bulb")
        if fixture:
            self._make_fixture(light, fixture, rgb, intensity)

    def _make_fixture(self, light: LocalLight, style: str, rgb, intensity: float) -> None:
        """Small emissive mesh so the light source is visible (and blooms later)."""
        mb = MeshBuilder()
        p = light.pos
        if style == "panel":
            mb.add_box((p.x, p.y, p.z + 0.03), (1.2, 0.3, 0.04))
        elif style == "flood":
            back = light.direction * -0.12
            mb.add_box((p.x + back.x, p.y + back.y, p.z + back.z), (0.35, 0.35, 0.2))
        else:
            mb.add_sphere((p.x, p.y, p.z), 0.07, rings=8, segments=12)
        node = mb.build("fixture")
        np_ = self.root.attachNewNode(node)
        mat = self.materials.get("pbr_white_rough")
        mat.apply(np_)
        glow = Vec3(*rgb) * min(intensity * 1.5, 60.0)
        np_.setShaderInput("u_emission", LVecBase4f(glow.x, glow.y, glow.z, 0))
        # fixtures must not block their own light
        from render.renderer import SHADOW_CAMERA_MASK
        np_.hide(SHADOW_CAMERA_MASK)

    def _weapon_model(self, e: dict) -> None:
        from weapons.models import build_weapon_model
        m = build_weapon_model(self.materials, e["model"], self.root)
        m.root.setPos(*e["pos"])
        m.root.setHpr(e.get("heading", 0.0), *e.get("pr", (0.0, 0.0)))
        m.root.setScale(e.get("scale", 1.0))

    def callout_at(self, x: float, y: float) -> str:
        """Name of the smallest callout area containing (x, y), or ''."""
        best, best_area = "", float("inf")
        for c in self.callouts:
            (x0, y0), (x1, y1) = c["min"][:2], c["max"][:2]
            if x0 <= x <= x1 and y0 <= y <= y1:
                area = (x1 - x0) * (y1 - y0)
                if area < best_area:
                    best, best_area = c["name"], area
        return best

    def zone_at(self, pos, kind: str = "bombsite") -> dict | None:
        for z in self.zones:
            if z["kind"] == kind and all(z["min"][i] <= pos[i] <= z["max"][i] for i in range(3)):
                return z
        return None

    def spawn_point(self, team: str | None = None) -> dict:
        for s in self.spawns:
            if team is None or s["team"] == team:
                return s
        return {"pos": [0, 0, 1], "heading": 0}
