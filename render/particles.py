"""Particle system: numpy simulation, one batched draw call per blend mode.

Each system keeps structure-of-arrays state (position, velocity, age, size,
colour, rotation, drag, gravity, stretch, atlas frame). Every frame the live
particles are integrated with numpy and written into one dynamic vertex
buffer (4 vertices per particle). The vertex shader builds camera-facing
quads in world space (or quads stretched along the velocity for sparks and
tracers), so the CPU never touches per-particle nodes.

Alpha-blended systems (smoke, dust) are depth-sorted back-to-front and lit
by the sky/sun (attenuated by the baked sky-visibility volume, so smoke
indoors is darker); additive systems (sparks, fire, flashes) emit HDR light
that will bloom in milestone 3.
"""
from __future__ import annotations

import math

import numpy as np
from panda3d.core import (
    ColorBlendAttrib,
    Geom,
    GeomNode,
    GeomTriangles,
    GeomVertexArrayFormat,
    GeomVertexData,
    GeomVertexFormat,
    InternalName,
    NodePath,
    OmniBoundingVolume,
    SamplerState,
    Texture,
    TransparencyAttrib,
)

from render import shader_loader

ATLAS_GRID = 4
CELL = 128

# atlas frames
F_SOFT, F_SMOKE1, F_SMOKE2, F_SPARK, F_GLOW, F_STAR, F_FLASH_SIDE, F_CHUNK, F_SPLINTER, F_MIST, F_FIRE, \
    F_RING, F_SMOKE3, F_DUST, F_STREAK, F_SHARD = range(16)

_FORMAT = None


def particle_format() -> GeomVertexFormat:
    global _FORMAT
    if _FORMAT is None:
        arr = GeomVertexArrayFormat()
        arr.addColumn(InternalName.getVertex(), 3, Geom.NTFloat32, Geom.CPoint)
        arr.addColumn(InternalName.getTexcoord(), 2, Geom.NTFloat32, Geom.CTexcoord)
        arr.addColumn(InternalName.getColor(), 4, Geom.NTFloat32, Geom.CColor)
        arr.addColumn(InternalName.make("params"), 4, Geom.NTFloat32, Geom.COther)
        arr.addColumn(InternalName.make("velocity"), 4, Geom.NTFloat32, Geom.CVector)
        _FORMAT = GeomVertexFormat.registerFormat(GeomVertexFormat(arr))
    return _FORMAT


# ------------------------------------------------------------------ textures
def _noise(n, rng, beta=2.2):
    fy = np.fft.fftfreq(n)[:, None] * n
    fx = np.fft.rfftfreq(n)[None, :] * n
    f = np.sqrt(fx * fx + fy * fy)
    f[0, 0] = 1
    amp = f ** (-beta / 2)
    amp[0, 0] = 0
    out = np.fft.irfft2(amp * np.exp(1j * rng.uniform(0, 2 * np.pi, amp.shape)), s=(n, n))
    return (out - out.mean()) / (out.std() + 1e-9)


