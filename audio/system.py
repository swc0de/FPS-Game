"""Audio playback: positional 3D sounds (OpenAL via Panda3D), 2D sounds,
ambience and music.

Sounds are pooled (several instances per name) so overlapping shots don't
cut each other off. Positional sounds use an inverse-distance rolloff with a
per-category reference distance: gunshots stay audible across the map,
footsteps fade within ~25 m (bots use the same numbers to "hear").

Milestone 7 additions:

* occlusion - a ray from the listener to the source; a blocked sound plays
  its low-passed "_muffled" variant (synthesised offline, OpenAL in Panda has
  no live filters) and quieter
* distance - gunshots more than FAR_DISTANCE away use the "_far" recording
  (no crack, rolling echo); shots fired under a roof add a room or hall tail
* ambience - looping wind / room tone / tunnel rumble crossfaded by where the
  listener stands (a ray up finds the roof), plus occasional distant
  one-shots (creaks, birds, clanks) outdoors
* music - the main menu loop and short stings (round start, won, lost, bomb
  planted) on the music volume
* volumes per category: effects, ambient, music and ui, under master
"""
from __future__ import annotations

import math
import random
import time

from direct.showbase.Audio3DManager import Audio3DManager
from panda3d.core import AudioSound, Filename, Point3, Vec3

from audio import synth
from engine import paths
from engine.physics import MASK_SIGHT

POOL = 5
CATEGORY_DISTANCE = {"shot": 9.0, "impact": 2.0, "step": 1.5, "explosion": 14.0, "misc": 2.5, "ambient": 8.0}
UI_SOUNDS = {"ui_tick", "buy", "hit", "hit_head", "ping", "camera_switch", "radio", "plant_tap"}
FAR_DISTANCE = 45.0
OCCLUDED_GAIN = 0.6
ROOF_CHECK = 25.0              # metres: anything solid this far above counts as a roof
HALL_HEIGHT = 4.5              # roof higher than this: big room (longer tail)

# ambience mix per listener environment: loop -> gain (before the ambient volume)
AMBIENCE = {
    "outdoor": {"amb_wind": 0.4},
    "indoor": {"amb_wind": 0.1, "amb_room": 0.3},
    "tunnel": {"amb_wind": 0.04, "amb_tunnel": 0.5},
}
LOOPS = ("amb_wind", "amb_room", "amb_tunnel")
DISTANT = ("amb_creak", "amb_birds", "amb_clank")
FADE_RATE = 1.2                # gain units per second for ambience / music fades


def shot_variant(distance: float, occluded: bool) -> str:
    """Which recording of a gunshot to play: '', '_far', '_muffled' or '_far_muffled'."""
    return ("_far" if distance > FAR_DISTANCE else "") + ("_muffled" if occluded else "")


def environment(roof_height: float | None, z: float) -> str:
    """'outdoor', 'indoor' or 'tunnel' from the height of the roof above a point (None = open sky)."""
    if roof_height is None:
        return "outdoor"
    return "tunnel" if z < -0.8 else "indoor"


def approach(value: float, target: float, step: float) -> float:
    if value < target:
        return min(value + step, target)
    return max(value - step, target)


