"""PBR material library.

A material is three textures plus a few scalars:

    albedo  (sRGB)          base colour
    normal  (RGBA, linear)  tangent-space normal (OpenGL +Y) in RGB, height in A
    orm     (RGB, linear)   R = ambient occlusion, G = roughness, B = metallic

Textures are looked up in ``assets/textures/<name>`` (downloaded CC0 sets),
then ``assets/textures/_generated/<name>`` (procedural cache). Missing
materials are synthesised with :mod:`render.texture_gen` (in parallel worker
processes) and cached as PNG so later launches are instant.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from panda3d.core import Filename, NodePath, SamplerState, Texture, Vec4

from engine import paths

DEFAULT_PROCEDURAL_SIZE = 1024
MAP_NAMES = ("albedo", "normal", "orm")


def load_definitions() -> dict:
    with open(paths.DATA_DIR / "materials.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _find_map(folder: Path, stem: str) -> Path | None:
    for ext in (".png", ".jpg", ".jpeg"):
        p = folder / (stem + ext)
        if p.exists():
            return p
    return None


def material_folder(name: str) -> tuple[Path | None, str]:
    """Return (folder, source) for the best available texture set."""
    downloaded = paths.TEXTURE_DIR / name
    if all(_find_map(downloaded, m) for m in MAP_NAMES):
        return downloaded, "downloaded"
    generated = paths.GENERATED_TEXTURE_DIR / name
    if all(_find_map(generated, m) for m in MAP_NAMES) and _generated_current(generated):
        return generated, "generated"
    return None, "missing"


def _generated_current(folder: Path) -> bool:
    from render.texture_gen import GENERATOR_VERSION
    try:
        meta = json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        return False
    return meta.get("version") == GENERATOR_VERSION


def write_png(arr: np.ndarray, path: Path) -> None:
    """Write an 8-bit array (rows bottom-up, Panda RAM order) as PNG."""
    h, w = arr.shape[:2]
    c = 1 if arr.ndim == 2 else arr.shape[2]
    fmt = {1: Texture.F_luminance, 3: Texture.F_rgb, 4: Texture.F_rgba}[c]
    tex = Texture(path.stem)
    tex.setup2dTexture(w, h, Texture.T_unsigned_byte, fmt)
    tex.setRamImageAs(np.ascontiguousarray(arr).tobytes(), {1: "G", 3: "RGB", 4: "RGBA"}[c])
    path.parent.mkdir(parents=True, exist_ok=True)
    tex.write(Filename.fromOsSpecific(str(path)))


def generate_material_files(name: str, definition: dict, size: int) -> str:
    """Worker entry point: synthesise one material and write its PNGs."""
    from render import texture_gen
    proc = dict(definition.get("procedural", {"type": "flat"}))
    kind = proc.pop("type", "flat")
    size = int(definition.get("size", size))
    seed = sum((i + 1) * ord(ch) for i, ch in enumerate(name)) * 7919  # deterministic per name
    maps = texture_gen.generate(kind, proc, size, seed)
    folder = paths.GENERATED_TEXTURE_DIR / name
    for m in MAP_NAMES:
        write_png(maps[m], folder / f"{m}.png")
    with open(folder / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"source": "procedural", "type": kind, "size": size,
                   "version": texture_gen.GENERATOR_VERSION}, f)
    return name


def ensure_textures(names, definitions: dict, size: int = DEFAULT_PROCEDURAL_SIZE,
                    workers: int | None = None, log=print) -> None:
    """Generate any missing procedural texture sets (in parallel)."""
    missing = [n for n in names if material_folder(n)[1] == "missing"]
    if not missing:
        return
    t0 = time.time()
    log(f"[materials] generating {len(missing)} procedural texture sets at {size}px "
        f"(one-time, cached in {paths.GENERATED_TEXTURE_DIR})...")
    paths.ensure_dirs()
    workers = workers or max(1, min(len(missing), (os.cpu_count() or 2)))
    if workers > 1 and len(missing) > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(generate_material_files, n, definitions[n], size) for n in missing]
                for fut in futures:
                    log(f"[materials]   {fut.result()}")
        except (OSError, RuntimeError) as exc:  # e.g. restricted environments
            log(f"[materials] parallel generation failed ({exc}); falling back to serial")
            for n in missing:
                if material_folder(n)[1] == "missing":
                    generate_material_files(n, definitions[n], size)
    else:
        for n in missing:
            generate_material_files(n, definitions[n], size)
    log(f"[materials] done in {time.time() - t0:.1f}s")


@dataclass
class Material:
    name: str
    surface: str
    uv_scale: float
    albedo: Texture
    normal: Texture
    orm: Texture
    params: Vec4          # x = parallax scale, y = normal strength, z = roughness mul, w = metal mul
    source: str

    def apply(self, np_: NodePath) -> None:
        np_.setShaderInput("u_albedo", self.albedo)
        np_.setShaderInput("u_normalMap", self.normal)
        np_.setShaderInput("u_orm", self.orm)
        np_.setShaderInput("u_matParams", self.params)


class MaterialLibrary:
    def __init__(self, loader, graphics: dict, procedural_size: int = DEFAULT_PROCEDURAL_SIZE):
        self.loader = loader
        self.graphics = graphics
        self.definitions = load_definitions()
        self.procedural_size = procedural_size
        self._cache: dict[str, Material] = {}

    def preload(self, names) -> None:
        names = [n for n in names if n in self.definitions]
        ensure_textures(names, self.definitions, self.procedural_size)
        for n in names:
            self.get(n)

    def _load_tex(self, path: Path, srgb: bool) -> Texture:
        tex = self.loader.loadTexture(Filename.fromOsSpecific(str(path)))
        if srgb:
            tex.setFormat(Texture.F_srgb_alpha if tex.getNumComponents() == 4 else Texture.F_srgb)
        tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
        tex.setMagfilter(SamplerState.FT_linear)
        tex.setWrapU(SamplerState.WM_repeat)
        tex.setWrapV(SamplerState.WM_repeat)
        tex.setAnisotropicDegree(int(self.graphics.get("anisotropy", 8)))
        return tex

    def get(self, name: str) -> Material:
        mat = self._cache.get(name)
        if mat is not None:
            return mat
        if name not in self.definitions:
            print(f"[materials] unknown material {name!r}, using concrete_wall")
            return self.get("concrete_wall")
        d = self.definitions[name]
        folder, source = material_folder(name)
        if folder is None:
            ensure_textures([name], self.definitions, self.procedural_size, workers=1)
            folder, source = material_folder(name)
        uv_scale = float(d.get("uv_scale", 2.0))
        meta_file = folder / "meta.json"
        if source == "downloaded" and meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                # Real-world size reported by the texture source wins (metres).
                if meta.get("world_size_m"):
                    uv_scale = float(meta["world_size_m"])
            except (OSError, ValueError):
                pass
        # the PARALLAX shader define decides whether this is used, so the
        # option can be toggled at runtime
        parallax = float(d.get("parallax", 0.0))
        mat = Material(
            name=name,
            surface=d.get("surface", "default"),
            uv_scale=uv_scale,
            albedo=self._load_tex(_find_map(folder, "albedo"), srgb=True),
            normal=self._load_tex(_find_map(folder, "normal"), srgb=False),
            orm=self._load_tex(_find_map(folder, "orm"), srgb=False),
            params=Vec4(parallax, float(d.get("normal_strength", 1.0)),
                        float(d.get("roughness_scale", 1.0)), float(d.get("metallic_scale", 1.0))),
            source=source,
        )
        self._cache[name] = mat
        return mat

    def set_anisotropy(self, degree: int) -> None:
        self.graphics = dict(self.graphics, anisotropy=degree)
        for m in self._cache.values():
            for tex in (m.albedo, m.normal, m.orm):
                tex.setAnisotropicDegree(int(degree))

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for m in self._cache.values():
            counts[m.source] = counts.get(m.source, 0) + 1
        return ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
