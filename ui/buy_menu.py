"""Buy menu (B in the buy zone during buy time).

Keyboard: a number picks the category, the next number buys an item
(CS 1.6 style); or click with the mouse. Right-click an item bought this
round to sell it back while buy time lasts.
"""
from __future__ import annotations

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.DirectGui import DirectFrame
from direct.showbase.DirectObject import DirectObject
from panda3d.core import TextNode

from gameplay.shop import CATEGORIES, catalogue
from ui import widgets as W


class BuyMenu(DirectObject):
    def __init__(self, game, director):
        DirectObject.__init__(self)
        self.game = game
        self.director = director
        self.category = "rifles"
        self.hover = None
        self.items = []
        self.root = DirectFrame(parent=game.aspect2d, frameColor=W.PANEL, frameSize=(-1.1, 1.1, -0.66, 0.7),
                                state=DGG.NORMAL, sortOrder=60)
        self.title = W.label(self.root, "BUY", (-1.02, 0.6), scale=0.055, fg=W.ACCENT)
        self.money = W.label(self.root, "", (1.02, 0.6), scale=0.05, fg=(0.55, 0.95, 0.45, 1), align=TextNode.ARight)
        self.timer = W.label(self.root, "", (1.02, 0.54), scale=0.03, fg=W.DIM, align=TextNode.ARight)
        self.cat_buttons = {}
        for i, (key, name) in enumerate(CATEGORIES):
            self.cat_buttons[key] = W.button(self.root, f"{i + 1}  {name.upper()}", (-0.9 + i * 0.36, 0.46),
                                             self.select_category, width=0.34, height=0.07, scale=0.032, extra=(key,))
        self.list_frame = DirectFrame(parent=self.root, frameColor=(0, 0, 0, 0))
        self.info_title = W.label(self.root, "", (0.12, 0.3), scale=0.045, fg=W.TEXT)
        self.info_price = W.label(self.root, "", (0.12, 0.22), scale=0.04, fg=(0.55, 0.95, 0.45, 1))
        self.info_text = W.label(self.root, "", (0.12, 0.12), scale=0.03, fg=W.DIM, text_wordwrap=30)
        self.info_why = W.label(self.root, "", (0.12, -0.1), scale=0.033, fg=W.WARN, text_wordwrap=30)
        self.status = W.label(self.root, "", (0, -0.6), scale=0.034, fg=W.ACCENT, align=TextNode.ACenter)
        W.label(self.root, "number keys: category, then item   |   right-click: sell back   |   B / ESC: close",
                (0, -0.52), scale=0.026, fg=W.DIM, align=TextNode.ACenter)
        self._first_key = None
        self.root.hide()

    @property
    def is_open(self) -> bool:
        return not self.root.isHidden()

    # -------------------------------------------------------------- open
    def open(self) -> bool:
        d = self.director
        human = d.player_agent
        if not human.alive:
            return False
        if not d.match.can_buy():
            self.game.hud.flash_msg("buy time is over", 1.2)
            return False
        if not d.in_buy_zone(human):
            self.game.hud.flash_msg("you are not in the buy zone", 1.2)
            return False
        self.root.show()
        self.status["text"] = ""
        self.select_category(self.category)
        self.game.input.set_captured(False)
        for i in range(1, 7):
            self.accept(str(i), self._number, [i])
        self._first_key = None
        return True

    def close(self) -> None:
        if not self.is_open:
            return
        self.root.hide()
        self.ignoreAll()
        if not self.game.paused:
            self.game.input.set_captured(True)
            self.game.input.clear_presses()

    def toggle(self) -> None:
        self.close() if self.is_open else self.open()

    # ---------------------------------------------------------- content
    def select_category(self, key: str) -> None:
        self.category = key
        for k, b in self.cat_buttons.items():
            W.set_active(b, k == key)
        for b in self.items:
            b.destroy()
        self.items = []
        d = self.director
        cat = catalogue(self.game.weapon_db, d.rules, d.player_agent.side).get(key, [])
        for i, it in enumerate(cat):
            b = W.button(self.list_frame, f"{i + 1}   {it.name}", (-0.56, 0.3 - i * 0.1), self.buy, width=0.96,
                         height=0.085, scale=0.036, extra=(it,), align=TextNode.ALeft)
            price = W.label(b, f"${d.shop.price(d.player_agent, it)}", (0.44, -0.012), scale=0.034,
                            fg=(0.55, 0.95, 0.45, 1), align=TextNode.ARight)
            ok, _ = d.shop.check(d.player_agent, it)
            if not ok:
                b["text_fg"] = W.DIM
                price["text_fg"] = W.DIM
            b.bind(DGG.ENTER, lambda _e, it=it: self._show_info(it))
            b.bind(DGG.B3PRESS, lambda _e, it=it: self.refund(it))
            self.items.append(b)
        self._show_info(cat[0] if cat else None)

    def _show_info(self, it) -> None:
        self.hover = it
        if it is None:
            for w in (self.info_title, self.info_price, self.info_text, self.info_why):
                w["text"] = ""
            return
        d = self.director
        ok, why = d.shop.check(d.player_agent, it)
        self.info_title["text"] = it.name
        self.info_price["text"] = f"${d.shop.price(d.player_agent, it)}"
        self.info_text["text"] = it.info
        refund = "  (right-click to sell back)" if d.shop.can_refund(d.player_agent, it) else ""
        self.info_why["text"] = ("" if ok else why) + refund

    def _number(self, n: int) -> None:
        if self._first_key is None:
            if n <= len(CATEGORIES):
                self._first_key = n
                self.select_category(CATEGORIES[n - 1][0])
        else:
            d = self.director
            cat = catalogue(self.game.weapon_db, d.rules, d.player_agent.side).get(self.category, [])
            if n <= len(cat):
                self.buy(cat[n - 1])
            self._first_key = None

    # ------------------------------------------------------------ actions
    def buy(self, it) -> None:
        d = self.director
        ok, msg = d.shop.buy(d.player_agent, it)
        self.status["text"] = msg if ok else f"can't buy {it.name}: {msg}"
        self.select_category(self.category)

    def refund(self, it) -> None:
        d = self.director
        if d.shop.refund(d.player_agent, it):
            self.status["text"] = f"sold back {it.name}"
            self.select_category(self.category)

    def update(self) -> None:
        if not self.is_open:
            return
        d = self.director
        m = d.match
        if not m.can_buy() or not d.player_agent.alive or not d.in_buy_zone(d.player_agent):
            self.close()
            return
        self.money["text"] = f"$ {d.player_agent.money}"
        self.timer["text"] = {"freeze": "freeze time", "prep": "preparation"}.get(m.phase, f"buy time {m.buy_time_left():.0f} s")
