"""Game: ShowBase subclass that owns every subsystem and runs the loop."""
from __future__ import annotations

import json
import math
import time

from direct.gui.OnscreenText import OnscreenText
from direct.showbase.ShowBase import ShowBase
from panda3d.core import ClockObject, Filename, Point3, Vec3, WindowProperties

from audio.system import AudioSystem
from engine import paths
from engine.app import GAME_TITLE, TICK_RATE
from engine.fixed_loop import FixedTimestep
from engine.input import InputManager
from engine.physics import PhysicsWorld
from gameplay.dummy import TargetDummy
from gameplay.player import PlayerController
from maps.level import Level
from render.effects import Effects
from render.materials import MaterialLibrary
from render.renderer import Renderer
from ui.debug_hud import DebugHud
from ui.hud import HUD
from ui.console import Console
from ui.menus import PauseMenu
from weapons.ballistics import Ballistics
from weapons.defs import database
from weapons.pickups import PickupManager
from weapons.player_weapons import PlayerWeapons

RESPAWN_TIME = 3.0


class Game(ShowBase):
    def __init__(self, settings, args):
        ShowBase.__init__(self)
        self.settings = settings
        self.args = args
        self.exit_code = 0
        self.disableMouse()
        self.setBackgroundColor(0, 0, 0, 1)
        self.graphics = settings.graphics
        self._loading = OnscreenText(text=f"{GAME_TITLE}\nloading...", scale=0.08, fg=(1, 1, 1, 1),
                                     shadow=(0, 0, 0, 1))
        for _ in range(2):
            self.graphicsEngine.renderFrame()

        t0 = time.time()
        self.zoom = 1.0
        self.camLens.setNearFar(0.05, 2000.0)
        self.apply_fov()
        self.loop = FixedTimestep(TICK_RATE)
        self.physics = PhysicsWorld(self.render)
        self.renderer = Renderer(self, self.graphics, log=self.log)
        self.materials = MaterialLibrary(self.loader, self.graphics, procedural_size=args.texture_res)
        self.materials.preload(self._weapon_materials())
        self.level = Level(self, self.renderer, self.physics, self.materials, args.map, log=self.log)
        self.renderer.setup_environment(self.level.data.get("environment", {}))
        self.level.build()

        self.weapon_db = database()
        self.ballistics = Ballistics(self.physics, self.weapon_db)
        self.audio = AudioSystem(self, settings.audio, self.weapon_db.weapons, log=self.log)
        self.effects = Effects(self)
        self.pickups = PickupManager(self)
        self.dummies: list[TargetDummy] = []
        self.grenades = []
        self.noise_events: list[tuple[float, Point3, float, float]] = []

        self.input = InputManager(self, settings.input["binds"])
        self.player = PlayerController(self, self.physics, self.input, settings)
        self.player.on_damaged = self._player_damaged
        self.player.on_step = self._player_step
        sp = self.level.spawn_point("attack")
        self.spawn = sp
        self.player.spawn(sp["pos"], sp.get("heading", 0.0))
        if args.pose:
            x, y, z, h, p = (float(v) for v in args.pose.split(","))
            self.player.set_pose((x, y, z), (h, p))
        self._respawn_timer = 0.0

        self.hud = HUD(self)
        self.debug_hud = DebugHud(self)
        self.weapons = PlayerWeapons(self)
        self.player.speed_scale = self.weapons.speed_scale
        self._spawn_props()
        self.director = None
        mode = getattr(args, "mode", "auto")
        if mode != "sandbox":
            from gameplay.director import MatchDirector, map_supports_match
            if mode == "match" or map_supports_match(self.level):
                side = args.team if getattr(args, "team", None) in ("attack", "defend") else "attack"
                self.director = MatchDirector(self, side, getattr(args, "opponents", None),
                                              getattr(args, "teammates", None), seed=getattr(args, "seed", None),
                                              difficulty=getattr(args, "difficulty", None) or
                                              settings.data.get("gameplay", {}).get("bot_difficulty"),
                                              standins=getattr(args, "bots", "on") == "off",
                                              spectate=bool(getattr(args, "spectate", False)))
        self.paused = False
        self.menu = PauseMenu(self)
        self.console = Console(self)
        self.match_hud = None
        self.buy_menu = None
        self.team_select = None
        if self.director is not None:
            from ui.buy_menu import BuyMenu
            from ui.match_hud import MatchHUD
            self.match_hud = MatchHUD(self, self.director)
            self.buy_menu = BuyMenu(self, self.director)
        self.renderer.post.set_brightness(float(settings.video.get("brightness", 0.0)))
        if not settings.video.get("show_fps", True):
            self.debug_hud.toggle()
        self._loading.destroy()
        self.log(f"[game] ready in {time.time() - t0:.1f}s - "
                 f"{self.win.getGsg().getDriverRenderer()} / GL {self.win.getGsg().getDriverVersion()}")

        self.accept("escape", self._on_escape)
        self.accept("mouse1", self._click_capture)
        self.accept("f1", self.debug_hud.toggle)
        self.accept("f12", self.screenshot)
        self.accept("f3", self.cycle_post_debug)
        self.accept("v", self.toggle_noclip)
        self.accept("b", self._toggle_buy)
        self.accept("tab", self._scoreboard, [True])
        self.accept("tab-up", self._scoreboard, [False])
        self.accept("`", self.console.toggle)
        self.accept("f10", self.console.toggle)
        self.accept("window-event", self._on_window_event)
        self.taskMgr.add(self._update, "game-update", sort=-10)

        self._frame = 0
        if getattr(args, "post_debug", 0):
            self.renderer.post.set_debug_view(args.post_debug)
        self._shot_queue = []
        self.demo = None
        if args.shots:
            self._prepare_shots(args.shots)
        elif getattr(args, "demo", None):
            from engine.demo import make_demo
            self.demo = make_demo(self, args.demo)
        elif not args.offscreen and not args.frames:
            self.input.set_captured(True)
        if self.director is not None and not args.shots:
            if args.team or self.demo is not None or args.frames or args.offscreen:
                self.director.start()
            else:
                from ui.team_select import TeamSelect
                self.team_select = TeamSelect(self, self._choose_side)
        self.hud.on_resize()

    # ------------------------------------------------------------- setup
    def _weapon_materials(self) -> list[str]:
        from weapons.models import model_defs
        mats = {"glove", "sleeve_camo", "brass", "dummy_polymer", "canvas", "metal_olive", "pbr_red_plastic",
                "pbr_white_rough", "steel", "metal_tan", "hazard"}
        for d in model_defs().values():
            for part in d["parts"]:
                mats.add(part.get("mat", "gun_metal"))
        return sorted(mats)

    def _spawn_props(self) -> None:
        for kind, e in self.level.props:
            if kind == "dummy":
                self.dummies.append(TargetDummy(self, e))
            elif kind == "sign":
                self._make_sign(e)
            elif kind == "pickup":
                item = e["item"]
                if item in self.weapon_db.weapons:
                    k, label = "weapon", self.weapon_db.weapons[item].name
                elif item in self.weapon_db.grenades:
                    k, label = "grenade", self.weapon_db.grenades[item].name
                else:
                    k, label = item, {"ammo": "ammo", "armor": "armour + helmet", "grenades": "grenades"}.get(item, item)
                self.pickups.spawn(k, item, e["pos"], e.get("heading", 0.0), static=e.get("static", True),
                                   respawn=float(e.get("respawn", 3.0)), label=e.get("label", label))

    def _make_sign(self, e: dict) -> None:
        from panda3d.core import TextNode
        from render.renderer import NO_DEPTH_PASSES
        tn = TextNode("sign")
        tn.setText(e["text"])
        tn.setAlign(TextNode.ACenter)
        tn.setTextColor(*e.get("color", (0.95, 0.85, 0.5, 1)))
        tn.setShadow(0.04, 0.04)
        np_ = self.render.attachNewNode(tn)
        np_.setPos(*e["pos"])
        np_.setH(e.get("heading", 0.0))
        np_.setScale(e.get("scale", 0.16))
        np_.setShaderOff(10)
        np_.setLightOff(10)
        np_.setTwoSided(True)
        np_.hide(NO_DEPTH_PASSES)

    # -------------------------------------------------------- game services
    def log(self, msg: str) -> None:
        print(msg, flush=True)

    def damageables(self) -> list:
        out = [d for d in self.dummies] + [self.player]
        if self.director is not None:
            out += [a for a in self.director.standins if a.root.isHidden() is False]
            out += [b for b in self.director.bots if b.active]
        return out

    def navmesh(self):
        """Navigation mesh of the current level (built once, cached on disk)."""
        if getattr(self, "_nav", None) is None:
            from ai.navmesh import NavConfig, NavMesh
            seeds = [s["pos"] for s in self.level.spawns]
            self._nav = NavMesh.cached(self.level.colliders, self.level.data["bounds"], seeds,
                                       paths.CACHE_DIR / "nav", self.level.path.stem,
                                       NavConfig.from_movement(self.player.char.cfg), log=self.log)
        return self._nav

    # ---------------------------------------------------- match services
    @property
    def player_agent(self):
        return self.director.player_agent if self.director is not None else None

    def combat_locked(self) -> bool:
        return self.director is not None and self.director.combat_locked()

    def drop_bomb(self, agent) -> None:
        if self.director is not None:
            self.director.drop_bomb(agent)

    def drop_weapon_at(self, key: str, pos, heading: float) -> None:
        self.pickups.spawn("weapon", key, pos, heading, vel=Vec3(0, 0, 1.0), static=False)

    def clear_world(self) -> None:
        """Round restart: grenades, smoke, decals, particles and dropped weapons go away."""
        for gr in self.grenades:
            gr.destroy()
        self.grenades = []
        self.effects.clear_world()
        self.pickups.clear_dropped()

    def spawn_grenade(self, g) -> None:
        self.grenades.append(g)

    def notify_noise(self, pos, loudness: float, radius: float, source=None) -> None:
        """Gunshots, explosions and footsteps, recorded with their source for AI hearing."""
        if source is self.player:
            source = self.player_agent
        elif source is not None and self.director is not None:
            source = self.director.agent_of(source)
        self.noise_events.append((self.loop.time, Point3(pos), loudness, radius, source))
        if len(self.noise_events) > 96:
            del self.noise_events[:-96]

    def _player_step(self, ev) -> None:
        if ev.kind == "land":
            self.audio.play_ui("land", 0.4 * ev.loudness + 0.1)
        else:
            self.audio.footstep(ev.surface, ev.pos, ev.loudness, own=True)
        if ev.loudness > 0:
            self.notify_noise(ev.pos, ev.loudness, 28.0 * ev.loudness, source=self.player)

    def _player_damaged(self, res) -> None:
        self.hud.damage_taken(res.health)
        if res.killed and self.director is None:
            self._respawn_timer = RESPAWN_TIME
            self.hud.flash_msg("killed by " + (res.info.weapon or "the world"), 2.5)

    # ---------------------------------------------------------------- view
    def base_vfov(self) -> float:
        return float(self.settings.video.get("fov", 74.0))

    def current_vfov(self) -> float:
        return math.degrees(2 * math.atan(math.tan(math.radians(self.base_vfov()) * 0.5) * self.zoom))

    def apply_fov(self) -> None:
        vfov = self.current_vfov()
        aspect = self.getAspectRatio() if self.win else 16 / 9
        hfov = math.degrees(2 * math.atan(math.tan(math.radians(vfov) / 2) * aspect))
        self.camLens.setFov(hfov, vfov)

    def set_zoom(self, zoom: float) -> None:
        if abs(zoom - self.zoom) > 1e-4:
            self.zoom = zoom
            self.apply_fov()
        zs = float(self.settings.input.get("zoom_sensitivity", 1.0)) if zoom < 0.999 else 1.0
        self.player.sens_scale = zoom * zs

    def _on_window_event(self, win) -> None:
        self.windowEvent(win)
        if win == self.win:
            self.apply_fov()
            self.renderer.on_window_resized()
            if hasattr(self, "hud"):
                self.hud.on_resize()
            props = win.getProperties()
            if props.getForeground() is False and self.input.captured:
                if hasattr(self, "menu") and not self._shot_queue and self.demo is None:
                    if not self.menu.is_open:
                        self.menu.open()
                else:
                    self.input.set_captured(False)

    def _on_escape(self) -> None:
        if self._shot_queue or self.demo is not None:
            return
        if self.console.is_open:
            self.console.close()
        elif self.buy_menu is not None and self.buy_menu.is_open:
            self.buy_menu.close()
        elif self.team_select is not None:
            return
        else:
            self.menu.on_escape()

    def _overlay_open(self) -> bool:
        return (self.console.is_open or self.team_select is not None
                or (self.buy_menu is not None and self.buy_menu.is_open))

    def _choose_side(self, side: str) -> None:
        self.team_select = None
        if side == "spectate":
            self.director.set_spectate()
        else:
            self.director.restart(side)
        self.input.set_captured(True)

    def _toggle_buy(self) -> None:
        if self.buy_menu is None or self.console.is_open or self.paused or self.team_select is not None:
            return
        self.buy_menu.toggle()

    def _scoreboard(self, show: bool) -> None:
        if self.match_hud is None or self.console.is_open:
            return
        self.match_hud.scoreboard.set_visible(show and not self.paused)

    def set_paused(self, paused: bool) -> None:
        """Pause menu: the simulation stops and the mouse is released."""
        self.paused = paused
        self.input.set_captured(not paused)
        self.input.clear_presses()
        self.hud.set_visible(not paused)
        self.debug_hud.set_hidden(paused)
        if self.match_hud is not None:
            self.match_hud.set_visible(not paused)
        if self.buy_menu is not None:
            self.buy_menu.close()
        if paused and self.console.is_open:
            self.console.close()
        if not paused:
            self.renderer.post.reset_adaptation()

    def apply_settings(self, data: dict) -> None:
        """Apply a full settings tree from the settings menu and save it."""
        import copy
        old_video = dict(self.settings.video)
        self.settings.data = copy.deepcopy(data)
        self.settings.save()
        restart = self.renderer.apply_graphics(self.settings.graphics, self.materials)
        self.graphics = self.settings.graphics
        v = self.settings.video
        if v["resolution"] != old_video.get("resolution") or v.get("fullscreen") != old_video.get("fullscreen"):
            props = WindowProperties()
            props.setSize(int(v["resolution"][0]), int(v["resolution"][1]))
            props.setFullscreen(bool(v.get("fullscreen")))
            self.win.requestProperties(props)
        clock = ClockObject.getGlobalClock()
        if v.get("max_fps"):
            clock.setMode(ClockObject.MLimited)
            clock.setFrameRate(float(v["max_fps"]))
        else:
            clock.setMode(ClockObject.MNormal)
        self.apply_fov()
        self.weapons.vm.base_fov = float(v.get("viewmodel_fov", 54.0))
        self.renderer.post.set_brightness(float(v.get("brightness", 0.0)))
        if bool(v.get("show_fps", True)) != self.debug_hud.visible:
            self.debug_hud.toggle()
        self.audio.settings = self.settings.audio
        self.audio.set_volume(float(self.settings.audio.get("master", 0.8)))
        self.hud.crosshair.cfg = self.settings.data["gameplay"]["crosshair"]
        self.hud.crosshair.rebuild()
        self.log(f"[settings] applied (preset {v['preset']}, overrides {sorted(self.settings.data.get('graphics', {}))})"
                 + (f"; restart needed for {restart}" if restart else ""))

    def toggle_capture(self) -> None:
        self.input.set_captured(not self.input.captured)
        if not self.input.captured:
            self.hud.flash_msg("mouse released - click to play")

    def _click_capture(self) -> None:
        if self.paused or self._overlay_open():
            return
        if not self.input.captured and not self._shot_queue and self.demo is None:
            self.input.set_captured(True)
            self.input.consume("fire")

    POST_DEBUG_VIEWS = ("final image", "ambient occlusion", "bloom", "normals", "depth")

    def cycle_post_debug(self) -> None:
        post = self.renderer.post
        mode = (post.debug_view + 1) % len(self.POST_DEBUG_VIEWS)
        post.set_debug_view(mode)
        self.hud.flash_msg(f"view: {self.POST_DEBUG_VIEWS[mode]}", 1.5)

    def toggle_noclip(self) -> None:
        if not self.input.captured:
            return
        self.player.noclip = not self.player.noclip
        if not self.player.noclip:
            self.player.char.teleport(self.player.char.pos)
        self.hud.flash_msg("noclip " + ("on" if self.player.noclip else "off"), 1.2)

    def screenshot(self, path: str | None = None) -> str:
        if path is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = str(paths.SCREENSHOT_DIR / f"shot_{stamp}.png")
        self.win.saveScreenshot(Filename.fromOsSpecific(path))
        self.log(f"[game] screenshot -> {path}")
        return path

    # ------------------------------------------------------------- shots
    def _prepare_shots(self, which: str) -> None:
        shots = self.level.camera_shots
        if which != "all":
            names = set(which.split(","))
            shots = [s for s in shots if s["name"] in names]
        self._shot_queue = list(shots)
        self._shot_wait = 0
        self.debug_hud.toggle()
        self.hud.set_visible(False)
        if self.match_hud is not None:
            self.match_hud.set_visible(False)
        self._next_shot()

    def _next_shot(self) -> None:
        shot = self._shot_queue[0]
        self.player.set_pose(shot["pos"], shot["hpr"])
        self.weapons.force_hide_vm = not shot.get("viewmodel", False)
        # eye adaptation jumps straight to the new view; local shadow maps
        # update on a budget, so give them a few frames
        self.renderer.post.reset_adaptation()
        self._shot_wait = 6

    def _process_shots(self) -> None:
        if self._shot_wait > 0:
            self._shot_wait -= 1
            return
        shot = self._shot_queue.pop(0)
        preset = self.settings.video["preset"]
        lum = self.renderer.post.adapted_luminance()
        if lum is not None:
            self.log(f"[post] {shot['name']}: adapted log2 luminance {lum:+.2f}, "
                     f"auto exposure {self.renderer.post.exposure_ev(lum):+.2f} EV")
        self.screenshot(str(paths.SCREENSHOT_DIR / f"{self.level.path.stem}_{shot['name']}_{preset}.png"))
        if self._shot_queue:
            self._next_shot()
        else:
            self.userExit()

    # -------------------------------------------------------------- loop
    def _fixed_update(self, dt: float) -> None:
        if self._shot_queue or self.paused:
            self.player.char.prev_pos = Point3(self.player.char.pos)
            return
        now = self.loop.time
        if self.demo is not None:
            self.demo.tick(dt)
        self.player.fixed_update(dt)
        for d in self.dummies:
            d.fixed_update(dt)
        for g in self.grenades:
            g.fixed_update(dt)
        self.grenades = [g for g in self.grenades if not g.done]
        self.physics.step(dt)
        self.weapons.fixed_update(dt, now)
        if self.director is not None:
            self.director.fixed_update(dt)
        elif not self.player.damageable.alive:
            self._respawn_timer -= dt
            if self._respawn_timer <= 0:
                self.player.damageable.reset(armor=0, helmet=False)
                self.player.spawn(self.spawn["pos"], self.spawn.get("heading", 0.0))
                self.weapons.inv.refill_ammo()
                self.weapons.select(self.weapons.inv.best_slot(), force=True)

    def _update(self, task):
        dt = min(ClockObject.getGlobalClock().getDt(), 0.25)
        if self.demo is not None:
            dt = self.demo.frame_dt   # deterministic: simulation and animation advance identically
        self.input.poll_mouse()
        alpha = self.loop.advance(dt, self._fixed_update)
        self.weapons.pre_frame(dt)
        self.player.frame_update(dt, alpha)
        self.weapons.frame_update(dt)
        for d in self.dummies:
            d.frame_update(dt)
        for g in self.grenades:
            g.frame_update()
        if self.director is not None:
            self.director.frame_update(dt, alpha)
        self.pickups.update(dt)
        self.effects.update(dt)
        self.renderer.update(dt)
        self.hud.update(dt)
        if self.match_hud is not None:
            self.match_hud.update(dt)
        if self.buy_menu is not None:
            self.buy_menu.update()
        self.debug_hud.update(dt)
        self._frame += 1
        if self.args.trace:
            self._trace_t = getattr(self, "_trace_t", 0.0) + dt
            if self._trace_t >= 0.5:
                self._trace_t = 0.0
                c = self.player.char
                ws = self.weapons.inv.current()
                ammo = f"{ws.d.key}:{ws.ammo}/{ws.reserve}" if ws else self.weapons.inv.slot
                self.log(f"[trace] t={self.loop.time:6.2f} pos=({c.pos.x:.2f},{c.pos.y:.2f},{c.pos.z:.2f}) "
                         f"yaw={self.player.yaw:.1f} pitch={self.player.pitch:.1f} speed={c.horizontal_speed:.2f} "
                         f"h={c.height:.2f} {self.player.state_text} weapon={ammo} hp={self.player.damageable.health:.0f} "
                         f"captured={self.input.captured}")
        if self._shot_queue:
            self._process_shots()
        elif self.demo is not None:
            if self.demo.frame():
                self.userExit()
        elif self.args.frames and self._frame >= self.args.frames:
            if self.args.screenshot:
                self.screenshot(self.args.screenshot)
            self.userExit()
        return task.cont

    def save_json(self, name: str, data) -> None:
        with open(paths.USER_DIR / name, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
