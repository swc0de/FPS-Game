"""Pause menu and settings screen.

ESC opens the pause menu (the game pauses and the mouse is released).
Settings are edited on a copy; *Apply* applies them live (renderer
rebuilds only what changed) and saves user/settings.json. Options marked
with * take effect after a restart.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.DirectGui import DirectFrame
from panda3d.core import TextNode

from ui import widgets as W

# ---------------------------------------------------------------- formatting


def _pct(v):
    return f"{v * 100:.0f}%"


def _onoff(v):
    return "On" if v else "Off"


def _deg(v):
    return f"{v:.0f}°"


ON_OFF = [(False, "Off"), (True, "On")]
SHADOW_LEVELS = {
    "low": {"shadow_cascades": 2, "shadow_resolution": 1024, "shadow_distance": 45.0, "shadow_pcf_taps": 4},
    "medium": {"shadow_cascades": 3, "shadow_resolution": 1536, "shadow_distance": 65.0, "shadow_pcf_taps": 8},
    "high": {"shadow_cascades": 4, "shadow_resolution": 2048, "shadow_distance": 90.0, "shadow_pcf_taps": 12},
    "ultra": {"shadow_cascades": 4, "shadow_resolution": 3072, "shadow_distance": 120.0, "shadow_pcf_taps": 24},
}
RESOLUTIONS = [(1280, 720), (1280, 800), (1366, 768), (1440, 900), (1600, 900), (1680, 1050), (1920, 1080),
               (1920, 1200), (2560, 1080), (2560, 1440), (2560, 1600), (3440, 1440), (3840, 2160)]
CROSSHAIR_COLORS = [((0.3, 1.0, 0.4, 1.0), "Green"), ((0.3, 0.95, 1.0, 1.0), "Cyan"), ((1.0, 0.95, 0.3, 1.0), "Yellow"),
                    ((1.0, 1.0, 1.0, 1.0), "White"), ((1.0, 0.3, 0.3, 1.0), "Red"), ((1.0, 0.35, 1.0, 1.0), "Magenta")]


@dataclass
class Opt:
    key: str
    label: str
    section: str                       # graphics | video | input | audio | crosshair | special
    choices: list | None = None        # [(value, text)] for cycling options
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.05
    fmt: Callable[[Any], str] = str
    restart: bool = False
    desc: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_slider(self) -> bool:
        return self.choices is None


TABS = {
    "GRAPHICS": [
        Opt("preset", "Quality preset", "special", [("low", "Low"), ("medium", "Medium"), ("high", "High"),
                                                     ("ultra", "Ultra")],
            desc="Sets every option below. Changing any option afterwards marks the preset as custom."),
        Opt("render_scale", "Render scale", "graphics", lo=0.5, hi=1.0, step=0.05, fmt=_pct,
            desc="3D resolution relative to the window. Lower is faster; sharpening restores some detail."),
        Opt("antialiasing", "Anti-aliasing", "graphics",
            [("off", "Off"), ("fxaa", "FXAA"), ("msaa2", "MSAA 2x"), ("msaa4", "MSAA 4x"), ("msaa8", "MSAA 8x"),
             ("msaa2+fxaa", "MSAA 2x + FXAA"), ("msaa4+fxaa", "MSAA 4x + FXAA")],
            desc="MSAA smooths geometry edges (costly at high resolutions); FXAA also smooths shading."),
        Opt("sharpening", "Sharpening", "graphics", lo=0.0, hi=1.0, step=0.05, fmt=_pct,
            desc="Contrast adaptive sharpening (CAS) on the final image."),
        Opt("shadow_quality", "Shadow quality", "special", [(k, k.title()) for k in SHADOW_LEVELS],
            desc="Sun shadow cascades, resolution, distance and softness (PCF taps)."),
        Opt("local_shadows", "Lamp shadows", "graphics", ON_OFF,
            desc="Shadow maps for indoor lamps and floodlights."),
        Opt("ssao", "Ambient occlusion", "graphics",
            [("off", "Off"), ("low", "Low"), ("medium", "Medium"), ("high", "High"), ("ultra", "Ultra")],
            desc="GTAO: soft contact shadows in corners and under objects (ambient light only)."),
        Opt("texture_size", "Texture quality", "graphics",
            [(512, "Low"), (1024, "Medium"), (2048, "High"), (4096, "Ultra")], restart=True,
            desc="Maximum texture resolution. Needs a restart."),
        Opt("anisotropy", "Anisotropic filtering", "graphics", [(1, "Off"), (2, "2x"), (4, "4x"), (8, "8x"),
                                                                 (16, "16x")],
            desc="Keeps floor textures sharp at shallow viewing angles."),
        Opt("parallax", "Parallax mapping", "graphics", ON_OFF, desc="Depth illusion on bricks and rough surfaces."),
        Opt("specular_aa", "Specular anti-aliasing", "graphics", ON_OFF,
            desc="Stops shiny bumpy surfaces from sparkling."),
        Opt("bloom", "Bloom", "graphics", ON_OFF, desc="Glow around lamps, muzzle flashes and explosions."),
        Opt("auto_exposure", "Eye adaptation", "graphics", ON_OFF,
            desc="Exposure adapts when you move between bright yards and dark interiors."),
        Opt("soft_particles", "Soft particles", "graphics", ON_OFF,
            desc="Smoke and dust fade where they touch walls and floors."),
        Opt("fog", "Atmospheric fog", "graphics", ON_OFF, desc="Height fog and sun haze."),
        Opt("vignette", "Vignette", "graphics", ON_OFF, desc="Subtle darkening toward the screen corners."),
        Opt("color_grading", "Colour grading", "graphics", ON_OFF, desc="The map's colour look (tint, contrast)."),
        Opt("chromatic_aberration", "Chromatic aberration", "graphics", ON_OFF,
            desc="Lens colour fringing toward the screen edges."),
        Opt("film_grain", "Film grain", "graphics", ON_OFF, desc="Fine animated noise."),
    ],
    "DISPLAY": [
        Opt("resolution", "Resolution", "special", [], desc="Window or fullscreen resolution."),
        Opt("fullscreen", "Display mode", "video", [(False, "Windowed"), (True, "Fullscreen")]),
        Opt("vsync", "V-Sync", "video", ON_OFF, restart=True, desc="Syncs to the monitor refresh. Needs a restart."),
        Opt("max_fps", "Frame rate limit", "video", [(0, "Unlimited"), (60, "60"), (120, "120"), (144, "144"),
                                                     (165, "165"), (240, "240")]),
        Opt("fov", "Field of view (vertical)", "video", lo=60, hi=90, step=1, fmt=None,
            desc="74 vertical = 106 horizontal at 16:9 (CS-like). The horizontal value follows your aspect ratio."),
        Opt("viewmodel_fov", "Weapon field of view", "video", lo=40, hi=70, step=1, fmt=_deg,
            desc="How large the weapon appears; does not change what you can see."),
        Opt("brightness", "Brightness", "video", lo=-1.0, hi=1.0, step=0.1, fmt=lambda v: f"{v:+.1f} EV",
            desc="Exposure offset on top of the map's lighting."),
        Opt("show_fps", "Debug overlay", "video", ON_OFF, desc="FPS and position readout (F1)."),
    ],
    "GAMEPLAY": [
        Opt("sensitivity", "Mouse sensitivity", "input", lo=0.1, hi=8.0, step=0.05, fmt=lambda v: f"{v:.2f}",
            desc="Same scale as CS (0.022 degrees per count x sensitivity)."),
        Opt("zoom_sensitivity", "Zoom sensitivity", "input", lo=0.5, hi=1.5, step=0.05, fmt=lambda v: f"{v:.2f}",
            desc="Multiplier while aiming down sights or scoped."),
        Opt("invert_y", "Invert mouse", "input", ON_OFF),
        Opt("size", "Crosshair length", "crosshair", lo=2, hi=16, step=1, fmt=lambda v: f"{v:.0f} px"),
        Opt("gap", "Crosshair gap", "crosshair", lo=-2, hi=12, step=1, fmt=lambda v: f"{v:.0f} px"),
        Opt("thickness", "Crosshair thickness", "crosshair", lo=1, hi=5, step=1, fmt=lambda v: f"{v:.0f} px"),
        Opt("color", "Crosshair colour", "crosshair", [(list(c), n) for c, n in CROSSHAIR_COLORS]),
        Opt("dot", "Centre dot", "crosshair", ON_OFF),
        Opt("dynamic", "Dynamic crosshair", "crosshair", ON_OFF,
            desc="The gap follows the real bullet spread (movement, jumping, spraying)."),
    ],
    "AUDIO": [
        Opt("master", "Master volume", "audio", lo=0.0, hi=1.0, step=0.05, fmt=_pct),
        Opt("effects", "Effects volume", "audio", lo=0.0, hi=1.0, step=0.05, fmt=_pct),
        Opt("ui", "Interface volume", "audio", lo=0.0, hi=1.0, step=0.05, fmt=_pct),
    ],
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class SettingsModel:
    """Reads/writes options on a pending copy of the settings tree."""

    def __init__(self, settings, display_size=None):
        self.settings = settings
        self.presets = settings.presets
        self.pending = copy.deepcopy(settings.data)
        self.display_size = display_size

    # ----------------------------------------------------------- helpers
    def preset_values(self) -> dict:
        name = self.pending["video"].get("preset", "high")
        return self.presets.get(name, self.presets["high"])

    def graphics(self) -> dict:
        return _deep_merge(self.preset_values(), self.pending.get("graphics", {}))

    def dirty(self) -> bool:
        return self.pending != self.settings.data

    def resolutions(self) -> list:
        cur = tuple(self.pending["video"]["resolution"])
        res = [r for r in RESOLUTIONS if self.display_size is None
               or (r[0] <= self.display_size[0] and r[1] <= self.display_size[1])]
        if cur not in res:
            res.append(cur)
        return sorted(res)

    # -------------------------------------------------------------- get
    def get(self, o: Opt):
        p = self.pending
        if o.section == "graphics":
            return self.graphics().get(o.key)
        if o.section == "video":
            default = {"brightness": 0.0, "show_fps": True, "max_fps": 0}.get(o.key)
            return p["video"].get(o.key, default)
        if o.section == "input":
            default = {"zoom_sensitivity": 1.0, "invert_y": False}.get(o.key)
            return p["input"].get(o.key, default)
        if o.section == "audio":
            return p["audio"].get(o.key, 1.0)
        if o.section == "crosshair":
            return p["gameplay"]["crosshair"].get(o.key)
        if o.key == "preset":
            return p["video"].get("preset", "high")
        if o.key == "shadow_quality":
            g = self.graphics()
            for name, vals in SHADOW_LEVELS.items():
                if all(g.get(k) == v for k, v in vals.items()):
                    return name
            return "custom"
        if o.key == "resolution":
            return tuple(p["video"]["resolution"])
        return None

    def text(self, o: Opt) -> str:
        v = self.get(o)
        if o.key == "preset":
            name = dict(o.choices).get(v, str(v))
            return name + (" (custom)" if self.pending.get("graphics") else "")
        if o.key == "resolution":
            return f"{v[0]} x {v[1]}"
        if o.key == "fov":
            w, h = self.pending["video"]["resolution"]
            return f"{v:.0f}° ({horizontal_fov(v, w / h):.0f}° h)"
        if o.choices is not None:
            for value, label in o.choices:
                if value == v:
                    return label
            return "Custom" if o.key == "shadow_quality" else str(v)
        return o.fmt(v) if o.fmt else str(v)

    def choices(self, o: Opt) -> list:
        if o.key == "resolution":
            return [(r, f"{r[0]} x {r[1]}") for r in self.resolutions()]
        return o.choices or []

    # -------------------------------------------------------------- set
    def set(self, o: Opt, value) -> None:
        p = self.pending
        if o.section == "graphics":
            self._set_graphics(o.key, value)
        elif o.section == "video":
            p["video"][o.key] = value
        elif o.section == "input":
            p["input"][o.key] = value
        elif o.section == "audio":
            p["audio"][o.key] = value
        elif o.section == "crosshair":
            p["gameplay"]["crosshair"][o.key] = value
        elif o.key == "preset":
            p["video"]["preset"] = value
            p["graphics"] = {}
        elif o.key == "shadow_quality" and value in SHADOW_LEVELS:
            for k, v in SHADOW_LEVELS[value].items():
                self._set_graphics(k, v)
        elif o.key == "resolution":
            p["video"]["resolution"] = [int(value[0]), int(value[1])]

    def _set_graphics(self, key: str, value) -> None:
        over = self.pending.setdefault("graphics", {})
        if self.preset_values().get(key) == value:
            over.pop(key, None)
        else:
            over[key] = value

    def step(self, o: Opt, direction: int) -> None:
        choices = self.choices(o)
        if not choices:
            return
        values = [c[0] for c in choices]
        cur = self.get(o)
        if cur in values:
            nxt = values[(values.index(cur) + direction) % len(values)]
        else:   # e.g. a custom shadow setup: jump to the first/last level
            nxt = values[0] if direction > 0 else values[-1]
        self.set(o, nxt)

    def restart_pending(self) -> list[str]:
        saved = SettingsModel(self.settings)
        return [o.label for opts in TABS.values() for o in opts if o.restart and self.get(o) != saved.get(o)]


class SettingsMenu:
    def __init__(self, game, on_close):
        self.game = game
        self.on_close = on_close
        self.model: SettingsModel | None = None
        self.tab = "GRAPHICS"
        self.rows: list[tuple[Opt, Any]] = []
        self.root = DirectFrame(parent=game.a2dLeftCenter, frameColor=W.PANEL, frameSize=(0.04, 1.62, -0.95, 0.95),
                                state=DGG.NORMAL, sortOrder=50)
        W.label(self.root, "SETTINGS", (0.1, 0.84), scale=0.06, fg=W.ACCENT)
        self.tab_buttons = {}
        for i, name in enumerate(TABS):
            self.tab_buttons[name] = W.button(self.root, name, (0.28 + i * 0.37, 0.74), self.select_tab,
                                              width=0.35, height=0.07, scale=0.036, extra=(name,))
        self.content = DirectFrame(parent=self.root, frameColor=(0, 0, 0, 0))
        self.desc = W.label(self.root, "", (0.1, -0.76), scale=0.031, fg=W.DIM, text_wordwrap=46)
        self.status = W.label(self.root, "", (0.1, -0.86), scale=0.032, fg=W.WARN)
        self.apply_btn = W.button(self.root, "APPLY", (0.98, -0.86), self.apply, width=0.3, height=0.07)
        self.back_btn = W.button(self.root, "BACK", (1.36, -0.86), self.close, width=0.3, height=0.07)
        self.root.hide()

    @property
    def visible(self) -> bool:
        return not self.root.isHidden()

    def open(self) -> None:
        g = self.game
        size = None
        if g.pipe is not None and g.pipe.getDisplayWidth() > 0:
            size = (g.pipe.getDisplayWidth(), g.pipe.getDisplayHeight())
        self.model = SettingsModel(g.settings, size)
        self.status["text"] = ""
        self.select_tab(self.tab)
        self.root.show()

    def close(self) -> None:
        self.root.hide()
        self.model = None
        self.on_close()

    def select_tab(self, name: str) -> None:
        self.tab = name
        for n, b in self.tab_buttons.items():
            W.set_active(b, n == name)
        for _, w in self.rows:
            w.destroy()
        self.rows = []
        y = 0.6
        for o in TABS[name]:
            hover = (lambda o=o: self._hover(o))
            if o.is_slider:
                w = W.SliderRow(self.content, y, o.label + (" *" if o.restart else ""), o.lo, o.hi, o.step,
                                lambda v, o=o: self._changed(o, v), on_hover=hover)
            else:
                w = W.OptionRow(self.content, y, o.label + (" *" if o.restart else ""),
                                lambda d, o=o: self._stepped(o, d), on_hover=hover)
            self.rows.append((o, w))
            y -= 0.07
        self.refresh()

    def _hover(self, o: Opt) -> None:
        self.desc["text"] = o.desc

    def _stepped(self, o: Opt, direction: int) -> None:
        self.model.step(o, direction)
        self.refresh()
        self.game.audio.play_ui("ui_tick", 0.4)

    def _changed(self, o: Opt, value: float) -> None:
        if isinstance(self.model.get(o), int) and float(value).is_integer():
            value = int(value)
        self.model.set(o, value)
        self.refresh()

    def refresh(self) -> None:
        m = self.model
        if m is None:
            return
        saved = SettingsModel(self.game.settings)
        for o, w in self.rows:
            text = m.text(o)
            changed = m.get(o) != saved.get(o)
            fg = W.ACCENT if changed else W.TEXT
            if o.is_slider:
                w.set(float(m.get(o)), text, label_fg=fg)
            else:
                w.set_text(text, label_fg=fg)
        W.set_active(self.apply_btn, m.dirty())

    def apply(self) -> None:
        if self.model is None or not self.model.dirty():
            return
        restart = self.model.restart_pending()
        self.game.apply_settings(self.model.pending)
        self.model = SettingsModel(self.game.settings, self.model.display_size)
        self.refresh()
        msg = "Settings applied and saved."
        if restart:
            msg += "  Restart needed for: " + ", ".join(restart)
        self.status["text"] = msg

    def destroy(self) -> None:
        self.root.destroy()


class PauseMenu:
    def __init__(self, game):
        self.game = game
        self.backdrop = DirectFrame(parent=game.render2d, frameColor=(0, 0, 0, 0.35), frameSize=(-1, 1, -1, 1),
                                    sortOrder=40)
        self.root = DirectFrame(parent=game.a2dLeftCenter, frameColor=W.PANEL, frameSize=(0.04, 0.84, -0.42, 0.5),
                                sortOrder=45)
        from engine.app import GAME_TITLE
        W.label(self.root, GAME_TITLE, (0.44, 0.38), scale=0.06, fg=W.ACCENT, align=TextNode.ACenter)
        W.label(self.root, "PAUSED", (0.44, 0.3), scale=0.034, fg=W.DIM, align=TextNode.ACenter)
        W.button(self.root, "RESUME", (0.44, 0.16), self.resume, width=0.62)
        W.button(self.root, "SETTINGS", (0.44, 0.06), self.open_settings, width=0.62)
        W.button(self.root, "QUIT TO DESKTOP", (0.44, -0.04), game.userExit, width=0.62)
        W.label(self.root, "ESC  resume / back", (0.44, -0.3), scale=0.03, fg=W.DIM, align=TextNode.ACenter)
        self.settings = SettingsMenu(game, self._settings_closed)
        self.backdrop.hide()
        self.root.hide()

    @property
    def is_open(self) -> bool:
        return not self.backdrop.isHidden()

    def open(self) -> None:
        self.backdrop.show()
        self.root.show()
        self.game.set_paused(True)

    def resume(self) -> None:
        self.settings.root.hide()
        self.root.hide()
        self.backdrop.hide()
        self.game.set_paused(False)

    def open_settings(self) -> None:
        self.root.hide()
        self.settings.open()

    def _settings_closed(self) -> None:
        self.root.show()

    def on_escape(self) -> None:
        if self.settings.visible:
            self.settings.close()
        elif self.is_open:
            self.resume()
        else:
            self.open()


def horizontal_fov(vfov_deg: float, aspect: float) -> float:
    return math.degrees(2 * math.atan(math.tan(math.radians(vfov_deg) / 2) * aspect))
