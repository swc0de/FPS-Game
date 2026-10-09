"""Bot debug overlay (console ``overlay``): what every bot is thinking, over its head.

Each living bot gets a label above its head - name and role, the running
action with its score and the two runners-up, the task, and the fact behind
what it is doing (source and age) - and lines from its feet to the spot it
is going to or holding (white) and to the point it is aiming at (yellow;
red while it sees its target). Legacy bots show their mode and task.

Drawn in the 3D scene, refreshed four times a second, nothing when off.
"""
from __future__ import annotations

from panda3d.core import LineSegs, NodePath, Point3, TextNode

REFRESH = 0.25


class BotOverlay:
    def __init__(self, game):
        self.game = game
        self.root = game.render.attachNewNode("bot_overlay")
        self.root.setLightOff(1)
        self.root.setShaderOff(1)
        self.root.setDepthTest(False)
        self.root.setDepthWrite(False)
        self.root.setBin("fixed", 60)
        self.labels: dict[int, NodePath] = {}
        self.lines: NodePath | None = None
        self.enabled = False
        self._t = 0.0

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        if not self.enabled:
            self.root.hide()
        else:
            self.root.show()
            self._t = 0.0
        return self.enabled

    def destroy(self) -> None:
        self.root.removeNode()

    def update(self, dt: float) -> None:
        if not self.enabled:
            return
        self._t -= dt
        if self._t > 0.0:
            return
        self._t = REFRESH
        d = self.game.director
        if d is None:
            return
        seen = set()
        segs = LineSegs("bot_overlay_lines")
        segs.setThickness(1.5)
        for b in d.bots:
            if not (b.active and b.alive):
                continue
            seen.add(id(b))
            label = self.labels.get(id(b))
            if label is None:
                tn = TextNode(f"label_{b.name}")
                tn.setAlign(TextNode.ACenter)
                tn.setTextColor(1, 1, 1, 1)
                tn.setShadow(0.06, 0.06)
                tn.setShadowColor(0, 0, 0, 1)
                label = self.root.attachNewNode(tn)
                label.setBillboardPointEye()
                label.setScale(0.16)
                self.labels[id(b)] = label
            label.node().setText(self._text(b))
            head = b.head_pos()
            label.setPos(head.x, head.y, head.z + 0.55)
            label.show()
            feet = b.position() + Point3(0, 0, 0.05)
            t = b.brain.task
            if t.pos is not None:
                segs.setColor(1, 1, 1, 0.6)
                segs.moveTo(feet)
                segs.drawTo(t.pos + Point3(0, 0, 0.05))
            aim = self._aim_point(b)
            if aim is not None:
                c = b.brain.ctx.target if hasattr(b.brain, "ctx") else None
                segs.setColor(*((1, 0.25, 0.2, 0.9) if c is not None and c.seen else (1, 0.85, 0.2, 0.7)))
                segs.moveTo(b.eye())
                segs.drawTo(aim)
        for k in [k for k in self.labels if k not in seen]:
            self.labels[k].hide()
        if self.lines is not None:
            self.lines.removeNode()
        self.lines = self.root.attachNewNode(segs.create())

    @staticmethod
    def _aim_point(b):
        brain = b.brain
        c = getattr(brain, "ctx", None)
        if c is not None and c.target is not None and c.target.seen:
            p = c.target.pos
            return Point3(p.x, p.y, p.z + 1.45)
        ap = getattr(getattr(brain, "aim_policy", None), "point", None)
        if ap is not None:
            return Point3(ap)
        return None

    @staticmethod
    def _text(b) -> str:
        brain = b.brain
        if getattr(brain, "ai", "legacy") != "v2":
            t = brain.task
            return f"{b.name} [legacy]\n{brain.mode} / {t.kind}{':' + t.tag if t.tag else ''}"
        role = brain.team.role_of(b) or "-"
        top = brain.scores[:3]
        head = f"{top[0][0]} {top[0][1]:.2f}" if top else brain.mode
        rest = "  ".join(f"{n} {s:.2f}" for n, s in top[1:])
        t = brain.task
        lines = [f"{b.name} [{role}] {b.damageable.health:.0f}hp", head + (f"   ({rest})" if rest else ""),
                 f"task {t.kind}{':' + t.tag if t.tag else ''}{' (there)' if brain.arrived else ''}"]
        f = brain.ctx.fact or brain.ctx.lost
        if brain.ctx.trade is not None:
            f = brain.ctx.trade[0]
        if f is not None:
            lines.append(f"fact {f.source} {b.now - f.time:.1f}s r{f.radius:.0f}")
        return "\n".join(lines)
