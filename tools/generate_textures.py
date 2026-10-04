#!/usr/bin/env python3
"""Pre-generate the procedural fallback textures (normally done on first start).

    python tools/generate_textures.py              # 1024 px, all materials
    python tools/generate_textures.py --res 2048   # higher resolution
    python tools/generate_textures.py --force      # rebuild even if cached
    python tools/generate_textures.py --preview    # also write a contact sheet PNG
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import paths  # noqa: E402
from render import materials  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=int, default=1024)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args(argv)
    defs = materials.load_definitions()
    if args.force:
        shutil.rmtree(paths.GENERATED_TEXTURE_DIR, ignore_errors=True)
    materials.ensure_textures(list(defs), defs, args.res)
    if args.preview:
        import numpy as np
        from panda3d.core import Filename, Texture
        tiles = []
        for name in defs:
            folder = paths.GENERATED_TEXTURE_DIR / name
            tex = Texture()
            tex.read(Filename.fromOsSpecific(str(folder / "albedo.png")))
            arr = np.frombuffer(bytes(tex.getRamImageAs("RGB")), np.uint8).reshape(tex.getYSize(), tex.getXSize(), 3)
            ys = (np.arange(128) * arr.shape[0] / 128).astype(int)
            xs = (np.arange(128) * arr.shape[1] / 128).astype(int)
            tiles.append(arr[ys][:, xs])
        cols = 8
        while len(tiles) % cols:
            tiles.append(np.zeros_like(tiles[0]))
        rows = [np.concatenate(tiles[i:i + cols], 1) for i in range(0, len(tiles), cols)]
        sheet = np.concatenate(rows[::-1], 0)
        out = paths.USER_DIR / "texture_preview.png"
        materials.write_png(sheet, out)
        print(f"preview -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
