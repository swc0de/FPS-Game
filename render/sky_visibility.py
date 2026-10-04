"""Baked sky-visibility volume (cheap large-scale ambient occlusion).

Without global illumination, image-based sky lighting would make building
interiors as bright as the outdoors. At load time we:

1. voxelise every opaque level piece into an occupancy grid (0.5 m voxels),
2. place probes on a coarse 3D grid (~1.5 m) over the playable volume,
3. march 32 cosine-distributed rays per probe over the upper hemisphere
   through the voxel grid (fully vectorised numpy),
4. store visibility = fraction of rays that escape to the sky, and the bent
   normal = mean escaping direction, in an RGBA8 3D texture.

The PBR shader samples it trilinearly (offset along the surface normal) and
scales the IBL diffuse/specular terms. Results are cached per level layout.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

import numpy as np

from engine import paths
from engine.geometry import hpr_matrix

CACHE_VERSION = 2


class Occluder:
    __slots__ = ("center", "half", "rot")

    def __init__(self, center, size, hpr=(0, 0, 0)):
        self.center = np.asarray(center, np.float64)
        self.half = np.asarray(size, np.float64) * 0.5
        self.rot = hpr_matrix(hpr) if any(abs(a) > 1e-6 for a in hpr) else None

    def aabb(self):
        if self.rot is None:
            return self.center - self.half, self.center + self.half
        ext = np.abs(self.rot).T @ self.half
        return self.center - ext, self.center + ext


def _hemisphere_dirs(n: int) -> np.ndarray:
    """Cosine-weighted directions on the +Z hemisphere (Hammersley-style)."""
    i = np.arange(n) + 0.5
    u1 = i / n
    u2 = (i * 0.6180339887) % 1.0
    r = np.sqrt(u1)
    phi = 2 * math.pi * u2
    return np.stack([r * np.cos(phi), r * np.sin(phi), np.sqrt(1 - u1)], -1)


def bake(occluders: list[Occluder], bounds_min, bounds_max, spacing=(1.5, 1.5, 1.0),
         voxel: float = 0.5, rays: int = 32, max_dist: float = 40.0, log=print) -> dict:
    t0 = time.time()
    bmin = np.asarray(bounds_min, np.float64)
    bmax = np.asarray(bounds_max, np.float64)
    vdims = np.maximum(np.ceil((bmax - bmin) / voxel).astype(int), 1)
    occ = np.zeros(vdims, bool)
    vc = [bmin[a] + (np.arange(vdims[a]) + 0.5) * voxel for a in range(3)]
    for o in occluders:
        lo, hi = o.aabb()
        i0 = np.clip(np.floor((lo - bmin) / voxel).astype(int), 0, vdims)
        i1 = np.clip(np.ceil((hi - bmin) / voxel).astype(int), 0, vdims)
        if np.any(i1 <= i0):
            continue
        if o.rot is None:
            # conservative fill: any voxel whose centre lies inside, or thin pieces
            lo_c = np.searchsorted(vc[0], lo[0] - voxel * 0.25), np.searchsorted(vc[1], lo[1] - voxel * 0.25), \
                np.searchsorted(vc[2], lo[2] - voxel * 0.25)
            hi_c = np.searchsorted(vc[0], hi[0] + voxel * 0.25), np.searchsorted(vc[1], hi[1] + voxel * 0.25), \
                np.searchsorted(vc[2], hi[2] + voxel * 0.25)
            occ[lo_c[0]:max(hi_c[0], lo_c[0] + 1), lo_c[1]:max(hi_c[1], lo_c[1] + 1),
                lo_c[2]:max(hi_c[2], lo_c[2] + 1)] = True
        else:
            xs, ys, zs = vc[0][i0[0]:i1[0]], vc[1][i0[1]:i1[1]], vc[2][i0[2]:i1[2]]
            g = np.stack(np.meshgrid(xs, ys, zs, indexing="ij"), -1) - o.center
            local = g @ o.rot.T   # world -> local (rot rows are local axes)
            inside = np.all(np.abs(local) <= o.half + voxel * 0.3, axis=-1)
            occ[i0[0]:i1[0], i0[1]:i1[1], i0[2]:i1[2]] |= inside

    sp = np.asarray(spacing, np.float64)
    pdims = np.maximum(np.round((bmax - bmin) / sp).astype(int), 1)
    origin = bmin + sp * 0.5
    gx, gy, gz = (origin[a] + np.arange(pdims[a]) * sp[a] for a in range(3))
    probes = np.stack(np.meshgrid(gx, gy, gz, indexing="ij"), -1).reshape(-1, 3)
    dirs = _hemisphere_dirs(rays)
    step = voxel * 0.9
    ts = np.arange(voxel * 0.6, max_dist, step)
    vis = np.zeros(len(probes), np.float32)
    bent = np.zeros((len(probes), 3), np.float32)
    chunk = max(1, int(4_000_000 // (rays * len(ts))))
    for c0 in range(0, len(probes), chunk):
        p = probes[c0:c0 + chunk]
        pos = p[:, None, None, :] + dirs[None, :, None, :] * ts[None, None, :, None]
        idx = np.floor((pos - bmin) / voxel).astype(np.int32)
        inb = np.all((idx >= 0) & (idx < vdims), axis=-1)
        idxc = np.clip(idx, 0, vdims - 1)
        solid = occ[idxc[..., 0], idxc[..., 1], idxc[..., 2]] & inb
        # rays that leave the volume through the sides/top escape; a ray only
        # counts as escaped if it never hit a solid voxel before leaving
        blocked = solid.any(axis=2)
        esc = ~blocked
        vis[c0:c0 + chunk] = esc.mean(axis=1)
        b = (esc[..., None] * dirs[None]).sum(1)
        bent[c0:c0 + chunk] = b
    norm = np.linalg.norm(bent, axis=1, keepdims=True)
    bent = np.where(norm > 1e-6, bent / np.maximum(norm, 1e-6), np.array([0, 0, 1.0]))

    vis = vis.reshape(pdims)
    bent = bent.reshape(*pdims, 3)
    # probes buried inside solid geometry take values from open neighbours
    pidx = np.floor((probes - bmin) / voxel).astype(int)
    pidx = np.clip(pidx, 0, vdims - 1)
    buried = occ[pidx[:, 0], pidx[:, 1], pidx[:, 2]].reshape(pdims)
    for _ in range(3):
        if not buried.any():
            break
        acc = np.zeros_like(vis)
        accb = np.zeros_like(bent)
        cnt = np.zeros_like(vis)
        for axis in range(3):
            for sh in (-1, 1):
                nv = np.roll(vis, sh, axis)
                nb = np.roll(bent, sh, axis)
                ok = ~np.roll(buried, sh, axis)
                acc += np.where(ok, nv, 0)
                accb += np.where(ok[..., None], nb, 0)
                cnt += ok
        fill = buried & (cnt > 0)
        vis = np.where(fill, acc / np.maximum(cnt, 1), vis)
        bent = np.where(fill[..., None], accb / np.maximum(cnt, 1)[..., None], bent)
        buried = buried & ~fill
    log(f"[skyvis] baked {pdims[0]}x{pdims[1]}x{pdims[2]} probes, {rays} rays in {time.time() - t0:.1f}s")
    return {
        "vis": vis.astype(np.float32),
        "bent": bent.astype(np.float32),
        "min": (bmin).astype(np.float32),
        "spacing": sp.astype(np.float32),
        "dims": pdims,
    }


def bake_cached(occluders: list[Occluder], bounds_min, bounds_max, key_extra="", log=print, **kw) -> dict:
    h = hashlib.sha1()
    h.update(json.dumps([CACHE_VERSION, list(map(float, bounds_min)), list(map(float, bounds_max)),
                         key_extra, sorted(kw.items())]).encode())
    for o in occluders:
        h.update(o.center.tobytes())
        h.update(o.half.tobytes())
        if o.rot is not None:
            h.update(o.rot.tobytes())
    cache = paths.CACHE_DIR / f"skyvis_{h.hexdigest()[:16]}.npz"
    if cache.exists():
        try:
            d = np.load(cache)
            return {k: d[k] for k in d.files}
        except (OSError, ValueError):
            pass
    result = bake(occluders, bounds_min, bounds_max, log=log, **kw)
    try:
        paths.ensure_dirs()
        np.savez_compressed(cache, **result)
    except OSError:
        pass
    return result


def make_texture(data: dict):
    """RGBA8 3D texture: rgb = bent normal * .5 + .5, a = visibility."""
    from panda3d.core import SamplerState, Texture
    vis = data["vis"]
    bent = data["bent"]
    nx, ny, nz = vis.shape
    rgba = np.concatenate([bent * 0.5 + 0.5, vis[..., None]], -1)
    # Panda 3D texture RAM layout: z pages, each page y rows, x columns
    arr = np.transpose(rgba, (2, 1, 0, 3))
    arr8 = (np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8)
    tex = Texture("sky_visibility")
    tex.setup3dTexture(nx, ny, nz, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs(np.ascontiguousarray(arr8).tobytes(), "RGBA")
    tex.setMinfilter(SamplerState.FT_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    for setter in (tex.setWrapU, tex.setWrapV, tex.setWrapW):
        setter(SamplerState.WM_clamp)
    sp = data["spacing"]
    vmin = data["min"]
    size = np.array([nx, ny, nz]) * sp
    return tex, vmin, 1.0 / size


def neutral_texture():
    from panda3d.core import Texture
    tex = Texture("sky_visibility_neutral")
    tex.setup3dTexture(1, 1, 1, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs(bytes([128, 128, 255, 255]), "RGBA")
    return tex
