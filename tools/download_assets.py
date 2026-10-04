#!/usr/bin/env python3
"""Download CC0 PBR textures and HDRIs (Poly Haven, ambientCG).

Every material in data/materials.json lists candidate asset ids per source.
For each material the first candidate that downloads successfully is
converted into the game's texture layout:

    assets/textures/<material>/albedo.jpg   sRGB base colour
    assets/textures/<material>/normal.png   RGB = OpenGL normal, A = height
    assets/textures/<material>/orm.png      R = AO, G = roughness, B = metallic
    assets/textures/<material>/meta.json    source, asset id, licence, real-world size

HDRIs named by the maps (environment.hdri / environment.hdri_candidates)
are saved to assets/hdri/<id>.hdr. Everything is CC0 (public domain).
If you are offline nothing breaks: the game generates procedural textures
and a procedural sky instead.

    python tools/download_assets.py               # 2K textures + HDRIs
    python tools/download_assets.py --res 4k      # higher resolution
    python tools/download_assets.py --only hdri   # just the skies
    python tools/download_assets.py --list        # show what would be fetched
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import paths  # noqa: E402

USER_AGENT = "ColdSector-AssetFetcher/1.0 (open-source game; CC0 assets)"
POLYHAVEN_API = "https://api.polyhaven.com"
AMBIENTCG_GET = "https://ambientcg.com/get?file={id}_{res}-JPG.zip"
TIMEOUT = 60

# Poly Haven map key aliases (the API uses slightly different names per asset)
PH_KEYS = {
    "albedo": ["Diffuse", "diffuse", "diff", "Color", "albedo"],
    "normal": ["nor_gl", "Normal", "normal_gl", "nor"],
    "rough": ["Rough", "rough", "Roughness", "roughness"],
    "ao": ["AO", "ao", "AmbientOcclusion"],
    "metal": ["Metal", "metal", "Metalness", "metallic"],
    "arm": ["arm", "ARM"],
    "height": ["Displacement", "disp", "displacement", "height"],
}


# ---------------------------------------------------------------- http
def http_get(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
        time.sleep(1.5 * (2 ** attempt))
    raise last


def http_json(url: str):
    return json.loads(http_get(url).decode("utf-8"))


# ------------------------------------------------------- Poly Haven API
def ph_pick(files: dict, kind: str, res: str, fmts=("png", "jpg")):
    """Return (url, format) for a map kind from a /files/<id> response."""
    for key in PH_KEYS[kind]:
        entry = files.get(key)
        if not isinstance(entry, dict):
            continue
        for r in (res, "2k", "1k", "4k"):
            by_res = entry.get(r)
            if not isinstance(by_res, dict):
                continue
            for fmt in fmts:
                f = by_res.get(fmt)
                if isinstance(f, dict) and f.get("url"):
                    return f["url"], fmt
    return None, None


def ph_world_size(info: dict) -> float | None:
    dims = info.get("dimensions")
    if isinstance(dims, (list, tuple)) and dims:
        try:
            return max(float(d) for d in dims) / 1000.0  # millimetres -> metres
        except (TypeError, ValueError):
            return None
    return None


def ph_hdri_url(files: dict, res: str):
    h = files.get("hdri", {})
    for r in (res, "2k", "4k", "1k"):
        f = h.get(r, {}).get("hdr")
        if isinstance(f, dict) and f.get("url"):
            return f["url"]
    return None


# ---------------------------------------------------------- image utils
def _load_rgba(data: bytes, suffix: str):
    """Decode image bytes with Panda3D -> float32 numpy (H, W, C) in [0,1], rows bottom-up."""
    import numpy as np
    from panda3d.core import Filename, Texture
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        name = tmp.name
    try:
        tex = Texture()
        if not tex.read(Filename.fromOsSpecific(name)):
            raise IOError("decode failed")
        w, h, c = tex.getXSize(), tex.getYSize(), tex.getNumComponents()
        fmt = {1: "G", 2: "GA", 3: "RGB", 4: "RGBA"}[c]
        raw = bytes(tex.getRamImageAs(fmt))
        if tex.getComponentType() == Texture.T_unsigned_short:
            arr = np.frombuffer(raw, np.uint16).astype(np.float32) / 65535.0
        elif tex.getComponentType() == Texture.T_float:
            arr = np.frombuffer(raw, np.float32)
        else:
            arr = np.frombuffer(raw, np.uint8).astype(np.float32) / 255.0
        return arr.reshape(h, w, c)
    finally:
        os.unlink(name)


def _resize(arr, w: int, h: int):
    """Nearest-ish box resample so separate maps match the albedo size."""
    import numpy as np
    if arr.shape[0] == h and arr.shape[1] == w:
        return arr
    ys = (np.arange(h) * arr.shape[0] / h).astype(int)
    xs = (np.arange(w) * arr.shape[1] / w).astype(int)
    return arr[ys][:, xs]


def _channel(arr, idx=0):
    return arr[..., idx] if arr.ndim == 3 else arr


def write_material(folder: Path, maps: dict[str, tuple[bytes, str]], meta: dict) -> None:
    """maps: kind -> (bytes, suffix). Produces albedo/normal/orm in the game layout."""
    import numpy as np
    from render.materials import write_png
    folder.mkdir(parents=True, exist_ok=True)
    albedo_bytes, albedo_suffix = maps["albedo"]
    albedo = _load_rgba(albedo_bytes, albedo_suffix)
    h, w = albedo.shape[:2]
    if albedo_suffix in (".jpg", ".jpeg"):
        (folder / "albedo.jpg").write_bytes(albedo_bytes)
    else:
        write_png((np.clip(albedo[..., :3], 0, 1) * 255 + 0.5).astype(np.uint8), folder / "albedo.png")

    if "normal" in maps:
        nrm = _resize(_load_rgba(*maps["normal"]), w, h)[..., :3]
    else:
        nrm = np.zeros((h, w, 3), np.float32)
        nrm[...] = (0.5, 0.5, 1.0)
    if "height" in maps:
        height = _channel(_resize(_load_rgba(*maps["height"]), w, h))
        lo, hi = float(height.min()), float(height.max())
        height = (height - lo) / max(hi - lo, 1e-6)
    else:
        height = np.full((h, w), 0.5, np.float32)
    normal_rgba = np.concatenate([nrm, height[..., None]], -1)
    write_png((np.clip(normal_rgba, 0, 1) * 255 + 0.5).astype(np.uint8), folder / "normal.png")

    if "arm" in maps:
        orm = _resize(_load_rgba(*maps["arm"]), w, h)[..., :3]
    else:
        ao = _channel(_resize(_load_rgba(*maps["ao"]), w, h)) if "ao" in maps else np.ones((h, w), np.float32)
        rough = _channel(_resize(_load_rgba(*maps["rough"]), w, h)) if "rough" in maps else np.full((h, w), 0.8, np.float32)
        metal = _channel(_resize(_load_rgba(*maps["metal"]), w, h)) if "metal" in maps else np.zeros((h, w), np.float32)
        orm = np.stack([ao, rough, metal], -1)
    write_png((np.clip(orm, 0, 1) * 255 + 0.5).astype(np.uint8), folder / "orm.png")
    (folder / "meta.json").write_text(json.dumps(meta, indent=2))


# ------------------------------------------------------------- sources
def fetch_polyhaven_material(asset_id: str, res: str) -> tuple[dict, dict]:
    files = http_json(f"{POLYHAVEN_API}/files/{asset_id}")
    maps = {}
    for kind in ("albedo", "normal", "arm", "rough", "ao", "metal", "height"):
        url, fmt = ph_pick(files, kind, res, ("jpg", "png") if kind == "albedo" else ("png", "jpg"))
        if url:
            maps[kind] = (http_get(url), "." + fmt)
    if "albedo" not in maps:
        raise IOError("no diffuse map")
    meta = {"source": "polyhaven", "id": asset_id, "license": "CC0",
            "url": f"https://polyhaven.com/a/{asset_id}", "resolution": res}
    try:
        info = http_json(f"{POLYHAVEN_API}/info/{asset_id}")
        size = ph_world_size(info)
        if size:
            meta["world_size_m"] = size
        if info.get("authors"):
            meta["authors"] = list(info["authors"].keys())
    except Exception:  # metadata is optional
        pass
    return maps, meta


def fetch_ambientcg_material(asset_id: str, res: str) -> tuple[dict, dict]:
    res_tag = res.upper()
    data = http_get(AMBIENTCG_GET.format(id=asset_id, res=res_tag))
    zf = zipfile.ZipFile(io.BytesIO(data))
    maps = {}
    for name in zf.namelist():
        low = name.lower()
        suffix = Path(name).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png"):
            continue
        if low.endswith(f"_color{suffix}"):
            maps["albedo"] = (zf.read(name), suffix)
        elif low.endswith(f"_normalgl{suffix}"):
            maps["normal"] = (zf.read(name), suffix)
        elif low.endswith(f"_roughness{suffix}"):
            maps["rough"] = (zf.read(name), suffix)
        elif low.endswith(f"_ambientocclusion{suffix}"):
            maps["ao"] = (zf.read(name), suffix)
        elif low.endswith(f"_metalness{suffix}"):
            maps["metal"] = (zf.read(name), suffix)
        elif low.endswith(f"_displacement{suffix}"):
            maps["height"] = (zf.read(name), suffix)
    if "albedo" not in maps:
        raise IOError("zip has no _Color map")
    meta = {"source": "ambientcg", "id": asset_id, "license": "CC0",
            "url": f"https://ambientcg.com/view?id={asset_id}", "resolution": res}
    return maps, meta


def download_material(name: str, definition: dict, res: str, force: bool, log=print) -> str | None:
    folder = paths.TEXTURE_DIR / name
    if not force and (folder / "meta.json").exists() and (folder / "orm.png").exists():
        log(f"  {name:18s} already present")
        return "present"
    sources = definition.get("sources", {})
    attempts = [("polyhaven", i) for i in sources.get("polyhaven", [])] + \
               [("ambientcg", i) for i in sources.get("ambientcg", [])]
    for source, asset_id in attempts:
        try:
            if source == "polyhaven":
                maps, meta = fetch_polyhaven_material(asset_id, res)
            else:
                maps, meta = fetch_ambientcg_material(asset_id, res)
            tmp = folder.with_name(folder.name + ".partial")
            shutil.rmtree(tmp, ignore_errors=True)
            write_material(tmp, maps, meta)
            shutil.rmtree(folder, ignore_errors=True)
            tmp.rename(folder)
            log(f"  {name:18s} <- {source}:{asset_id}")
            return f"{source}:{asset_id}"
        except Exception as exc:  # try the next candidate
            log(f"  {name:18s}    {source}:{asset_id} failed ({exc.__class__.__name__}: {exc})")
    log(f"  {name:18s} no source available - procedural fallback will be used")
    return None


def map_hdri_candidates() -> list[str]:
    ids: list[str] = []
    for p in sorted(paths.MAPS_DIR.glob("*.json")):
        try:
            env = json.loads(p.read_text()).get("environment", {})
        except (OSError, ValueError):
            continue
        for hid in [env.get("hdri")] + list(env.get("hdri_candidates", [])):
            if hid and hid not in ids:
                ids.append(hid)
    return ids


def download_hdris(res: str, force: bool, log=print) -> None:
    paths.HDRI_DIR.mkdir(parents=True, exist_ok=True)
    for hid in map_hdri_candidates():
        out = paths.HDRI_DIR / f"{hid}.hdr"
        if out.exists() and not force:
            log(f"  {hid} already present")
            continue
        try:
            files = http_json(f"{POLYHAVEN_API}/files/{hid}")
            url = ph_hdri_url(files, res)
            if not url:
                raise IOError("no .hdr file listed")
            data = http_get(url)
            out.write_bytes(data)
            log(f"  {hid} <- polyhaven ({len(data) / 1e6:.1f} MB)")
        except Exception as exc:
            log(f"  {hid} failed ({exc.__class__.__name__}: {exc})")


def write_credits() -> None:
    lines = ["# Third-party assets", "", "All downloaded assets are CC0 (public domain).", ""]
    for folder in sorted(paths.TEXTURE_DIR.iterdir()) if paths.TEXTURE_DIR.exists() else []:
        meta = folder / "meta.json"
        if folder.name.startswith("_") or not meta.exists():
            continue
        m = json.loads(meta.read_text())
        if m.get("source") in ("polyhaven", "ambientcg"):
            authors = ", ".join(m.get("authors", [])) or "see source"
            lines.append(f"- `{folder.name}`: {m['source']} `{m['id']}` ({m.get('url', '')}) - {authors} - CC0")
    for hdr in sorted(paths.HDRI_DIR.glob("*.hdr")) if paths.HDRI_DIR.exists() else []:
        lines.append(f"- HDRI `{hdr.stem}`: Poly Haven (https://polyhaven.com/a/{hdr.stem}) - CC0")
    (paths.ASSETS_DIR / "CREDITS.md").write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", default="2k", choices=["1k", "2k", "4k"])
    ap.add_argument("--hdri-res", default="2k", choices=["1k", "2k", "4k"])
    ap.add_argument("--only", choices=["materials", "hdri"])
    ap.add_argument("--material", action="append", help="only this material (repeatable)")
    ap.add_argument("--force", action="store_true", help="re-download existing assets")
    ap.add_argument("--list", action="store_true", help="list candidates and exit")
    args = ap.parse_args(argv)

    from render.materials import load_definitions
    defs = load_definitions()
    if args.list:
        for name, d in defs.items():
            s = d.get("sources", {})
            print(f"{name:18s} polyhaven={s.get('polyhaven', [])} ambientcg={s.get('ambientcg', [])}")
        print("HDRIs:", map_hdri_candidates())
        return 0
    paths.ensure_dirs()
    if args.only in (None, "materials"):
        print(f"Downloading {args.res} PBR materials...")
        ok = 0
        for name, d in defs.items():
            if args.material and name not in args.material:
                continue
            if not d.get("sources"):
                continue
            if download_material(name, d, args.res, args.force):
                ok += 1
        print(f"{ok} materials available.")
    if args.only in (None, "hdri"):
        print("Downloading HDRIs...")
        download_hdris(args.hdri_res, args.force)
    write_credits()
    print("Done. Cached lighting data in assets/cache is rebuilt automatically on next start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
