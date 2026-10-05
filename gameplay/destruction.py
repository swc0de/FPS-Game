"""Destructible soft walls and floors (Siege-style).

A *panel* is a thin box (a soft wall segment, a floor hatch) cut into a grid
of chunks of about 25 cm. Each chunk has hit points from its surface
(data/destruction.json):

* bullets chip the chunk they hit (several rifle rounds open a murder hole,
  a shotgun blast opens a hole at once),
* melee and the breaching hammer smash a small area,
* explosions destroy everything inside a radius that grows with their
  power (frags blow holes, the bomb levels nearby walls),
* wall charges and the thermal lance cut a door-sized opening.

When chunks die the panel rebuilds its mesh (numpy: front and back faces of
every surviving chunk plus the edges around holes) and its Bullet collision
(surviving chunks merged into as few boxes as possible), so bullets, sight
lines, grenades and movement go through the holes. Dust, splinters and a
few physical debris pieces fly, and bullet-hole decals over the removed
area disappear.

Defenders can *reinforce* marked walls: steel plates make them bullet-proof
and immune to everything except the thermal lance.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass

import numpy as np
from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode
from panda3d.core import Point3, TransformState, Vec3

from engine import paths
from engine.geometry import _BOX_FACES, FLOATS_PER_VERTEX, MeshBuilder, hpr_matrix
from engine.physics import GROUP_DEBRIS, GROUP_DESTRUCTIBLE, WORLD_COLLIDE

_CFG = None


def load_config() -> dict:
    global _CFG
    if _CFG is None:
        with open(paths.DATA_DIR / "destruction.json", "r", encoding="utf-8") as f:
            _CFG = json.load(f)
    return _CFG


@dataclass
class PanelSpec:
    """A destructible piece emitted by the level builder (world space)."""
    center: tuple
    size: tuple                 # full extents in the panel's own frame
    hpr: tuple
    mat: str                    # +thin side and the edges of holes
    mat2: str                   # -thin side
    surface: str
    reinforceable: bool = False
    breach: bool = False        # bots may blow this wall open (shortcut into a site)
    kind: str = "wall"          # wall | floor
    name: str = ""


def _faces(mb: MeshBuilder, R: np.ndarray, c: np.ndarray, centers: np.ndarray, half: np.ndarray, face: int,
           uv_scale: float) -> None:
    """One box face (geometry._BOX_FACES order) for many chunks at once."""
    if len(centers) == 0:
        return
    n, t, b = (np.array(v, np.float64) for v in _BOX_FACES[face])
    hn, ht, hb = abs(n @ half), abs(t @ half), abs(b @ half)
    signs = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], np.float64)
    offs = n * hn + signs[:, :1] * t * ht + signs[:, 1:] * b * hb          # (4, 3)
    local = centers[:, None, :] + offs[None, :, :]                           # (N, 4, 3)
    world = local @ R + c
    nw, tw, bw = n @ R, t @ R, b @ R
    count = len(centers)
    v = np.zeros((count, 4, FLOATS_PER_VERTEX), np.float32)
    v[..., 0:3] = world
    v[..., 3:6] = nw
    v[..., 6:9] = tw
    v[..., 9:12] = bw
    v[..., 12] = (world @ tw) / uv_scale
    v[..., 13] = (world @ bw) / uv_scale
    idx = (np.arange(count, dtype=np.uint32)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3], np.uint32)).ravel()
    mb.add(v.reshape(-1, FLOATS_PER_VERTEX), idx)


def merge_rects(alive: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Greedy cover of the True cells by rectangles (i0, i1, j0, j1) inclusive."""
    nu, nv = alive.shape
    used = np.zeros_like(alive)
    out = []
    for j in range(nv):
        i = 0
        while i < nu:
            if not alive[i, j] or used[i, j]:
                i += 1
                continue
            i2 = i
            while i2 + 1 < nu and alive[i2 + 1, j] and not used[i2 + 1, j]:
                i2 += 1
            j2 = j
            while j2 + 1 < nv and alive[i:i2 + 1, j2 + 1].all() and not used[i:i2 + 1, j2 + 1].any():
                j2 += 1
            used[i:i2 + 1, j:j2 + 1] = True
            out.append((i, i2, j, j2))
            i = i2 + 1
    return out


