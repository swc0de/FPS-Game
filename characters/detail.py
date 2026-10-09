"""The character detail atlas: four tileable 512 px tiles generated at startup.

    0 skin    pores and fine creases
    1 fabric  ripstop weave (a plain weave with a heavier thread every 6 mm)
    2 hard    fine grain and light scratches (polymer, painted metal, rubber)
    3 hair    strands along one axis, with strand-to-strand brightness

Channels: rg the detail normal's x and y (0.5 = flat), b a cavity term from
the height, a a grime/variation mask. render/shaders/character.frag samples
it in bind space from three axes.

Everything is made from periodic noise (random spectra through an inverse
FFT are periodic by construction), so tiles repeat without seams. About
50 ms with numpy; nothing is written to disk.
"""
from __future__ import annotations

import numpy as np

TILE = 512


def _spectral(rng: np.random.Generator, n: int, beta: float, lo: float = 0.0, hi: float = 1.0,
              aniso: tuple = (1.0, 1.0)) -> np.ndarray:
    """Periodic noise with a power spectrum ~ 1/f^beta between relative frequencies lo..hi."""
    fy = np.fft.fftfreq(n)[:, None] * aniso[1]
    fx = np.fft.fftfreq(n)[None, :] * aniso[0]
    f = np.sqrt(fx * fx + fy * fy) * 2.0
    amp = np.where((f > lo) & (f <= hi), 1.0 / np.maximum(f, 1e-6) ** (beta / 2.0), 0.0)
    phase = rng.uniform(0, 2 * np.pi, (n, n))
    out = np.real(np.fft.ifft2(amp * np.exp(1j * phase)))
    out -= out.mean()
    return out / (np.abs(out).max() + 1e-12)


def _normal_from_height(h: np.ndarray, strength: float) -> tuple[np.ndarray, np.ndarray]:
    dx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) * 0.5 * strength
    dy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) * 0.5 * strength
    n = np.stack([-dx, -dy, np.ones_like(h)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return n[..., 0], n[..., 1]


def _tile(height: np.ndarray, strength: float, grime: np.ndarray) -> np.ndarray:
    nx, ny = _normal_from_height(height, strength)
    h = (height - height.min()) / (np.ptp(height) + 1e-9)
    cav = np.clip(0.55 + 0.9 * (h - 0.5), 0.0, 1.0)
    g = np.clip(0.5 + 0.5 * grime, 0.0, 1.0)
    return np.stack([0.5 + 0.5 * nx, 0.5 + 0.5 * ny, cav, g], -1)


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """Periodic Gaussian blur through the FFT."""
    n = img.shape[0]
    f = np.fft.fftfreq(n)
    g = np.exp(-2 * (np.pi * sigma) ** 2 * (f[:, None] ** 2 + f[None, :] ** 2))
    return np.real(np.fft.ifft2(np.fft.fft2(img) * g))


def skin(rng) -> np.ndarray:
    n = TILE
    # pores: random impulses blurred into small dimples, two sizes
    h = np.zeros((n, n))
    for count, sigma in ((1800, 1.1), (500, 2.0)):
        imp = np.zeros((n, n))
        ix = rng.integers(0, n, (count, 2))
        np.add.at(imp, (ix[:, 0], ix[:, 1]), rng.uniform(0.4, 1.0, count))
        h -= _blur(imp, sigma) * sigma
    h = h / (np.abs(h).max() + 1e-9)
    h += 0.35 * _spectral(rng, n, 2.0, 0.02, 0.5)                    # fine creases and bumps
    return _tile(h, 6.0, _spectral(rng, n, 3.0, 0.0, 0.1))


def fabric(rng) -> np.ndarray:
    n = TILE
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    period = 8.0                                                    # threads per tile side: n / period
    warp = np.sin(xx / period * 2 * np.pi)
    weft = np.sin(yy / period * 2 * np.pi)
    over = np.sign(np.sin(xx / period * np.pi) * np.sin(yy / period * np.pi))
    h = 0.5 * (warp * (over > 0) + weft * (over <= 0))
    rip = 64.0                                                      # the ripstop grid
    grid = np.maximum(np.exp(-((xx % rip) - 2) ** 2 / 4.0), np.exp(-((yy % rip) - 2) ** 2 / 4.0))
    h += 0.9 * grid
    h += 0.25 * _spectral(rng, n, 2.5, 0.0, 0.2)                    # slubs and unevenness
    return _tile(h, 2.5, _spectral(rng, n, 3.0, 0.0, 0.08))


def hard(rng) -> np.ndarray:
    n = TILE
    h = 0.4 * _spectral(rng, n, 1.5, 0.1, 1.0)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    for _ in range(40):                                             # light scratches
        a = rng.uniform(0, np.pi)
        c = rng.uniform(0, n, 2)
        d = np.abs(((xx - c[0]) * np.sin(a) - (yy - c[1]) * np.cos(a)))
        along = np.abs(((xx - c[0]) * np.cos(a) + (yy - c[1]) * np.sin(a)))
        h -= 0.5 * np.exp(-d * d / 0.8) * (along < rng.uniform(10, 60))
    return _tile(h, 2.0, _spectral(rng, n, 3.0, 0.0, 0.1))


def hair(rng) -> np.ndarray:
    n = TILE
    h = _spectral(rng, n, 1.0, 0.05, 1.0, aniso=(1.0, 0.06))         # strands along y
    var = _spectral(rng, n, 1.5, 0.02, 0.6, aniso=(1.0, 0.08))
    return _tile(h, 3.0, var)


def atlas(seed: int = 7) -> np.ndarray:
    """(1024, 1024, 4) uint8, rows bottom-up (Panda's RAM order is the same either way here)."""
    rng = np.random.default_rng(seed)
    tiles = [skin(rng), fabric(rng), hard(rng), hair(rng)]
    out = np.zeros((TILE * 2, TILE * 2, 4))
    for k, t in enumerate(tiles):
        r, c = k >> 1, k & 1
        out[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = t
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)


def texture(seed: int = 7):
    """The atlas as a Panda texture (linear data, mipmapped, repeating)."""
    from panda3d.core import SamplerState, Texture
    img = atlas(seed)
    tex = Texture("character_detail")
    tex.setup2dTexture(img.shape[1], img.shape[0], Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs(np.ascontiguousarray(img).tobytes(), "RGBA")
    tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    tex.setWrapU(SamplerState.WM_repeat)
    tex.setWrapV(SamplerState.WM_repeat)
    tex.setAnisotropicDegree(4)
    return tex


_shared = None


def shared_texture():
    """One atlas for every body."""
    global _shared
    if _shared is None:
        _shared = texture()
    return _shared
