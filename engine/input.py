"""Keyboard/mouse input with rebindable actions.

* ``is_down(action)``      - level state, sampled every fixed tick.
* ``consume(action)``      - edge-triggered press, buffered until a tick reads it
                             (so a quick tap between two ticks is never lost).
* ``mouse_delta()``        - accumulated pointer motion since the last call,
                             captured by re-centring the hidden cursor each frame.
"""
from __future__ import annotations

from direct.showbase.DirectObject import DirectObject
from panda3d.core import ButtonHandle, ButtonRegistry, KeyboardButton, MouseButton, WindowProperties

MOUSE_NAMES = {
    "mouse1": MouseButton.one(),
    "mouse2": MouseButton.two(),
    "mouse3": MouseButton.three(),
    "mouse4": MouseButton.four(),
    "mouse5": MouseButton.five(),
}


def button_handle(name: str):
    if name in MOUSE_NAMES:
        return MOUSE_NAMES[name]
    if len(name) == 1:
        return KeyboardButton.asciiKey(name)
    handle = ButtonRegistry.ptr().findButton(name)
    if handle == ButtonHandle.none():
        return None
    return handle


class InputManager(DirectObject):
    """Owns its own event registrations (a separate DirectObject), so game
    code can also listen to the same keys without replacing these handlers."""

    def __init__(self, base, binds: dict[str, str]):
        DirectObject.__init__(self)
        self.base = base
        self.binds: dict[str, str] = {}
        self._handles = {}
        self._pressed: set[str] = set()
        self._dx = 0.0
        self._dy = 0.0
        self.captured = False
        self._skip_frames = 0
        self.virtual: set[str] = set()      # scripted input (demo mode / tests)
        self.virtual_mode = False
        self.set_binds(binds)

    # --------------------------------------------------------------- binds
    def set_binds(self, binds: dict[str, str]) -> None:
        self.ignoreAll()
        self.binds = dict(binds)
        self._handles = {a: button_handle(n) for a, n in self.binds.items()}
        events: dict[str, list[str]] = {}
        for action, name in self.binds.items():
            events.setdefault(name, []).append(action)
        for name, actions in events.items():
            self.accept(name, self._on_press, [actions])
            # also catch presses while modifiers are held (e.g. shift-walk + jump)
            for mod in ("shift", "control", "alt"):
                if name not in (mod, "l" + mod, "r" + mod):
                    self.accept(f"{mod}-{name}", self._on_press, [actions])

    def _on_press(self, actions) -> None:
        if self.captured:
            self._pressed.update(actions)

    def is_down(self, action: str) -> bool:
        if self.virtual_mode:
            return action in self.virtual
        if not self.captured:
            return False
        handle = self._handles.get(action)
        if handle is None:
            return False
        mw = self.base.mouseWatcherNode
        return bool(mw is not None and mw.isButtonDown(handle))

    def press(self, action: str) -> None:
        """Inject an edge-triggered press (demo mode / tests)."""
        self._pressed.add(action)

    def consume(self, action: str) -> bool:
        if action in self._pressed:
            self._pressed.discard(action)
            return True
        return False

    def clear_presses(self) -> None:
        self._pressed.clear()

    # --------------------------------------------------------------- mouse
    def set_captured(self, captured: bool) -> None:
        self.captured = captured
        win = self.base.win
        if win is None or not hasattr(win, "requestProperties"):
            return
        props = WindowProperties()
        props.setCursorHidden(captured)
        props.setMouseMode(WindowProperties.M_confined if captured else WindowProperties.M_absolute)
        win.requestProperties(props)
        self._skip_frames = 2  # ignore the jump caused by the first re-centre
        self._dx = self._dy = 0.0
        if captured:
            self._recenter()

    def _recenter(self) -> None:
        win = self.base.win
        if win is None or not hasattr(win, "movePointer"):
            return
        win.movePointer(0, win.getXSize() // 2, win.getYSize() // 2)

    def poll_mouse(self) -> None:
        """Call once per rendered frame."""
        win = self.base.win
        if not self.captured or win is None or not hasattr(win, "getPointer"):
            return
        ptr = win.getPointer(0)
        if not ptr.getInWindow():
            self._recenter()
            return
        cx, cy = win.getXSize() // 2, win.getYSize() // 2
        dx, dy = ptr.getX() - cx, ptr.getY() - cy
        if self._skip_frames > 0:
            self._skip_frames -= 1
        else:
            self._dx += dx
            self._dy += dy
        if dx or dy:
            self._recenter()

    def mouse_delta(self) -> tuple[float, float]:
        dx, dy = self._dx, self._dy
        self._dx = self._dy = 0.0
        return dx, dy
