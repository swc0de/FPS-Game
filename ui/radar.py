"""Radar (minimap) and compass strip.

The radar image is rasterised once per map from the navmesh: every walkable
cell becomes a floor pixel shaded by its height, the cells around the floor
become walls, destructible panels are drawn in brown, bomb sites are tinted
red and floors with a tunnel underneath are tinted blue. The HUD shows the
image on a square card whose texture transform centres it on the viewed
player and, with "rotate" on, turns it so their heading points up. Markers
(teammates, enemy intel, the bomb, site letters) are small cards placed with
the same mapping (``world_to_radar``).

The compass is a strip of headings under the score bar with markers for the
bomb sites, pings and the bomb.
"""
from __future__ import annotations

import math

import numpy as np
from direct.gui.DirectGui import DirectFrame
from panda3d.core import (CardMaker, LMatrix4f, SamplerState, TextNode, Texture, TextureStage,
                          TransformState, TransparencyAttrib)

from ui import widgets as W

FLOOR = np.array((0.34, 0.37, 0.35), np.float32)
UPPER = np.array((0.52, 0.55, 0.50), np.float32)      # floors ~4 m up (catwalks, upper storeys)
LOWER = np.array((0.20, 0.24, 0.32), np.float32)      # below ground only (tunnels)
TUNNEL = np.array((0.30, 0.38, 0.52), np.float32)     # ground floor with a tunnel underneath
WALL = np.array((0.80, 0.82, 0.76), np.float32)
PANEL = np.array((0.72, 0.46, 0.22), np.float32)
SITE = np.array((0.85, 0.22, 0.16), np.float32)
VOID_ALPHA = 0.55

ENEMY = (1.0, 0.25, 0.2, 1.0)
BOMB = (1.0, 0.55, 0.15, 1.0)
SHADOW = (0, 0, 0, 0.85)


# ----------------------------------------------------------------- image
def radar_image(nav, zones=(), panels=()) -> np.ndarray:
    """RGBA float image [ny, nx, 4] (row 0 = south edge), one pixel per navmesh cell."""
    nx, ny, cs = nav.nx, nav.ny, nav.cfg.cell
    ncols = nx * ny
    cols, z = nav.node_col, nav.node_z
    count = np.bincount(cols, minlength=ncols)
    ztop = np.full(ncols, -np.inf)
    np.maximum.at(ztop, cols, z)
    zlow = np.full(ncols, np.inf)
    np.minimum.at(zlow, cols, z)
    walk = count > 0
    img = np.zeros((ncols, 4), np.float32)
    img[:, 3] = VOID_ALPHA
    # floors: brighter the higher they are, tunnels and overlaps tinted blue
    t = np.clip(ztop[walk] / 4.0, 0.0, 1.0)[:, None]
    img[walk, :3] = FLOOR * (1 - t) + UPPER * t
    under = walk & (ztop < -0.6)
    img[under, :3] = LOWER
    overlap = walk & (count > 1) & (ztop - zlow > 2.0) & ~under
    img[overlap, :3] = img[overlap, :3] * 0.4 + TUNNEL * 0.6
    img[walk, 3] = 0.92
    img = img.reshape(ny, nx, 4)
    walk2 = walk.reshape(ny, nx)
    # walls: every non-walkable cell touching the floor
    near = np.zeros_like(walk2)
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            near |= np.roll(np.roll(walk2, dj, axis=0), di, axis=1)
    wall = near & ~walk2
    img[wall, :3] = WALL
    img[wall, 3] = 0.95
    # bomb sites
    jj, ii = np.mgrid[0:ny, 0:nx]
    xs = nav.x0 + (ii + 0.5) * cs
    ys = nav.y0 + (jj + 0.5) * cs
    for zone in zones:
        if zone.get("kind") != "bombsite":
            continue
        (x0, y0), (x1, y1) = zone["min"][:2], zone["max"][:2]
        m = walk2 & (xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1)
        img[m, :3] = img[m, :3] * 0.6 + SITE * 0.4
    # destructible panels: oriented footprints, at least one pixel thick
    for p in panels:
        if getattr(p, "kind", "wall") != "wall":
            continue
        (cx, cy, _), (sx, sy, _) = p.center, p.size
        h = math.radians(p.hpr[0] if p.hpr else 0.0)
        hx, hy = max(sx * 0.5, cs * 0.6), max(sy * 0.5, cs * 0.6)
        r = math.hypot(hx, hy)
        i0, i1 = int((cx - r - nav.x0) / cs), int((cx + r - nav.x0) / cs) + 1
        j0, j1 = int((cy - r - nav.y0) / cs), int((cy + r - nav.y0) / cs) + 1
        i0, j0, i1, j1 = max(i0, 0), max(j0, 0), min(i1, nx), min(j1, ny)
        if i0 >= i1 or j0 >= j1:
            continue
        dx = xs[j0:j1, i0:i1] - cx
        dy = ys[j0:j1, i0:i1] - cy
        lx = dx * math.cos(h) + dy * math.sin(h)
        ly = -dx * math.sin(h) + dy * math.cos(h)
        m = (np.abs(lx) <= hx) & (np.abs(ly) <= hy)
        img[j0:j1, i0:i1][m] = (*PANEL, 0.95)
    return img


