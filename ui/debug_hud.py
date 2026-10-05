"""Performance / developer overlay (F1 cycles: off -> FPS -> full).

The FPS line shows the frame time and how much of it the game logic took
(simulation ticks, bots, effects, HUD - everything in Game._update); the
rest is Panda3D's culling/drawing plus waiting for the GPU or v-sync."""
from __future__ import annotations

from panda3d.core import TextNode

from ui import widgets as W


class DebugHud:
    MODES = (False, True, "full")

    def __init__(self, app):
        self.app = app
        self.mode = "full"
        self._logic = 0.0
        self._acc = 0.0
        self._frames = 0
        self._fps = 0.0
        self._ms = 0.0
        self.text = W.Text(text="", parent=app.a2dTopLeft, pos=(0.04, -0.07), scale=0.042,
                                 fg=(0.92, 0.95, 0.9, 1), shadow=(0, 0, 0, 0.8), align=TextNode.ALeft,
                                 mayChange=True)
        self.help = W.Text(
            text="WASD move  SHIFT walk  CTRL crouch  SPACE jump  Q/E lean  LMB fire  RMB aim/scope  R reload  "
                 "1-4/wheel weapons  F use/reinforce  G drop  Y inspect\n"
                 "X gadget  C wall charge  6 drones/cameras  MMB ping  V noclip  ESC menu  F1 hud  F3 buffers  "
                 "F12 screenshot",
            parent=app.a2dTopLeft, pos=(0.04, -0.29), scale=0.034, fg=(0.85, 0.88, 0.85, 0.85),
            shadow=(0, 0, 0, 0.8), align=TextNode.ALeft)
        self.message = W.Text(text="", parent=app.aspect2d, pos=(0, 0.25), scale=0.055,
                                    fg=(1, 1, 1, 1), shadow=(0, 0, 0, 0.9), mayChange=True)
        self._msg_timer = 0.0

    @property
    def visible(self) -> bool:
        return bool(self.mode)

    def toggle(self) -> None:
        """F1: off -> FPS -> full -> off."""
        self.set_mode(self.MODES[(self.MODES.index(self.mode) + 1) % len(self.MODES)])

    def set_mode(self, mode) -> None:
        self.mode = mode if mode in self.MODES else True
        self._apply()

    def set_hidden(self, hidden: bool) -> None:
        """Temporarily hide (menus) without changing the user's choice."""
        self._hidden = hidden
        self._apply()

    def _apply(self) -> None:
        show = self.visible and not getattr(self, "_hidden", False)
        self.text.show() if show else self.text.hide()
        self.help.show() if show and self.mode == "full" else self.help.hide()

    def flash(self, msg: str, seconds: float = 2.5) -> None:
        self.message.setText(msg)
        self._msg_timer = seconds

    def update(self, dt: float, extra: str = "") -> None:
        self._acc += dt
        self._frames += 1
        self._logic += getattr(self.app, "logic_ms", 0.0)
        if self._acc >= 0.25:
            self._fps = self._frames / self._acc
            self._ms = 1000.0 * self._acc / self._frames
            self._logic_ms = self._logic / self._frames
            self._acc = 0.0
            self._frames = 0
            self._logic = 0.0
        if self._msg_timer > 0:
            self._msg_timer -= dt
            if self._msg_timer <= 0:
                self.message.setText("")
        if not self.visible:
            return
        fps_line = f"{self._fps:4.0f} fps  {self._ms:5.1f} ms  (logic {getattr(self, '_logic_ms', 0.0):4.1f} ms)"
        if self.mode != "full":
            self.text.setText(fps_line)
            return
        p = self.app.player
        c = p.char
        lines = [
            f"{fps_line}   preset {self.app.settings.video['preset']}",
            f"pos {c.pos.x:7.2f} {c.pos.y:7.2f} {c.pos.z:6.2f}   yaw {p.yaw:6.1f} pitch {p.pitch:5.1f}",
            f"speed {c.horizontal_speed:4.2f} m/s  vz {c.vel.z:5.2f}  {p.state_text}  surface {c.ground_surface}",
        ]
        post = self.app.renderer.post
        lines.append(f"render {post.rsize[0]}x{post.rsize[1]} ({post.scale * 100:.0f}%)  AA {post.g['antialiasing']}  "
                     f"AO {post.ssao_mode}  bloom {'on' if post.bloom_on else 'off'}  F3 view")
        if extra:
            lines.append(extra)
        self.text.setText("\n".join(lines))
