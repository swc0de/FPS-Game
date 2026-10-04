# assets/

Nothing in here is committed except this file - everything is downloaded or
generated on demand.

| Folder | Contents | Created by |
|---|---|---|
| `textures/<material>/` | `albedo.jpg/png`, `normal.png` (RGB normal + height in A), `orm.png` (AO / roughness / metallic), `meta.json` | `python tools/download_assets.py` (CC0, Poly Haven / ambientCG) |
| `textures/_generated/<material>/` | same layout, procedurally synthesised | the game on first start (or `python tools/generate_textures.py`) |
| `hdri/<id>.hdr` | equirectangular HDR skies | `python tools/download_assets.py` (CC0, Poly Haven) |
| `cache/` | prefiltered IBL environments, baked sky-visibility volumes | the game (safe to delete; rebuilt automatically) |
| `CREDITS.md` | list of downloaded CC0 assets and their sources | `tools/download_assets.py` |

Downloaded textures always take priority over procedural ones. Delete a
material's folder to fall back to the procedural version.
