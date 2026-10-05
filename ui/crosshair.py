"""Customisable crosshair (pixel space). The gap follows the weapon's real
inaccuracy cone (``set_spread``), so it widens when moving/jumping/spraying
exactly as the bullet spread does."""
from __future__ import annotations

from panda3d.core import CardMaker, NodePath


class Crosshair:
    def __init__(self, app, cfg: dict, bin_name: str = "fixed", bin_sort: int = 0):
        self.app = app
        self.bin = (bin_name, bin_sort)
        self.root = app.pixel2d.attachNewNode("crosshair")
        self.root.setTransparency(True)
        self.cfg = cfg
        self.spread_px = 0.0
        self.bars: list[tuple[NodePath, tuple[int, int]]] = []
        self.dot = None
        self.rebuild()

    def rebuild(self) -> None:
        for np_, _ in self.bars:
            np_.removeNode()
        if self.dot is not None:
            self.dot.removeNode()
            self.dot = None
        self.bars = []
        c = self.cfg
        size, thick = float(c.get("size", 6)), float(c.get("thickness", 2))
        color = c.get("color", [0.3, 1.0, 0.4, 1.0])
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            bar = self.root.attachNewNode("bar")
            if dx == 0:
                l, r, b, t = -thick / 2, thick / 2, 0, size
            else:
                l, r, b, t = 0, size, -thick / 2, thick / 2
            if dx < 0:
                l, r = -r, -l
            if dy < 0:
                b, t = -t, -b
            outline = CardMaker("o")
            outline.setFrame(l - 1, r + 1, b - 1, t + 1)
            o = bar.attachNewNode(outline.generate())
            o.setColor(0, 0, 0, 0.5)
            o.setBin(self.bin[0], self.bin[1])
            cm = CardMaker("b")
            cm.setFrame(l, r, b, t)
            p = bar.attachNewNode(cm.generate())
            p.setColor(*color)
            p.setBin(self.bin[0], self.bin[1] + 1)
            self.bars.append((bar, (dx, dy)))
        if c.get("dot"):
            cm = CardMaker("dot")
            cm.setFrame(-thick / 2, thick / 2, -thick / 2, thick / 2)
            self.dot = self.root.attachNewNode(cm.generate())
            self.dot.setColor(*color)
            self.dot.setBin(self.bin[0], self.bin[1] + 1)
        self.center()
        self._place()

    def _place(self) -> None:
        gap = float(self.cfg.get("gap", 3)) + (self.spread_px if self.cfg.get("dynamic", True) else 0.0)
        for bar, (dx, dy) in self.bars:
            bar.setPos(dx * gap, 0, dy * gap)

    def set_spread_px(self, px: float) -> None:
        px = min(px, 120.0)
        if abs(px - self.spread_px) > 0.25:
            self.spread_px = px
            self._place()

    def center(self, *_):
        win = self.app.win
        if win is None:
            return
        # pixel2d: +X right, +Z up, origin at the top-left corner
        self.root.setPos(win.getXSize() // 2, 0, -(win.getYSize() // 2))

    def set_visible(self, v: bool) -> None:
        self.root.show() if v else self.root.hide()
