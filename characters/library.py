"""Character meshes on demand: build, cache on disk, share between bodies.

``meshes(name, seed, style)`` returns the LOD meshes of a bot's appearance
in one team's kit. They are built once (a few seconds, characters/build.py),
written to ``assets/cache/characters/<key>.npz`` and read back on later
starts; bodies of the same bot on the same side share one copy.
``prebuild`` builds a whole roster in parallel worker processes at match
start, the same way procedural textures are generated
(render/materials.py), so the first round does not stall on meshing.

Everything here is generated from code: no downloaded asset is needed, so
this is also the offline path (OVERHAUL_PLAN 6).
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from characters import build
from characters.appearance import appearance

_memory: dict[str, list] = {}


def cache_dir() -> Path:
    from engine import paths
    d = Path(paths.CACHE_DIR) / "characters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str, seed: int, style: str) -> Path:
    return cache_dir() / f"{appearance(name, seed).key(style)}.npz"


def _build_file(name: str, seed: int, style: str) -> str:
    """Worker entry point."""
    path = _path(name, seed, style)
    if not path.exists():
        lods = build.assemble(appearance(name, seed), style)
        tmp = path.with_suffix(".tmp.npz")
        build.save(tmp, lods)
        os.replace(tmp, path)
    return name


def meshes(name: str, seed: int, style: str) -> list:
    key = appearance(name, seed).key(style)
    lods = _memory.get(key)
    if lods is None:
        path = _path(name, seed, style)
        if not path.exists():
            _build_file(name, seed, style)
        lods = build.load(path)
        _memory[key] = lods
    return lods


def prebuild(names, seed: int, styles=("vanguard", "bastion"), log=print, workers: int | None = None) -> None:
    jobs = [(n, seed, s) for n in names for s in styles if not _path(n, seed, s).exists()]
    if not jobs:
        return
    t0 = time.time()
    log(f"[characters] building {len(jobs)} soldier meshes (one-time, cached in {cache_dir()})...")
    workers = workers or max(1, min(len(jobs), os.cpu_count() or 2))
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for fut in [pool.submit(_build_file, *j) for j in jobs]:
                fut.result()
    except (OSError, RuntimeError) as exc:            # restricted environments
        log(f"[characters] parallel build failed ({exc}); building one by one")
        for j in jobs:
            _build_file(*j)
    log(f"[characters] done in {time.time() - t0:.1f}s")
