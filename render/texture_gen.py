"""Procedural, tileable PBR texture synthesis (offline fallback).

When CC0 textures cannot be downloaded the game synthesises its own
material sets with numpy. Every map tiles seamlessly because all noise is
built from periodic primitives:

* ``fbm``      - spectral synthesis: random-phase white noise shaped by a
                 1/f^beta power spectrum and inverse-FFT'd. The FFT basis is
                 periodic, so the result tiles perfectly.
* ``worley``   - cellular noise on a wrapped jittered grid (F1, F2, cell id).
* patterns     - bricks, planks, tiles, corrugation, diamond plate computed
                 analytically from UVs and perturbed with noise.

Each generator returns linear-space arrays in [0, 1]:
    albedo (sRGB-encoded colour), height, roughness, metallic, ao
and the normal map is derived from the height field with central
differences (OpenGL convention, +Y = +V).

Array row ``j`` corresponds to texture coordinate v = (j + .5) / size, which
is exactly Panda3D's RAM-image row order (bottom row first).
"""
from __future__ import annotations

import numpy as np


# Bump when generators change so cached procedural textures are rebuilt.
GENERATOR_VERSION = 2


class TexGen:
    def __init__(self, size: int, seed: int):
        self.n = int(size)
        self.rng = np.random.default_rng(seed)
        y, x = np.mgrid[0:self.n, 0:self.n].astype(np.float32)
        self.u = (x + 0.5) / self.n   # 0..1 across columns
        self.v = (y + 0.5) / self.n   # 0..1 across rows
        fy = np.fft.fftfreq(self.n)[:, None] * self.n
        fx = np.fft.rfftfreq(self.n)[None, :] * self.n
        self._fx, self._fy = fx.astype(np.float32), fy.astype(np.float32)

    # -------------------------------------------------------------- noise
    def fbm(self, beta: float = 2.0, fmin: float = 1.0, fmax: float | None = None,
            aniso=(1.0, 1.0)) -> np.ndarray:
        """Tileable fractal noise, zero mean / unit std.

        beta  : spectral slope (2 = natural "pink/brown" look, higher = smoother)
        fmin/fmax : band limits in cycles per texture
        aniso : stretch factors (sx, sy); sx > sy gives streaks along u
        """
        sx, sy = aniso
        f = np.sqrt((self._fx / sx) ** 2 + (self._fy / sy) ** 2)
        f[0, 0] = 1.0
        amp = f ** (-beta / 2.0)
        amp[f < fmin] = 0.0
        if fmax is not None:
            amp *= np.clip((fmax * 1.25 - f) / (fmax * 0.25), 0, 1)
        amp[0, 0] = 0.0
        phase = self.rng.uniform(0, 2 * np.pi, amp.shape).astype(np.float32)
        spec = amp * np.exp(1j * phase)
        out = np.fft.irfft2(spec, s=(self.n, self.n)).astype(np.float32)
        out -= out.mean()
        std = out.std()
        return out / (std if std > 1e-12 else 1.0)

    def worley(self, cells: int, jitter: float = 0.9):
        """Wrapped cellular noise. Returns (F1, F2, cell_id) with distances in cell units."""
        n = self.n
        pts = (self.rng.random((cells, cells, 2)) - 0.5) * jitter + 0.5
        px = self.u * cells
        py = self.v * cells
        ix = np.floor(px).astype(np.int32)
        iy = np.floor(py).astype(np.int32)
        fx = px - ix
        fy = py - iy
        f1 = np.full((n, n), 10.0, np.float32)
        f2 = np.full((n, n), 10.0, np.float32)
        cid = np.zeros((n, n), np.int32)
        for oy in (-1, 0, 1):
            for ox in (-1, 0, 1):
                cx = (ix + ox) % cells
                cy = (iy + oy) % cells
                p = pts[cy, cx]
                dx = ox + p[..., 0] - fx
                dy = oy + p[..., 1] - fy
                d = np.sqrt(dx * dx + dy * dy)
                closer = d < f1
                f2 = np.where(closer, f1, np.minimum(f2, d))
                cid = np.where(closer, cy * cells + cx, cid)
                f1 = np.where(closer, d, f1)
        return f1, f2, cid

    def blur(self, img: np.ndarray, sigma: float) -> np.ndarray:
        """Tileable Gaussian blur (sigma in pixels) via FFT."""
        if sigma <= 0:
            return img
        f2 = (self._fx / self.n) ** 2 + (self._fy / self.n) ** 2
        kernel = np.exp(-2.0 * (np.pi ** 2) * (sigma ** 2) * f2)
        spec = np.fft.rfft2(img) * kernel
        return np.fft.irfft2(spec, s=img.shape).astype(np.float32)

    def warp(self, img: np.ndarray, du: np.ndarray, dv: np.ndarray) -> np.ndarray:
        """Domain-warp by per-pixel offsets (in texture units), wrapped."""
        n = self.n
        x = ((self.u + du) * n - 0.5) % n
        y = ((self.v + dv) * n - 0.5) % n
        x0 = np.floor(x).astype(np.int32)
        y0 = np.floor(y).astype(np.int32)
        tx = x - x0
        ty = y - y0
        x1 = (x0 + 1) % n
        y1 = (y0 + 1) % n
        a = img[y0, x0] * (1 - tx) + img[y0, x1] * tx
        b = img[y1, x0] * (1 - tx) + img[y1, x1] * tx
        return a * (1 - ty) + b * ty

    # ------------------------------------------------------ derived maps
    def normal_from_height(self, h: np.ndarray, strength: float) -> np.ndarray:
        """Tangent-space normal (OpenGL +Y) from height, encoded to [0,1]."""
        scale = strength * self.n / 256.0
        dx = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * 0.5 * scale
        dy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5 * scale
        nrm = np.stack([-dx, -dy, np.ones_like(h)], axis=-1)
        nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
        return nrm * 0.5 + 0.5

    def cavity(self, h: np.ndarray, sigma: float, strength: float) -> np.ndarray:
        """Cheap AO: points below their blurred neighbourhood are occluded."""
        diff = self.blur(h, sigma) - h
        return np.clip(1.0 - diff * strength, 0.0, 1.0)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def remap(x, lo, hi):
        return lo + (hi - lo) * x

    @staticmethod
    def sat(x):
        return np.clip(x, 0.0, 1.0)

    @staticmethod
    def smooth(e0, e1, x):
        t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
        return t * t * (3 - 2 * t)