def make_texture(img: np.ndarray, name: str = "radar") -> Texture:
    h, w = img.shape[:2]
    tex = Texture(name)
    tex.setup2dTexture(w, h, Texture.T_unsigned_byte, Texture.F_rgba8)
    rgba = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    tex.setRamImageAs(np.ascontiguousarray(rgba).tobytes(), "RGBA")
    tex.setWrapU(SamplerState.WM_border_color)
    tex.setWrapV(SamplerState.WM_border_color)
    tex.setBorderColor((0, 0, 0, 0))
    tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    tex.setAnisotropicDegree(4)
    return tex


# --------------------------------------------------------------- mapping
def world_to_radar(dx: float, dy: float, yaw_deg: float, half_width: float) -> tuple[float, float]:
    """World offset from the radar centre -> card coordinates (-1..1 across the card)."""
    h = math.radians(yaw_deg)
    c, s = math.cos(h), math.sin(h)
    return (c * dx + s * dy) / half_width, (-s * dx + c * dy) / half_width


def tex_transform(px: float, py: float, yaw_deg: float, half_width: float, origin, dims):
    """Affine texture-coordinate map (a00, a01, b0, a10, a11, b1) for the radar card:
    card UV (0..1) -> image UV, centred on (px, py) and turned by yaw."""
    h = math.radians(yaw_deg)
    c, s = math.cos(h), math.sin(h)
    wx, wy = dims
    hw = half_width
    return (2 * hw * c / wx, -2 * hw * s / wx, (px - origin[0]) / wx - hw * (c - s) / wx,
            2 * hw * s / wy, 2 * hw * c / wy, (py - origin[1]) / wy - hw * (s + c) / wy)


def bearing(yaw_deg: float) -> float:
    """Compass bearing (0 = north = +Y, 90 = east = +X) of a Panda heading."""
    return (-yaw_deg) % 360.0


