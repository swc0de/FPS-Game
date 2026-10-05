"""Small styled widget set on top of DirectGUI (buttons, option rows,
sliders) shared by the pause and settings menus."""
from __future__ import annotations

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.OnscreenText import OnscreenText
from direct.gui.DirectGui import DirectButton, DirectFrame, DirectLabel, DirectSlider
from panda3d.core import TextNode

PANEL = (0.045, 0.05, 0.05, 0.9)
PANEL_LIGHT = (0.09, 0.1, 0.1, 0.9)
ACCENT = (1.0, 0.78, 0.36, 1.0)
TEXT = (0.9, 0.92, 0.88, 1.0)
DIM = (0.6, 0.63, 0.6, 1.0)
WARN = (1.0, 0.55, 0.4, 1.0)
BTN_STATES = ((0.13, 0.14, 0.14, 0.95), (0.34, 0.28, 0.14, 1.0), (0.24, 0.21, 0.15, 1.0), (0.1, 0.1, 0.1, 0.6))
BTN_ACTIVE = ((0.32, 0.26, 0.12, 1.0), (0.42, 0.34, 0.16, 1.0), (0.38, 0.31, 0.15, 1.0), (0.1, 0.1, 0.1, 0.6))


class Text(OnscreenText):
    """OnscreenText that ignores updates which change nothing.

    The HUDs set their texts and colours every frame; a TextNode rebuilds its
    glyph geometry on every change, so skipping identical updates saves a
    lot of per-frame work."""

    def setText(self, text):
        if text == getattr(self, "_cached_text", None):
            return
        self._cached_text = text
        super().setText(text)

    def setFg(self, fg):
        fg = tuple(fg)
        if fg == getattr(self, "_cached_fg", None):
            return
        self._cached_fg = fg
        super().setFg(fg)


def panel(parent, frame, color=PANEL, **kw) -> DirectFrame:
    return DirectFrame(parent=parent, frameColor=color, frameSize=frame, **kw)


def label(parent, text, pos, scale=0.04, fg=TEXT, align=TextNode.ALeft, **kw) -> DirectLabel:
    return DirectLabel(parent=parent, text=text, pos=(pos[0], 0, pos[1]), text_scale=scale, text_fg=fg,
                       text_align=align, frameColor=(0, 0, 0, 0), **kw)


def button(parent, text, pos, command, width=0.42, height=0.075, scale=0.042, extra=(), active=False,
           align=TextNode.ACenter) -> DirectButton:
    hw, hh = width / 2, height / 2
    text_x = 0.0 if align == TextNode.ACenter else -hw + 0.03
    b = DirectButton(parent=parent, text=text, text_scale=scale, text_fg=TEXT, text_align=align,
                     text_pos=(text_x, -scale * 0.35), frameSize=(-hw, hw, -hh, hh),
                     frameColor=BTN_ACTIVE if active else BTN_STATES, relief=DGG.FLAT, pressEffect=0,
                     command=command, extraArgs=list(extra), pos=(pos[0], 0, pos[1]),
                     rolloverSound=None, clickSound=None)
    return b


def set_active(b: DirectButton, active: bool) -> None:
    b["frameColor"] = BTN_ACTIVE if active else BTN_STATES


class OptionRow:
    """'Label        <  value  >' cycling through a list of choices."""

    def __init__(self, parent, y: float, text: str, on_step, on_hover=None, x_label=0.1, x_value=1.18,
                 width=0.5):
        self.on_step = on_step
        self.frame = DirectFrame(parent=parent, frameColor=(1, 1, 1, 0.025), frameSize=(0.06, 1.58, -0.03, 0.035),
                                 pos=(0, 0, y), state=DGG.NORMAL)
        self.label = label(self.frame, text, (x_label, 0), scale=0.036)
        self.value = label(self.frame, "", (x_value, 0), scale=0.036, fg=ACCENT, align=TextNode.ACenter)
        self.left = button(self.frame, "<", (x_value - width / 2, 0.012), self.on_step, width=0.06, height=0.05,
                           scale=0.036, extra=(-1,))
        self.right = button(self.frame, ">", (x_value + width / 2, 0.012), self.on_step, width=0.06, height=0.05,
                            scale=0.036, extra=(1,))
        if on_hover is not None:
            for w in (self.frame, self.left, self.right):
                w.bind(DGG.ENTER, lambda _e: on_hover())

    def set_text(self, value: str, label_fg=TEXT) -> None:
        self.value["text"] = value
        self.label["text_fg"] = label_fg

    def destroy(self) -> None:
        self.frame.destroy()


class SliderRow:
    """'Label   [----|-----]  value' for continuous options."""

    def __init__(self, parent, y: float, text: str, lo: float, hi: float, step: float, on_change, on_hover=None,
                 x_label=0.1, x_slider=1.12, width=0.62):
        self.lo, self.hi, self.step = lo, hi, step
        self.on_change = on_change
        self._last = None
        self.frame = DirectFrame(parent=parent, frameColor=(1, 1, 1, 0.025), frameSize=(0.06, 1.58, -0.03, 0.035),
                                 pos=(0, 0, y), state=DGG.NORMAL)
        self.label = label(self.frame, text, (x_label, 0), scale=0.036)
        self.value = label(self.frame, "", (x_slider + width / 2 + 0.07, 0), scale=0.034, fg=ACCENT,
                           align=TextNode.ACenter)
        self.slider = DirectSlider(parent=self.frame, range=(lo, hi), value=lo, pageSize=step,
                                   pos=(x_slider - 0.03, 0, 0.012), frameSize=(-width / 2, width / 2, -0.008, 0.008),
                                   frameColor=(0.25, 0.26, 0.25, 1), thumb_frameSize=(-0.014, 0.014, -0.026, 0.026),
                                   thumb_frameColor=ACCENT, thumb_relief=DGG.FLAT, relief=DGG.FLAT,
                                   command=self._changed)
        if on_hover is not None:
            for w in (self.frame, self.slider, self.slider.thumb):
                w.bind(DGG.ENTER, lambda _e: on_hover())

    def _changed(self) -> None:
        # DirectSlider reports every value assignment through the event
        # queue (also our own), so only real changes are forwarded
        v = float(self.slider["value"])
        v = min(max(round((v - self.lo) / self.step) * self.step + self.lo, self.lo), self.hi)
        if self._last is not None and abs(v - self._last) < self.step * 0.5:
            return
        self._last = v
        self.on_change(v)

    def set(self, value: float, text: str, label_fg=TEXT) -> None:
        if self._last is None or abs(float(value) - self._last) > 1e-6:
            self._last = float(value)
            self.slider["value"] = value
        self.value["text"] = text
        self.label["text_fg"] = label_fg

    def destroy(self) -> None:
        self.frame.destroy()


class BindRow:
    """'Action        [ key ]' for the CONTROLS tab; clicking the key starts a rebind."""

    def __init__(self, parent, y: float, text: str, on_click, x0: float = 0.06, width: float = 0.74):
        self.frame = DirectFrame(parent=parent, frameColor=(1, 1, 1, 0.025),
                                 frameSize=(x0, x0 + width, -0.026, 0.03), pos=(0, 0, y), state=DGG.NORMAL)
        self.label = label(self.frame, text, (x0 + 0.03, 0), scale=0.03)
        self.key = button(self.frame, "", (x0 + width - 0.15, 0.01), lambda: on_click(), width=0.26, height=0.048,
                          scale=0.028)

    def set_text(self, value: str, label_fg=TEXT) -> None:
        self.key["text"] = value
        self.label["text_fg"] = label_fg

    def destroy(self) -> None:
        self.frame.destroy()
