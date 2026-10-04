"""Bullet-hole / scorch / blood decals.

All decals live in one dynamic vertex buffer used as a ring (oldest decal is
overwritten when full), so hundreds of holes cost a single draw call. They
use the PBR shader compiled with ``DECAL`` (alpha from the albedo atlas) and
three procedurally generated atlases (albedo+alpha, normal, ORM), so holes
are lit, shadowed and fogged exactly like the surfaces they sit on: metal
impacts show bright scraped rings, concrete gets chipped craters, etc.
"""
from __future__ import annotations

import math
import random

import numpy as np
from panda3d.core import (
    Geom,
    GeomNode,
    GeomTriangles,
    GeomVertexData,
    LVecBase4f,
    OmniBoundingVolume,
    SamplerState,
    Texture,
    TransparencyAttrib,
    Vec3,
)

from engine.geometry import FLOATS_PER_VERTEX, vertex_format
from render import shader_loader

CELL = 256
GRID = 4
STYLES = {"concrete": 0, "brick": 1, "plaster": 2, "wood": 3, "metal": 4, "fabric": 5, "dirt": 6, "glass": 7,
          "scorch": 8, "blood": 9, "concrete2": 10, "metal2": 11}
SIZES = {"concrete": 0.22, "brick": 0.22, "plaster": 0.22, "wood": 0.16, "metal": 0.13, "fabric": 0.12,
         "dirt": 0.24, "glass": 0.25, "scorch": 2.6, "blood": 0.55, "concrete2": 0.24, "metal2": 0.13}


def _noise(n, rng, beta=2.0):
    fy = np.fft.fftfreq(n)[:, None] * n
    fx = np.fft.rfftfreq(n)[None, :] * n
    f = np.sqrt(fx * fx + fy * fy)
    f[0, 0] = 1
    amp = f ** (-beta / 2)
    amp[0, 0] = 0
    out = np.fft.irfft2(amp * np.exp(1j * rng.uniform(0, 2 * np.pi, amp.shape)), s=(n, n))
    return (out - out.mean()) / (out.std() + 1e-9)