def bearing_to(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


def yaw_of(agent, game) -> float:
    if agent is getattr(game.player, "agent", None):
        return game.player.yaw
    aim = getattr(agent, "aim", None)
    if aim is not None:
        return aim.yaw
    f = agent.forward()
    return math.degrees(math.atan2(-f.x, f.y))


def _shape_texture(name: str, kind: str, size: int = 64) -> Texture:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    u = (x + 0.5) / size * 2 - 1
    v = (y + 0.5) / size * 2 - 1
    if kind == "arrow":
        # chevron pointing up (+v): inside the triangle, minus a notch at the back
        inside = (v > -0.75) & (np.abs(u) < (0.85 - v) * 0.55) & ~((v < -0.2) & (np.abs(u) < (-0.2 - v) * 0.9))
        edge = 1.0
        a = inside.astype(np.float32) * edge
    elif kind == "ring":
        r = np.sqrt(u * u + v * v)
        a = np.clip(1 - np.abs(r - 0.72) / 0.18, 0, 1)
    else:   # dot
        r = np.sqrt(u * u + v * v)
        a = np.clip((0.92 - r) / 0.12, 0, 1)
    img = np.ones((size, size, 4), np.float32)
    img[..., 3] = a
    tex = Texture(name)
    tex.setup2dTexture(size, size, Texture.T_unsigned_byte, Texture.F_rgba8)
    tex.setRamImageAs((img * 255).astype(np.uint8).tobytes(), "RGBA")
    tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    return tex


class _Pool:
    """Reused marker cards of one shape; ``begin`` / ``take`` / ``end`` every frame."""

    def __init__(self, parent, tex: Texture | None, size: float, name: str):
        self.parent, self.tex, self.size, self.name = parent, tex, size, name
        self.items = []
        self.used = 0

    def begin(self) -> None:
        self.used = 0

    def take(self):
        if self.used == len(self.items):
            cm = CardMaker(self.name)
            s = self.size
            cm.setFrame(-s, s, -s, s)
            n = self.parent.attachNewNode(cm.generate())
            if self.tex is not None:
                n.setTexture(self.tex)
            n.setTransparency(TransparencyAttrib.MAlpha)
            self.items.append(n)
        n = self.items[self.used]
        self.used += 1
        n.show()
        return n

    def end(self) -> None:
        for n in self.items[self.used:]:
            n.hide()


# ------------------------------------------------------------------ radar
class Radar:
    R = 0.27               # card half size (aspect2d units)
    RANGE = 28.0           # metres from the centre to the card edge at zoom 1

    def __init__(self, game, director):
        self.game = game
        self.director = director
        nav = game.navmesh()
        level = game.level
        self.origin = (nav.x0, nav.y0)
        self.dims = (nav.nx * nav.cfg.cell, nav.ny * nav.cfg.cell)
        self.tex = make_texture(radar_image(nav, level.zones, level.panel_specs))
        R = self.R
        self.root = game.a2dTopLeft.attachNewNode("radar")
        self.root.setPos(R + 0.04, 0, -R - 0.04)
        self.frame = DirectFrame(parent=self.root, frameColor=(0.02, 0.025, 0.03, 0.7),
                                 frameSize=(-R - 0.008, R + 0.008, -R - 0.008, R + 0.008))
        cm = CardMaker("radar_map")
        cm.setFrame(-R, R, -R, R)
        self.card = self.root.attachNewNode(cm.generate())
        self.card.setTexture(self.tex)
        self.card.setTransparency(TransparencyAttrib.MAlpha)
        self.ts = TextureStage.getDefault()
        self.marks = self.root.attachNewNode("marks")
        self.arrows = _Pool(self.marks, _shape_texture("radar_arrow", "arrow"), 0.022, "arrow")
        self.dots = _Pool(self.marks, _shape_texture("radar_dot", "dot"), 0.014, "dot")
        self.rings = _Pool(self.marks, _shape_texture("radar_ring", "ring"), 0.03, "ring")
        self.squares = _Pool(self.marks, None, 0.011, "square")
        self.texts = []
        self._texts_used = 0
        self.north = W.Text(text="N", parent=self.root, scale=0.03, fg=W.ACCENT, shadow=SHADOW, mayChange=True)
        self.sites = [(z["name"], ((z["min"][0] + z["max"][0]) * 0.5, (z["min"][1] + z["max"][1]) * 0.5))
                      for z in level.zones if z.get("kind") == "bombsite"]
        self.visible = True

    @property
    def hud_cfg(self) -> dict:
        return self.game.settings.data["gameplay"]["hud"]

    def destroy(self) -> None:
        self.root.removeNode()

    def set_visible(self, v: bool) -> None:
        self.visible = v
        self.root.show() if v and self.hud_cfg.get("minimap", True) else self.root.hide()

    # ---------------------------------------------------------------- view
    def viewer(self):
        """(agent or None, x, y, yaw) the radar is centred on."""
        g, d = self.game, self.director
        sp = d.spectator
        if sp.active and not sp.free and sp.target is not None:
            t = sp.target
            p = t.position()
            return t, p.x, p.y, yaw_of(t, g)
        if sp.active:
            p = g.camera.getPos(g.render)
            return None, p.x, p.y, g.camera.getH(g.render)
        a = d.player_agent
        p = a.position()
        return a, p.x, p.y, g.player.yaw

    def _text(self, text: str, u: float, v: float, color, scale: float = 0.032):
        if self._texts_used == len(self.texts):
            self.texts.append(W.Text(text="", parent=self.marks, scale=scale, fg=W.TEXT, shadow=SHADOW,
                                     mayChange=True))
        t = self.texts[self._texts_used]
        self._texts_used += 1
        t.setText(text)
        t.setFg(color)
        t.setPos(u * self.R, v * self.R - scale * 0.35)
        t.show()
        return t

    def _place(self, pool: _Pool, u: float, v: float, color, roll: float = 0.0, scale: float = 1.0):
        n = pool.take()
        n.setPos(u * self.R, 0, v * self.R)
        n.setColor(*color)
        n.setR(roll)
        n.setScale(scale)
        return n

    @staticmethod
    def _clamp(u: float, v: float, edge: float = 0.93) -> tuple[float, float, bool]:
        m = max(abs(u), abs(v))
        if m <= edge:
            return u, v, True
        return u * edge / m, v * edge / m, False

    def update(self, dt: float) -> None:
        cfg = self.hud_cfg
        show = self.visible and cfg.get("minimap", True)
        if not show:
            self.root.hide()
            return
        self.root.show()
        g, d = self.game, self.director
        viewed, cx, cy, yaw = self.viewer()
        ryaw = yaw if cfg.get("minimap_rotate", True) else 0.0
        hw = self.RANGE / max(float(cfg.get("minimap_zoom", 1.0)), 0.25)
        a00, a01, b0, a10, a11, b1 = tex_transform(cx, cy, ryaw, hw, self.origin, self.dims)
        m = LMatrix4f(a00, a10, 0, 0,
                      a01, a11, 0, 0,
                      0, 0, 1, 0,
                      b0, b1, 0, 1)
        self.card.setTexTransform(self.ts, TransformState.makeMat(m))
        nu, nv = world_to_radar(0.0, 1.0, ryaw, 1.0)
        n = max(abs(nu), abs(nv))
        self.north.setPos(nu / n * self.R * 0.9, nv / n * self.R * 0.9 - 0.01)
        for pool in (self.arrows, self.dots, self.rings, self.squares):
            pool.begin()
        self._texts_used = 0
        now = g.loop.time
        rules = d.rules

        def to_card(x, y):
            return world_to_radar(x - cx, y - cy, ryaw, hw)

        # bomb sites
        for name, (sx, sy) in self.sites:
            u, v, _ = self._clamp(*to_card(sx, sy), edge=0.88)
            self._text(name, u, v, (1.0, 0.55, 0.45, 0.95), 0.04)
        side = viewed.side if viewed is not None else None
        omniscient = d.spectate_only or viewed is None
        # players
        for a in d.match.participants:
            if a.side not in ("attack", "defend") or not getattr(a, "active", True):
                continue
            friendly = omniscient or a.side == side
            if not friendly:
                continue
            col = tuple(rules["teams"][a.side]["color"]) + (1.0,)
            p = a.position()
            u, v, inside = self._clamp(*to_card(p.x, p.y))
            if not a.alive:
                if inside:
                    self._text("x", u, v, (col[0] * 0.6, col[1] * 0.6, col[2] * 0.6, 0.8), 0.04)
                continue
            if a is viewed:
                continue
            roll = -(yaw_of(a, g) - ryaw)
            self._place(self.arrows, u, v, col, roll, 0.8 if inside else 0.6)
        # enemy intel: pings and the team's recent sightings
        if not omniscient:
            seen = {}
            tb = d.brain_of(side) if side else None
            if tb is not None:
                for t, eid, pos in tb.reports[-30:]:
                    if now - t < 3.0:
                        seen[eid] = (pos, t)
            if g.tactical is not None:
                for p in g.tactical.team_pings(side):
                    if p.agent is not None and p.until > g.tactical.now:
                        seen[id(p.agent)] = (p.where(), now)
            for pos, t in seen.values():
                u, v, inside = self._clamp(*to_card(pos.x, pos.y))
                if inside:
                    fade = 1.0 - max(now - t - 1.5, 0.0) / 1.5
                    self._place(self.dots, u, v, (ENEMY[0], ENEMY[1], ENEMY[2], max(fade, 0.2)))
        # bomb
        b = d.bomb
        bpos = None
        if b.state == "carried" and b.carrier is not None and (omniscient or side == "attack"):
            if b.carrier is not viewed:
                bpos = b.carrier.position()
        elif b.state == "dropped" and (omniscient or side == "attack"):
            bpos = b.pos
        elif b.state == "planted":
            bpos = b.pos
        if bpos is not None:
            u, v, _ = self._clamp(*to_card(bpos.x, bpos.y))
            blink = b.state != "planted" or (now * 2.0) % 1.0 < 0.6
            if blink:
                self._place(self.squares, u, v, BOMB, 45.0)
        # the viewer: arrow in the middle
        if viewed is not None or d.spectator.active:
            col = (1, 1, 1, 1) if viewed is None or viewed is d.player_agent else \
                tuple(rules["teams"][viewed.side]["color"]) + (1.0,)
            self._place(self.arrows, 0.0, 0.0, col, -(yaw - ryaw), 1.1)
            self._place(self.rings, 0.0, 0.0, (1, 1, 1, 0.25), 0.0, 1.0)
        for pool in (self.arrows, self.dots, self.rings, self.squares):
            pool.end()
        for t in self.texts[self._texts_used:]:
            t.hide()


# ---------------------------------------------------------------- compass
class Compass:
    """Heading strip: ticks every 15 degrees, letters at the cardinal points,
    markers for the bomb sites, pings and the bomb."""

    HALF_FOV = 50.0       # degrees from the centre to each end
    WIDTH = 0.42          # half width (aspect2d units)
    Y = -0.255

    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.root = game.a2dTopCenter.attachNewNode("compass")
        self.root.setPos(0, 0, self.Y)
        W2 = self.WIDTH
        DirectFrame(parent=self.root, frameColor=(0.02, 0.025, 0.03, 0.45), frameSize=(-W2, W2, -0.032, 0.032))
        DirectFrame(parent=self.root, frameColor=W.ACCENT, frameSize=(-0.003, 0.003, 0.032, 0.046))
        self.ticks = []
        for _ in range(int(self.HALF_FOV * 2 / 15) + 2):
            f = DirectFrame(parent=self.root, frameColor=(0.9, 0.92, 0.88, 0.7),
                            frameSize=(-0.0018, 0.0018, 0.004, 0.03))
            t = W.Text(text="", parent=self.root, scale=0.026, fg=W.TEXT, shadow=SHADOW, mayChange=True)
            self.ticks.append((f, t))
        self.heading = W.Text(text="", parent=self.root, pos=(W2 + 0.012, -0.01), scale=0.028, fg=W.ACCENT,
                              shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
        self.marks = []
        self.visible = True

    @property
    def hud_cfg(self) -> dict:
        return self.game.settings.data["gameplay"]["hud"]

    def set_visible(self, v: bool) -> None:
        self.visible = v
        self.root.show() if v and self.hud_cfg.get("compass", True) else self.root.hide()

    def _x(self, b: float, centre: float) -> float | None:
        d = (b - centre + 180.0) % 360.0 - 180.0
        if abs(d) > self.HALF_FOV:
            return None
        return d / self.HALF_FOV * self.WIDTH

    def _mark(self, i: int, text: str, x: float, color):
        while len(self.marks) <= i:
            self.marks.append(W.Text(text="", parent=self.root, scale=0.03, fg=W.TEXT, shadow=SHADOW,
                                     mayChange=True))
        t = self.marks[i]
        t.setText(text)
        t.setFg(color)
        t.setPos(x, -0.066)
        t.show()

    def update(self, dt: float, viewer) -> None:
        if not (self.visible and self.hud_cfg.get("compass", True)):
            self.root.hide()
            return
        self.root.show()
        viewed, cx, cy, yaw = viewer
        centre = bearing(yaw)
        self.heading.setText(f"{int(round(centre)) % 360:03d}")
        first = math.floor((centre - self.HALF_FOV) / 15.0) * 15
        names = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
        for k, (f, t) in enumerate(self.ticks):
            b = first + k * 15
            x = self._x(b % 360, centre)
            if x is None:
                f.hide()
                t.setText("")
                continue
            label = names.get(int(b) % 360, "")
            f.show()
            f.setPos(x, 0, 0)
            f.setScale(1, 1, 1.0 if label else 0.5)
            f.setZ(0 if label else 0.015)
            t.setText(label)
            t.setPos(x, -0.024)
            t.setFg(W.ACCENT if label == "N" else W.TEXT)
        # markers
        i = 0
        d, g = self.director, self.game
        marks = []
        for z in g.level.zones:
            if z.get("kind") == "bombsite":
                sx = (z["min"][0] + z["max"][0]) * 0.5
                sy = (z["min"][1] + z["max"][1]) * 0.5
                marks.append((z["name"], sx, sy, (1.0, 0.55, 0.45, 1)))
        side = viewed.side if viewed is not None else None
        if g.tactical is not None and side and not d.spectate_only:
            for p in g.tactical.team_pings(side)[-6:]:
                if p.until > g.tactical.now:
                    w = p.where()
                    marks.append(("o" if p.agent is not None else "+", w.x, w.y, ENEMY if p.agent else W.ACCENT))
        if d.bomb.state == "planted":
            marks.append(("[*]", d.bomb.pos.x, d.bomb.pos.y, BOMB))
        for text, mx, my, col in marks:
            if abs(mx - cx) + abs(my - cy) < 1.0:
                continue
            x = self._x(bearing_to(mx - cx, my - cy), centre)
            if x is None:
                continue
            self._mark(i, text, x, col)
            i += 1
        for t in self.marks[i:]:
            t.hide()
