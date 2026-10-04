"""Developer overlay: FPS, frame time, player state, help."""
from __future__ import annotations

from direct.gui.OnscreenText import OnscreenText
from panda3d.core import TextNode


class DebugHud:
    def __init__(self, app):
        self.app = app
        self.visible = True
        self._acc = 0.0
        self._frames = 0
        self._fps = 0.0
        self._ms = 0.0
        self.text = OnscreenText(text="", parent=app.a2dTopLeft, pos=(0.04, -0.07), scale=0.042,
                                 fg=(0.92, 0.95, 0.9, 1), shadow=(0, 0, 0, 0.8), align=TextNode.ALeft,
                                 mayChange=True)
        self.help = OnscreenText(
            text="WASD move  SHIFT walk  CTRL crouch  SPACE jump  LMB fire  RMB aim/scope  R reload  "
                 "1-4/wheel weapons  F use  G drop  Y inspect  V noclip  F1 hud  F12 screenshot",
            parent=app.a2dTopLeft, pos=(0.04, -0.24), scale=0.034, fg=(0.85, 0.88, 0.85, 0.85),
            shadow=(0, 0, 0, 0.8), align=TextNode.ALeft)
        self.message = OnscreenText(text="", parent=app.aspect2d, pos=(0, 0.25), scale=0.055,
                                    fg=(1, 1, 1, 1), shadow=(0, 0, 0, 0.9), mayChange=True)
        self._msg_timer = 0.0

    def toggle(self) -> None:
        self.visible = not self.visible
        for t in (self.text, self.help):
            t.show() if self.visible else t.hide()

    def flash(self, msg: str, seconds: float = 2.5) -> None:
        self.message.setText(msg)
        self._msg_timer = seconds

    def update(self, dt: float, extra: str = "") -> None:
        self._acc += dt
        self._frames += 1
        if self._acc >= 0.25:
            self._fps = self._frames / self._acc
            self._ms = 1000.0 * self._acc / self._frames
            self._acc = 0.0
            self._frames = 0
        if self._msg_timer > 0:
            self._msg_timer -= dt
            if self._msg_timer <= 0:
                self.message.setText("")
        if not self.visible:
            return
        p = self.app.player
        c = p.char
        lines = [
            f"{self._fps:5.0f} fps  {self._ms:5.1f} ms   preset {self.app.settings.video['preset']}",
            f"pos {c.pos.x:7.2f} {c.pos.y:7.2f} {c.pos.z:6.2f}   yaw {p.yaw:6.1f} pitch {p.pitch:5.1f}",
            f"speed {c.horizontal_speed:4.2f} m/s  vz {c.vel.z:5.2f}  {p.state_text}  surface {c.ground_surface}",
        ]
        if extra:
            lines.append(extra)
        self.text.setText("\n".join(lines))
