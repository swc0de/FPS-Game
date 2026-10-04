"""Central place for filesystem locations used by the game.

Everything is resolved relative to the project root so the game can be
started from any working directory.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
MAPS_DIR = ROOT / "maps" / "data"
SHADER_DIR = ROOT / "render" / "shaders"

ASSETS_DIR = ROOT / "assets"
TEXTURE_DIR = ASSETS_DIR / "textures"          # downloaded CC0 textures
GENERATED_TEXTURE_DIR = TEXTURE_DIR / "_generated"  # procedural fallback cache
HDRI_DIR = ASSETS_DIR / "hdri"
CACHE_DIR = ASSETS_DIR / "cache"               # IBL / bake caches

USER_DIR = ROOT / "user"                        # per-user settings, screenshots
SETTINGS_FILE = USER_DIR / "settings.json"
SCREENSHOT_DIR = USER_DIR / "screenshots"


def ensure_dirs() -> None:
    for d in (TEXTURE_DIR, GENERATED_TEXTURE_DIR, HDRI_DIR, CACHE_DIR, USER_DIR, SCREENSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