class Panel:
    def __init__(self, mgr, spec: PanelSpec, index: int):
        self.mgr = mgr
        self.spec = spec
        self.index = index
        self.center = np.asarray(spec.center, np.float64)
        self.size = np.asarray(spec.size, np.float64)
        self.thin = int(np.argmin(self.size))
        self.ax = [i for i in range(3) if i != self.thin]
        a, b = self.ax
        cs = mgr.chunk
        self.nu = max(1, int(round(self.size[a] / cs)))
        self.nv = max(1, int(round(self.size[b] / cs)))
        self.cu = self.size[a] / self.nu
        self.cv = self.size[b] / self.nv
        self.R = hpr_matrix(spec.hpr)
        props = mgr.surface_props(spec.surface)
        self.max_hp = float(props["hp"])
        self.hp = np.full((self.nu, self.nv), self.max_hp, np.float32)
        self.alive = np.ones((self.nu, self.nv), bool)
        self.reinforced = False
        self.reinforcing = None          # (agent, progress) while plates go up
        self.root = mgr.root.attachNewNode(f"panel:{spec.name or index}")
        self.mesh_np = None
        self.intact = True               # drawn by the manager's shared batch until damaged
        self.plates = None
        self.body_np = None
        self.dirty = True
        iu = (np.arange(self.nu) + 0.5) * self.cu - self.size[a] / 2
        iv = (np.arange(self.nv) + 0.5) * self.cv - self.size[b] / 2
        self.cu_pos, self.cv_pos = np.meshgrid(iu, iv, indexing="ij")
        self.version = 0

    # ------------------------------------------------------------ frames
    def to_local(self, p) -> np.ndarray:
        return (np.asarray([p[0], p[1], p[2]], np.float64) - self.center) @ self.R.T

    def to_world(self, local) -> Point3:
        w = np.asarray(local, np.float64) @ self.R + self.center
        return Point3(*w)

    @property
    def normal(self) -> Vec3:
        return Vec3(*self.R[self.thin])

    def cell_center_local(self, i: int, j: int) -> np.ndarray:
        v = np.zeros(3)
        v[self.ax[0]] = self.cu_pos[i, j]
        v[self.ax[1]] = self.cv_pos[i, j]
        return v

    def destroyed_fraction(self) -> float:
        return 1.0 - float(self.alive.mean())

    def intact_fraction_around(self, u: float, v0: float, v1: float, width: float) -> float:
        """Fraction of chunks still standing in a window (panel-local metres) -
        used for navmesh links through holes."""
        m = (np.abs(self.cu_pos - u) <= width / 2) & (self.cv_pos >= v0) & (self.cv_pos <= v1)
        if not m.any():
            return 1.0
        return float(self.alive[m].mean())

    # ------------------------------------------------------------ damage
    def damage(self, point, radius: float, amount: float, kind: str, direction=None) -> int:
        """Damage chunks within ``radius`` of a world point. Returns chunks destroyed."""
        if self.reinforced and kind != "thermal":
            return 0
        lp = self.to_local(point)
        half_t = self.size[self.thin] / 2
        if abs(lp[self.thin]) > half_t + radius + 0.05:
            return 0
        a, b = self.ax
        dx = np.maximum(np.abs(self.cu_pos - lp[a]) - self.cu / 2, 0.0)
        dy = np.maximum(np.abs(self.cv_pos - lp[b]) - self.cv / 2, 0.0)
        d = np.hypot(dx, dy)
        m = self.alive & (d <= radius)
        if not m.any():
            return 0
        fall = 1.0 - 0.5 * np.clip(d / max(radius, 1e-3), 0.0, 1.0)
        self.hp[m] -= (amount * fall[m]).astype(np.float32)
        dead = m & (self.hp <= 0.0)
        if not dead.any():
            return 0
        self._kill(dead, point, kind, direction)
        return int(dead.sum())

    def cut(self, u0: float, u1: float, v0: float, v1: float, point=None, kind: str = "charge",
            direction=None) -> int:
        """Remove every chunk whose centre lies in the panel-local rectangle."""
        if self.reinforced and kind != "thermal":
            return 0
        m = self.alive & (self.cu_pos >= u0) & (self.cu_pos <= u1) & (self.cv_pos >= v0) & (self.cv_pos <= v1)
        if not m.any():
            return 0
        if self.reinforced:
            # the lance burns through the plates too
            self.reinforced = False
            if self.plates is not None:
                self.plates.removeNode()
                self.plates = None
        self._kill(m, point if point is not None else self.to_world(np.zeros(3)), kind, direction)
        return int(m.sum())

    def _kill(self, dead: np.ndarray, point, kind: str, direction) -> None:
        self.alive[dead] = False
        self.hp[dead] = 0.0
        self.dirty = True
        if self.intact:
            self.intact = False
            self.mgr.batch_dirty = True
        self.version += 1
        self.mgr.on_chunks_destroyed(self, dead, point, kind, direction)

    # ------------------------------------------------------------ build
    def rebuild(self) -> None:
        self.dirty = False
        if self.mesh_np is not None:
            self.mesh_np.removeNode()
            self.mesh_np = None
        if not self.intact:
            self._build_mesh()
        self._build_body()

    def add_intact_box(self, builders: dict) -> None:
        """The whole panel as one box into per-material builders (the shared batch).
        World-planar UVs make it look exactly like the chunked mesh."""
        spec = self.spec
        mats = self.mgr.materials
        half = self.size / 2
        centers = np.zeros((1, 3))
        for face in range(6):
            mat = spec.mat2 if face == self.thin * 2 + 1 else spec.mat
            mb = builders.get(mat)
            if mb is None:
                mb = builders[mat] = MeshBuilder()
            _faces(mb, self.R, self.center, centers, half, face, mats.get(mat).uv_scale)

    def _cell_arrays(self, mask: np.ndarray):
        ii, jj = np.nonzero(mask)
        centers = np.zeros((len(ii), 3))
        centers[:, self.ax[0]] = self.cu_pos[ii, jj]
        centers[:, self.ax[1]] = self.cv_pos[ii, jj]
        return centers

    def _build_mesh(self) -> None:
        mats = self.mgr.materials
        alive = self.alive
        if not alive.any():
            return
        spec = self.spec
        a, b = self.ax
        half = np.zeros(3)
        half[a], half[b], half[self.thin] = self.cu / 2, self.cv / 2, self.size[self.thin] / 2
        R, c = self.R, self.center
        mb_a, mb_b = MeshBuilder(), MeshBuilder()
        uv_a = mats.get(spec.mat).uv_scale
        uv_b = mats.get(spec.mat2).uv_scale
        centers = self._cell_arrays(alive)
        _faces(mb_a, R, c, centers, half, self.thin * 2, uv_a)          # + side
        _faces(mb_b, R, c, centers, half, self.thin * 2 + 1, uv_b)      # - side
        # edges where the neighbour is gone (or the panel ends)
        pad = np.zeros((self.nu + 2, self.nv + 2), bool)
        pad[1:-1, 1:-1] = alive
        for axis, sign, di, dj in ((a, 1, 1, 0), (a, -1, -1, 0), (b, 1, 0, 1), (b, -1, 0, -1)):
            neigh = pad[1 + di:1 + di + self.nu, 1 + dj:1 + dj + self.nv]
            need = alive & ~neigh
            _faces(mb_a, R, c, self._cell_arrays(need), half, axis * 2 + (0 if sign > 0 else 1), uv_a)
        self.mesh_np = self.root.attachNewNode("mesh")
        for mb, mat in ((mb_a, spec.mat), (mb_b, spec.mat2)):
            node = mb.build(f"panel:{mat}")
            if node is not None:
                np_ = self.mesh_np.attachNewNode(node)
                mats.get(mat).apply(np_)

    def _build_body(self) -> None:
        phys = self.mgr.game.physics
        if self.body_np is not None:
            phys.world.removeRigidBody(self.body_np.node())
            self.body_np.removeNode()
            self.body_np = None
        if self.reinforced:
            rects = [(0, self.nu - 1, 0, self.nv - 1)]
        else:
            rects = merge_rects(self.alive)
        if not rects:
            return
        node = BulletRigidBodyNode(f"panel:{self.spec.name or self.index}")
        a, b = self.ax
        for i0, i1, j0, j1 in rects:
            half = np.zeros(3)
            half[a] = (i1 - i0 + 1) * self.cu / 2
            half[b] = (j1 - j0 + 1) * self.cv / 2
            half[self.thin] = self.size[self.thin] / 2
            off = np.zeros(3)
            off[a] = (self.cu_pos[i0, 0] + self.cu_pos[i1, 0]) / 2
            off[b] = (self.cv_pos[0, j0] + self.cv_pos[0, j1]) / 2
            node.addShape(BulletBoxShape(Vec3(*half)), TransformState.makePos(Point3(*off)))
        node.setTag("surface", "reinforced" if self.reinforced else self.spec.surface)
        node.setPythonTag("panel", self)
        node.setIntoCollideMask(GROUP_DESTRUCTIBLE | WORLD_COLLIDE)
        self.body_np = phys.root.attachNewNode(node)
        self.body_np.setPos(Point3(*self.center))
        self.body_np.setHpr(*self.spec.hpr)
        phys.world.attachRigidBody(node)

    # ------------------------------------------------------- reinforce
    def can_reinforce(self) -> bool:
        cfg = self.mgr.cfg["reinforce"]
        return (self.spec.reinforceable and not self.reinforced
                and self.destroyed_fraction() <= float(cfg.get("max_damaged_fraction", 0.15)))

    def reinforce(self) -> None:
        """Steel plates on both faces: bullet-proof, only the thermal lance gets through."""
        self.reinforced = True
        self.alive[:] = True
        self.hp[:] = self.max_hp
        self.version += 1
        if not self.intact:
            self.intact = True
            self.mgr.batch_dirty = True
        mats = self.mgr.materials
        plate_mat = self.mgr.cfg["reinforce"].get("plate_mat", "steel_painted")
        mb = MeshBuilder()
        a, b = self.ax
        R, c = self.R, self.center
        t = self.size[self.thin] / 2
        uv = mats.get(plate_mat).uv_scale
        # two plates and three horizontal braces per side
        for side in (1, -1):
            plate = np.zeros(3)
            plate[a], plate[b], plate[self.thin] = self.size[a] / 2 - 0.02, self.size[b] / 2 - 0.02, 0.012
            cen = np.zeros(3)
            cen[self.thin] = side * (t + 0.012)
            _box_mesh(mb, R, c, cen, plate, uv)
            for k in (-0.35, 0.0, 0.35):
                brace = np.zeros(3)
                brace[a], brace[b], brace[self.thin] = self.size[a] / 2 - 0.05, 0.05, 0.03
                bc = np.zeros(3)
                bc[self.thin] = side * (t + 0.05)
                bc[b] = k * self.size[b]
                _box_mesh(mb, R, c, bc, brace, uv)
        node = mb.build("plates")
        self.plates = self.root.attachNewNode(node)
        mats.get(plate_mat).apply(self.plates)
        self.dirty = True

    def destroy(self) -> None:
        if self.body_np is not None:
            self.mgr.game.physics.world.removeRigidBody(self.body_np.node())
            self.body_np.removeNode()
        self.root.removeNode()


