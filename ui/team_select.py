"""Side selection shown when a match starts: pick a side and the bot
difficulty, or watch a bot match."""
from __future__ import annotations

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.DirectGui import DirectFrame
from panda3d.core import TextNode

from ui import widgets as W

LEVELS = ("easy", "normal", "hard", "expert")


class TeamSelect:
    def __init__(self, game, on_choose):
        self.game = game
        self.on_choose = on_choose
        d = game.director
        rules = d.rules
        self.backdrop = DirectFrame(parent=game.render2d, frameColor=(0, 0, 0, 0.45), frameSize=(-1, 1, -1, 1),
                                    sortOrder=70)
        self.root = DirectFrame(parent=game.aspect2d, frameColor=W.PANEL, frameSize=(-1.0, 1.0, -0.62, 0.55),
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
        if d.use_bots:
            W.label(self.root, f"You + {d.n_teammates} bots  vs  {d.n_opponents} bots", (0, -0.24), scale=0.034,
                    fg=W.TEXT, align=TextNode.ACenter)
            W.label(self.root, "Bot difficulty:", (-0.62, -0.335), scale=0.032, fg=W.DIM)
            self.level_buttons = {}
            for i, lvl in enumerate(LEVELS):
                self.level_buttons[lvl] = W.button(self.root, lvl.upper(), (-0.12 + i * 0.25, -0.325),
                                                   self.set_level, width=0.23, height=0.065, scale=0.03,
                                                   extra=(lvl,), active=lvl == d.difficulty)
            W.button(self.root, "WATCH A BOT MATCH", (0, -0.45), self.choose, width=0.6, height=0.07, scale=0.032,
                     extra=("spectate",))
        else:
            W.label(self.root, f"Practice: {d.n_opponents} stand-ins holding positions (they do not shoot back).",
                    (0, -0.3), scale=0.03, fg=W.DIM, align=TextNode.ACenter)
        W.label(self.root, "Console (` or F10): difficulty hard, bots 5 4, plant A, money 16000, help",
                (0, -0.56), scale=0.028, fg=W.DIM, align=TextNode.ACenter)
        self.game.input.set_captured(False)

    def set_level(self, level: str) -> None:
        d = self.game.director
        d.set_difficulty(level)
        for lvl, b in self.level_buttons.items():
            W.set_active(b, lvl == level)
        self.game.settings.data.setdefault("gameplay", {})["bot_difficulty"] = level
        self.game.settings.save()

    def choose(self, side: str) -> None:
        self.destroy()
        self.on_choose(side)

    def destroy(self) -> None:
        self.root.destroy()
        self.backdrop.destroy()
