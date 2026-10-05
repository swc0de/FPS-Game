"""Match HUD: score bar with round clock and alive pips, money with reward
pop-ups, kill feed, round banners, plant/defuse progress, hints, the buy
indicator and the scoreboard (Tab)."""
from __future__ import annotations

import math

from direct.gui.DirectGui import DirectFrame
from panda3d.core import TextNode

from ui import widgets as W

REASONS = {
    "elimination": "Enemy team eliminated",
    "time": "Time ran out - targets saved",
    "bomb_detonated": "Target destroyed",
    "bomb_defused": "The bomb has been defused",
}
GREEN = (0.55, 0.95, 0.45, 1)
SHADOW = (0, 0, 0, 0.85)


def _fmt_clock(t: float) -> str:
    t = max(t, 0.0)
    if t < 10.0:
        return f"0:{t:04.1f}"
    m, s = divmod(int(math.ceil(t)), 60)
    return f"{m}:{s:02d}"


class MatchHUD:
    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.rules = director.rules
        a2 = game
        self.colors = {s: tuple(self.rules["teams"][s]["color"]) + (1,) for s in ("attack", "defend")}
        self.root = game.aspect2d.attachNewNode("match_hud")
        # --- score bar
        self.bar = DirectFrame(parent=a2.a2dTopCenter, frameColor=(0.02, 0.025, 0.03, 0.62),
                               frameSize=(-0.42, 0.42, -0.2, 0.0), pos=(0, 0, -0.01))
        self.clock = W.Text(text="", parent=a2.a2dTopCenter, pos=(0, -0.1), scale=0.072, fg=W.TEXT,
                                  shadow=SHADOW, mayChange=True)
        self.phase = W.Text(text="", parent=a2.a2dTopCenter, pos=(0, -0.17), scale=0.032, fg=W.ACCENT,
                                  shadow=SHADOW, mayChange=True)
        self.score = {}
        self.name = {}
        self.pips = {}
        for side, x in (("attack", -0.27), ("defend", 0.27)):
            c = self.colors[side]
            self.name[side] = W.Text(text=self.rules["teams"][side]["short"], parent=a2.a2dTopCenter,
                                           pos=(x, -0.06), scale=0.032, fg=c, shadow=SHADOW, mayChange=True)
            self.score[side] = W.Text(text="0", parent=a2.a2dTopCenter, pos=(x, -0.13), scale=0.07, fg=c,
                                            shadow=SHADOW, mayChange=True)
            row = []
            for i in range(5):
                px = x + (i - 2) * 0.034
                f = DirectFrame(parent=a2.a2dTopCenter, frameColor=c, frameSize=(-0.012, 0.012, -0.012, 0.012),
                                pos=(px, 0, -0.175))
                row.append(f)
            self.pips[side] = row
        # --- money
        self.money = W.Text(text="", parent=a2.a2dBottomLeft, pos=(0.08, 0.3), scale=0.055, fg=GREEN,
                                  shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
        self.popups: list[list] = []          # [text node, time left]
        # --- feed
        self.feed_lines = [W.Text(text="", parent=a2.a2dTopRight, pos=(-0.05, -0.08 - i * 0.055), scale=0.036,
                                        fg=W.TEXT, shadow=SHADOW, align=TextNode.ARight, mayChange=True)
                           for i in range(6)]
        # --- team radio (bot callouts)
        self.radio_lines = [W.Text(text="", parent=a2.a2dLeftCenter, pos=(0.06, 0.16 - i * 0.048), scale=0.033,
                                         fg=W.TEXT, shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
                            for i in range(5)]
        # --- banners and progress
        self.banner = W.Text(text="", parent=a2.aspect2d, pos=(0, 0.48), scale=0.085, fg=W.TEXT,
                                   shadow=SHADOW, mayChange=True)
        self.sub = W.Text(text="", parent=a2.aspect2d, pos=(0, 0.4), scale=0.042, fg=W.TEXT, shadow=SHADOW,
                                mayChange=True)
        self._banner_t = 0.0
        from ui.tactical_hud import TacticalHUD
        self.tactical = TacticalHUD(game, director)
        self.prog_bg = DirectFrame(parent=a2.aspect2d, frameColor=(0, 0, 0, 0.6), frameSize=(-0.32, 0.32, -0.018, 0.018),
                                   pos=(0, 0, -0.42))
        self.prog_fill = DirectFrame(parent=self.prog_bg, frameColor=W.ACCENT, frameSize=(0, 0.62, -0.011, 0.011),
                                     pos=(-0.31, 0, 0))
        self.prog_label = W.Text(text="", parent=a2.aspect2d, pos=(0, -0.38), scale=0.036, fg=W.TEXT,
                                       shadow=SHADOW, mayChange=True)
        self.prog_bg.hide()
        self.hint = W.Text(text="", parent=a2.aspect2d, pos=(0, -0.5), scale=0.036, fg=W.ACCENT,
                                 shadow=SHADOW, mayChange=True)
        self.buy = W.Text(text="", parent=a2.a2dBottomLeft, pos=(0.08, 0.38), scale=0.036, fg=W.ACCENT,
                                shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
        self.bomb_icon = W.Text(text="", parent=a2.a2dBottomRight, pos=(-0.06, 0.28), scale=0.038,
                                      fg=(1.0, 0.45, 0.25, 1), shadow=SHADOW, align=TextNode.ARight, mayChange=True)
        self.scoreboard = Scoreboard(game, director)
        self.visible = True
        director.listeners.append(self._on_event)

    # ------------------------------------------------------------ events
    def show_banner(self, text: str, sub: str = "", color=W.TEXT, seconds: float = 4.0) -> None:
        self.banner.setText(text)
        self.banner.setFg(color)
        self.sub.setText(sub)
        self._banner_t = seconds

    def _on_event(self, kind: str, data: dict) -> None:
        d = self.director
        teams = self.rules["teams"]
        if kind == "round_start":
            self.show_banner(f"ROUND {data['round']}", "Buy your gear (B) - the round starts soon", seconds=3.0)
        elif kind == "round_end":
            r = data["result"]
            name = teams[r.winner_side]["name"].upper()
            won = r.winner_side == d.player_agent.side or d.spectate_only
            sub = REASONS.get(r.reason, r.reason) + (f"   -   MVP {r.mvp}" if r.mvp else "")
            self.show_banner(f"{name} WIN", sub, self.colors[r.winner_side] if won else (1, 0.45, 0.4, 1), 5.5)
        elif kind == "bomb_planted":
            self.show_banner("BOMB PLANTED", f"Site {d.bomb.site} - 40 seconds to detonation", (1, 0.4, 0.3, 1), 3.0)
        elif kind == "halftime":
            self.show_banner("HALFTIME", "Switching sides - money resets", seconds=6.0)
        elif kind == "match_end":
            w = data["winner"]
            mine = d.player_agent.team
            a, b = d.match.teams[0].score, d.match.teams[1].score
            if w is None:
                self.show_banner("DRAW", f"{a} : {b}", seconds=30.0)
            elif d.spectate_only:
                self.show_banner(f"{teams[w.side]['name'].upper()} WIN THE MATCH", f"{a} : {b}",
                                 self.colors[w.side], 30.0)
            elif w is mine:
                self.show_banner("VICTORY", f"{a} : {b}   -   console 'restart' for a new match", GREEN, 30.0)
            else:
                self.show_banner("DEFEAT", f"{a} : {b}   -   console 'restart' for a new match", (1, 0.4, 0.35, 1), 30.0)
        elif kind == "money" and data["who"] is d.player_agent:
            amount = data["amount"]
            sign = "+" if amount >= 0 else "-"
            t = W.Text(text=f"{sign}${abs(amount)}  {data['reason']}", parent=self.game.a2dBottomLeft,
                             pos=(0.08, 0.36), scale=0.03, fg=GREEN if amount >= 0 else (1, 0.4, 0.3, 1),
                             shadow=SHADOW, align=TextNode.ALeft, mayChange=True)
            self.popups.append([t, 3.0])

    # ------------------------------------------------------------ update
    def set_visible(self, v: bool) -> None:
        self.visible = v
        nodes = [self.bar, self.clock, self.phase, self.money, self.buy, self.bomb_icon, self.hint, self.banner,
                 self.sub] + list(self.score.values()) + list(self.name.values()) + self.feed_lines + self.radio_lines
        nodes += [p for row in self.pips.values() for p in row]
        for n in nodes:
            n.show() if v else n.hide()
        self.tactical.set_visible(v)
        if not v:
            self.prog_bg.hide()
            self.prog_label.setText("")

    def update(self, dt: float) -> None:
        d = self.director
        m = d.match
        g = self.game
        human = d.player_agent
        self.tactical.update(dt)
        if not self.visible:
            return
        # clock / phase
        clock = m.clock()
        self.clock.setText(_fmt_clock(clock) if m.phase not in ("waiting", "match_end") else "")
        if m.phase == "planted":
            blink = (g.loop.time * 2.0) % 1.0 < 0.5
            self.clock.setFg((1, 0.25, 0.2, 1) if blink else (1, 0.6, 0.5, 1))
            self.phase.setText(f"BOMB PLANTED  {d.bomb.site}")
        else:
            self.clock.setFg((1, 0.45, 0.35, 1) if m.phase == "live" and clock < 10 else W.TEXT)
            if m.phase == "freeze":
                self.phase.setText("FREEZE TIME")
            elif m.phase == "prep":
                self.phase.setText("PREPARATION")
            elif m.phase == "live" and m.can_buy():
                self.phase.setText(f"BUY TIME {m.buy_time_left():.0f}")
            elif m.phase == "halftime":
                self.phase.setText("HALFTIME")
            else:
                self.phase.setText(f"ROUND {m.round}" if m.phase != "waiting" else "")
        for side in ("attack", "defend"):
            team = m.team_of_side(side)
            self.score[side].setText(str(team.score))
            alive = len(team.alive_members())
            for i, pip in enumerate(self.pips[side]):
                if i < len(team.members):
                    pip.show()
                    c = self.colors[side] if i < alive else (0.25, 0.25, 0.25, 0.9)
                    pip["frameColor"] = c
                else:
                    pip.hide()
        # money and pop-ups
        self.money.setText("" if d.spectate_only else f"$ {human.money}")
        # radio
        for i, line in enumerate(self.radio_lines):
            if i < len(d.radio_log):
                r = d.radio_log[-1 - i]
                line.setText(f"{r['who']}: {r['text']}")
                c = self.colors.get(r["side"], W.TEXT)
                line.setFg((c[0], c[1], c[2], min(1.0, (6.0 - (g.loop.time - r["t"])) / 1.0)))
            else:
                line.setText("")
        y = 0.36
        keep = []
        for t, left in self.popups:
            left -= dt
            if left <= 0:
                t.destroy()
                continue
            y += 0.04
            t.setPos(0.08, y)
            t.setAlphaScale(min(left / 0.6, 1.0))
            keep.append([t, left])
        self.popups = keep[-5:]
        for t, _ in keep[:-5]:
            t.destroy()
        # kill feed
        for i, line in enumerate(self.feed_lines):
            if i < len(d.feed):
                e = d.feed[-1 - i]
                killer = e.get("killer")
                victim = e["victim"]
                wname = self._weapon_name(e.get("weapon", ""))
                hs = " (HS)" if e.get("headshot") else ""
                kname = killer.name if killer is not None and killer is not victim else ""
                assist = f" + {e['assister'].name}" if e.get("assister") is not None else ""
                line.setText(f"{kname}{assist}  [{wname}{hs}]  {victim.name}" if kname else f"[{wname}]  {victim.name}")
                involved = human in (killer, victim)
                line.setFg((1, 0.85, 0.5, 1) if involved else W.TEXT)
            else:
                line.setText("")
        # banner
        if self._banner_t > 0:
            self._banner_t -= dt
            a = min(self._banner_t / 0.6, 1.0)
            self.banner.setAlphaScale(a)
            self.sub.setAlphaScale(a)
            if self._banner_t <= 0:
                self.banner.setText("")
                self.sub.setText("")
        # progress (plant / defuse): yours, or the spectated bot's
        progress = d.progress
        spec = d.spectator.target if d.spectator.active and not d.spectator.free else None
        if progress is None and spec is not None and spec.brain is not None:
            b = spec.brain
            timers = self.rules["timers"]
            if b.plant_t > 0:
                progress = ("plant", min(b.plant_t / float(timers["plant_time"]), 1.0))
            elif b.defuse_t > 0:
                total = float(timers["defuse_time_kit" if spec.has_kit else "defuse_time"])
                progress = ("defuse", min(b.defuse_t / total, 1.0))
        if progress is not None:
            kind, frac = progress
            self.prog_bg.show()
            self.prog_fill["frameSize"] = (0, 0.62 * frac, -0.011, 0.011)
            kit = (spec or human).has_kit
            label = {"plant": "PLANTING", "reinforce": "REINFORCING"}.get(
                kind, "DEFUSING (kit)" if kit else "DEFUSING")
            self.prog_label.setText(label)
        else:
            self.prog_bg.hide()
            self.prog_label.setText("")
        dead = not human.alive and m.phase not in ("waiting",)
        spectating = dead and d.spectator.active
        self.hint.setPos(0, -0.86 if spectating else -0.5)
        if spectating:
            self.hint.setText(d.spectator.status())
        elif dead and not d.use_bots:
            self.hint.setText("You are dead - spectating (fly with WASD, Space/Ctrl up/down)")
        else:
            self.hint.setText("" if dead else d.hint)
        # buy indicator / bomb carrier
        if m.can_buy() and human.alive and d.in_buy_zone(human):
            self.buy.setText(f"[B] BUY   {m.buy_time_left():.0f} s")
        else:
            self.buy.setText("")
        carrying = d.bomb.state == "carried" and d.bomb.carrier is human
        self.bomb_icon.setText("[5] BREACH CHARGE" if carrying else ("[KIT]" if human.has_kit else ""))
        self.scoreboard.update()

    def _weapon_name(self, key: str) -> str:
        db = self.game.weapon_db
        if key in db.weapons:
            return db.weapons[key].name
        if key in db.grenades:
            return db.grenades[key].name
        return {"bomb": "Breach charge"}.get(key, key or "world")


COLS = (("NAME", -0.96, TextNode.ALeft), ("K", 0.02, TextNode.ARight), ("D", 0.14, TextNode.ARight),
        ("A", 0.26, TextNode.ARight), ("HS%", 0.42, TextNode.ARight), ("MVP", 0.56, TextNode.ARight),
        ("SCORE", 0.74, TextNode.ARight), ("MONEY", 0.97, TextNode.ARight))


class Scoreboard:
    """Hold Tab: both teams' K / D / A / HS% / MVP / score, money for your team, round history."""

    ROWS = 16

    def __init__(self, game, director):
        self.game = game
        self.director = director
        self.root = DirectFrame(parent=game.aspect2d, frameColor=W.PANEL, frameSize=(-1.05, 1.05, -0.62, 0.68),
                                sortOrder=30)
        self.title = W.label(self.root, "", (0, 0.58), scale=0.05, fg=W.ACCENT, align=TextNode.ACenter)
        for name, x, align in COLS:
            W.label(self.root, name, (x, 0.48), scale=0.03, fg=W.DIM, align=align)
        self.cells = [[W.Text(text="", parent=self.root, pos=(x, 0.42 - r * 0.052), scale=0.034, fg=W.TEXT,
                                    align=align, mayChange=True) for _, x, align in COLS] for r in range(self.ROWS)]
        self.history = W.label(self.root, "", (0, -0.56), scale=0.03, fg=W.DIM, align=TextNode.ACenter)
        self.root.hide()

    @property
    def visible(self) -> bool:
        return not self.root.isHidden()

    def set_visible(self, v: bool) -> None:
        self.root.show() if v else self.root.hide()

    def update(self) -> None:
        if not self.visible:
            return
        d = self.director
        m = d.match
        rules = d.rules
        self.title["text"] = (f"{rules['teams']['attack']['name'].upper()} {m.team_of_side('attack').score}   :   "
                              f"{m.team_of_side('defend').score} {rules['teams']['defend']['name'].upper()}"
                              f"        round {m.round} / {rules['rounds']['max_rounds']}")
        rows = []
        for side in ("attack", "defend"):
            team = m.team_of_side(side)
            color = tuple(rules["teams"][side]["color"]) + (1,)
            who = "your team" if d.player_agent.team is team else "enemy"
            rows.append(([f"{rules['teams'][side]['name'].upper()}  ({who})"] + [""] * 7, color))
            for p in sorted(team.members, key=lambda p: (-p.stats.score, -p.stats.kills)):
                st = p.stats
                hs = f"{st.headshots * 100 // st.kills}%" if st.kills else "-"
                money = f"${p.money}" if p.team is d.player_agent.team else ""
                name = ("x  " if not p.alive else "    ") + p.name + ("  (you)" if p.is_human else "")
                rows.append(([name, st.kills, st.deaths, st.assists, hs, st.mvps, st.score, money],
                             W.TEXT if p.alive else W.DIM))
        for r, cells in enumerate(self.cells):
            values, color = rows[r] if r < len(rows) else ([""] * 8, W.TEXT)
            for cell, v in zip(cells, values):
                cell.setText(str(v))
                cell.setFg(color)
        hist = []
        for res in m.history:
            tag = "V" if res.winner_side == "attack" else "B"
            glyph = {"elimination": "x", "time": "t", "bomb_detonated": "*", "bomb_defused": "d"}.get(res.reason, "?")
            hist.append(tag + glyph)
        self.history["text"] = ("rounds: " + " ".join(hist) +
                                "      V/B = winner;  x eliminated  * detonated  d defused  t time")