def _box_mesh(mb, R, c, center, half, uv) -> None:
    centers = np.asarray([center], np.float64)
    for f in range(6):
        _faces(mb, R, c, centers, np.asarray(half, np.float64), f, uv)


class Debris:
    """A few physical chunks that tumble out of destroyed walls."""

    def __init__(self, game, cfg: dict):
        self.game = game
        self.max = int(cfg.get("max_bodies", 48))
        self.life = float(cfg.get("lifetime", 8.0))
        self.items: list[list] = []          # [body_np, visual, time left, size]
        self._meshes = {}

    def _mesh(self, mat: str):
        np_ = self._meshes.get(mat)
        if np_ is None:
            mb = MeshBuilder()
            mb.add_box((0, 0, 0), (1, 1, 1), uv_scale=0.5)
            from panda3d.core import NodePath
            np_ = NodePath(mb.build("debris"))
            self.game.materials.get(mat).apply(np_)
            self._meshes[mat] = np_
        return np_

    def spawn(self, pos, vel: Vec3, size: float, mat: str) -> None:
        if len(self.items) >= self.max:
            self._remove(0)
        node = BulletRigidBodyNode("debris")
        dims = Vec3(size, size * random.uniform(0.4, 1.0), size * random.uniform(0.15, 0.4))
        node.addShape(BulletBoxShape(dims * 0.5))
        node.setMass(0.4)
        node.setFriction(0.8)
        node.setRestitution(0.2)
        node.setIntoCollideMask(GROUP_DEBRIS)
        body = self.game.physics.root.attachNewNode(node)
        body.setPos(Point3(*pos))
        body.setHpr(random.uniform(0, 360), random.uniform(0, 360), 0)
        node.setLinearVelocity(vel)
        node.setAngularVelocity(Vec3(random.uniform(-8, 8), random.uniform(-8, 8), random.uniform(-8, 8)))
        self.game.physics.world.attachRigidBody(node)
        vis = self._mesh(mat).copyTo(self.game.render)
        vis.setTransform(body.getTransform(self.game.render))
        vis.setScale(dims)
        self.items.append([body, vis, self.life, dims])

    def _remove(self, k: int) -> None:
        body, vis = self.items.pop(k)[:2]
        self.game.physics.world.removeRigidBody(body.node())
        body.removeNode()
        vis.removeNode()

    def update(self, dt: float) -> None:
        render = self.game.render
        for k in range(len(self.items) - 1, -1, -1):
            it = self.items[k]
            it[2] -= dt
            if it[2] <= 0:
                self._remove(k)
                continue
            body, vis, dims = it[0], it[1], it[3]
            vis.setTransform(body.getTransform(render))
            vis.setScale(dims * min(it[2] / 1.0, 1.0))     # shrink away during the last second

    def clear(self) -> None:
        while self.items:
            self._remove(len(self.items) - 1)