def _rgb(c) -> np.ndarray:
    c = np.asarray(c, np.float32)
    return c / 255.0 if c.max() > 1.0 else c


def _mix(a, b, t):
    t = t[..., None] if np.ndim(t) == 2 else t
    return a * (1 - t) + b * t


def _tint(base, variation: np.ndarray, amount: float) -> np.ndarray:
    """Apply luminance variation (zero-mean noise) to a base colour."""
    return base[None, None, :] * (1.0 + amount * variation[..., None])


# =================================================================== generators
# Every generator: (g: TexGen, p: dict) -> dict(albedo, height, roughness, metallic, ao)

def gen_concrete(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (150, 148, 142)))
    large = g.fbm(2.4, 1, 24)
    mid = g.fbm(2.0, 8, 128)
    fine = g.fbm(1.2, 64)
    stains = g.sat(g.smooth(0.4, 1.6, g.fbm(2.6, 1, 16)) * p.get("stains", 0.5) * 1.6)
    col = _tint(base, large * 0.07 + mid * 0.04 + fine * 0.03, 1.0)
    stain_col = base * np.array([0.78, 0.74, 0.68])
    col = _mix(col, stain_col[None, None, :] * (1 + 0.05 * mid[..., None]), stains * 0.7)

    # pores / air bubbles
    f1, _, _ = g.worley(int(48 * p.get("pore_scale", 1.0)), 1.0)
    pore_mask = g.smooth(0.16, 0.05, f1) * (g.fbm(2.0, 2, 32) > -0.2) * p.get("pores", 0.5)
    # cracks: thin cell boundaries, only where a low-freq mask allows
    c1, c2, _ = g.worley(6, 1.0)
    edge = c2 - c1
    crack_zone = g.smooth(0.3, 1.4, g.fbm(2.5, 1, 8)) * p.get("cracks", 0.3) * 2.0
    warp_edge = g.warp(edge, g.fbm(2.0, 4, 64) * 0.004, g.fbm(2.0, 4, 64) * 0.004)
    cracks = g.sat(g.smooth(0.035, 0.0, warp_edge) * crack_zone)

    height = 0.5 + mid * 0.06 + fine * 0.05 + large * 0.03 - pore_mask * 0.35 - cracks * 0.4
    col = col * (1 - 0.45 * pore_mask[..., None]) * (1 - 0.6 * cracks[..., None])
    r_lo, r_hi = p.get("roughness", (0.72, 0.95))
    rough = g.remap(g.sat(0.5 + fine * 0.15 + mid * 0.15 - stains * 0.25), r_lo, r_hi)
    ao = g.cavity(height, 3.0, 4.0) * (1 - 0.3 * pore_mask)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_asphalt(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (52, 52, 55)))
    f1, f2, cid = g.worley(110, 1.0)
    stone_val = (np.sin(cid * 12.9898) * 43758.5453) % 1.0
    stones = g.smooth(0.55, 0.25, f1)
    large = g.fbm(2.4, 1, 16)
    fine = g.fbm(1.0, 128)
    col = _tint(base, large * 0.08 + fine * 0.05, 1.0)
    stone_col = _mix(np.array([0.30, 0.30, 0.31])[None, None], np.array([0.62, 0.60, 0.56])[None, None],
                     stone_val[..., None] ** 3)
    col = _mix(col, stone_col, stones * g.sat(stone_val + 0.3) * 0.65)
    c1, c2, _ = g.worley(5, 1.0)
    cracks = g.sat(g.smooth(0.03, 0.0, g.warp(c2 - c1, g.fbm(2, 4, 64) * 0.006, g.fbm(2, 4, 64) * 0.006))
                   * g.smooth(0.2, 1.2, g.fbm(2.4, 1, 8)) * p.get("cracks", 0.5) * 2)
    wear = g.smooth(-0.2, 1.4, g.fbm(2.8, 1, 8))
    col = col * (1 + 0.15 * wear[..., None]) * (1 - 0.7 * cracks[..., None])
    height = 0.5 + stones * 0.25 * stone_val + fine * 0.05 - cracks * 0.5
    rough = g.sat(0.88 - stones * stone_val * 0.18 - wear * 0.08 + fine * 0.04)
    ao = g.cavity(height, 2.0, 5.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_gravel(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (112, 98, 80)))
    layers = []
    height = np.full((g.n, g.n), 0.3, np.float32)
    col = _tint(base, g.fbm(2.2, 1, 32) * 0.12 + g.fbm(1.0, 64) * 0.06, 1.0)
    for cells, amount in ((22, 0.9), (40, 0.8), (70, 0.6)):
        f1, f2, cid = g.worley(cells, 1.0)
        val = (np.sin(cid * 78.233 + cells) * 43758.5453) % 1.0
        dome = g.sat(1.0 - (f1 / 0.48) ** 2)
        mask = dome * (val < p.get("density", 0.75))
        h = np.sqrt(dome) * mask * (0.5 + 0.5 * val)
        update = h > height
        height = np.where(update, h, height)
        stone = np.array([0.55, 0.52, 0.48]) * (0.6 + 0.6 * val[..., None])
        stone = stone * (1 + 0.08 * g.fbm(1.0, 128)[..., None])
        col = np.where(update[..., None], _mix(col, stone, g.sat(dome * 3)), col)
        layers.append(mask)
    height += g.fbm(1.2, 64) * 0.03
    rough = g.sat(0.93 - height * 0.12)
    ao = g.cavity(height, 4.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_dirt(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (104, 88, 66)))
    large = g.fbm(2.3, 1, 16)
    mid = g.fbm(1.8, 8, 96)
    fine = g.fbm(1.0, 96)
    col = _tint(base, large * 0.06 + mid * 0.05 + fine * 0.05, 1.0)
    dry = g.smooth(-0.5, 1.5, large)
    col = _mix(col, col * np.array([1.12, 1.08, 1.03]), dry * 0.5)
    f1, _, cid = g.worley(60, 1.0)
    val = (np.sin(cid * 3.7) * 9123.3) % 1.0
    pebble = g.sat(1 - (f1 / 0.35) ** 2) * (val > 0.72)
    col = _mix(col, np.array([0.5, 0.47, 0.42]) * (0.7 + 0.5 * val[..., None]), pebble * 0.8)
    height = 0.45 + mid * 0.08 + fine * 0.06 + large * 0.05 + pebble * 0.3
    rough = g.sat(0.9 + fine * 0.04 - (1 - dry) * 0.06)
    ao = g.cavity(height, 3.0, 4.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_brick(g: TexGen, p: dict) -> dict:
    courses = int(p.get("courses", 16))
    per_row = int(p.get("bricks_per_row", 5))
    mortar = p.get("mortar", 0.06)          # fraction of brick height
    base = _rgb(p.get("color", (128, 62, 44)))
    mortar_col = _rgb(p.get("mortar_color", (150, 146, 138)))
    row = np.floor(g.v * courses)
    fv = g.v * courses - row
    offset = (row % 2) * 0.5
    col_f = g.u * per_row + offset
    brick_i = np.floor(col_f)
    fu = col_f - brick_i
    brick_id = (row * 131 + (brick_i % per_row) * 17).astype(np.int64)
    rnd = (np.sin(brick_id * 12.9898) * 43758.5453) % 1.0
    rnd2 = (np.sin(brick_id * 78.233) * 12543.123) % 1.0
    # distance to brick edge in "brick height" units (aspect corrected)
    aspect = (courses / per_row)
    du = np.minimum(fu, 1 - fu) * aspect
    dv = np.minimum(fv, 1 - fv)
    edge = np.minimum(du, dv)
    chip_noise = g.fbm(1.6, 16, 256) * 0.035
    edge_n = edge + chip_noise
    brick_mask = g.smooth(mortar * 0.5, mortar * 0.5 + 0.06, edge_n)
    bevel = g.smooth(mortar * 0.5, mortar * 0.5 + 0.18, edge_n)
    surface = g.fbm(1.5, 8, 256)
    tone = (rnd - 0.5) * 0.35
    burnt = (rnd2 > 0.88).astype(np.float32)
    bcol = base[None, None, :] * (1 + tone[..., None] + 0.08 * surface[..., None])
    bcol = _mix(bcol, bcol * np.array([0.55, 0.5, 0.5]), burnt * 0.7)
    soot = g.smooth(0.2, 1.6, g.fbm(2.4, 1, 8))
    bcol = bcol * (1 - 0.25 * soot[..., None])
    mcol = mortar_col[None, None, :] * (1 + 0.08 * g.fbm(1.2, 32)[..., None])
    col = _mix(mcol, bcol, brick_mask)
    height = 0.25 + brick_mask * (0.55 + 0.25 * bevel) + surface * 0.04 * brick_mask + g.fbm(1.0, 128) * 0.02
    rough = g.sat(np.where(brick_mask > 0.5, 0.82 + surface * 0.05, 0.95))
    ao = g.cavity(height, 4.0, 3.0) * (0.75 + 0.25 * brick_mask)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def _wood_grain(g: TexGen, along_u=True, ring_freq=40.0, stretch=12.0):
    a = (stretch, 1.0) if along_u else (1.0, stretch)
    streak = g.fbm(1.6, 2, 256, aniso=a)
    warp = g.fbm(2.2, 1, 32, aniso=a) * 0.02
    coord = (g.v if along_u else g.u) + warp
    rings = np.sin(coord * ring_freq * 2 * np.pi + streak * 1.5) * 0.5 + 0.5
    return streak, rings


def gen_planks(g: TexGen, p: dict) -> dict:
    planks = int(p.get("planks", 8))           # planks across v
    lengths = int(p.get("segments", 2))        # end joints along u
    base = _rgb(p.get("color", (122, 88, 58)))
    row = np.floor(g.v * planks)
    fv = g.v * planks - row
    rnd_row = (np.sin(row * 91.7) * 4375.85) % 1.0
    seg_f = g.u * lengths + rnd_row
    seg = np.floor(seg_f)
    fu = seg_f - seg
    pid = (row * 37 + seg % lengths).astype(np.int64)
    tone = ((np.sin(pid * 12.9898) * 43758.5453) % 1.0 - 0.5)
    streak, rings = _wood_grain(g, True, 30.0)
    col = base[None, None, :] * (1 + 0.28 * tone[..., None] + 0.1 * streak[..., None]
                                 - 0.12 * rings[..., None])
    gap_v = g.smooth(0.0, 0.035, np.minimum(fv, 1 - fv))
    gap_u = g.smooth(0.0, 0.006, np.minimum(fu, 1 - fu))
    gap = gap_v * gap_u
    wear = g.smooth(0.2, 1.5, g.fbm(2.3, 1, 12))
    col = _mix(col, col * np.array([0.72, 0.72, 0.75]), wear * p.get("weathered", 0.5))
    col = col * (0.25 + 0.75 * gap[..., None])
    height = 0.3 + gap * (0.6 + streak * 0.03 + rings * 0.02) + g.fbm(1.0, 128) * 0.01
    rough = g.sat(g.remap(g.sat(0.5 + streak * 0.2 + wear * 0.3), *p.get("roughness", (0.55, 0.85))))
    ao = g.cavity(height, 3.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_plywood(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (186, 152, 108)))
    streak, rings = _wood_grain(g, True, 6.0, stretch=5.0)
    patches = g.fbm(2.6, 1, 8)
    col = base[None, None, :] * (1 + 0.12 * streak[..., None] - 0.1 * rings[..., None] + 0.06 * patches[..., None])
    # sheet seams every half texture + screw holes along the seams
    seam_u = np.minimum(g.u % 0.5, 0.5 - g.u % 0.5)
    seam = g.smooth(0.0, 0.004, seam_u)
    f1, _, cid = g.worley(16, 0.0)
    near_seam = seam_u < 0.03
    holes = g.smooth(0.12, 0.05, f1) * near_seam
    knots_f1, _, kid = g.worley(5, 0.9)
    kval = (np.sin(kid * 3.1) * 999.7) % 1.0
    knots = g.smooth(0.12, 0.02, knots_f1) * (kval > 0.6)
    col = col * (1 - 0.45 * knots[..., None]) * (0.4 + 0.6 * seam[..., None]) * (1 - 0.6 * holes[..., None])
    dirt = g.smooth(0.3, 1.6, g.fbm(2.2, 1, 16))
    col = _mix(col, col * np.array([0.7, 0.68, 0.66]), dirt * 0.5)
    height = 0.5 + streak * 0.02 + seam * 0.2 - holes * 0.3 - knots * 0.05
    rough = g.sat(0.78 + streak * 0.04 + dirt * 0.08)
    ao = g.cavity(height, 2.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def _paint_wear(g: TexGen, amount: float):
    chips = g.fbm(1.9, 2, 256)
    mask = g.smooth(1.6 - amount * 1.2, 1.9 - amount * 1.2, chips + g.fbm(2.5, 1, 8) * 0.6)
    scratches = g.fbm(1.4, 32, 512, aniso=(40.0, 1.0))
    scratch_mask = g.smooth(2.4, 2.9, scratches) * amount
    return g.sat(mask), g.sat(scratch_mask)


def gen_painted_metal(g: TexGen, p: dict) -> dict:
    paint = _rgb(p.get("color", (78, 84, 58)))
    metal = np.array([0.56, 0.57, 0.58])
    rust = np.array([0.36, 0.17, 0.08])
    var = g.fbm(2.2, 1, 32) * 0.05 + g.fbm(1.0, 128) * 0.02
    col = _tint(paint, var, 1.0)
    chip, scratch = _paint_wear(g, p.get("wear", 0.5))
    rust_mask = g.sat(g.smooth(0.6, 1.8, g.fbm(2.2, 1, 64)) * p.get("rust", 0.3) * 2.5) * (chip > 0.3)
    bare = g.sat(chip + scratch)
    col = _mix(col, metal * (1 + 0.1 * g.fbm(1.0, 128)[..., None]), bare)
    col = _mix(col, rust * (1 + 0.2 * g.fbm(1.5, 32)[..., None]), rust_mask)
    grime = g.smooth(0.0, 1.8, g.fbm(2.4, 2, 32)) * 0.3
    col = _mix(col, np.array([0.25, 0.23, 0.2]), grime)
    metallic = g.sat(bare * (1 - rust_mask) * (1 - grime * 2))
    rough = g.sat(np.where(metallic > 0.5, 0.38, p.get("paint_roughness", 0.55)) + grime * 0.3 + rust_mask * 0.4
                  + g.fbm(1.2, 64) * 0.03)
    height = 0.6 - chip * 0.08 - scratch * 0.04 + rust_mask * 0.05 + g.fbm(1.0, 128) * 0.01
    ao = g.cavity(height, 2.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=metallic, ao=ao)


def gen_corrugated(g: TexGen, p: dict) -> dict:
    ridges = int(p.get("ridges", 12))
    profile = np.sin(g.u * ridges * 2 * np.pi)
    height = 0.5 + 0.45 * profile
    d = gen_painted_metal(g, p) if p.get("painted", True) else None
    if d is None:
        zinc = np.array([0.62, 0.63, 0.64])
        col = _tint(zinc, g.fbm(2, 1, 64) * 0.06, 1.0)
        d = dict(albedo=col, roughness=np.full_like(height, 0.45), metallic=np.ones_like(height))
    # streaks of rust/dirt running down the ridges
    streaks = g.smooth(0.5, 2.0, g.fbm(2.0, 2, 128, aniso=(1.0, 20.0)))
    d["albedo"] = d["albedo"] * (1 - 0.25 * streaks[..., None])
    d["roughness"] = g.sat(d["roughness"] + streaks * 0.15)
    d["height"] = height + d.get("height", 0.5) * 0.1
    d["ao"] = g.sat(0.75 + 0.25 * profile) * g.cavity(d["height"], 2.0, 2.0)
    return d


def gen_diamond_plate(g: TexGen, p: dict) -> dict:
    reps = int(p.get("reps", 10))
    su = g.u * reps
    sv = g.v * reps
    cu = su - np.floor(su) - 0.5
    cv = sv - np.floor(sv) - 0.5
    cell = (np.floor(su) + np.floor(sv)) % 2
    # alternate the diamond orientation per cell
    a = np.where(cell > 0, cu + cv, cu - cv)
    b = np.where(cell > 0, cu - cv, cu + cv)
    diamond = g.sat(1.0 - (np.abs(a) / 0.36) ** 2 - (np.abs(b) / 0.09) ** 2)
    bump = np.sqrt(diamond)
    steel = np.array([0.58, 0.59, 0.60])
    col = _tint(steel, g.fbm(1.2, 32) * 0.04 + g.fbm(2.2, 2, 32) * 0.04, 1.0)
    # dust collects in the recesses between treads; it is a dielectric layer
    dust_amt = g.sat(g.smooth(-0.6, 1.4, g.fbm(2.0, 4, 64)) * (1 - bump) * 0.85)
    dust_col = np.array([0.32, 0.30, 0.27])
    col = _mix(col, dust_col * (1 + 0.1 * g.fbm(1.0, 128)[..., None]), dust_amt)
    scratches = g.smooth(2.2, 2.8, g.fbm(1.4, 32, 512, aniso=(30.0, 1.0)))
    rough = g.sat(0.36 + dust_amt * 0.5 - bump * 0.08 + scratches * 0.08 + g.fbm(1.2, 64) * 0.03)
    metal = g.sat(0.95 - dust_amt)
    height = 0.4 + bump * 0.5 + g.fbm(1.0, 128) * 0.01
    ao = g.cavity(height, 2.5, 2.5)
    return dict(albedo=col, height=height, roughness=rough, metallic=metal, ao=ao)


def gen_plaster(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (200, 194, 180)))
    large = g.fbm(2.5, 1, 16)
    mid = g.fbm(2.0, 8, 128)
    fine = g.fbm(1.0, 128)
    col = _tint(base, large * 0.04 + mid * 0.02 + fine * 0.015, 1.0)
    dirt = g.smooth(0.3, 1.8, g.fbm(2.6, 1, 12)) * p.get("dirt", 0.5)
    col = _mix(col, col * np.array([0.75, 0.72, 0.66]), dirt)
    dents_f1, _, did = g.worley(9, 1.0)
    dval = (np.sin(did * 7.1) * 3133.7) % 1.0
    dents = g.smooth(0.18, 0.0, dents_f1) * (dval > 0.75)
    col = col * (1 - 0.25 * dents[..., None])
    height = 0.5 + mid * 0.04 + fine * 0.03 - dents * 0.25
    rough = g.sat(0.88 + fine * 0.03 - dirt * 0.05)
    ao = g.cavity(height, 3.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_fabric(g: TexGen, p: dict) -> dict:
    threads = int(p.get("threads", 96))
    base = _rgb(p.get("color", (150, 132, 96)))
    wu = np.sin(g.u * threads * 2 * np.pi)
    wv = np.sin(g.v * threads * 2 * np.pi)
    over = (np.floor(g.u * threads) + np.floor(g.v * threads)) % 2
    weave = np.where(over > 0, np.abs(wv), np.abs(wu))
    irregular = g.fbm(1.4, 16, 256)
    col = _tint(base, irregular * 0.06 + g.fbm(2.4, 1, 16) * 0.08 + (weave - 0.5) * 0.12, 1.0)
    dirt = g.smooth(0.2, 1.6, g.fbm(2.4, 1, 12)) * p.get("dirt", 0.5)
    col = _mix(col, col * np.array([0.68, 0.62, 0.55]), dirt)
    height = 0.4 + weave * 0.35 + irregular * 0.03
    rough = g.sat(0.95 - weave * 0.04)
    ao = g.cavity(height, 1.5, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_tiles(g: TexGen, p: dict) -> dict:
    count = int(p.get("count", 4))
    base = _rgb(p.get("color", (150, 152, 140)))
    grout_col = _rgb(p.get("grout_color", (90, 90, 86)))
    su = g.u * count
    sv = g.v * count
    tid = (np.floor(su) * 17 + np.floor(sv) * 131).astype(np.int64)
    rnd = (np.sin(tid * 12.9898) * 43758.5453) % 1.0
    fu = su - np.floor(su)
    fv = sv - np.floor(sv)
    edge = np.minimum(np.minimum(fu, 1 - fu), np.minimum(fv, 1 - fv))
    tile = g.smooth(0.012, 0.03, edge)
    bevel = g.smooth(0.012, 0.06, edge)
    col = base[None, None, :] * (1 + (rnd[..., None] - 0.5) * 0.12 + 0.04 * g.fbm(1.5, 16)[..., None])
    dirt = g.smooth(0.0, 1.5, g.fbm(2.4, 1, 16))
    col = _mix(col, col * 0.75, dirt * 0.4)
    col = _mix(grout_col[None, None, :] * (1 + 0.1 * g.fbm(1.0, 128)[..., None]), col, tile)
    height = 0.3 + tile * (0.5 + bevel * 0.15) + g.fbm(1.0, 128) * 0.01
    rough = g.sat(np.where(tile > 0.5, p.get("tile_roughness", 0.35) + dirt * 0.3, 0.9))
    ao = g.cavity(height, 2.0, 3.0)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough), ao=ao)


def gen_brushed_metal(g: TexGen, p: dict) -> dict:
    base = _rgb(p.get("color", (150, 152, 155)))
    brush = g.fbm(1.2, 16, 512, aniso=(60.0, 1.0))
    col = _tint(base, brush * 0.04 + g.fbm(2.2, 2, 32) * 0.03, 1.0)
    dust = g.sat(g.smooth(0.4, 2.2, g.fbm(2.0, 4, 64)) * 0.6)
    col = _mix(col, np.array([0.30, 0.28, 0.25]), dust)
    rough = g.sat(p.get("roughness", 0.35) + brush * 0.04 + dust * 0.45)
    height = 0.5 + brush * 0.01
    return dict(albedo=col, height=height, roughness=rough, metallic=g.sat(1.0 - dust),
                ao=np.ones_like(rough))


def gen_flat(g: TexGen, p: dict) -> dict:
    """Uniform material (PBR calibration spheres, unlit markers)."""
    col = np.ones((g.n, g.n, 3), np.float32) * _rgb(p.get("color", (200, 200, 200)))[None, None, :]
    rough = np.full((g.n, g.n), p.get("roughness", 0.5), np.float32)
    metal = np.full((g.n, g.n), p.get("metallic", 0.0), np.float32)
    return dict(albedo=col, height=np.full_like(rough, 0.5), roughness=rough, metallic=metal,
                ao=np.ones_like(rough))


def gen_hazard(g: TexGen, p: dict) -> dict:
    """Yellow/black hazard stripes (painted on metal or concrete)."""
    stripes = int(p.get("stripes", 4))
    t = ((g.u + g.v) * stripes) % 1.0
    mask = g.smooth(0.48, 0.52, t) * g.smooth(1.0, 0.96, t)
    yellow = _rgb(p.get("color", (200, 160, 20)))
    black = np.array([0.04, 0.04, 0.04])
    col = _mix(yellow[None, None, :] * np.ones((g.n, g.n, 1)), black[None, None, :] * np.ones((g.n, g.n, 1)), mask)
    chip, scratch = _paint_wear(g, p.get("wear", 0.6))
    under = _rgb(p.get("under", (120, 118, 112)))
    col = _mix(col, under[None, None, :] * (1 + 0.1 * g.fbm(1.0, 64)[..., None]), g.sat(chip + scratch))
    grime = g.smooth(0.0, 1.6, g.fbm(2.4, 1, 16)) * 0.4
    col = col * (1 - grime[..., None] * 0.5)
    rough = g.sat(0.55 + grime * 0.3 + chip * 0.2)
    height = 0.5 - chip * 0.05
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough),
                ao=g.cavity(height, 2.0, 2.0))