class AudioSystem:
    def __init__(self, game, settings: dict, weapons: dict, log=print):
        self.game = game
        self.settings = settings
        self.enabled = False
        self.pools3d: dict[str, list] = {}
        self.pools2d: dict[str, list] = {}
        self._rr: dict[str, int] = {}
        self.rng = random.Random(7)
        self.env = "outdoor"
        self._env_t = 0.0
        self._distant_t = 8.0
        self.loops: dict[str, AudioSound] = {}
        self.loop_gain = {k: 0.0 for k in LOOPS}
        self.ambient_on = True
        self.menu = False
        self.music = None
        self.music_gain = 0.0
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
            for name in LOOPS:
                fn = self._variant(name)
                if fn is not None:
                    snd = game.loader.loadSfx(fn)
                    snd.setLoop(True)
                    snd.setVolume(0.0)
                    self.loops[name] = snd
        log(f"[audio] {sum(len(v) for v in self.lib.values())} synthesized sounds "
            f"({'OpenAL' if self.enabled else 'no audio device - muted'}) in {time.time() - t0:.1f}s")

    # ------------------------------------------------------------ volumes
    def set_volume(self, master: float) -> None:
        if self.enabled:
            m = max(0.0, min(master, 1.0))
            self.mgr.setVolume(m)
            music = getattr(self.game, "musicManager", None)
            if music is not None and music.isValid():
                music.setVolume(m)

    def volume(self, category: str) -> float:
        key = {"ambient": "ambient", "music": "music", "ui": "ui"}.get(category, "effects")
        return float(self.settings.get(key, 1.0))

    # ------------------------------------------------------------ queries
    def listener_pos(self) -> Point3:
        return self.game.camera.getPos(self.game.render)

    def roof_height(self, pos) -> float | None:
        """Height of the first solid surface above pos, or None under open sky."""
        p = Point3(*pos)
        hit = self.game.physics.ray_cast(p + Vec3(0, 0, 0.2), p + Vec3(0, 0, ROOF_CHECK), MASK_SIGHT)
        return None if hit is None else hit.pos.z - p.z

    def environment_at(self, pos) -> str:
        return environment(self.roof_height(pos), Point3(*pos).z)

    def occluded(self, pos, listener=None) -> bool:
        """Solid geometry between the listener and pos (a sound on a wall's surface is not occluded)."""
        p = Point3(*pos) + Vec3(0, 0, 0.25)
        lp = listener if listener is not None else self.listener_pos()
        hit = self.game.physics.ray_cast(lp, p, MASK_SIGHT)
        return hit is not None and (hit.pos - p).length() > 0.5

    # ------------------------------------------------------------ playback
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

    def play_at(self, name: str, pos, volume: float = 1.0, category: str = "misc", occlude: bool = True) -> None:
        if not self.enabled or name not in self.lib:
            return
        if occlude and self.occluded(pos):
            volume *= OCCLUDED_GAIN
            if f"{name}_muffled" in self.lib:
                name = f"{name}_muffled"
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
        snd.setVolume(volume * self.volume(category))
        snd.setPlayRate(random.uniform(0.95, 1.05))
        snd.play()

    def play_ui(self, name: str, volume: float = 0.7, category: str | None = None) -> None:
        if not self.enabled or name not in self.lib:
            return
        fn = self._variant(name)
        key = fn.getBasename()
        snd = self._pooled(self.pools2d, key, lambda: self.game.loader.loadSfx(fn))
        if snd is None:
            return
        cat = category or ("ui" if name in UI_SOUNDS else "effects")
        snd.setVolume(volume * self.volume(cat))
        snd.play()

    def play_shot(self, profile: str, pos, own: bool = False) -> None:
        name = f"shot_{profile}"
        if own:
            self.play_ui(name, 0.85)
            if self.enabled:
                roof = self.roof_height(self.listener_pos() - Vec3(0, 0, 1.0))
                if roof is not None:
                    self.play_ui("tail_hall" if roof > HALL_HEIGHT else "tail_room", 0.35)
            return
        if not self.enabled:
            return
        p = Point3(*pos)
        lp = self.listener_pos()
        dist = (p - lp).length()
        occl = self.occluded(p, lp)
        self.play_at(name + shot_variant(dist, occl), p, OCCLUDED_GAIN if occl else 1.0, "shot", occlude=False)
        if dist < FAR_DISTANCE:
            roof = self.roof_height(p)
            if roof is not None:
                self.play_at("tail_hall" if roof > HALL_HEIGHT else "tail_room", p,
                             0.4 * (OCCLUDED_GAIN if occl else 1.0), "shot", occlude=False)

    def footstep(self, surface: str, pos, loudness: float, own: bool = False) -> None:
        if loudness <= 0.01:
            return
        name = f"step_{surface}" if f"step_{surface}" in self.lib else "step_default"
        if own:
            self.play_ui(name, 0.28 * loudness)
        else:
            self.play_at(name, pos, loudness, "step")

    # ------------------------------------------------------------ music
    def play_sting(self, kind: str, volume: float = 0.8) -> None:
        self.play_ui(f"sting_{kind}", volume, category="music")

    def set_menu(self, on: bool) -> None:
        """Main menu shown: start its music (faded in) and soften the ambience."""
        self.menu = on
        if on and self.enabled and self.music is None:
            fn = self._variant("music_menu")
            if fn is not None:
                self.music = self.game.loader.loadSfx(fn)
                self.music.setLoop(True)
                self.music.setVolume(0.0)
                self.music.play()
                self.music_gain = 0.0

    # ------------------------------------------------------------ frame
    def frame_update(self, dt: float) -> None:
        if not self.enabled:
            return
        self._env_t -= dt
        if self._env_t <= 0:
            self._env_t = 0.25
            self.env = self.environment_at(self.listener_pos() - Vec3(0, 0, 1.0))
        amb = self.volume("ambient") if self.ambient_on else 0.0
        if self.menu:
            amb *= 0.6
        target = AMBIENCE.get(self.env, {})
        for name, snd in self.loops.items():
            g = approach(self.loop_gain[name], target.get(name, 0.0), FADE_RATE * 0.5 * dt)
            self.loop_gain[name] = g
            v = g * amb
            if v > 1e-3:
                if snd.status() != AudioSound.PLAYING:
                    snd.play()
                snd.setVolume(v)
            elif snd.status() == AudioSound.PLAYING:
                snd.stop()
        # distant one-shots outdoors
        self._distant_t -= dt
        if self._distant_t <= 0:
            self._distant_t = self.rng.uniform(7.0, 16.0)
            if self.env == "outdoor" and amb > 0:
                a = self.rng.uniform(0, 2 * math.pi)
                r = self.rng.uniform(22.0, 40.0)
                lp = self.listener_pos()
                pos = Point3(lp.x + math.cos(a) * r, lp.y + math.sin(a) * r, lp.z + self.rng.uniform(1.0, 6.0))
                self.play_at(self.rng.choice(DISTANT), pos, 0.5, "ambient", occlude=False)
        # menu music
        if self.music is not None:
            self.music_gain = approach(self.music_gain, 1.0 if self.menu else 0.0, FADE_RATE * 0.6 * dt)
            self.music.setVolume(self.music_gain * 0.7 * self.volume("music"))
            if self.music_gain <= 0 and not self.menu:
                self.music.stop()
                self.music = None
