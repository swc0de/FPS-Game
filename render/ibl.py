"""Environment processing for image based lighting (numpy, cached on disk).

Pipeline (run once per sky, results cached in assets/cache):

1. Get an equirectangular HDR sky: a downloaded CC0 HDRI or a procedural
   sky (gradient + Mie sun glow + fBm clouds).
2. Find the sun (brightest blurred region) so the shadow-casting
   directional light lines up with the HDRI, then clamp the sun out of the
   image - the analytic sun light provides that energy instead, which avoids
   double lighting and fireflies in the specular prefilter.
3. Replace the lower hemisphere with a ground-bounce colour (many "puresky"
   HDRIs are black below the horizon).
4. Project to 9 spherical-harmonic coefficients for diffuse irradiance.
5. Build a cube map and a GGX-prefiltered mip chain (roughness = mip /
   max_mip) for specular reflections.

Conventions: Z-up world. Equirect pixel row 0 is the *bottom* (latitude
-90 deg) which matches Panda3D's RAM image order. Longitude is
atan2(y, x). Cube faces follow the OpenGL layout so a world-space direction
can be used directly with ``texture(samplerCube, dir)``.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from engine import paths

CACHE_VERSION = 4

# ---------------------------------------------------------------- directions

def equirect_dirs(width: int, height: int) -> np.ndarray:
    """(H, W, 3) unit directions for equirect pixel centres (row 0 = bottom)."""
    lon = ((np.arange(width) + 0.5) / width - 0.5) * 2 * math.pi
    lat = ((np.arange(height) + 0.5) / height - 0.5) * math.pi
    lon, lat = np.meshgrid(lon, lat)
    cl = np.cos(lat)
    return np.stack([cl * np.cos(lon), cl * np.sin(lon), np.sin(lat)], -1).astype(np.float32)


def dir_to_equirect_uv(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    u = np.arctan2(d[..., 1], d[..., 0]) / (2 * math.pi) + 0.5
    v = np.arcsin(np.clip(d[..., 2], -1, 1)) / math.pi + 0.5
    return u, v


def cube_face_dirs(size: int) -> np.ndarray:
    """(6, S, S, 3) directions for cube texels, OpenGL face order/orientation.

    Row index j maps to the GL 't' coordinate (row 0 = t of -1)."""
    c = (np.arange(size) + 0.5) / size * 2 - 1
    s, t = np.meshgrid(c, c)  # s varies along columns, t along rows
    one = np.ones_like(s)
    faces = [
        np.stack([one, -t, -s], -1),    # +X
        np.stack([-one, -t, s], -1),    # -X
        np.stack([s, one, t], -1),      # +Y
        np.stack([s, -one, -t], -1),    # -Y
        np.stack([s, -t, one], -1),     # +Z
        np.stack([-s, -t, -one], -1),   # -Z
    ]
    d = np.stack(faces).astype(np.float32)
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def cube_texel_solid_angle(size: int) -> np.ndarray:
    c = (np.arange(size) + 0.5) / size * 2 - 1
    s, t = np.meshgrid(c, c)
    return ((4.0 / (size * size)) / (1 + s * s + t * t) ** 1.5).astype(np.float32)


def sample_equirect(eq: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Bilinear lookup of an equirect image for directions d (..., 3)."""
    h, w = eq.shape[:2]
    u, v = dir_to_equirect_uv(d)
    x = u * w - 0.5
    y = np.clip(v * h - 0.5, 0, h - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    x0 %= w
    x1 = (x0 + 1) % w
    y1 = np.minimum(y0 + 1, h - 1)
    a = eq[y0, x0] * (1 - fx) + eq[y0, x1] * fx
    b = eq[y1, x0] * (1 - fx) + eq[y1, x1] * fx
    return (a * (1 - fy) + b * fy).astype(np.float32)


def downsample_equirect(eq: np.ndarray, max_width: int) -> np.ndarray:
    while eq.shape[1] > max_width and eq.shape[0] % 2 == 0 and eq.shape[1] % 2 == 0:
        eq = 0.25 * (eq[0::2, 0::2] + eq[1::2, 0::2] + eq[0::2, 1::2] + eq[1::2, 1::2])
    return eq


# ------------------------------------------------------------ procedural sky

def procedural_sky(width: int, height: int, sun_dir, params: dict | None = None,
                   seed: int = 7) -> np.ndarray:
    """Artist-driven HDR sky: zenith/horizon gradient, haze, Mie glow, clouds."""
    p = {
        "zenith": [0.16, 0.30, 0.58],
        "horizon": [0.62, 0.68, 0.74],
        "haze": [0.85, 0.80, 0.72],
        "ground": [0.18, 0.16, 0.13],
        "intensity": 1.6,
        "sun_glow": 2.5,
        "clouds": 0.45,
        "cloud_color": [1.0, 0.97, 0.92],
    }
    p.update(params or {})
    d = equirect_dirs(width, height)
    sun = np.asarray(sun_dir, np.float32)
    sun = sun / np.linalg.norm(sun)
    z = d[..., 2]
    up = np.clip(z, 0, 1)
    zen = np.asarray(p["zenith"], np.float32)
    hor = np.asarray(p["horizon"], np.float32)
    haze = np.asarray(p["haze"], np.float32)
    t = 1.0 - np.power(up, 0.45)[..., None]
    sky = zen * (1 - t) + hor * t
    # haze band at the horizon
    band = np.exp(-np.abs(z) * 9.0)[..., None]
    sky = sky * (1 - 0.6 * band) + haze * 0.6 * band
    # Mie forward scattering around the sun (Henyey-Greenstein-ish lobes)
    cos_s = np.clip((d * sun).sum(-1), -1, 1)
    g = 0.76
    hg = (1 - g * g) / np.power(1 + g * g - 2 * g * cos_s, 1.5) / (4 * math.pi)
    glow = (hg * p["sun_glow"] * 0.9 + np.power(np.clip(cos_s, 0, 1), 6) * 0.5)[..., None]
    sky = sky + glow * np.array([1.0, 0.86, 0.66], np.float32) * (0.4 + 0.6 * band)
    sky *= p["intensity"]

    # clouds: tileable noise projected onto a flat cloud plane
    if p["clouds"] > 0:
        rng = np.random.default_rng(seed)
        n = 512
        fy = np.fft.fftfreq(n)[:, None] * n
        fx = np.fft.rfftfreq(n)[None, :] * n
        f = np.sqrt(fx * fx + fy * fy)
        f[0, 0] = 1
        amp = f ** -1.4
        amp[0, 0] = 0
        amp[f > 128] = 0
        noise = np.fft.irfft2(amp * np.exp(1j * rng.uniform(0, 2 * np.pi, amp.shape)), s=(n, n))
        noise = (noise - noise.mean()) / noise.std()
        zz = np.maximum(z, 0.03)
        px = d[..., 0] / zz * 0.12
        py = d[..., 1] / zz * 0.12
        ix = ((px % 1.0) * n).astype(np.int64) % n
        iy = ((py % 1.0) * n).astype(np.int64) % n
        c = noise[iy, ix]
        cover = np.clip((c - (1.2 - p["clouds"] * 1.6)) * 0.8, 0, 1)
        cover *= np.clip((z - 0.02) * 6, 0, 1) * (1 - 0.5 * np.clip(z, 0, 1))
        lit = np.asarray(p["cloud_color"], np.float32) * p["intensity"] * (
            0.55 + 0.45 * np.clip(cos_s, 0, 1)[..., None] + 0.8 * np.power(np.clip(cos_s, 0, 1), 8)[..., None])
        shade = lit * (0.75 + 0.25 * up[..., None])
        sky = sky * (1 - cover[..., None] * 0.85) + shade * cover[..., None] * 0.85

    below = z < 0
    ground = np.asarray(p["ground"], np.float32) * p["intensity"]
    fade = np.clip(-z * 12, 0, 1)[..., None]
    sky = np.where(below[..., None], sky * (1 - fade) + ground * fade, sky)
    return sky.astype(np.float32)


# ------------------------------------------------------------------- HDRI

def load_hdri(path: Path) -> np.ndarray:
    """Load an .hdr/.exr via Panda3D into a float32 (H, W, 3) RGB array."""
    from panda3d.core import Filename, Texture
    tex = Texture()
    if not tex.read(Filename.fromOsSpecific(str(path))):
        raise IOError(f"could not read HDRI {path}")
    w, h = tex.getXSize(), tex.getYSize()
    comps = tex.getNumComponents()
    ctype = tex.getComponentType()
    raw = bytes(tex.getRamImageAs("RGB"))
    if ctype == Texture.T_float:
        arr = np.frombuffer(raw, np.float32)
    elif ctype == Texture.T_half_float:
        arr = np.frombuffer(raw, np.float16).astype(np.float32)
    elif ctype == Texture.T_unsigned_short:
        arr = np.frombuffer(raw, np.uint16).astype(np.float32) / 65535.0
    else:
        arr = np.frombuffer(raw, np.uint8).astype(np.float32) / 255.0
        arr = arr ** 2.2
    del comps
    return arr.reshape(h, w, 3).copy()


def find_sun(eq: np.ndarray) -> tuple[np.ndarray, float]:
    """Direction and peak value of the brightest (blurred) spot in the sky."""
    small = downsample_equirect(eq, 256)
    lum = small @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    h, w = lum.shape
    lum[: h // 2] = 0  # only the upper hemisphere
    idx = int(np.argmax(lum))
    y, x = divmod(idx, w)
    d = equirect_dirs(w, h)[y, x]
    return d / np.linalg.norm(d), float(eq.max())


def clamp_highlights(eq: np.ndarray, limit: float) -> np.ndarray:
    lum = eq @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    scale = np.where(lum > limit, limit / np.maximum(lum, 1e-6), 1.0)
    return eq * scale[..., None]


def replace_ground(eq: np.ndarray, ground_rgb) -> np.ndarray:
    h = eq.shape[0]
    d = equirect_dirs(eq.shape[1], h)
    z = d[..., 2]
    fade = np.clip(-z * 10, 0, 1)[..., None]
    return eq * (1 - fade) + np.asarray(ground_rgb, np.float32) * fade


# ---------------------------------------------------------- SH projection
SH_C = np.array([0.282095, 0.488603, 0.488603, 0.488603, 1.092548, 1.092548, 0.315392, 1.092548, 0.546274],
                np.float32)
SH_BAND = np.array([0, 1, 1, 1, 2, 2, 2, 2, 2])
A_HAT = np.array([math.pi, 2 * math.pi / 3, math.pi / 4])


def sh9_irradiance(eq: np.ndarray) -> np.ndarray:
    """(9, 3) coefficients such that diffuse = albedo * sum(c_k * basis_k(n))."""
    small = downsample_equirect(eq, 256)
    h, w = small.shape[:2]
    d = equirect_dirs(w, h)
    x, y, z = d[..., 0], d[..., 1], d[..., 2]
    lat = ((np.arange(h) + 0.5) / h - 0.5) * math.pi
    dw = (2 * math.pi / w) * (math.pi / h) * np.cos(lat)[:, None]
    basis = np.stack([np.ones_like(x), y, z, x, x * y, y * z, 3 * z * z - 1, x * z, x * x - y * y], -1)
    basis = basis * SH_C  # Y_lm without constants -> with constants
    coeffs = np.einsum("hwk,hwc,hw->kc", basis, small, dw)
    # convolve with clamped cosine and divide by pi; fold basis constant for the shader
    scale = (A_HAT[SH_BAND] / math.pi) * SH_C
    return (coeffs * scale[:, None]).astype(np.float32)


# --------------------------------------------------------------- cube maps

def equirect_to_cube(eq: np.ndarray, size: int) -> np.ndarray:
    src = downsample_equirect(eq, size * 8)
    return sample_equirect(src, cube_face_dirs(size))


def downsample_cube(cube: np.ndarray) -> np.ndarray:
    return 0.25 * (cube[:, 0::2, 0::2] + cube[:, 1::2, 0::2] + cube[:, 0::2, 1::2] + cube[:, 1::2, 1::2])


def upsample_cube(cube: np.ndarray, size: int) -> np.ndarray:
    """Bilinear per-face resample (edges clamped)."""
    s = cube.shape[1]
    if s == size:
        return cube
    c = (np.arange(size) + 0.5) / size * s - 0.5
    c = np.clip(c, 0, s - 1)
    i0 = np.floor(c).astype(np.int64)
    i1 = np.minimum(i0 + 1, s - 1)
    f = (c - i0).astype(np.float32)
    rows = cube[:, i0] * (1 - f)[None, :, None, None] + cube[:, i1] * f[None, :, None, None]
    return rows[:, :, i0] * (1 - f)[None, None, :, None] + rows[:, :, i1] * f[None, None, :, None]


def ggx_prefilter(src: np.ndarray, out_size: int, roughness: float) -> np.ndarray:
    """Brute-force GGX convolution of cube ``src`` into a cube of ``out_size``.

    With N = V = R (split-sum assumption) each source texel L contributes
    D_ggx(N.H) * (N.L) * solid_angle(L)."""
    s = src.shape[1]
    sdirs = cube_face_dirs(s).reshape(-1, 3)
    sw = np.tile(cube_texel_solid_angle(s).reshape(-1), 6)
    srad = src.reshape(-1, 3)
    odirs = cube_face_dirs(out_size).reshape(-1, 3)
    alpha = max(roughness * roughness, 1e-3)
    a2 = alpha * alpha
    out = np.empty((len(odirs), 3), np.float32)
    chunk = 1024
    for i in range(0, len(odirs), chunk):
        ndl = odirs[i:i + chunk] @ sdirs.T
        np.maximum(ndl, 0, out=ndl)
        ndh2 = (1 + ndl) * 0.5
        f = ndh2 * (a2 - 1) + 1
        wgt = a2 / (f * f) * ndl * sw
        out[i:i + chunk] = (wgt @ srad) / np.maximum(wgt.sum(1, keepdims=True), 1e-8)
    return out.reshape(6, out_size, out_size, 3)


def specular_mips(eq: np.ndarray, base_size: int = 256, levels: int = 6, conv_size: int = 32) -> list:
    """Mip chain for the specular cube: level k has roughness k/(levels-1)."""
    base = equirect_to_cube(eq, base_size)
    src = base
    while src.shape[1] > conv_size:
        src = downsample_cube(src)
    mips = [base]
    for k in range(1, levels):
        size = max(base_size >> k, 1)
        r = k / (levels - 1)
        conv = ggx_prefilter(src, min(size, conv_size), r)
        mips.append(upsample_cube(conv, size))
    return mips


# ------------------------------------------------------------------ driver

def build_environment(env: dict, log=print) -> dict:
    """Return dict(equirect, sun_dir, sh, mips) for a map's environment config.

    env keys: hdri (name in assets/hdri, optional), sun_dir (fallback / procedural),
    sky (procedural params), sun_clamp, ground (rgb), sky_target (normalised
    average upper-hemisphere radiance)."""
    hdri_path = None
    for name in [env.get("hdri")] + list(env.get("hdri_candidates", [])):
        if not name or hdri_path is not None:
            continue
        for ext in (".hdr", ".exr"):
            p = paths.HDRI_DIR / (name + ext)
            if p.exists():
                hdri_path = p
                break
    key_src = {
        "v": CACHE_VERSION,
        "hdri": str(hdri_path.name) if hdri_path else None,
        "hdri_size": hdri_path.stat().st_size if hdri_path else 0,
        "env": {k: env.get(k) for k in ("sun_dir", "sky", "sun_clamp", "ground", "sky_target", "hdri_rotation")},
    }
    key = hashlib.sha1(json.dumps(key_src, sort_keys=True).encode()).hexdigest()[:16]
    cache = paths.CACHE_DIR / f"env_{key}.npz"
    if cache.exists():
        try:
            data = np.load(cache)
            levels = int(data["levels"])
            return {
                "equirect": data["equirect"].astype(np.float32),
                "sun_dir": data["sun_dir"],
                "sh": data["sh"],
                "mips": [data[f"mip{k}"] for k in range(levels)],
                "source": str(data["source"]),
            }
        except (OSError, KeyError, ValueError) as exc:
            log(f"[ibl] cache unreadable ({exc}); rebuilding")

    sun_dir = np.asarray(env.get("sun_dir", [0.45, -0.55, 0.70]), np.float32)
    sun_dir /= np.linalg.norm(sun_dir)
    if hdri_path is not None:
        log(f"[ibl] processing HDRI {hdri_path.name}")
        eq = load_hdri(hdri_path)
        rot = float(env.get("hdri_rotation", 0.0))
        if rot:
            shift = int(round(rot / 360.0 * eq.shape[1]))
            eq = np.roll(eq, shift, axis=1)
        eq = downsample_equirect(eq, int(env.get("sky_max_width", 2048)))
        found, peak = find_sun(eq)
        if peak > 20.0 and found[2] > 0.05:
            sun_dir = found
        source = f"hdri:{hdri_path.stem}"
    else:
        log("[ibl] generating procedural sky")
        eq = procedural_sky(2048, 1024, sun_dir, env.get("sky"))
        source = "procedural"
    eq = clamp_highlights(eq, float(env.get("sun_clamp", 12.0)))
    # normalise brightness so any HDRI matches the game's exposure/sun balance
    target = env.get("sky_target")
    if target:
        small = downsample_equirect(eq, 256)
        d = equirect_dirs(small.shape[1], small.shape[0])
        upper = small[d[..., 2] > 0.05]
        mean = float((upper @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean())
        if mean > 1e-6:
            eq = eq * (float(target) / mean)
    ground = env.get("ground")
    if ground is not None:
        eq = replace_ground(eq, ground)
    sh = sh9_irradiance(eq)
    log("[ibl] prefiltering specular cube map...")
    mips = specular_mips(eq)
    try:
        paths.ensure_dirs()
        save = {"equirect": eq.astype(np.float16), "sun_dir": sun_dir, "sh": sh,
                "levels": len(mips), "source": source}
        for k, m in enumerate(mips):
            save[f"mip{k}"] = m
        np.savez(cache, **save)
    except OSError as exc:
        log(f"[ibl] could not write cache: {exc}")
    return {"equirect": eq, "sun_dir": sun_dir, "sh": sh, "mips": mips, "source": source}