def gen_plastic(g: TexGen, p: dict) -> dict:
    """Moulded polymer: fine texture stipple, scuffs and light edge-less wear."""
    base = _rgb(p.get("color", (60, 62, 58)))
    stipple = g.fbm(1.0, 128)
    col = _tint(base, g.fbm(2.2, 1, 16) * 0.04 + stipple * 0.02, 1.0)
    scuffs = g.smooth(2.0, 2.8, g.fbm(1.5, 16, 256, aniso=(12.0, 1.0)))
    col = _mix(col, col * 1.35 + 0.03, scuffs * p.get("wear", 0.5))
    dirt = g.smooth(0.2, 1.8, g.fbm(2.4, 1, 16)) * 0.25
    col = col * (1 - dirt[..., None] * 0.4)
    r0 = p.get("roughness", 0.55)
    rough = g.sat(r0 + stipple * 0.04 + scuffs * 0.15 + dirt * 0.2)
    height = 0.5 + stipple * 0.06 - scuffs * 0.02
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough),
                ao=np.ones_like(rough))


def gen_gunmetal(g: TexGen, p: dict) -> dict:
    """Parkerised / anodised gun steel: dark, fine-grained, slightly worn."""
    base = _rgb(p.get("color", (52, 54, 56)))
    grain = g.fbm(1.0, 96)
    col = _tint(base, grain * 0.05 + g.fbm(2.2, 2, 32) * 0.05, 1.0)
    wear = g.smooth(2.1, 2.7, g.fbm(1.6, 8, 256, aniso=(6.0, 1.0)))
    col = _mix(col, np.array([0.48, 0.48, 0.47]), wear * p.get("wear", 0.4))
    rough = g.sat(p.get("roughness", 0.5) + grain * 0.05 - wear * 0.2)
    metal = g.sat(np.full_like(rough, p.get("metallic", 0.8)) + wear * 0.2)
    height = 0.5 + grain * 0.03
    return dict(albedo=col, height=height, roughness=rough, metallic=metal, ao=np.ones_like(rough))


