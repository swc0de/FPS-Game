"""Game: ShowBase subclass that owns every subsystem and runs the loop."""
from __future__ import annotations

import math
import time

from direct.showbase.ShowBase import ShowBase
from direct.gui.OnscreenText import OnscreenText
from panda3d.core import ClockObject, Filename, Point3

from engine import paths
from engine.app import GAME_TITLE, TICK_RATE
from engine.fixed_loop import FixedTimestep
from engine.input import InputManager
from engine.physics import PhysicsWorld
from gameplay.player import PlayerController
from maps.level import Level
from render.materials import MaterialLibrary
from render.renderer import Renderer
from ui.crosshair import Crosshair
from ui.debug_hud import DebugHud


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
        self.camLens.setNearFar(0.05, 2000.0)
        self.apply_fov()
        self.physics = PhysicsWorld(self.render)
        self.renderer = Renderer(self, self.graphics, log=self.log)
        self.materials = MaterialLibrary(self.loader, self.graphics, procedural_size=args.texture_res)
        self.level = Level(self, self.renderer, self.physics, self.materials, args.map, log=self.log)
        self.renderer.setup_environment(self.level.data.get("environment", {}))
        self.level.build()

        self.input = InputManager(self, settings.input["binds"])
        self.player = PlayerController(self, self.physics, self.input, settings)
        sp = self.level.spawn_point("attack")
        self.player.spawn(sp["pos"], sp.get("heading", 0.0))
        if args.pose:
            x, y, z, h, p = (float(v) for v in args.pose.split(","))
            self.player.set_pose((x, y, z), (h, p))

        self.hud = DebugHud(self)
        self.crosshair = Crosshair(self, settings.data["gameplay"]["crosshair"])
        self.loop = FixedTimestep(TICK_RATE)
        self._loading.destroy()
        self.log(f"[game] ready in {time.time() - t0:.1f}s - "
                 f"{self.win.getGsg().getDriverRenderer()} / GL {self.win.getGsg().getDriverVersion()}")

        self.accept("escape", self.toggle_capture)
        self.accept("mouse1", self._click_capture)
        self.accept("f1", self.hud.toggle)
        self.accept("f12", self.screenshot)
        self.accept("v", self.toggle_noclip)
        self.accept("window-event", self._on_window_event)
        self.taskMgr.add(self._update, "game-update", sort=-10)

        self._frame = 0
        self._shot_queue = []
        if args.shots:
            self._prepare_shots(args.shots)
        elif not args.offscreen and not args.frames:
            self.input.set_captured(True)

    # ------------------------------------------------------------------ util
    def log(self, msg: str) -> None:
        print(msg, flush=True)

    def apply_fov(self) -> None:
        vfov = float(self.settings.video.get("fov", 74.0))
        aspect = self.getAspectRatio() if self.win else 16 / 9
        hfov = math.degrees(2 * math.atan(math.tan(math.radians(vfov) / 2) * aspect))
        self.camLens.setFov(hfov, vfov)

    def _on_window_event(self, win) -> None:
        self.windowEvent(win)
        if win == self.win:
            self.apply_fov()
            if hasattr(self, "crosshair"):
                self.crosshair.center()
            props = win.getProperties()
            if props.getForeground() is False and self.input.captured:
                self.input.set_captured(False)

    def toggle_capture(self) -> None:
        self.input.set_captured(not self.input.captured)
        if not self.input.captured:
            self.hud.flash("mouse released - click to play")

    def _click_capture(self) -> None:
        if not self.input.captured and not self._shot_queue:
            self.input.set_captured(True)

    def toggle_noclip(self) -> None:
        if not self.input.captured:
            return
        self.player.noclip = not self.player.noclip
        if not self.player.noclip:
            self.player.char.teleport(self.player.char.pos)
        self.hud.flash("noclip " + ("on" if self.player.noclip else "off"), 1.2)

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
        self.hud.toggle()
        self.crosshair.set_visible(False)
        self._next_shot()

    def _next_shot(self) -> None:
        shot = self._shot_queue[0]
        self.player.set_pose(shot["pos"], shot["hpr"])
        # local shadow maps update on a budget; give them a few frames
        self._shot_wait = 6

    def _process_shots(self) -> None:
        if self._shot_wait > 0:
            self._shot_wait -= 1
            return
        shot = self._shot_queue.pop(0)
        preset = self.settings.video["preset"]
        self.screenshot(str(paths.SCREENSHOT_DIR / f"{self.level.path.stem}_{shot['name']}_{preset}.png"))
        if self._shot_queue:
            self._next_shot()
        else:
            self.userExit()

    # -------------------------------------------------------------- loop
    def _fixed_update(self, dt: float) -> None:
        if self._shot_queue:
            self.player.char.prev_pos = Point3(self.player.char.pos)
            return
        self.player.fixed_update(dt)
        self.physics.step(dt)

    def _update(self, task):
        dt = ClockObject.getGlobalClock().getDt()
        self.input.poll_mouse()
        alpha = self.loop.advance(dt, self._fixed_update)
        self.player.frame_update(dt, alpha)
        self.renderer.update()
        self.hud.update(dt)
        self._frame += 1
        if self.args.trace:
            self._trace_t = getattr(self, "_trace_t", 0.0) + dt
            if self._trace_t >= 0.5:
                self._trace_t = 0.0
                c = self.player.char
                self.log(f"[trace] t={self.loop.time:6.2f} pos=({c.pos.x:.2f},{c.pos.y:.2f},{c.pos.z:.2f}) "
                         f"yaw={self.player.yaw:.1f} pitch={self.player.pitch:.1f} speed={c.horizontal_speed:.2f} "
                         f"h={c.height:.2f} {self.player.state_text} captured={self.input.captured}")
        if self._shot_queue:
            self._process_shots()
        elif self.args.frames and self._frame >= self.args.frames:
            if self.args.screenshot:
                self.screenshot(self.args.screenshot)
            self.userExit()
        return task.cont
