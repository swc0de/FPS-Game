"""Audio playback: positional 3D sounds (OpenAL via Panda3D) + 2D UI sounds.

Sounds are pooled (several instances per name) so overlapping shots don't
cut each other off. Positional sounds use an inverse-distance rolloff with a
per-category reference distance: gunshots stay audible across the map,
footsteps fade within ~25 m (bots will use the same numbers to "hear").
"""
from __future__ import annotations

import random
import time

from direct.showbase.Audio3DManager import Audio3DManager
from panda3d.core import Filename, Point3

from audio import synth
from engine import paths

POOL = 5
CATEGORY_DISTANCE = {"shot": 9.0, "impact": 2.0, "step": 1.5, "explosion": 14.0, "misc": 2.5}


class AudioSystem:
    def __init__(self, game, settings: dict, weapons: dict, log=print):
        self.game = game
        self.settings = settings
        self.enabled = False
        self.pools3d: dict[str, list] = {}
        self.pools2d: dict[str, list] = {}
        self._rr: dict[str, int] = {}
        t0 = time.time()
        self.lib = synth.build_library(paths.CACHE_DIR / "sounds", weapons)
        mgrs = getattr(game, "sfxManagerList", None) or []
        mgr = mgrs[0] if mgrs else None
        if mgr is not None and mgr.isValid():
            self.enabled = True
            self.mgr = mgr
            self.a3d = Audio3DManager(mgr, game.camera)
            self.a3d.setDistanceFactor(1.0)
            self.a3d.setDropOffFactor(0.6)
            self.set_volume(settings.get("master", 0.8))
        log(f"[audio] {sum(len(v) for v in self.lib.values())} synthesized sounds "
            f"({'OpenAL' if self.enabled else 'no audio device - muted'}) in {time.time() - t0:.1f}s")

    def set_volume(self, master: float) -> None:
        if self.enabled:
            self.mgr.setVolume(max(0.0, min(master, 1.0)))

    def _variant(self, name: str) -> Filename | None:
        variants = self.lib.get(name)
        if not variants:
            return None
        return Filename.fromOsSpecific(str(random.choice(variants)))

    def _pooled(self, pools: dict, name: str, make):
        lst = pools.get(name)
        if lst is None:
            lst = pools[name] = []
        if len(lst) < POOL:
            snd = make()
            if snd is None:
                return None
            lst.append(snd)
            return snd
        i = self._rr.get(name, 0)
        self._rr[name] = (i + 1) % POOL
        return lst[i]

    def play_at(self, name: str, pos, volume: float = 1.0, category: str = "misc") -> None:
        if not self.enabled or name not in self.lib:
            return
        fn = self._variant(name)
        key = f"{name}:{fn.getBasename()}"

        def make():
            s = self.a3d.loadSfx(fn)
            s.set3dMinDistance(CATEGORY_DISTANCE.get(category, 2.5))
            return s
        snd = self._pooled(self.pools3d, key, make)
        if snd is None:
            return
        p = Point3(*pos)
        snd.set3dAttributes(p.x, p.y, p.z, 0, 0, 0)
        snd.setVolume(volume * self.settings.get("effects", 1.0))
        snd.setPlayRate(random.uniform(0.95, 1.05))
        snd.play()

    def play_ui(self, name: str, volume: float = 0.7) -> None:
        if not self.enabled or name not in self.lib:
            return
        fn = self._variant(name)
        key = fn.getBasename()
        snd = self._pooled(self.pools2d, key, lambda: self.game.loader.loadSfx(fn))
        if snd is None:
            return
        snd.setVolume(volume * self.settings.get("effects", 1.0))
        snd.play()

    def play_shot(self, profile: str, pos, own: bool = False) -> None:
        name = f"shot_{profile}"
        if own:
            self.play_ui(name, 0.85)
        else:
            self.play_at(name, pos, 1.0, "shot")

    def footstep(self, surface: str, pos, loudness: float, own: bool = False) -> None:
        if loudness <= 0.01:
            return
        name = f"step_{surface}" if f"step_{surface}" in self.lib else "step_default"
        if own:
            self.play_ui(name, 0.28 * loudness)
        else:
            self.play_at(name, pos, loudness, "step")
