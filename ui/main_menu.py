"""Main menu: the title screen shown when the game starts (and after
"Quit to main menu").

Behind the menu the camera tours the map's predefined camera shots (each
shot drifts slowly forward, with a fade between shots). The panel offers:

    PLAY              side, bot difficulty and team sizes, then START
    WATCH BOT MATCH   ten bots play, you spectate
    HOW TO PLAY       the objective and your current key bindings
    SETTINGS          the settings screen (graphics, display, gameplay,
                      controls, audio)
    QUIT
"""
from __future__ import annotations

import math

from direct.gui import DirectGuiGlobals as DGG
from direct.gui.DirectGui import DirectFrame
from panda3d.core import ClockObject, Point3, Quat, TextNode, Vec3

from ui import widgets as W

LEVELS = ("easy", "normal", "hard", "expert")
SHOT_TIME = 11.0          # seconds per camera shot
FADE = 0.9                # fade-to-black between shots
DRIFT = 0.28              # metres per second the camera drifts forward
TOUR = ("overview", "a_site", "comms_tower", "hangar", "b_long", "fuel_depot", "hq_lobby", "mid", "barracks")

# what each bind does, in the order shown on the HOW TO PLAY page
BIND_HELP = [("forward", "Move forward"), ("back", "Move back"), ("left", "Strafe left"), ("right", "Strafe right"),
             ("walk", "Walk (quiet)"), ("crouch", "Crouch"), ("jump", "Jump"), ("lean_left", "Lean left"),
             ("lean_right", "Lean right"), ("fire", "Fire"), ("aim", "Aim / scope"), ("reload", "Reload"),
             ("use", "Use / defuse / reinforce"), ("drop", "Drop weapon"), ("gadget", "Specialist gadget"),
             ("charge", "Wall charge"), ("observe", "Drones / cameras"), ("ping", "Ping"),
             ("buy_menu", "Buy menu"), ("scoreboard", "Scoreboard")]


def key_label(name: str) -> str:
    """Readable name of a Panda3D button ('lcontrol' -> 'L-Ctrl', 'mouse1' -> 'Mouse 1')."""
    special = {"mouse1": "Mouse 1", "mouse2": "Mouse 3", "mouse3": "Mouse 2", "wheel_up": "Wheel up",
               "wheel_down": "Wheel down", "space": "Space", "lshift": "L-Shift", "rshift": "R-Shift",
               "lcontrol": "L-Ctrl", "rcontrol": "R-Ctrl", "lalt": "L-Alt", "ralt": "R-Alt", "tab": "Tab",
               "enter": "Enter", "backspace": "Backspace", "escape": "Esc", "caps_lock": "Caps",
               "arrow_up": "Up", "arrow_down": "Down", "arrow_left": "Left", "arrow_right": "Right"}
    if name in special:
        return special[name]
    return name.upper() if len(name) == 1 else name.replace("_", " ").title()


