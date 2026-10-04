"""User settings: defaults, graphics presets, persistence and CLI overrides.

Settings are plain nested dicts so they serialise trivially to JSON.  The
effective graphics configuration is ``preset values`` overlaid with any
per-key overrides the user made (``settings["graphics"]``).
"""
from __future__ import annotations

import copy
import json
from typing import Any

from engine import paths

DEFAULT_KEYBINDS = {
    "forward": "w",
    "back": "s",
    "left": "a",
    "right": "d",
    "jump": "space",
    "crouch": "lcontrol",
    "walk": "lshift",
    "lean_left": "q",
    "lean_right": "e",
    "fire": "mouse1",
    "aim": "mouse3",
    "reload": "r",
    "use": "f",
    "inspect": "y",
    "drop": "g",
    "buy_menu": "b",
    "scoreboard": "tab",
    "slot1": "1",
    "slot2": "2",
    "slot3": "3",
    "slot4": "4",
    "slot5": "5",
    "gadget": "x",
    "last_weapon": "z",
    "wheel_up": "wheel_up",
    "wheel_down": "wheel_down",
}

DEFAULTS: dict[str, Any] = {
    "video": {
        "resolution": [1280, 720],
        "fullscreen": False,
        "vsync": True,
        "fov": 74.0,            # vertical field of view in degrees (~106 horizontal at 16:9)
        "viewmodel_fov": 54.0,  # vertical FOV of the weapon viewmodel camera
        "preset": "high",
        "max_fps": 0,           # 0 = unlimited (vsync still applies)
        "show_fps": True,
    },
    "graphics": {},             # per-key overrides of the active preset
    "input": {
        "sensitivity": 2.0,     # same scale as CS: degrees = mouse counts * 0.022 * sensitivity
        "invert_y": False,
        "head_bob": True,
        "binds": dict(DEFAULT_KEYBINDS),
    },
    "audio": {
        "master": 0.8,
        "effects": 1.0,
        "ambient": 0.6,
        "ui": 0.8,
    },
    "gameplay": {
        "crosshair": {"style": "classic", "size": 6, "gap": 3, "thickness": 2,
                      "color": [0.3, 1.0, 0.4, 1.0], "dynamic": True, "dot": False},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_presets() -> dict[str, dict]:
    with open(paths.DATA_DIR / "graphics_presets.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


class Settings:
    """Holds the merged settings tree and computes effective graphics options."""

    def __init__(self, data: dict | None = None):
        self.data = _deep_merge(DEFAULTS, data or {})
        self.presets = load_presets()

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls) -> "Settings":
        data = {}
        if paths.SETTINGS_FILE.exists():
            try:
                with open(paths.SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[settings] could not read {paths.SETTINGS_FILE}: {exc}; using defaults")
        return cls(data)

    def save(self) -> None:
        paths.ensure_dirs()
        with open(paths.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    # ------------------------------------------------------------- access
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def video(self) -> dict:
        return self.data["video"]

    @property
    def input(self) -> dict:
        return self.data["input"]

    @property
    def audio(self) -> dict:
        return self.data["audio"]

    @property
    def graphics(self) -> dict:
        """Effective graphics options: preset overlaid with user overrides."""
        preset_name = self.video.get("preset", "high")
        preset = self.presets.get(preset_name, self.presets["high"])
        return _deep_merge(preset, self.data.get("graphics", {}))

    def set_preset(self, name: str, clear_overrides: bool = True) -> None:
        if name not in self.presets:
            raise KeyError(f"unknown preset {name!r}; choose from {list(self.presets)}")
        self.video["preset"] = name
        if clear_overrides:
            self.data["graphics"] = {}

    def set_graphics_option(self, key: str, value: Any) -> None:
        self.data.setdefault("graphics", {})[key] = value

    def apply_cli(self, args) -> None:
        """Apply argparse overrides (not persisted unless save() is called)."""
        if getattr(args, "preset", None):
            self.set_preset(args.preset, clear_overrides=False)
        if getattr(args, "res", None):
            w, h = (int(v) for v in args.res.lower().split("x"))
            self.video["resolution"] = [w, h]
        if getattr(args, "fullscreen", False):
            self.video["fullscreen"] = True
        if getattr(args, "windowed", False):
            self.video["fullscreen"] = False
        if getattr(args, "fov", None):
            self.video["fov"] = float(args.fov)
        if getattr(args, "no_vsync", False):
            self.video["vsync"] = False
        for item in getattr(args, "gfx", None) or []:
            key, _, raw = item.partition("=")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            self.set_graphics_option(key.strip(), value)