def gen_camo(g: TexGen, p: dict) -> dict:
    """Woven fabric with a multi-tone disruptive camouflage print."""
    palette = [_rgb(c) for c in p.get("palette", [(120, 112, 84), (86, 92, 62), (62, 56, 42), (150, 136, 104)])]
    col = np.ones((g.n, g.n, 3), np.float32) * palette[0][None, None, :]
    for i, c in enumerate(palette[1:]):
        blob = g.fbm(2.6, 2, 24)
        blob = g.warp(blob, g.fbm(2.0, 4, 64) * 0.02, g.fbm(2.0, 4, 64) * 0.02)
        mask = g.smooth(0.35 + 0.15 * i, 0.45 + 0.15 * i, blob)
        col = _mix(col, c[None, None, :] * np.ones_like(col), mask)
    threads = int(p.get("threads", 160))
    wu = np.abs(np.sin(g.u * threads * np.pi))
    wv = np.abs(np.sin(g.v * threads * np.pi))
    over = (np.floor(g.u * threads) + np.floor(g.v * threads)) % 2
    weave = np.where(over > 0, wv, wu)
    col = col * (0.88 + 0.12 * weave[..., None]) * (1 + 0.05 * g.fbm(1.2, 32)[..., None])
    dirt = g.smooth(0.2, 1.8, g.fbm(2.4, 1, 12)) * p.get("dirt", 0.4)
    col = _mix(col, col * np.array([0.7, 0.65, 0.58]), dirt)
    height = 0.4 + weave * 0.3
    rough = g.sat(0.9 - weave * 0.05)
    return dict(albedo=col, height=height, roughness=rough, metallic=np.zeros_like(rough),
                ao=g.cavity(height, 1.2, 2.0))


