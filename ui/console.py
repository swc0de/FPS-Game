"""Developer console (` or F10): practice and testing commands.

    help                      list commands
    money <n>                 set your money
    plant [A|B]               plant the bomb at a site (defuse practice)
    endround [attack|defend]  end the round with that side winning
    restart                   restart the match
    team <attack|defend>      switch side and restart
    bots <opponents> [mates]  roster size (bots or stand-ins), restarts the match
    difficulty <level>        bot difficulty: easy normal hard expert
    botinfo                   what every bot is doing
    spectate                  leave the match and watch ten bots play
    give <weapon>             e.g. give sr90 (r7 c9 mx5 s12 p9 frag smoke flash)
    god / noclip / kill       invulnerable / fly / suicide
    freeze|buytime|roundtime <s>   change match timers
    pos / tp x y z            print / set your position
"""
from __future__ import annotations

from direct.gui.DirectGui import DirectEntry, DirectFrame
from direct.gui.OnscreenText import OnscreenText
from panda3d.core import Point3, TextNode

from ui import widgets as W

LINES = 12


class Console:
    def __init__(self, game):
        self.game = game
        self.log_lines: list[str] = ["developer console - type 'help'"]
        self.root = DirectFrame(parent=game.a2dTopLeft, frameColor=(0.02, 0.03, 0.03, 0.88),
                                frameSize=(0, 4.0, -0.86, 0), sortOrder=80)
        self.texts = [OnscreenText(text="", parent=self.root, pos=(0.04, -0.06 - i * 0.052), scale=0.036,
                                   fg=W.TEXT, align=TextNode.ALeft, mayChange=True) for i in range(LINES)]
        self.entry = DirectEntry(parent=self.root, scale=0.04, pos=(0.04, 0, -0.8), width=60, numLines=1,
                                 focus=0, command=self._submit, frameColor=(0.1, 0.11, 0.11, 1),
                                 text_fg=W.ACCENT, initialText="")
        self.history: list[str] = []
        self.root.hide()

    @property
    def is_open(self) -> bool:
        return not self.root.isHidden()

    def toggle(self) -> None:
        if self.is_open:
            self.close()
        else:
            self.root.show()
            self.entry.enterText("")
            self.entry["focus"] = 1
            self.game.input.set_captured(False)
            self._refresh()

    def close(self) -> None:
        self.root.hide()
        self.entry["focus"] = 0
        if not self.game.paused:
            self.game.input.set_captured(True)
            self.game.input.clear_presses()

    def print(self, text: str) -> None:
        for line in str(text).split("\n"):
            self.log_lines.append(line)
        self.log_lines = self.log_lines[-200:]
        self._refresh()

    def _refresh(self) -> None:
        shown = self.log_lines[-LINES:]
        for i, t in enumerate(self.texts):
            t.setText(shown[i] if i < len(shown) else "")

    def _submit(self, text: str) -> None:
        text = text.strip().lstrip("`")
        self.entry.enterText("")
        self.entry["focus"] = 1
        if not text:
            return
        self.history.append(text)
        self.print("> " + text)
        try:
            out = self.run(text)
        except Exception as exc:          # noqa: BLE001 - report any command error in the console
            out = f"error: {exc}"
        if out:
            self.print(out)

    # ------------------------------------------------------------- commands
    def run(self, text: str) -> str:
        g = self.game
        d = g.director
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "help":
            return __doc__.split("\n", 2)[2].rstrip()
        if cmd == "pos":
            c = g.player.char.pos
            return f"{c.x:.2f} {c.y:.2f} {c.z:.2f}  yaw {g.player.yaw:.1f}"
        if cmd == "tp" and len(args) >= 3:
            g.player.char.teleport(Point3(*map(float, args[:3])))
            return "teleported"
        if cmd == "noclip":
            g.player.noclip = not g.player.noclip
            return f"noclip {'on' if g.player.noclip else 'off'}"
        if cmd == "give" and args:
            key = args[0]
            w = g.weapons
            if key in g.weapon_db.weapons:
                w.inv.give_weapon(key)
                w.select(g.weapon_db.weapons[key].slot, force=True)
                return f"gave {key}"
            if key in g.weapon_db.grenades:
                w.inv.give_grenade(key, 1)
                return f"gave {key}"
            return f"unknown item {key}"
        if d is None:
            return "match commands need a map with bomb sites (sandbox mode)"
        m = d.match
        if cmd == "money" and args:
            d.player_agent.money = max(0, min(int(args[0]), m.max_money))
            return f"money ${d.player_agent.money}"
        if cmd == "plant":
            site = (args[0] if args else "A").upper()
            return f"bomb planted at {site}" if d.plant_at_site(site) else "can't plant now (round must be live)"
        if cmd == "endround":
            side = args[0] if args else d.player_agent.side
            m.force_end_round(side)
            return f"round ended ({side} wins)"
        if cmd == "restart":
            d.restart()
            return "match restarted"
        if cmd == "team" and args and args[0] in ("attack", "defend"):
            d.restart(args[0])
            return f"you are now on {args[0]}"
        if cmd == "bots" and args:
            d.set_opponents(int(args[0]), int(args[1]) if len(args) > 1 else 0)
            n = len(d.bots) if d.use_bots else len(d.standins)
            return f"{n} {'bots' if d.use_bots else 'stand-ins'}, match restarted"
        if cmd == "difficulty" and args:
            d.set_difficulty(args[0])
            return f"bot difficulty: {d.difficulty}"
        if cmd == "spectate":
            d.set_spectate()
            return "watching a bot match (start the game again to play)"
        if cmd == "botinfo":
            return "\n".join(b.describe() for b in d.bots if b.active) or "no bots"
        if cmd == "god":
            d.god = not d.god
            return f"god {'on' if d.god else 'off'}"
        if cmd == "kill":
            from gameplay.damage import DamageInfo
            g.player.damageable.take_damage(DamageInfo(999, 1.0, "chest", "fall", None, "world"))
            return "you died"
        if cmd in ("freeze", "buytime", "roundtime") and args:
            key = {"freeze": "freeze_time", "buytime": "buy_time", "roundtime": "round_time"}[cmd]
            d.rules["timers"][key] = float(args[0])
            return f"{key} = {args[0]} s"
        return f"unknown command '{cmd}' (try help)"
