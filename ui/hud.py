"""In-game HUD: health/armour with bars, ammo, weapon, crosshair, hit
markers, scope overlay, flashbang blindness and damage feedback (a red
vignette plus arcs around the crosshair pointing at the damage source)."""
from __future__ import annotations

import math

import numpy as np
from direct.gui.DirectGui import DirectFrame
from panda3d.core import CardMaker, Point3, SamplerState, TextNode, Texture, TransparencyAttrib

from ui import widgets as W
from ui.crosshair import Crosshair
from ui.radar import world_to_radar

AMBER = (1.0, 0.86, 0.55, 1.0)
WHITE = (0.95, 0.96, 0.92, 1.0)
DIM = (0.75, 0.78, 0.72, 0.85)


def _radial_texture(name: str, inner: float, outer: float, size: int = 256, invert: bool = False,
                    rgb=(1, 1, 1)) -> Texture:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    r = np.sqrt(((x + 0.5) / size * 2 - 1) ** 2 + ((y + 0.5) / size * 2 - 1) ** 2)
    a = np.clip((r - inner) / max(outer - inner, 1e-3), 0, 1)
    if invert:
        a = 1 - a
    img = np.zeros((size, size, 4), np.float32)
    img[..., 0:3] = rgb
    img[..., 3] = a
    tex = Texture(name)
    tex.setup2dTexture(size, size, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs((img * 255).astype(np.uint8).tobytes(), "RGBA")
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    return tex


def _arc_texture(size: int = 256, half_angle: float = 28.0) -> Texture:
    """Ring segment centred on 'up' with soft ends (damage direction arc)."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    u = (x + 0.5) / size * 2 - 1
    v = (y + 0.5) / size * 2 - 1
    r = np.sqrt(u * u + v * v)
    ang = np.degrees(np.abs(np.arctan2(u, v)))
    radial = np.clip(1 - np.abs(r - 0.86) / 0.08, 0, 1)
    ends = np.clip((half_angle - ang) / 10.0, 0, 1)
    img = np.zeros((size, size, 4), np.float32)
    img[..., 0] = 1.0
    img[..., 1] = 0.12 + 0.2 * radial
    img[..., 2] = 0.08
    img[..., 3] = radial * ends
    tex = Texture("damage_arc")
    tex.setup2dTexture(size, size, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs((img * 255).astype(np.uint8).tobytes(), "RGBA")
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    return tex


def arc_angle(dx: float, dy: float, yaw_deg: float) -> float:
    """Screen angle (degrees clockwise from straight up) of a world offset seen with this heading."""
    u, v = world_to_radar(dx, dy, yaw_deg, 1.0)
    return math.degrees(math.atan2(u, v))


class _Bar:
    def __init__(self, parent, pos, width: float, color):
        self.width = width
        self.bg = DirectFrame(parent=parent, frameColor=(0, 0, 0, 0.45), frameSize=(0, width, -0.006, 0.006),
                              pos=(pos[0], 0, pos[1]))
        self.fill = DirectFrame(parent=self.bg, frameColor=color, frameSize=(0, width, -0.004, 0.004))
        self.frac = -1.0

    def set(self, frac: float, color=None) -> None:
        frac = max(0.0, min(frac, 1.0))
        if abs(frac - self.frac) > 1e-3:
            self.frac = frac
            self.fill.setSx(max(frac, 1e-3))
        if color is not None:
            self.fill["frameColor"] = color

    def show(self) -> None:
        self.bg.show()

    def hide(self) -> None:
        self.bg.hide()


class HUD:
    def __init__(self, game):
        self.game = game
        self.root = game.aspect2d.attachNewNode("hud")
        a2 = game
        font_scale = 0.05
        # health / armour (bottom left)
        self.health = W.Text(text="", parent=a2.a2dBottomLeft, pos=(0.08, 0.1), scale=0.085,
                                   fg=WHITE, shadow=(0, 0, 0, 0.8), align=TextNode.ALeft, mayChange=True)
        self.armor = W.Text(text="", parent=a2.a2dBottomLeft, pos=(0.42, 0.1), scale=0.06,
                                  fg=DIM, shadow=(0, 0, 0, 0.8), align=TextNode.ALeft, mayChange=True)
        # ammo / weapon (bottom right)
        self.ammo = W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.24, 0.1), scale=0.1, fg=WHITE,
                                 shadow=(0, 0, 0, 0.8), align=TextNode.ARight, mayChange=True)
        self.reserve = W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.22, 0.1), scale=0.055, fg=DIM,
                                    shadow=(0, 0, 0, 0.8), align=TextNode.ALeft, mayChange=True)
        self.weapon = W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.06, 0.2), scale=font_scale,
                                   fg=AMBER, shadow=(0, 0, 0, 0.8), align=TextNode.ARight, mayChange=True)
        self.status = W.Text(text="", parent=a2.aspect2d, pos=(0, -0.22), scale=0.045, fg=AMBER,
                                   shadow=(0, 0, 0, 0.8), mayChange=True)
        self.message = W.Text(text="", parent=a2.aspect2d, pos=(0, 0.3), scale=0.05, fg=WHITE,
                                    shadow=(0, 0, 0, 0.9), mayChange=True)
        # map location (callout areas) and bomb-site indicator, bottom left
        self.location = W.Text(text="", parent=a2.a2dBottomLeft, pos=(0.08, 0.2), scale=0.042, fg=AMBER,
                                     shadow=(0, 0, 0, 0.8), align=TextNode.ALeft, mayChange=True)
        self.health_bar = _Bar(a2.a2dBottomLeft, (0.08, 0.065), 0.28, WHITE)
        self.armor_bar = _Bar(a2.a2dBottomLeft, (0.42, 0.065), 0.2, (0.55, 0.75, 1.0, 0.9))
        self._health_col = None
        self._msg_t = 0.0
        self.crosshair = Crosshair(game, game.settings.data["gameplay"]["crosshair"])

        # hit marker: four short diagonal strokes around the centre
        self.hitmarker = game.pixel2d.attachNewNode("hitmarker")
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            cm = CardMaker("hm")
            cm.setFrame(-6, 6, -1.2, 1.2)
            s = self.hitmarker.attachNewNode(cm.generate())
            s.setPos(sx * 11, 0, sy * 11)
            s.setR(-45 * sx * sy)
        self.hitmarker.setTransparency(True)
        self.hitmarker.hide()
        self._hit_t = 0.0
        self._hit_kill = False

        # full-screen overlays (render2d spans -1..1 regardless of aspect)
        def fullscreen(name, tex=None):
            cm = CardMaker(name)
            cm.setFrameFullscreenQuad()
            np_ = game.render2d.attachNewNode(cm.generate())
            np_.setTransparency(TransparencyAttrib.MAlpha)
            if tex is not None:
                np_.setTexture(tex)
            np_.setBin("fixed", 50)
            np_.hide()
            return np_
        self.blind_card = fullscreen("blind")
        self.blind_card.setColor(1, 1, 1, 0)
        self._blind_t = 0.0
        self._blind_dur = 1.0
        self._blind_strength = 0.0
        self.damage_card = fullscreen("damage", _radial_texture("dmg_vignette", 0.35, 1.35, rgb=(0.6, 0.02, 0.02)))
        self._dmg_t = 0.0
        # damage direction arcs around the crosshair: [node, source point, time left]
        self.arc_tex = _arc_texture()
        self.arc_root = game.aspect2d.attachNewNode("damage_arcs")
        self.arc_root.setBin("fixed", 45)
        self.arcs: list[list] = []

        # sniper scope: circular window + black borders + fine reticle
        self.scope = game.aspect2d.attachNewNode("scope")
        cm = CardMaker("scope_ring")
        cm.setFrame(-1, 1, -1, 1)
        ring = self.scope.attachNewNode(cm.generate())
        ring.setTexture(_radial_texture("scope_mask", 0.94, 0.985, 512, rgb=(0, 0, 0)))
        ring.setTransparency(TransparencyAttrib.MAlpha)
        for side in (-1, 1):
            cm = CardMaker("scope_side")
            cm.setFrame(0, 4, -1.2, 1.2)
            b = self.scope.attachNewNode(cm.generate())
            b.setColor(0, 0, 0, 1)
            b.setPos(side * 0.995 if side > 0 else -0.995 - 4, 0, 0)
        for (l, r, b, t) in ((-1, 1, -0.0015, 0.0015), (-0.0015, 0.0015, -1, 1)):
            cm = CardMaker("reticle")
            cm.setFrame(l, r, b, t)
            line = self.scope.attachNewNode(cm.generate())
            line.setColor(0, 0, 0, 0.95)
        self.scope.setBin("fixed", 40)
        self.scope.hide()
        self.scoped = False
        self.visible = True

    # -------------------------------------------------------------- events
    def hit_marker(self, killed: bool = False, headshot: bool = False) -> None:
        self._hit_t = 0.22 if not killed else 0.4
        self._hit_kill = killed
        col = (1.0, 0.25, 0.2, 1) if killed else ((1.0, 0.85, 0.3, 1) if headshot else (1, 1, 1, 1))
        self.hitmarker.setColor(*col)
        self.hitmarker.show()

    def blind(self, duration: float, strength: float) -> None:
        if duration > self._blind_t:
            self._blind_t = duration
            self._blind_dur = duration
            self._blind_strength = min(strength * 1.4, 1.0)
            self.blind_card.show()

    def damage_taken(self, amount: float) -> None:
        self._dmg_t = min(0.3 + amount / 60.0, 1.0)
        self.damage_card.show()

    ARC_TIME = 1.6

    def damage_from(self, source, amount: float) -> None:
        """Show an arc pointing at a world position the damage came from."""
        if source is None:
            return
        source = Point3(source)
        for arc in self.arcs:
            if (arc[1] - source).length() < 2.0:
                arc[1] = source
                arc[2] = self.ARC_TIME
                arc[3] = max(arc[3], amount)
                return
        if len(self.arcs) >= 4:
            self.arcs.pop(0)[0].removeNode()
        cm = CardMaker("arc")
        cm.setFrame(-0.34, 0.34, -0.34, 0.34)
        n = self.arc_root.attachNewNode(cm.generate())
        n.setTexture(self.arc_tex)
        n.setTransparency(TransparencyAttrib.MAlpha)
        self.arcs.append([n, source, self.ARC_TIME, amount])

    def flash_msg(self, text: str, seconds: float = 1.6) -> None:
        self.message.setText(text)
        self._msg_t = seconds

    def set_scope(self, scoped: bool) -> None:
        if scoped != self.scoped:
            self.scoped = scoped
            self.scope.show() if scoped else self.scope.hide()
            self.crosshair.set_visible(not scoped and self.visible)

    def set_spread(self, spread_deg: float, vfov_deg: float) -> None:
        win = self.game.win
        if win is None:
            return
        half_h = win.getYSize() * 0.5
        px = math.tan(math.radians(spread_deg)) / math.tan(math.radians(vfov_deg) * 0.5) * half_h
        self.crosshair.set_spread_px(px)

    def set_visible(self, v: bool) -> None:
        self.visible = v
        for w in (self.health, self.armor, self.ammo, self.reserve, self.weapon, self.status, self.location,
                  self.health_bar, self.arc_root):
            w.show() if v else w.hide()
        if not v:
            self.armor_bar.hide()
        self.crosshair.set_visible(v and not self.scoped)

    def on_resize(self) -> None:
        self.crosshair.center()
        win = self.game.win
        if win is not None:
            self.hitmarker.setPos(win.getXSize() // 2, 0, -(win.getYSize() // 2))

    # -------------------------------------------------------------- update
    def update(self, dt: float) -> None:
        g = self.game
        p = g.player.damageable
        spec = None
        d = g.director
        if d is not None and d.spectator.active and not d.spectator.free:
            spec = d.spectator.target
        if spec is not None:
            p = spec.damageable
        observing = g.tactical is not None and g.tactical.observing
        self.crosshair.set_visible(self.visible and not self.scoped and g.player.damageable.alive and not observing)
        self.health.setText(f"+ {int(math.ceil(p.health))}")
        low = p.health <= 25
        self.health.setFg((1, 0.35, 0.3, 1) if low else WHITE)
        col = (1, 0.35, 0.3, 1) if low else WHITE
        self.health_bar.set(p.health / max(p.max_health, 1.0), col if col != self._health_col else None)
        self._health_col = col
        self.armor.setText((f"[A] {int(p.armor)}" + ("  [H]" if p.helmet else "")) if p.armor > 0 else "")
        if p.armor > 0 and self.visible:
            self.armor_bar.show()
            self.armor_bar.set(p.armor / 100.0)
        else:
            self.armor_bar.hide()
        c = spec.char.pos if spec is not None else g.player.char.pos
        loc = g.level.callout_at(c.x, c.y)
        site = g.level.zone_at((c.x, c.y, c.z))
        self.location.setText(loc + (f"   [bomb site {site['name']}]" if site else ""))
        w = g.weapons
        mag, res = w.ammo_text()
        name = w.current_name()
        if spec is not None:
            sws = spec.weapons.inv.current()
            mag, res = (f"{sws.ammo}", f"/ {sws.reserve}") if sws is not None and sws.d.magazine > 0 else ("", "")
            name = sws.d.name if sws is not None else ("Breach charge" if spec.weapons.inv.slot == "bomb" else "")
        self.ammo.setText(mag)
        self.reserve.setText(res)
        self.weapon.setText(name)
        ws = w.inv.current()
        status = ""
        if spec is not None:
            pass
        elif not p.alive:
            status = "YOU DIED" if g.director is not None else "YOU DIED - respawning..."
        elif ws is not None and ws.reloading:
            status = "reloading"
        elif ws is not None and ws.d.magazine and ws.ammo == 0:
            status = "[R] reload"
        else:
            look = g.pickups.look_at(w.eye(), g.camera.getQuat(g.render).getForward())
            if look is not None:
                status = f"[F] {look.label or look.kind}" if look.kind != "weapon" else \
                    f"[F] pick up {g.weapon_db.weapons[look.item if isinstance(look.item, str) else look.item.d.key].name}"
        self.status.setText(status)
        if self._msg_t > 0:
            self._msg_t -= dt
            if self._msg_t <= 0:
                self.message.setText("")
        if self._hit_t > 0:
            self._hit_t -= dt
            self.hitmarker.setAlphaScale(min(self._hit_t / 0.12, 1.0))
            s = 1.0 + (0.4 if self._hit_kill else 0.15) * max(self._hit_t, 0)
            self.hitmarker.setScale(s)
            if self._hit_t <= 0:
                self.hitmarker.hide()
        if self._blind_t > 0:
            self._blind_t -= dt
            t = self._blind_t / max(self._blind_dur, 1e-3)
            # full white, then fades out over the last 60%
            a = self._blind_strength * min(1.0, t / 0.6)
            self.blind_card.setColor(1, 1, 1, a)
            if self._blind_t <= 0:
                self.blind_card.hide()
        if self._dmg_t > 0:
            self._dmg_t -= dt * 1.6
            self.damage_card.setAlphaScale(max(self._dmg_t, 0))
            if self._dmg_t <= 0:
                self.damage_card.hide()
        if self.arcs:
            pl = g.player
            pos = pl.char.pos
            keep = []
            for arc in self.arcs:
                arc[2] -= dt
                if arc[2] <= 0 or not pl.damageable.alive:
                    arc[0].removeNode()
                    continue
                n, src, left, amount = arc
                n.setR(arc_angle(src.x - pos.x, src.y - pos.y, pl.yaw))
                n.setAlphaScale(min(left / 0.5, 1.0) * min(0.55 + amount / 60.0, 1.0))
                keep.append(arc)
            self.arcs = keep
