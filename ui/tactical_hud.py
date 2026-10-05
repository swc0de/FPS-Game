"""Milestone 6 HUD: the specialist kit (bottom right), team ping markers
(seen through walls), the drone/camera view overlay and gadget messages."""
from __future__ import annotations

import math

from direct.gui.DirectGui import DirectFrame
from panda3d.core import Point2, Point3, TextNode

from ui import widgets as W

SHADOW = (0, 0, 0, 0.85)
PING_COLORS = {"spot": (1.0, 0.3, 0.25, 1), "drone": (1.0, 0.3, 0.25, 1), "sensor": (1.0, 0.55, 0.2, 1),
               "pulse": (0.45, 0.85, 1.0, 1), "gadget": (1.0, 0.8, 0.3, 1), "mark": (1.0, 0.9, 0.4, 1)}
MAX_MARKERS = 12


class TacticalHUD:
    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.tac = director.tactical
        a2 = game
        self.visible = True
        # --- kit, above the ammo counter
        self.kit_title = W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.06, 0.47), scale=0.036, fg=W.ACCENT,
                                      shadow=SHADOW, align=TextNode.ARight, mayChange=True)
        self.kit_lines = [W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.06, 0.42 - i * 0.042), scale=0.032,
                                       fg=W.TEXT, shadow=SHADOW, align=TextNode.ARight, mayChange=True)
                          for i in range(3)]
        self.message = W.Text(text="", parent=a2.aspect2d, pos=(0, -0.3), scale=0.04, fg=W.ACCENT,
                                    shadow=SHADOW, mayChange=True)
        # --- ping markers
        self.markers = []
        for _ in range(MAX_MARKERS):
            f = DirectFrame(parent=a2.aspect2d, frameColor=(1, 0.3, 0.25, 1), frameSize=(-0.014, 0.014, -0.014, 0.014))
            f.setR(45)
            t = W.Text(text="", parent=a2.aspect2d, scale=0.026, fg=W.TEXT, shadow=SHADOW, mayChange=True)
            f.hide()
            t.hide()
            self.markers.append((f, t))
        # --- drone / camera overlay
        self.view_root = a2.aspect2d.attachNewNode("view_overlay")
        for fs in ((-2.0, 2.0, 0.93, 1.2), (-2.0, 2.0, -1.2, -0.93), (-2.0, -1.3, -1.2, 1.2), (1.3, 2.0, -1.2, 1.2)):
            DirectFrame(parent=self.view_root, frameColor=(0, 0, 0, 0.35), frameSize=fs)
        self.view_title = W.Text(text="", parent=self.view_root, pos=(0, 0.7), scale=0.05, fg=W.TEXT,
                                       shadow=SHADOW, mayChange=True)
        self.view_rec = W.Text(text="", parent=self.view_root, pos=(-1.2, 0.7), scale=0.04,
                                     fg=(1, 0.25, 0.2, 1), shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
        self.view_help = W.Text(text="", parent=self.view_root, pos=(0, -0.88), scale=0.036, fg=W.DIM,
                                      shadow=SHADOW, mayChange=True)
        self.view_state = W.Text(text="", parent=self.view_root, pos=(0, 0.1), scale=0.09,
                                       fg=(0.5, 0.75, 1.0, 1), shadow=SHADOW, mayChange=True)
        self.noise = DirectFrame(parent=self.view_root, frameColor=(0.35, 0.4, 0.5, 0.0), frameSize=(-2, 2, -1.2, 1.2))
        self.reticle = DirectFrame(parent=self.view_root, frameColor=(1, 1, 1, 0.8), frameSize=(-0.004, 0.004, -0.004, 0.004))
        self.view_root.hide()
        self.lost = W.Text(text="", parent=a2.aspect2d, pos=(0, 0.1), scale=0.08, fg=(1, 0.4, 0.3, 1),
                                 shadow=SHADOW, mayChange=True)

    def set_visible(self, v: bool) -> None:
        self.visible = v
        for n in [self.kit_title, self.message, self.lost] + self.kit_lines:
            n.show() if v else n.hide()
        if not v:
            self.view_root.hide()
            for f, t in self.markers:
                f.hide()
                t.hide()

    # ------------------------------------------------------------------ kit
    def _kit_text(self, agent) -> tuple[str, list[str], tuple]:
        tac = self.tac
        kit = tac.kit(agent)
        sp = tac.specialist(kit.specialist)
        title = f"{sp['name'].upper()}  -  {sp.get('role', '')}"
        lines = []
        if kit.gadget:
            name = tac.gcfg[kit.gadget].get("name", kit.gadget)
            cd = kit.ready_t - tac.now
            lines.append(f"[X] {name}  x{kit.gadget_left}" + (f"  ({cd:.0f} s)" if cd > 0.5 and kit.gadget_left else ""))
        if agent.side == "attack":
            lines.append(f"[C] Wall charge  x{kit.charges}")
            own = sum(1 for d in tac.drones if d.owner is agent)
            lines.append(f"[6] Drones  {kit.drones} left" + (f", {own} out" if own else ""))
        elif agent.side == "defend":
            lines.append(f"[F] Reinforce  x{kit.reinforcements}")
            lines.append(f"[6] Cameras  {sum(1 for c in tac.cameras if c.alive)}/{len(tac.cameras)}")
        color = tuple(sp.get("color", (1, 0.78, 0.36))) + (1,)
        return title, lines, color

    # ---------------------------------------------------------------- update
    def update(self, dt: float) -> None:
        if not self.visible:
            return
        d = self.director
        tac = self.tac
        human = d.player_agent
        spec = d.spectator.target if d.spectator.active and not d.spectator.free else None
        who = spec if spec is not None else (human if not d.spectate_only and human.side else None)
        if who is not None and who.side in ("attack", "defend") and d.match.phase != "waiting":
            title, lines, color = self._kit_text(who)
            self.kit_title.setText(title)
            self.kit_title.setFg(color)
            for i, t in enumerate(self.kit_lines):
                t.setText(lines[i] if i < len(lines) else "")
        else:
            self.kit_title.setText("")
            for t in self.kit_lines:
                t.setText("")
        self.message.setText(tac.message)
        self.lost.setText("SIGNAL LOST" if tac.view_lost_t > 0 else "")
        self._update_view()
        side = who.side if who is not None else None
        self._update_markers(side, d.spectate_only and spec is None)

    def _update_view(self) -> None:
        v = self.tac.view
        if v is None:
            self.view_root.hide()
            return
        self.view_root.show()
        from gameplay.observation import Drone
        views = self.tac.views(self.director.player_agent.side)
        idx = views.index(v) + 1 if v in views else 0
        if isinstance(v, Drone):
            who = "your drone" if v.owner is self.director.player_agent else f"{v.owner.name}'s drone"
            self.view_title.setText(f"DRONE {idx}/{len(views)}  -  {who}")
            self.view_help.setText("[WASD] drive   [SPACE] hop   [LMB] ping   [Q/E] switch   [6] back")
        else:
            self.view_title.setText(f"CAM {idx}/{len(views)}  -  {v.label}")
            self.view_help.setText("[MOUSE] pan   [LMB] ping   [Q/E] switch   [6] back")
        t = self.game.loop.time
        self.view_rec.setText("REC" if (t * 1.2) % 1.0 < 0.6 else "")
        if v.jammed:
            self.view_state.setText("SIGNAL JAMMED" if not v.disabled else "EMP - OFFLINE")
            self.noise["frameColor"] = (0.35, 0.4, 0.5, 0.35 + 0.25 * math.sin(t * 40.0) ** 2)
        else:
            self.view_state.setText("")
            self.noise["frameColor"] = (0.35, 0.4, 0.5, 0.0)

    def _update_markers(self, side, all_sides: bool) -> None:
        g = self.game
        pings = [p for p in self.tac.pings if all_sides or p.side == side]
        cam = g.cam
        lens = g.camLens
        aspect = g.getAspectRatio()
        eye = cam.getPos(g.render)
        used = 0
        now = self.tac.now
        for p in pings:
            if used >= MAX_MARKERS:
                break
            w = p.where()
            rel = cam.getRelativePoint(g.render, Point3(w))
            if rel.y <= 0.1:
                continue
            film = Point2()
            if not lens.project(rel, film):
                continue
            f, t = self.markers[used]
            used += 1
            x, y = film.x * aspect, film.y
            color = PING_COLORS.get(p.kind, (1, 0.3, 0.25, 1))
            fade = min(1.0, (p.until - now) / 0.5)
            f["frameColor"] = (color[0], color[1], color[2], 0.9 * fade)
            f.setPos(x, 0, y)
            f.show()
            dist = (Point3(w) - eye).length()
            label = {"mark": "", "gadget": "gadget", "pulse": "pulse", "sensor": "sensor"}.get(p.kind, "")
            t.setText(f"{dist:.0f}m" + (f"  {label}" if label else ""))
            t.setFg((color[0], color[1], color[2], fade))
            t.setPos(x, y - 0.05)
            t.show()
        for f, t in self.markers[used:]:
            f.hide()
            t.hide()
