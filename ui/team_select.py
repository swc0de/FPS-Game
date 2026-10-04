"""Side selection shown when a match starts."""
from __future__ import annotations

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.DirectGui import DirectFrame
from panda3d.core import TextNode

from ui import widgets as W


class TeamSelect:
    def __init__(self, game, on_choose):
        self.game = game
        self.on_choose = on_choose
        rules = game.director.rules
        practice = rules.get("practice", {})
        self.backdrop = DirectFrame(parent=game.render2d, frameColor=(0, 0, 0, 0.45), frameSize=(-1, 1, -1, 1),
                                    sortOrder=70)
        self.root = DirectFrame(parent=game.aspect2d, frameColor=W.PANEL, frameSize=(-1.0, 1.0, -0.55, 0.55),
                                state=DGG.NORMAL, sortOrder=75)
        W.label(self.root, "CHOOSE YOUR SIDE", (0, 0.42), scale=0.065, fg=W.ACCENT, align=TextNode.ACenter)
        for side, x, desc in (("attack", -0.48, "Plant the breach charge at\nsite A (armory) or B (motor pool)."),
                              ("defend", 0.48, "Hold both sites. Defuse the\nbreach charge if it is planted.")):
            t = rules["teams"][side]
            b = W.button(self.root, t["name"].upper(), (x, 0.18), self.choose, width=0.86, height=0.16, scale=0.06,
                         extra=(side,))
            b["text_fg"] = tuple(t["color"]) + (1,)
            W.label(self.root, "ATTACKERS" if side == "attack" else "DEFENDERS", (x, 0.04), scale=0.032,
                    fg=W.DIM, align=TextNode.ACenter)
            W.label(self.root, desc, (x, -0.06), scale=0.034, fg=W.TEXT, align=TextNode.ACenter)
        W.label(self.root, f"Opponents: {practice.get('opponents', 5)} stand-ins holding positions "
                           "(AI bots arrive in Milestone 5).", (0, -0.3), scale=0.03, fg=W.DIM, align=TextNode.ACenter)
        W.label(self.root, "Console (` or F10): plant A, money 16000, bots 3, team defend, help",
                (0, -0.38), scale=0.028, fg=W.DIM, align=TextNode.ACenter)
        self.game.input.set_captured(False)

    def choose(self, side: str) -> None:
        self.destroy()
        self.on_choose(side)

    def destroy(self) -> None:
        self.root.destroy()
        self.backdrop.destroy()
