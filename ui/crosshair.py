"""Simple configurable crosshair drawn in pixel space (dynamic spread comes
with the weapon system in milestone 2)."""
from __future__ import annotations

from panda3d.core import CardMaker, NodePath


class Crosshair:
    def __init__(self, app, cfg: dict):
        self.app = app
        self.root = app.pixel2d.attachNewNode("crosshair")
        self.cfg = cfg
        self.spread = 0.0
        self._parts: list[NodePath] = []
        self.rebuild()

    def rebuild(self) -> None:
        for p in self._parts:
            p.removeNode()
        self._parts = []
        c = self.cfg
        size, gap, thick = c.get("size", 6), c.get("gap", 3), c.get("thickness", 2)
        color = c.get("color", [0.3, 1.0, 0.4, 1.0])
        gap = gap + self.spread
        rects = [(-thick / 2, thick / 2, gap, gap + size), (-thick / 2, thick / 2, -gap - size, -gap),
                 (gap, gap + size, -thick / 2, thick / 2), (-gap - size, -gap, -thick / 2, thick / 2)]
        if c.get("dot"):
            rects.append((-thick / 2, thick / 2, -thick / 2, thick / 2))
        for (l, r, b, t) in rects:
            outline = CardMaker("xh_outline")
            outline.setFrame(l - 1, r + 1, b - 1, t + 1)
            o = self.root.attachNewNode(outline.generate())
            o.setColor(0, 0, 0, 0.5)
            o.setBin("fixed", 0)
            bar = CardMaker("xh")
            bar.setFrame(l, r, b, t)
            p = self.root.attachNewNode(bar.generate())
            p.setColor(*color)
            p.setBin("fixed", 1)
            self._parts += [o, p]
        self.root.setTransparency(True)
        self.center()

    def center(self, *_):
        win = self.app.win
        if win is None:
            return
        # pixel2d has +Z up with origin top-left; Y is unused for 2D
        self.root.setPos(win.getXSize() / 2, 0, -win.getYSize() / 2)

    def set_visible(self, v: bool) -> None:
        self.root.show() if v else self.root.hide()