GENERATORS = {
    "concrete": gen_concrete,
    "asphalt": gen_asphalt,
    "gravel": gen_gravel,
    "dirt": gen_dirt,
    "brick": gen_brick,
    "planks": gen_planks,
    "plywood": gen_plywood,
    "painted_metal": gen_painted_metal,
    "corrugated": gen_corrugated,
    "diamond_plate": gen_diamond_plate,
    "plaster": gen_plaster,
    "fabric": gen_fabric,
    "tiles": gen_tiles,
    "brushed_metal": gen_brushed_metal,
    "flat": gen_flat,
    "hazard": gen_hazard,
    "plastic": gen_plastic,
    "gunmetal": gen_gunmetal,
    "camo": gen_camo,
}


def generate(kind: str, params: dict, size: int, seed: int) -> dict:
    """Run a generator and return packed 8-bit maps.

    Returns dict with uint8 arrays:
        albedo (H,W,3) sRGB, normal (H,W,4) = xyz + height, orm (H,W,3) = AO/rough/metal
    """
    if kind not in GENERATORS:
        raise KeyError(f"unknown procedural material type {kind!r}")
    g = TexGen(size, seed)
    d = GENERATORS[kind](g, params)
    albedo = np.clip(d["albedo"], 0, 1)
    height = d["height"].astype(np.float32)
    hn = (height - height.min()) / max(float(height.max() - height.min()), 1e-6)
    normal = g.normal_from_height(height, params.get("normal_strength", 6.0))
    orm = np.stack([np.clip(d["ao"], 0, 1), np.clip(d["roughness"], 0.03, 1), np.clip(d["metallic"], 0, 1)], -1)
    to8 = lambda a: (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)
    return {
        "albedo": to8(albedo),
        "normal": to8(np.concatenate([normal, hn[..., None]], -1)),
        "orm": to8(orm),
    }