def make_atlas(seed: int = 3) -> Texture:
    """Procedural 4x4 atlas (RGBA): soft blobs, smoke, sparks, flashes, debris."""
    rng = np.random.default_rng(seed)
    n = CELL
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    u = (x + 0.5) / n * 2 - 1
    v = (y + 0.5) / n * 2 - 1
    r = np.sqrt(u * u + v * v)
    ang = np.arctan2(v, u)
    cells = []

    def rgba(a, rgb=(1, 1, 1)):
        c = np.zeros((n, n, 4), np.float32)
        c[..., 0:3] = np.asarray(rgb, np.float32)[None, None, :] if np.ndim(rgb) == 1 else rgb
        c[..., 3] = np.clip(a, 0, 1)
        return c

    soft = np.clip(1 - r, 0, 1) ** 2
    cells.append(rgba(soft))                                                   # F_SOFT
    for k in range(2):                                                         # F_SMOKE1/2
        nz = _noise(n, rng, 2.4)
        a = np.clip(1 - r * (1.0 + 0.35 * nz), 0, 1) ** 1.6 * np.clip(0.75 + 0.3 * nz, 0, 1)
        shade = np.clip(0.8 + 0.2 * _noise(n, rng, 2.0), 0.5, 1.0)
        cells.append(rgba(a, np.stack([shade] * 3, -1)))
    spark = np.exp(-(u * u) / 0.02) * np.exp(-(v * v) / 0.6)                 # F_SPARK (vertical streak)
    cells.append(rgba(spark))
    cells.append(rgba(np.exp(-r * r * 4.5)))                                   # F_GLOW
    star = np.zeros_like(r)
    for k in range(6):                                                         # F_STAR (muzzle flash front)
        a0 = k * math.pi / 3 + rng.uniform(-0.15, 0.15)
        d = np.abs(np.angle(np.exp(1j * (ang - a0))))
        star += np.exp(-(d * d) / 0.012) * np.clip(1 - r / rng.uniform(0.7, 1.0), 0, 1)
    star = np.clip(star + np.exp(-r * r * 6) * 1.2, 0, 1)
    cells.append(rgba(star, np.stack([np.ones_like(r), 0.8 - 0.2 * r, 0.45 - 0.3 * r], -1)))
    side = np.exp(-((v * 0.5 + 0.5)) * 1.5) * np.exp(-(u * u) / (0.05 + 0.25 * (1 - (v * 0.5 + 0.5))))
    side *= np.clip(0.7 + 0.5 * _noise(n, rng, 1.8), 0, 1)                     # F_FLASH_SIDE (cone along +v)
    cells.append(rgba(np.clip(side * 1.4, 0, 1), np.stack([np.ones_like(r), 0.75 + 0 * r, 0.4 + 0 * r], -1)))
    chunk = (r < 0.6 + 0.25 * np.sin(ang * 5 + 1.3) * np.cos(ang * 3)).astype(np.float32)
    cells.append(rgba(chunk, (0.35, 0.33, 0.3)))                               # F_CHUNK
    spl = np.exp(-(u * u) / 0.01) * (np.abs(v) < 0.85)
    cells.append(rgba(spl, (0.55, 0.42, 0.28)))                                # F_SPLINTER
    nz = _noise(n, rng, 2.0)
    cells.append(rgba(np.clip(1 - r * (1 + 0.5 * nz), 0, 1) ** 1.3))         # F_MIST
    fire_n = _noise(n, rng, 1.8)
    fire = np.clip(1 - r * (1.0 + 0.5 * fire_n), 0, 1)
    cells.append(rgba(fire ** 0.8, np.stack([np.ones_like(r), 0.55 + 0.35 * fire, 0.2 + 0.3 * fire ** 3], -1)))
    ring = np.exp(-((r - 0.75) ** 2) / 0.01)
    cells.append(rgba(ring))                                                   # F_RING
    nz = _noise(n, rng, 2.6)
    a = np.clip(1 - r * (1.1 + 0.45 * nz), 0, 1) ** 1.3
    cells.append(rgba(a, np.stack([np.clip(0.85 + 0.15 * nz, 0.6, 1)] * 3, -1)))   # F_SMOKE3
    dust = np.clip(1 - r, 0, 1) ** 3 * np.clip(0.6 + 0.6 * _noise(n, rng, 1.5), 0, 1)
    cells.append(rgba(dust))                                                   # F_DUST
    streak = np.exp(-(u * u) / 0.004) * np.clip(1 - np.abs(v), 0, 1)
    cells.append(rgba(streak))                                                 # F_STREAK (tracers)
    shard = ((np.abs(u) + np.abs(v) * 0.5) < 0.6).astype(np.float32)
    cells.append(rgba(shard, (0.8, 0.85, 0.85)))                               # F_SHARD

    atlas = np.zeros((n * ATLAS_GRID, n * ATLAS_GRID, 4), np.float32)
    for i, c in enumerate(cells[:16]):
        cx, cy = i % ATLAS_GRID, i // ATLAS_GRID
        atlas[cy * n:(cy + 1) * n, cx * n:(cx + 1) * n] = c
    data = (np.clip(atlas, 0, 1) * 255 + 0.5).astype(np.uint8)
    tex = Texture("particle_atlas")
    tex.setup2dTexture(n * ATLAS_GRID, n * ATLAS_GRID, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs(data.tobytes(), "RGBA")
    tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    return tex


# ------------------------------------------------------------------ system
class ParticleSystem:
    CORNERS = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)

    def __init__(self, parent: NodePath, atlas: Texture, capacity: int = 2048, additive: bool = False,
                 sort: int = 20, name: str = "particles"):
        self.capacity = capacity
        self.additive = additive
        self.n = 0
        f32 = np.float32
        self.pos = np.zeros((capacity, 3), f32)
        self.vel = np.zeros((capacity, 3), f32)
        self.age = np.zeros(capacity, f32)
        self.life = np.ones(capacity, f32)
        self.size0 = np.zeros(capacity, f32)
        self.size1 = np.zeros(capacity, f32)
        self.col0 = np.zeros((capacity, 4), f32)
        self.col1 = np.zeros((capacity, 4), f32)
        self.rot = np.zeros(capacity, f32)
        self.rotv = np.zeros(capacity, f32)
        self.drag = np.zeros(capacity, f32)
        self.grav = np.zeros(capacity, f32)
        self.stretch = np.zeros(capacity, f32)
        self.frame = np.zeros(capacity, f32)
        self.lit = np.zeros(capacity, f32)

        self.vdata = GeomVertexData(name, particle_format(), Geom.UHDynamic)
        self.vdata.uncleanSetNumRows(capacity * 4)
        prim = GeomTriangles(Geom.UHDynamic)
        prim.setIndexType(Geom.NTUint32)
        base = np.arange(capacity, dtype=np.uint32)[:, None] * 4
        self._indices = (base + np.array([0, 1, 2, 0, 2, 3], np.uint32)[None, :]).astype(np.uint32).ravel()
        self.geom = Geom(self.vdata)
        self.geom.addPrimitive(prim)
        self._rows = -1
        node = GeomNode(name)
        node.addGeom(self.geom)
        node.setBounds(OmniBoundingVolume())
        node.setFinal(True)
        self.np = parent.attachNewNode(node)
        self.np.setShader(shader_loader.load("particle.vert", "particle.frag",
                                             {"ADDITIVE": additive}), 50)
        self.np.setShaderInput("u_atlas", atlas)
        self.np.setDepthWrite(False)
        self.np.setBin("fixed", sort)
        self.np.setTwoSided(True)
        if additive:
            self.np.setAttrib(ColorBlendAttrib.make(ColorBlendAttrib.MAdd, ColorBlendAttrib.OIncomingAlpha,
                                                    ColorBlendAttrib.OOne))
        else:
            self.np.setTransparency(TransparencyAttrib.MAlpha)
        self._verts = np.zeros((capacity * 4, 17), np.float32)
        self._verts[:, 3:5] = np.tile(self.CORNERS, (capacity, 1))
        self._write(0)

    # ---------------------------------------------------------------- emit
    def emit(self, count: int, pos, vel, life, size0, size1, col0, col1=None, rot=None, rotv=0.0, drag=0.0,
             grav=0.0, stretch=0.0, frame=F_SOFT, lit=0.0) -> None:
        count = int(min(count, self.capacity - self.n))
        if count <= 0:
            return
        s = slice(self.n, self.n + count)

        def arr(v, shape):
            a = np.asarray(v, np.float32)
            return np.broadcast_to(a, shape)
        self.pos[s] = arr(pos, (count, 3))
        self.vel[s] = arr(vel, (count, 3))
        self.age[s] = 0.0
        self.life[s] = np.maximum(arr(life, (count,)), 1e-3)
        self.size0[s] = arr(size0, (count,))
        self.size1[s] = arr(size1, (count,))
        self.col0[s] = arr(col0, (count, 4))
        self.col1[s] = arr(col1 if col1 is not None else col0, (count, 4))
        self.rot[s] = arr(rot if rot is not None else np.random.uniform(0, 6.283, count), (count,))
        self.rotv[s] = arr(rotv, (count,))
        self.drag[s] = arr(drag, (count,))
        self.grav[s] = arr(grav, (count,))
        self.stretch[s] = arr(stretch, (count,))
        self.frame[s] = arr(frame, (count,))
        self.lit[s] = arr(lit, (count,))
        self.n += count

    # -------------------------------------------------------------- update
    def update(self, dt: float, cam_pos=None) -> None:
        n = self.n
        if n == 0:
            if self._rows != 0:
                self._write(0)
            return
        s = slice(0, n)
        self.age[s] += dt
        alive = self.age[s] < self.life[s]
        if not alive.all():
            keep = np.nonzero(alive)[0]
            m = len(keep)
            for a in self._arrays():
                a[:m] = a[keep]
            n = self.n = m
            s = slice(0, n)
        if n == 0:
            self._write(0)
            return
        damp = np.exp(-self.drag[s] * dt)[:, None]
        self.vel[s] *= damp
        self.vel[s, 2] -= self.grav[s] * dt
        self.pos[s] += self.vel[s] * dt
        self.rot[s] += self.rotv[s] * dt
        order = None
        if not self.additive and cam_pos is not None and n > 1:
            d = ((self.pos[s] - np.asarray(cam_pos, np.float32)) ** 2).sum(1)
            order = np.argsort(-d)
        self._write(n, order)

    def _arrays(self):
        return (self.pos, self.vel, self.age, self.life, self.size0, self.size1, self.col0, self.col1,
                self.rot, self.rotv, self.drag, self.grav, self.stretch, self.frame, self.lit)

    def _write(self, n: int, order=None) -> None:
        if n != self._rows:
            ib = self._indices[:n * 6]
            h = self.geom.modifyPrimitive(0).modifyVertices()
            h.uncleanSetNumRows(len(ib))
            if len(ib):
                memoryview(h).cast("B")[:] = ib.tobytes()
            self._rows = n
        if n == 0:
            return
        idx = np.arange(n) if order is None else order
        t = (self.age[idx] / self.life[idx])[:, None]
        size = self.size0[idx] + (self.size1[idx] - self.size0[idx]) * t[:, 0]
        col = self.col0[idx] + (self.col1[idx] - self.col0[idx]) * t
        v = self._verts
        rows = n * 4
        v[:rows, 0:3] = np.repeat(self.pos[idx], 4, axis=0)
        v[:rows, 5:9] = np.repeat(col, 4, axis=0)
        params = np.stack([size, self.rot[idx], self.stretch[idx], self.frame[idx]], -1)
        v[:rows, 9:13] = np.repeat(params, 4, axis=0)
        vel = np.concatenate([self.vel[idx], self.lit[idx][:, None]], -1)
        v[:rows, 13:17] = np.repeat(vel, 4, axis=0)
        memoryview(self.geom.modifyVertexData().modifyArray(0)).cast("B")[:rows * 68] = v[:rows].tobytes()

    def clear(self) -> None:
        self.n = 0
