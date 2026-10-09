"""Thin wrapper around Panda3D's Bullet world.

Collision groups are expressed as Bullet "into" masks.  Queries (sweeps,
ray casts) pass a mask selecting which groups they can hit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from panda3d.bullet import (
    BulletBoxShape,
    BulletCylinderShape,
    BulletRigidBodyNode,
    BulletWorld,
    ZUp,
)
from panda3d.core import BitMask32, NodePath, Point3, TransformState, Vec3

# --------------------------------------------------------------- groups
GROUP_WORLD = BitMask32.bit(0)        # static level geometry (blocks everything)
GROUP_CHARACTER = BitMask32.bit(1)    # character movement capsules
GROUP_HITBOX = BitMask32.bit(2)       # per-bone hitboxes (bullets only)
GROUP_DEBRIS = BitMask32.bit(3)       # dynamic debris chunks
GROUP_PLAYER_CLIP = BitMask32.bit(4)  # invisible walls: block movement, not bullets
GROUP_DESTRUCTIBLE = BitMask32.bit(5)  # breakable soft walls / floors

MASK_MOVEMENT = GROUP_WORLD | GROUP_PLAYER_CLIP | GROUP_DESTRUCTIBLE
MASK_BULLETS = GROUP_WORLD | GROUP_HITBOX | GROUP_DESTRUCTIBLE
MASK_SIGHT = GROUP_WORLD | GROUP_DESTRUCTIBLE

# Static level geometry also carries this bit so dynamic props (grenades,
# dropped weapons, debris) collide with it, while bullets/movement queries
# (which never include GROUP_DEBRIS) ignore those props.
WORLD_COLLIDE = GROUP_DEBRIS

GRAVITY = 20.0  # m/s^2 - slightly above real gravity for snappier jumps

# Python tag of bodies whose ray hits are re-tested exactly (the soldiers' hit capsules): a
# callable (start, end) -> (t_in, t_out, pos_in, normal_in, pos_out, normal_out) or None.
# Bullet tests a ray against a capsule as a convex cast with a tolerance: it reports hits up to
# about 6 mm outside the surface and up to 5 mm early (14 mm at grazing angles), enough to pick
# the wrong one of two overlapping capsules. Spheres and boxes are exact.
EXACT_RAY = "exact_ray"


@dataclass
class RayHit:
    pos: Point3
    normal: Vec3
    fraction: float
    node: object

    @property
    def surface(self) -> str:
        return surface_of(self.node)


def _span(qa: float, qb: float, qc: float):
    """Roots of qa t^2 + 2 qb t + qc = 0 (qa > 0), or None."""
    disc = qb * qb - qa * qc
    if disc < 0.0:
        return None
    q = math.sqrt(disc)
    return (-qb - q) / qa, (-qb + q) / qa


def segment_capsule(start, end, a, b, r: float):
    """The segment start-end through the capsule of radius ``r`` around a-b, exactly:
    (t_in, t_out, pos_in, normal_in, pos_out, normal_out) as fractions of the segment, or None
    if it misses or starts inside (as Bullet). ``pos_out`` is None if the segment ends inside.
    Plain floats: it runs for every ray that reaches a soldier."""
    sx, sy, sz = start[0], start[1], start[2]
    dx, dy, dz = end[0] - sx, end[1] - sy, end[2] - sz
    dd = dx * dx + dy * dy + dz * dz
    if dd < 1e-18:
        return None
    ax, ay, az = a[0], a[1], a[2]
    ux, uy, uz = b[0] - ax, b[1] - ay, b[2] - az
    length = math.sqrt(ux * ux + uy * uy + uz * uz)
    rr = r * r
    spans = []                        # (t_in, t_out, kind) with kind 0 cylinder, 1 sphere a, 2 sphere b
    if length > 1e-9:
        ux, uy, uz = ux / length, uy / length, uz / length
        mx, my, mz = sx - ax, sy - ay, sz - az
        du, mu = dx * ux + dy * uy + dz * uz, mx * ux + my * uy + mz * uz
        px, py, pz = dx - ux * du, dy - uy * du, dz - uz * du
        qx, qy, qz = mx - ux * mu, my - uy * mu, mz - uz * mu
        qa = px * px + py * py + pz * pz
        qc = qx * qx + qy * qy + qz * qz - rr
        if qa < 1e-18:
            cyl = (-math.inf, math.inf) if qc <= 0.0 else None
        else:
            cyl = _span(qa, qx * px + qy * py + qz * pz, qc)
        if cyl is not None:
            if abs(du) < 1e-18:
                slab = (-math.inf, math.inf) if 0.0 <= mu <= length else None
            else:
                t0, t1 = -mu / du, (length - mu) / du
                slab = (t0, t1) if t0 < t1 else (t1, t0)
            if slab is not None:
                lo, hi = max(cyl[0], slab[0]), min(cyl[1], slab[1])
                if lo <= hi:
                    spans.append((lo, hi, 0))
        centres = ((ax, ay, az, 1), (b[0], b[1], b[2], 2))
    else:
        centres = ((ax, ay, az, 1),)
    for cx, cy, cz, kind in centres:
        mx, my, mz = sx - cx, sy - cy, sz - cz
        sp = _span(dd, mx * dx + my * dy + mz * dz, mx * mx + my * my + mz * mz - rr)
        if sp is not None:
            spans.append((sp[0], sp[1], kind))
    if not spans:
        return None
    t_in = min(sp[0] for sp in spans)
    if t_in < 0.0 or t_in > 1.0:
        return None
    t_out = max(sp[1] for sp in spans)

    def surface(t: float, kind: int):
        x, y, z = sx + dx * t, sy + dy * t, sz + dz * t
        if kind == 0:
            vx, vy, vz = x - ax, y - ay, z - az
            k = vx * ux + vy * uy + vz * uz
            nx, ny, nz = vx - ux * k, vy - uy * k, vz - uz * k
        else:
            c = centres[kind - 1]
            nx, ny, nz = x - c[0], y - c[1], z - c[2]
        n = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        return Point3(x, y, z), Vec3(nx / n, ny / n, nz / n)
    pos_in, n_in = surface(t_in, next(sp[2] for sp in spans if sp[0] == t_in))
    if t_out > 1.0:
        return t_in, t_out, pos_in, n_in, None, None
    pos_out, n_out = surface(t_out, next(sp[2] for sp in spans if sp[1] == t_out))
    return t_in, t_out, pos_in, n_in, pos_out, n_out


def exact_hits(hits: list, start, end) -> list:
    """Re-test the hits on EXACT_RAY bodies exactly (drop the misses, move the entry points);
    the others as Bullet reports them. Sorted by fraction."""
    out = []
    for h in hits:
        test = h.node.getPythonTag(EXACT_RAY) if h.node is not None and h.node.hasPythonTag(EXACT_RAY) else None
        if test is None:
            out.append(h)
            continue
        r = test(Point3(*start), Point3(*end))
        if r is not None:
            out.append(RayHit(r[2], r[3], r[0], h.node))
    out.sort(key=lambda h: h.fraction)
    return out


def surface_of(node) -> str:
    if node is None:
        return "default"
    tag = node.getTag("surface")
    return tag or "default"


class PhysicsWorld:
    def __init__(self, root: NodePath):
        self.world = BulletWorld()
        self.world.setGravity(Vec3(0, 0, -GRAVITY))
        self.root = root.attachNewNode("physics")
        self._static_count = 0
        # called just before each step, when Bullet syncs kinematic bodies from the scene graph
        # (the soldiers keep the pose their hit capsules are synced at: EXACT_RAY)
        self.before_step: list = []

    # ---------------------------------------------------------- building
    def add_static_box(self, center, half_extents, hpr=(0, 0, 0), surface: str = "concrete",
                       group: BitMask32 = GROUP_WORLD, name: str = "static") -> NodePath:
        node = BulletRigidBodyNode(name)
        node.addShape(BulletBoxShape(Vec3(*half_extents)))
        node.setTag("surface", surface)
        node.setIntoCollideMask(group | WORLD_COLLIDE if group == GROUP_WORLD else group)
        np = self.root.attachNewNode(node)
        np.setPos(Point3(*center))
        np.setHpr(*hpr)
        self.world.attachRigidBody(node)
        self._static_count += 1
        return np

    def add_static_cylinder(self, center, radius, height, surface="metal",
                            group: BitMask32 = GROUP_WORLD, name="static_cyl") -> NodePath:
        node = BulletRigidBodyNode(name)
        node.addShape(BulletCylinderShape(radius, height, ZUp))
        node.setTag("surface", surface)
        node.setIntoCollideMask(group | WORLD_COLLIDE if group == GROUP_WORLD else group)
        np = self.root.attachNewNode(node)
        np.setPos(Point3(*center))
        self.world.attachRigidBody(node)
        return np

    def add_static_mesh(self, geom_np: NodePath, surface="concrete", group=GROUP_WORLD,
                        name="static_mesh") -> NodePath:
        from panda3d.bullet import BulletTriangleMesh, BulletTriangleMeshShape
        mesh = BulletTriangleMesh()
        for gnode_np in geom_np.findAllMatches("**/+GeomNode"):
            gnode = gnode_np.node()
            ts = gnode_np.getTransform(geom_np)
            for geom in gnode.getGeoms():
                mesh.addGeom(geom, True, ts)
        node = BulletRigidBodyNode(name)
        node.addShape(BulletTriangleMeshShape(mesh, dynamic=False))
        node.setTag("surface", surface)
        node.setIntoCollideMask(group | WORLD_COLLIDE if group == GROUP_WORLD else group)
        np = self.root.attachNewNode(node)
        np.setTransform(geom_np.getTransform(self.root))
        self.world.attachRigidBody(node)
        return np

    def remove(self, np: NodePath) -> None:
        node = np.node()
        if isinstance(node, BulletRigidBodyNode):
            self.world.removeRigidBody(node)
        np.removeNode()

    # ----------------------------------------------------------- queries
    def ray_cast(self, start, end, mask: BitMask32 = MASK_BULLETS) -> RayHit | None:
        res = self.world.rayTestClosest(Point3(*start), Point3(*end), mask)
        if not res.hasHit():
            return None
        node = res.getNode()
        if node is not None and node.hasPythonTag(EXACT_RAY):
            # Bullet's capsule hits are early and generous, never late: the exact first hit is
            # among all of Bullet's hits
            hits = self.ray_cast_all(start, end, mask)
            return hits[0] if hits else None
        return RayHit(Point3(res.getHitPos()), Vec3(res.getHitNormal()), res.getHitFraction(), node)

    def ray_cast_all(self, start, end, mask: BitMask32 = MASK_BULLETS) -> list[RayHit]:
        res = self.world.rayTestAll(Point3(*start), Point3(*end), mask)
        hits = [RayHit(Point3(h.getHitPos()), Vec3(h.getHitNormal()), h.getHitFraction(), h.getNode())
                for h in res.getHits()]
        return exact_hits(hits, start, end)

    def sweep(self, shape, start, end, mask: BitMask32 = MASK_MOVEMENT):
        return self.world.sweepTestClosest(
            shape, TransformState.makePos(Point3(*start)), TransformState.makePos(Point3(*end)), mask, 0.0)

    def step(self, dt: float) -> None:
        for hook in self.before_step:
            hook()
        # One fixed sub-step per game tick keeps physics deterministic.
        self.world.doPhysics(dt, 1, dt)
