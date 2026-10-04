"""Thin wrapper around Panda3D's Bullet world.

Collision groups are expressed as Bullet "into" masks.  Queries (sweeps,
ray casts) pass a mask selecting which groups they can hit.
"""
from __future__ import annotations

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


@dataclass
class RayHit:
    pos: Point3
    normal: Vec3
    fraction: float
    node: object

    @property
    def surface(self) -> str:
        return surface_of(self.node)


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
        return RayHit(Point3(res.getHitPos()), Vec3(res.getHitNormal()), res.getHitFraction(), res.getNode())

    def ray_cast_all(self, start, end, mask: BitMask32 = MASK_BULLETS) -> list[RayHit]:
        res = self.world.rayTestAll(Point3(*start), Point3(*end), mask)
        hits = [RayHit(Point3(h.getHitPos()), Vec3(h.getHitNormal()), h.getHitFraction(), h.getNode())
                for h in res.getHits()]
        hits.sort(key=lambda h: h.fraction)
        return hits

    def sweep(self, shape, start, end, mask: BitMask32 = MASK_MOVEMENT):
        return self.world.sweepTestClosest(
            shape, TransformState.makePos(Point3(*start)), TransformState.makePos(Point3(*end)), mask, 0.0)

    def step(self, dt: float) -> None:
        # One fixed sub-step per game tick keeps physics deterministic.
        self.world.doPhysics(dt, 1, dt)