def _cell(style: str, rng) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns albedo RGBA, height, ORM for one decal cell."""
    n = CELL
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    u = (x + 0.5) / n * 2 - 1
    v = (y + 0.5) / n * 2 - 1
    r = np.sqrt(u * u + v * v)
    ang = np.arctan2(v, u)
    nz = _noise(n, rng, 1.8)
    rough_r = r * (1 + 0.18 * nz)
    alb = np.zeros((n, n, 4), np.float32)
    height = np.zeros((n, n), np.float32)
    orm = np.zeros((n, n, 3), np.float32)
    orm[..., 0] = 1.0
    orm[..., 1] = 0.9

    def crater(hole_r, rim_r, dust_rgb, hole_rgb, chip_amount=0.0, cracks=0.0):
        """Dark core, a chipped darker crater around it and a faint dust halo."""
        core = np.clip((hole_r - rough_r) / 0.025, 0, 1)
        crater_r = hole_r * 2.3
        crater_m = np.clip((crater_r - rough_r) / 0.035, 0, 1) * (1 - core)
        halo = np.clip((rim_r - rough_r) / 0.3, 0, 1) * (1 - core) * (1 - crater_m)
        a = np.clip(core + crater_m * 0.95 + halo * 0.55, 0, 1)
        dust = np.asarray(dust_rgb)[None, None, :] * (1 + 0.15 * nz[..., None])
        crater_col = np.asarray(dust_rgb)[None, None, :] * 0.55 * (1 + 0.25 * nz[..., None])
        col = dust * halo[..., None] + crater_col * crater_m[..., None] + np.asarray(hole_rgb)[None, None, :] * core[..., None]
        col = col / np.maximum(halo + crater_m + core, 1e-3)[..., None]
        h = -core * 1.0 - crater_m * 0.45 - halo * 0.05
        if cracks > 0:
            crack = np.zeros_like(r)
            for k in range(rng.integers(3, 6)):
                a0 = rng.uniform(0, 2 * math.pi)
                d = np.abs(np.angle(np.exp(1j * (ang - a0 - 0.3 * nz * 0.2))))
                line = np.exp(-(d * r * 30) ** 2) * (r < rng.uniform(0.6, 0.95)) * (r > hole_r)
                crack = np.maximum(crack, line)
            a = np.maximum(a, crack * cracks)
            col = col * (1 - crack[..., None] * 0.7)
            h -= crack * 0.3
        if chip_amount > 0:
            chips = (np.clip(_noise(n, rng, 1.0), 0, None) > 1.6) * (rough_r < rim_r * 1.2)
            a = np.maximum(a, chips * chip_amount)
            col = col * (1 - 0.3 * chips[..., None])
        return col, a, h

    if style in ("concrete", "concrete2"):
        col, a, h = crater(0.17, 0.75 if style == "concrete" else 0.85, (0.66, 0.64, 0.6), (0.05, 0.05, 0.05), 0.8, 0.8)
    elif style == "brick":
        col, a, h = crater(0.17, 0.72, (0.6, 0.36, 0.28), (0.06, 0.04, 0.03), 0.7, 0.4)
    elif style == "plaster":
        col, a, h = crater(0.16, 0.8, (0.9, 0.88, 0.84), (0.1, 0.1, 0.09), 0.5, 1.0)
    elif style == "dirt":
        col, a, h = crater(0.2, 0.8, (0.22, 0.18, 0.14), (0.04, 0.03, 0.03), 0.3, 0.0)
    elif style == "wood":
        col, a, h = crater(0.17, 0.5, (0.78, 0.62, 0.42), (0.05, 0.03, 0.02), 0.0, 0.0)
        fibre = np.exp(-(u * 3) ** 2) * np.clip(1 - np.abs(v) * 1.2, 0, 1) * (0.5 + 0.5 * _noise(n, rng, 0.8))
        a = np.maximum(a, np.clip(fibre, 0, 1) * 0.9)
        col = col * (1 - 0.0 * fibre[..., None]) + np.array([0.82, 0.68, 0.48]) * fibre[..., None] * 0.2
    elif style in ("metal", "metal2"):
        hole = np.clip((0.2 - rough_r) / 0.03, 0, 1)
        ring = np.exp(-((r - 0.3) ** 2) / 0.008) * (1 - hole)
        scorch = np.clip(1 - r / 0.55, 0, 1) * (1 - hole)
        a = np.clip(hole + ring + scorch * 0.6, 0, 1)
        col = np.ones((n, n, 3), np.float32) * 0.2
        col = col * (1 - ring[..., None]) + np.array([0.75, 0.75, 0.74]) * ring[..., None]
        col = col * (1 - hole[..., None]) + np.array([0.02, 0.02, 0.02]) * hole[..., None]
        h = -hole * 1.0 + ring * 0.4
        orm[..., 1] = np.clip(0.6 - ring * 0.4, 0.1, 1)
        orm[..., 2] = ring
    elif style == "fabric":
        col, a, h = crater(0.12, 0.3, (0.3, 0.27, 0.22), (0.04, 0.04, 0.03), 0.0, 0.0)
        fray = (np.clip(_noise(n, rng, 0.9), 0, None) > 1.2) * (r < 0.35)
        a = np.maximum(a, fray * 0.8)
    elif style == "glass":
        hole = np.clip((0.08 - r) / 0.02, 0, 1)
        crack = np.zeros_like(r)
        for k in range(9):
            a0 = rng.uniform(0, 2 * math.pi)
            d = np.abs(np.angle(np.exp(1j * (ang - a0))))
            crack = np.maximum(crack, np.exp(-(d * r * 60) ** 2) * (r < rng.uniform(0.5, 1.0)))
        rings = np.exp(-((r - 0.3) ** 2) / 0.0006) * 0.6
        a = np.clip(hole + crack + rings, 0, 1)
        col = np.ones((n, n, 3), np.float32) * 0.85
        h = -hole + crack * 0.2
        orm[..., 1] = 0.1
    elif style == "scorch":
        nz2 = _noise(n, rng, 2.4)
        a = np.clip((1 - r * (1 + 0.35 * nz2)) * 1.6, 0, 1) ** 1.3
        col = np.ones((n, n, 3), np.float32) * np.array([0.05, 0.045, 0.04])
        col = col + np.clip(nz2, 0, None)[..., None] * 0.04
        h = -a * 0.05
        orm[..., 1] = 0.95
    else:  # blood
        nz2 = _noise(n, rng, 2.2)
        blob = np.clip((0.45 - r * (1 + 0.5 * nz2)) / 0.05, 0, 1)
        drops = np.zeros_like(r)
        for k in range(14):
            cx, cy = rng.uniform(-0.8, 0.8, 2)
            rr = rng.uniform(0.02, 0.07)
            drops = np.maximum(drops, np.clip((rr - np.sqrt((u - cx) ** 2 + (v - cy) ** 2)) / 0.01, 0, 1))
        a = np.clip(blob + drops, 0, 1) * 0.92
        col = np.ones((n, n, 3), np.float32) * np.array([0.22, 0.02, 0.02])
        h = a * 0.1
        orm[..., 1] = 0.35
    alb[..., 0:3] = np.clip(col, 0, 1)
    alb[..., 3] = np.clip(a, 0, 1) * np.clip((1 - r) / 0.08, 0, 1)
    height = h
    orm[..., 0] = np.clip(1 + h * 0.5, 0.3, 1)
    if style not in ("metal", "metal2", "glass", "scorch", "blood"):
        orm[..., 1] = np.clip(0.95 + h * 0.05, 0, 1)
    return alb, height, orm


def make_atlases(seed: int = 11):
    rng = np.random.default_rng(seed)
    n = CELL * GRID
    alb = np.zeros((n, n, 4), np.float32)
    hgt = np.zeros((n, n), np.float32)
    orm = np.zeros((n, n, 3), np.float32)
    orm[..., 0] = 1
    for style, idx in STYLES.items():
        a, h, o = _cell(style, rng)
        cx, cy = idx % GRID, idx // GRID
        sl = (slice(cy * CELL, (cy + 1) * CELL), slice(cx * CELL, (cx + 1) * CELL))
        alb[sl] = a
        hgt[sl] = h
        orm[sl] = o
    s = 6.0
    dx = (np.roll(hgt, -1, 1) - np.roll(hgt, 1, 1)) * 0.5 * s
    dy = (np.roll(hgt, -1, 0) - np.roll(hgt, 1, 0)) * 0.5 * s
    nrm = np.stack([-dx, -dy, np.ones_like(hgt)], -1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    nrm = np.concatenate([nrm * 0.5 + 0.5, np.full((n, n, 1), 0.5)], -1)

    def tex(name, arr, fmt, srgb=False):
        t = Texture(name)
        c = arr.shape[2]
        t.setup2dTexture(n, n, Texture.T_unsigned_byte, fmt)
        t.setRamImageAs((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes(), "RGBA" if c == 4 else "RGB")
        if srgb:
            t.setFormat(Texture.F_srgb_alpha)
        t.setMinfilter(SamplerState.FT_linear_mipmap_linear)
        t.setMagfilter(SamplerState.FT_linear)
        t.setWrapU(SamplerState.WM_clamp)
        t.setWrapV(SamplerState.WM_clamp)
        t.setAnisotropicDegree(4)
        return t
    return (tex("decal_albedo", alb, Texture.F_rgba8, srgb=True), tex("decal_normal", nrm, Texture.F_rgba8),
            tex("decal_orm", orm, Texture.F_rgb8))


class DecalSystem:
    def __init__(self, base, defines: dict, capacity: int = 640):
        self.capacity = capacity
        self.next = 0
        vdata = GeomVertexData("decals", vertex_format(), Geom.UHDynamic)
        vdata.uncleanSetNumRows(capacity * 4)
        memoryview(vdata.modifyArray(0)).cast("B")[:] = bytes(capacity * 4 * FLOATS_PER_VERTEX * 4)
        prim = GeomTriangles(Geom.UHStatic)
        prim.setIndexType(Geom.NTUint32)
        base_idx = np.arange(capacity, dtype=np.uint32)[:, None] * 4
        idx = (base_idx + np.array([0, 1, 2, 0, 2, 3], np.uint32)[None, :]).astype(np.uint32).ravel()
        h = prim.modifyVertices()
        h.uncleanSetNumRows(len(idx))
        memoryview(h).cast("B")[:] = idx.tobytes()
        self.geom = Geom(vdata)
        self.geom.addPrimitive(prim)
        node = GeomNode("decals")
        node.addGeom(self.geom)
        node.setBounds(OmniBoundingVolume())
        node.setFinal(True)
        self.np = base.render.attachNewNode(node)
        self.set_defines(defines)
        alb, nrm, orm = make_atlases()
        self.np.setShaderInput("u_albedo", alb)
        self.np.setShaderInput("u_normalMap", nrm)
        self.np.setShaderInput("u_orm", orm)
        self.np.setShaderInput("u_matParams", LVecBase4f(0, 1, 1, 1))
        self.np.setTransparency(TransparencyAttrib.MAlpha)
        self.np.setDepthWrite(False)
        self.np.setDepthOffset(2)
        self.np.setBin("fixed", 5)
        from render.renderer import NO_DEPTH_PASSES
        self.np.hide(NO_DEPTH_PASSES)

    def set_defines(self, defines: dict) -> None:
        """(Re)compile the decal variant of the PBR shader for these quality defines."""
        d = dict(defines)
        d["DECAL"] = True
        d["PARALLAX"] = False
        self.np.setShader(shader_loader.load("pbr.vert", "pbr.frag", d), 40)

    def add(self, pos, normal, style: str, size: float | None = None, rotation: float | None = None) -> None:
        if style not in STYLES:
            return
        n = Vec3(*normal)
        if n.lengthSquared() < 1e-8:
            return
        n.normalize()
        size = (size if size is not None else SIZES.get(style, 0.1)) * random.uniform(0.85, 1.15)
        up = Vec3(0, 0, 1) if abs(n.z) < 0.9 else Vec3(0, 1, 0)
        t = up.cross(n)
        t.normalize()
        b = n.cross(t)
        a = random.uniform(0, 2 * math.pi) if rotation is None else rotation
        ca, sa = math.cos(a), math.sin(a)
        t2 = t * ca + b * sa
        b2 = n.cross(t2)
        p = Vec3(*pos) + n * 0.004
        half = size * 0.5
        idx = STYLES[style]
        u0 = (idx % GRID) / GRID
        v0 = (idx // GRID) / GRID
        du = 1.0 / GRID
        e = 0.5 / (CELL * GRID)
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        uvs = [(u0 + e, v0 + e), (u0 + du - e, v0 + e), (u0 + du - e, v0 + du - e), (u0 + e, v0 + du - e)]
        rows = np.zeros((4, FLOATS_PER_VERTEX), np.float32)
        for i, ((cx, cy), (uu, vv)) in enumerate(zip(corners, uvs)):
            q = p + t2 * (cx * half) + b2 * (cy * half)
            rows[i, 0:3] = (q.x, q.y, q.z)
            rows[i, 3:6] = (n.x, n.y, n.z)
            rows[i, 6:9] = (t2.x, t2.y, t2.z)
            rows[i, 9:12] = (b2.x, b2.y, b2.z)
            rows[i, 12:14] = (uu, vv)
        slot = self.next
        self.next = (self.next + 1) % self.capacity
        stride = FLOATS_PER_VERTEX * 4
        handle = self.geom.modifyVertexData().modifyArray(0).modifyHandle()
        handle.setSubdata(slot * 4 * stride, 4 * stride, rows.tobytes())

    def clear(self) -> None:
        memoryview(self.geom.modifyVertexData().modifyArray(0)).cast("B")[:] = \
            bytes(self.capacity * 4 * FLOATS_PER_VERTEX * 4)
        self.next = 0