class DestructionManager:
    def __init__(self, game, specs: list[PanelSpec]):
        self.game = game
        self.cfg = load_config()
        self.chunk = float(self.cfg.get("chunk_size", 0.25))
        self.materials = game.materials
        self.root = game.render.attachNewNode("destructible")
        self.specs = list(specs)
        self.panels: list[Panel] = []
        self.debris = Debris(game, self.cfg.get("debris", {}))
        self.listeners: list = []            # callback(panel) when a panel changes shape
        self.version = 0
        self.batch_np = None                 # every intact panel, one mesh per material
        self.batch_dirty = True
        self.build()

    def surface_props(self, surface: str) -> dict:
        s = self.cfg["surfaces"]
        return s.get(surface) or s["default"]

    def build(self) -> None:
        for p in self.panels:
            p.destroy()
        self.panels = [Panel(self, spec, i) for i, spec in enumerate(self.specs)]
        for p in self.panels:
            p.rebuild()
        self._rebuild_batch()

    def _rebuild_batch(self) -> None:
        """Intact panels are drawn together: a few draw calls for all of them
        instead of two per panel. A panel gets its own chunked mesh once it
        is damaged (and comes back here when the round resets)."""
        self.batch_dirty = False
        if self.batch_np is not None:
            self.batch_np.removeNode()
        self.batch_np = self.root.attachNewNode("intact_panels")
        builders: dict = {}
        for p in self.panels:
            if p.intact:
                p.add_intact_box(builders)
        for mat, mb in builders.items():
            node = mb.build(f"panels:{mat}")
            if node is not None:
                self.materials.get(mat).apply(self.batch_np.attachNewNode(node))

    def reset(self) -> None:
        """New round: every wall is whole again, plates come off."""
        changed = [p for p in self.panels if p.version > 0]
        for p in changed:
            p.alive[:] = True
            p.hp[:] = p.max_hp
            if not p.intact:
                p.intact = True
                self.batch_dirty = True
            p.reinforced = False
            p.reinforcing = None
            if p.plates is not None:
                p.plates.removeNode()
                p.plates = None
            p.version = 0
            p.rebuild()
        self.debris.clear()
        if self.batch_dirty:
            self._rebuild_batch()
        if changed:
            self.version += 1
            for cb in list(self.listeners):
                cb(None)

    # --------------------------------------------------------- lookups
    def panel_of(self, node) -> Panel | None:
        if node is None:
            return None
        return node.getPythonTag("panel")

    def panels_near(self, pos, radius: float) -> list[Panel]:
        p = np.asarray([pos[0], pos[1], pos[2]], np.float64)
        out = []
        for panel in self.panels:
            r = float(np.linalg.norm(panel.size)) / 2 + radius
            if np.sum((panel.center - p) ** 2) <= r * r:
                out.append(panel)
        return out

    # ---------------------------------------------------------- damage
    def bullet_hit(self, node, pos, direction, damage: float, pellets: int = 1) -> None:
        panel = self.panel_of(node)
        if panel is None:
            return
        props = self.surface_props(panel.spec.surface)
        panel.damage(pos, 0.05 if pellets <= 1 else 0.09, damage * float(props["bullet"]), "bullet", direction)

    def melee_hit(self, node, pos, direction, damage: float, radius: float = 0.14) -> None:
        panel = self.panel_of(node)
        if panel is None:
            return
        props = self.surface_props(panel.spec.surface)
        panel.damage(pos, radius, damage * float(props["melee"]), "melee", direction)

    def explosion(self, pos, power: float, radius: float | None = None) -> int:
        """Blast at pos: chunks inside the destruction radius take ``power``
        (with falloff); radius defaults to power * explosion_radius_scale / 10."""
        if radius is None:
            radius = power * float(self.cfg.get("explosion_radius_scale", 0.16)) / 10.0
        n = 0
        for panel in self.panels_near(pos, radius):
            props = self.surface_props(panel.spec.surface)
            n += panel.damage(pos, radius, power * float(props["explosion"]), "explosion")
        return n

    def cut_at(self, panel: Panel, pos, width: float, below: float, above: float, kind: str) -> int:
        """Door-sized opening around a charge planted at ``pos`` on ``panel``."""
        lp = panel.to_local(pos)
        a, b = panel.ax
        return panel.cut(lp[a] - width / 2, lp[a] + width / 2, lp[b] - below, lp[b] + above, pos, kind,
                         panel.normal)

    # ---------------------------------------------------------- events
    def on_chunks_destroyed(self, panel: Panel, dead: np.ndarray, point, kind: str, direction) -> None:
        g = self.game
        props = self.surface_props(panel.spec.surface)
        ii, jj = np.nonzero(dead)
        count = len(ii)
        color = props.get("debris", (0.7, 0.7, 0.7))
        n = panel.normal
        d = Vec3(*direction) if direction is not None else n
        if d.lengthSquared() < 1e-6:
            d = n
        d.normalize()
        out = n if n.dot(d) > 0 else -n
        picks = random.sample(range(count), min(count, 10))
        for k in picks:
            w = panel.to_world(panel.cell_center_local(ii[k], jj[k]))
            g.effects.debris_burst(w, out, color, props.get("effect", "dust_grey"), strong=kind != "bullet")
        if kind in ("explosion", "charge", "thermal", "melee") or count >= 3:
            for k in random.sample(range(count), min(count, 4 if kind != "bullet" else 1)):
                w = panel.to_world(panel.cell_center_local(ii[k], jj[k]))
                speed = 6.0 if kind in ("explosion", "charge", "thermal") else 2.5
                vel = out * random.uniform(0.5, 1.0) * speed + Vec3(random.uniform(-1, 1), random.uniform(-1, 1),
                                                                     random.uniform(0.5, 2.0))
                self.debris.spawn(w, vel, self.chunk * random.uniform(0.5, 0.9), panel.spec.mat)
        if count >= 2 or kind != "bullet":
            snd = "wall_break" if panel.spec.surface != "wood" else "wood_break"
            g.audio.play_at(snd, Point3(*point), volume=min(0.4 + 0.08 * count, 1.0))
        g.effects.decals.remove_where(lambda pts: _decals_in_holes(panel, pts))
        g.notify_noise(Point3(*point), 1.0, 30.0)

    def flush(self) -> None:
        """Rebuild changed panels (once per tick, however many hits they took)."""
        changed = [p for p in self.panels if p.dirty]
        for p in changed:
            p.rebuild()
        if self.batch_dirty:
            self._rebuild_batch()
        if changed:
            self.version += 1
            for cb in list(self.listeners):
                for p in changed:
                    cb(p)

    def update(self, dt: float) -> None:
        self.debris.update(dt)


def _decals_in_holes(panel: Panel, pts: np.ndarray) -> np.ndarray:
    """Mask of decal positions that sit on chunks of ``panel`` that are gone."""
    local = (pts - panel.center) @ panel.R.T
    a, b = panel.ax
    half_t = panel.size[panel.thin] / 2
    on = np.abs(local[:, panel.thin]) <= half_t + 0.05
    i = np.floor((local[:, a] + panel.size[a] / 2) / panel.cu).astype(int)
    j = np.floor((local[:, b] + panel.size[b] / 2) / panel.cv).astype(int)
    inside = on & (i >= 0) & (i < panel.nu) & (j >= 0) & (j < panel.nv)
    out = np.zeros(len(pts), bool)
    k = np.nonzero(inside)[0]
    out[k] = ~panel.alive[i[k], j[k]]
    return out