def tour_pose(shots: list, t: float):
    """Camera (pos, hpr, fade 0..1) at time t of the menu tour."""
    if not shots:
        return Point3(0, 0, 10), (0.0, -20.0, 0.0), 0.0
    i = int(t // SHOT_TIME) % len(shots)
    u = t % SHOT_TIME
    s = shots[i]
    h, p, r = s["hpr"]
    hr, pr = math.radians(h), math.radians(p)
    fwd = Vec3(-math.sin(hr) * math.cos(pr), math.cos(hr) * math.cos(pr), math.sin(pr))
    pos = Point3(*s["pos"]) + fwd * (u * DRIFT)
    # a gentle pan across the shot
    yaw = h + (u / SHOT_TIME - 0.5) * 6.0
    fade = max(0.0, 1.0 - u / FADE, (u - (SHOT_TIME - FADE)) / FADE)
    return pos, (yaw, p, r), min(fade, 1.0)


class MainMenu:
    def __init__(self, game, on_play, on_watch):
        self.game = game
        self.on_play = on_play
        self.on_watch = on_watch
        d = game.director
        self.side = "attack"
        self.difficulty = d.difficulty
        self.teammates = max(0, min(d.n_teammates, 4)) if not d.spectate_only else 4
        self.opponents = max(1, min(d.n_opponents, 5))
        shots = {s["name"]: s for s in game.level.camera_shots}
        self.shots = [shots[n] for n in TOUR if n in shots] or list(game.level.camera_shots)
        self.t = 0.0
        self.page = ""
        self.fader = DirectFrame(parent=game.render2d, frameColor=(0, 0, 0, 0), frameSize=(-1, 1, -1, 1), sortOrder=30)
        self.shade = DirectFrame(parent=game.a2dLeftCenter, frameColor=(0.0, 0.0, 0.0, 0.55),
                                 frameSize=(0.0, 0.95, -2.0, 2.0), sortOrder=31)
        self.root = DirectFrame(parent=game.a2dLeftCenter, frameColor=(0, 0, 0, 0), sortOrder=35)
        from engine.app import GAME_TITLE
        W.label(self.root, GAME_TITLE, (0.12, 0.62), scale=0.11, fg=W.ACCENT)
        W.label(self.root, "a tactical shooter  -  " + game.level.data.get("name", ""), (0.13, 0.53), scale=0.035,
                fg=W.DIM)
        self.buttons = {}
        for i, (key, text) in enumerate((("play", "PLAY"), ("watch", "WATCH BOT MATCH"), ("help", "HOW TO PLAY"),
                                         ("settings", "SETTINGS"), ("quit", "QUIT"))):
            self.buttons[key] = W.button(self.root, text, (0.46, 0.3 - i * 0.12), self._menu, width=0.66,
                                         height=0.09, scale=0.042, extra=(key,))
        self.version = W.label(self.root, "Milestone 7  |  ` console  |  F12 screenshot", (0.13, -0.86),
                               scale=0.028, fg=W.DIM)
        # right-hand pages
        self.page_root = DirectFrame(parent=game.aspect2d, frameColor=W.PANEL, frameSize=(-0.62, 0.92, -0.72, 0.62),
                                     pos=(0.42, 0, 0), state=DGG.NORMAL, sortOrder=36)
        self.page_items: list = []
        self.page_root.hide()
        from ui.menus import SettingsMenu
        self.settings = SettingsMenu(game, self._settings_closed)
        self.visible = False
        self.show()

    # -------------------------------------------------------------- show
    def show(self) -> None:
        g = self.game
        self.visible = True
        self.root.show()
        self.shade.show()
        self.fader.show()
        g.input.set_captured(False)
        g.player.external_view = lambda dx, dy: None
        g.weapons.force_hide_vm = True
        g.hud.set_visible(False)
        if g.match_hud is not None:
            g.match_hud.set_visible(False)
        g.debug_hud.set_hidden(True)
        g.taskMgr.add(self._update, "main-menu", sort=20)
        g.audio.set_menu(True)

    def hide(self) -> None:
        g = self.game
        self.visible = False
        self.root.hide()
        self.shade.hide()
        self.fader.hide()
        self.page_root.hide()
        self.settings.root.hide()
        g.taskMgr.remove("main-menu")
        g.player.external_view = None
        g.weapons.force_hide_vm = False
        g.hud.set_visible(True)
        if g.match_hud is not None:
            g.match_hud.set_visible(True)
        g.debug_hud.set_hidden(False)
        g.audio.set_menu(False)

    def destroy(self) -> None:
        self.hide()
        self._clear_page()
        for w in (self.root, self.shade, self.fader, self.page_root):
            w.destroy()
        self.settings.destroy()

    def _update(self, task):
        dt = min(ClockObject.getGlobalClock().getDt(), 0.1)
        self.t += dt
        pos, hpr, fade = tour_pose(self.shots, self.t)
        cam = self.game.camera
        cam.setPos(pos)
        q = Quat()
        q.setHpr(Vec3(*hpr))
        cam.setQuat(q)
        self.fader["frameColor"] = (0, 0, 0, fade)
        return task.cont

    # -------------------------------------------------------------- pages
    def _menu(self, key: str) -> None:
        self.game.audio.play_ui("ui_tick", 0.4)
        if key == "quit":
            self.game.userExit()
        elif key == "settings":
            self.page_root.hide()
            self.root.hide()
            self.settings.open()
        elif key == "watch":
            self.on_watch()
        elif key == "help":
            self._help_page()
        elif key == "play":
            self._play_page()
        for k, b in self.buttons.items():
            W.set_active(b, k == key and key in ("play", "help"))

    def _settings_closed(self) -> None:
        self.root.show()

    def _clear_page(self) -> None:
        for w in self.page_items:
            w.destroy()
        self.page_items = []

    def _play_page(self) -> None:
        self.page = "play"
        self._clear_page()
        self.page_root.show()
        P = self.page_root
        rules = self.game.director.rules
        add = self.page_items.append
        add(W.label(P, "PLAY A MATCH", (-0.54, 0.5), scale=0.055, fg=W.ACCENT))
        add(W.label(P, "Side", (-0.54, 0.36), scale=0.036, fg=W.DIM))
        for i, side in enumerate(("attack", "defend")):
            t = rules["teams"][side]
            b = W.button(P, f"{t['name'].upper()}  ({'attack' if side == 'attack' else 'defend'})",
                         (-0.12 + i * 0.6, 0.37), self._set, width=0.56, height=0.08, scale=0.034,
                         extra=("side", side), active=self.side == side)
            b["text_fg"] = tuple(t["color"]) + (1,)
            add(b)
        add(W.label(P, "Difficulty", (-0.54, 0.22), scale=0.036, fg=W.DIM))
        for i, lvl in enumerate(LEVELS):
            add(W.button(P, lvl.upper(), (-0.2 + i * 0.29, 0.23), self._set, width=0.27, height=0.07, scale=0.03,
                         extra=("difficulty", lvl), active=self.difficulty == lvl))
        add(W.label(P, "Your team", (-0.54, 0.078), scale=0.036, fg=W.DIM))
        add(W.label(P, "Enemies", (-0.54, -0.042), scale=0.036, fg=W.DIM))
        self.team_value = self._stepper(P, 0.09, "teammates")
        self.enemy_value = self._stepper(P, -0.03, "opponents")
        desc = {"attack": "Attack: plant the breach charge at site A (armory) or B (motor pool).\n"
                          "Scout with drones in the preparation phase, breach walls, take the site.",
                "defend": "Defend: hold both sites. Reinforce walls and set up gadgets in the\n"
                          "preparation phase; defuse the charge if it is planted."}
        self.desc = W.label(P, desc[self.side], (-0.54, -0.22), scale=0.03, fg=W.TEXT)
        add(self.desc)
        add(W.label(P, "Pick your specialist in the buy menu (B, tab 7) once the match starts.",
                    (-0.54, -0.36), scale=0.028, fg=W.DIM))
        add(W.button(P, "START", (0.15, -0.55), self._start, width=0.5, height=0.1, scale=0.05))
        self._refresh_play()

    def _stepper(self, parent, y: float, what: str):
        """'<  value  >' control; returns the value label."""
        self.page_items.append(W.button(parent, "<", (-0.12, y), self._step, width=0.07, height=0.06, scale=0.034,
                                        extra=(what, -1)))
        self.page_items.append(W.button(parent, ">", (0.62, y), self._step, width=0.07, height=0.06, scale=0.034,
                                        extra=(what, 1)))
        value = W.label(parent, "", (0.25, y - 0.012), scale=0.034, fg=W.ACCENT, align=TextNode.ACenter)
        self.page_items.append(value)
        return value

    def _refresh_play(self) -> None:
        n = self.teammates
        self.team_value["text"] = "you alone" if n == 0 else f"you + {n} bot{'s' if n > 1 else ''}"
        self.enemy_value["text"] = f"{self.opponents} bot{'s' if self.opponents > 1 else ''}"

    def _set(self, what: str, value: str) -> None:
        setattr(self, what, value)
        self.game.audio.play_ui("ui_tick", 0.4)
        if what == "difficulty":
            s = self.game.settings
            s.data.setdefault("gameplay", {})["bot_difficulty"] = value
            s.save()
        self._play_page()

    def _step(self, what: str, d: int) -> None:
        if what == "teammates":
            self.teammates = max(0, min(4, self.teammates + d))
        else:
            self.opponents = max(1, min(5, self.opponents + d))
        self.game.audio.play_ui("ui_tick", 0.4)
        self._refresh_play()

    def _start(self) -> None:
        self.on_play(self.side, self.difficulty, self.opponents, self.teammates)

    def _help_page(self) -> None:
        self.page = "help"
        self._clear_page()
        self.page_root.show()
        P = self.page_root
        add = self.page_items.append
        add(W.label(P, "HOW TO PLAY", (-0.54, 0.5), scale=0.055, fg=W.ACCENT))
        text = ("Rounds: freeze time (buy) -> preparation -> live. Attackers win by planting the breach charge\n"
                "and letting it blow, or by eliminating the defenders; defenders by defusing it, eliminating\n"
                "the attackers or running out the clock. Money comes from kills, plants and round results.\n"
                "Soft walls break; defenders reinforce some of them. Every player has a specialist gadget.")
        add(W.label(P, text, (-0.54, 0.4), scale=0.028, fg=W.TEXT))
        binds = self.game.settings.input["binds"]
        col = 0
        for i, (action, label) in enumerate(BIND_HELP):
            x = -0.54 + col * 0.74
            y = 0.17 - (i % 10) * 0.065
            add(W.label(P, label, (x, y), scale=0.03, fg=W.DIM))
            add(W.label(P, key_label(binds.get(action, "?")), (x + 0.62, y), scale=0.03, fg=W.ACCENT,
                        align=TextNode.ARight))
            if i % 10 == 9:
                col += 1
        add(W.label(P, "Change keys in SETTINGS > CONTROLS.  Esc pauses the game.", (-0.54, -0.6), scale=0.028,
                    fg=W.DIM))

    # ------------------------------------------------------------- keys
    def on_escape(self) -> bool:
        """Esc closes an open page or the settings; True if it did something."""
        if self.settings.visible and self.settings.ate_escape():
            return True
        if self.settings.visible:
            self.settings.close()
            return True
        if not self.page_root.isHidden():
            self.page_root.hide()
            for b in self.buttons.values():
                W.set_active(b, False)
            return True
        return False
