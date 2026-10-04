"""Match director: connects the pure round logic (gameplay/match.py) to the
world - spawning, round resets, kill attribution, friendly fire, freeze
time, death spectating, the bomb and the plant/defuse interaction.

Only maps with bomb-site zones and spawns for both teams run a match; other
maps (the test range) stay in sandbox mode.
"""
from __future__ import annotations

import random

from panda3d.core import Point3, Vec3

from gameplay.agents import PlayerAgent, StandInAgent
from gameplay.bomb import Bomb
from gameplay.match import Match, Participant, load_rules, other_side
from gameplay.shop import Shop
from weapons.weapon import angles_to_dir

SPECTATE_DELAY = 2.5


def map_supports_match(level) -> bool:
    sites = [z for z in level.zones if z["kind"] == "bombsite"]
    teams = {s["team"] for s in level.spawns}
    return len(sites) >= 1 and {"attack", "defend"} <= teams


class MatchDirector:
    def __init__(self, game, side: str = "attack", opponents: int | None = None, teammates: int | None = None,
                 seed: int | None = None):
        self.game = game
        self.rules = load_rules()
        self.rng = random.Random(seed)
        practice = self.rules.get("practice", {})
        opponents = int(practice.get("opponents", 5) if opponents is None else opponents)
        teammates = int(practice.get("teammates", 0) if teammates is None else teammates)
        self.player_agent = PlayerAgent(game)
        self.standins: list[StandInAgent] = []
        self.match = Match(self.rules, [], side, on_event=self._event, on_round_reset=self._round_reset)
        self.match.add(self.player_agent, 0)
        names = {s: list(self.rules["names"][s]) for s in ("attack", "defend")}
        for s in names.values():
            self.rng.shuffle(s)
        for i in range(teammates):
            self._add_standin(names[side][i % len(names[side])], side, 0)
        enemy = other_side(side)
        for i in range(opponents):
            self._add_standin(names[enemy][i % len(names[enemy])], enemy, 1)
        for a in [self.player_agent] + self.standins:
            a.damageable.damage_filter = self._filter
            a.damageable.on_damage.append(lambda res, a=a: self._damaged(a, res))
        self.bomb = Bomb(game, self.rules)
        self.shop = Shop(self)
        # until the first round starts the player only has the starting gear
        game.weapons.inv.clear()
        game.weapons.give_loadout(["knife", self.rules["teams"][side]["default_pistol"]], {})
        self.listeners: list = []             # UI: callback(kind, data)
        self.progress: tuple[str, float] | None = None   # ("plant"|"defuse", 0..1) for the HUD
        self.hint = ""
        self._plant_t = 0.0
        self._defuse_t = 0.0
        self._dead_t = 0.0
        self.god = False
        self._prev_alive: dict[int, bool] = {}
        self.feed: list[dict] = []

    # ------------------------------------------------------------ roster
    def _add_standin(self, name: str, side: str, team_index: int) -> None:
        cfg = self.rules["teams"][side]
        weapon = "r7" if side == "attack" else "c9"
        a = StandInAgent(self.game, name, side, cfg.get("uniform", "dummy_polymer"), weapon)
        self.match.add(a, team_index)
        self.standins.append(a)

    def set_opponents(self, opponents: int, teammates: int = 0) -> None:
        """Console: change the stand-in roster and restart the match."""
        for a in self.standins:
            a.destroy()
        self.match.teams[0].members = [self.player_agent]
        self.match.teams[1].members = []
        self.match.participants = [self.player_agent]
        self.standins = []
        side = self.player_agent.side
        names = {s: list(self.rules["names"][s]) for s in ("attack", "defend")}
        for i in range(teammates):
            self._add_standin(names[side][i % 8], side, 0)
        for i in range(opponents):
            self._add_standin(names[other_side(side)][i % 8], other_side(side), 1)
        for a in self.standins:
            a.damageable.damage_filter = self._filter
            a.damageable.on_damage.append(lambda res, a=a: self._damaged(a, res))
        self.start()

    def agents(self) -> list[Participant]:
        return list(self.match.participants)

    def agent_of(self, obj) -> Participant | None:
        if obj is None:
            return None
        if isinstance(obj, Participant):
            return obj
        return getattr(obj, "agent", None)

    # ------------------------------------------------------------- flow
    def start(self) -> None:
        self.feed = []
        self.match.start()

    def restart(self, side: str | None = None) -> None:
        if side is not None and side != self.player_agent.side:
            for t in self.match.teams:
                t.side = other_side(t.side)
        self.start()

    def combat_locked(self) -> bool:
        return self.match.phase in ("freeze", "match_end")

    def fixed_update(self, dt: float) -> None:
        g = self.game
        m = self.match
        m.update(dt)
        player = g.player
        human = self.player_agent
        self.hint = ""
        self.progress = None
        planting = self._update_plant(dt)
        defusing = self._update_defuse(dt)
        player.move_lock = m.phase == "freeze" or planting or defusing
        if m.phase == "planted" and m.bomb_time_left() <= 0.0 and self.bomb.state == "planted":
            self.bomb.explode()
            m.on_bomb_exploded()
        # bomb pickup by walking over it
        if self.bomb.state == "dropped" and human.side == "attack" and human.alive:
            if self.bomb.try_pickup(human, player.char.pos):
                g.weapons.inv.has_bomb = True
                g.hud.flash_msg("picked up the breach charge", 1.5)
                g.audio.play_ui("pickup", 0.6)
        # death: switch to a free spectator camera after a moment
        if not human.alive:
            self._dead_t += dt
            if self._dead_t >= SPECTATE_DELAY and not player.noclip:
                player.noclip = True
                c = player.char
                c.teleport((c.pos.x, c.pos.y, c.pos.z + 1.2))
        else:
            self._dead_t = 0.0

    def frame_update(self, dt: float) -> None:
        self.bomb.update(dt, self.match.bomb_time_left())
        for a in self.standins:
            a.frame_update(dt)
        now = self.game.loop.time
        self.feed = [f for f in self.feed if now - f["t"] < 7.0]

    # --------------------------------------------------- round reset
    def _round_reset(self, match: Match, swapped: bool) -> None:
        g = self.game
        g.clear_world()
        self.shop.new_round()
        self.bomb.reset()
        self._plant_t = self._defuse_t = 0.0
        self._dead_t = 0.0
        human = self.player_agent
        survived = {id(a): a.alive for a in match.participants}
        if match.round == 1:
            survived = {k: False for k in survived}
        # ---- the human
        player = g.player
        player.noclip = False
        kept = survived.get(id(human), False) and not swapped
        armor = player.damageable.armor if kept else 0.0
        helmet = player.damageable.helmet if kept else False
        player.damageable.reset(armor=armor, helmet=helmet)
        human.alive_flag = True
        inv = g.weapons.inv
        if not kept:
            inv.clear()
            human.has_kit = False
            g.weapons.give_loadout(["knife", self.rules["teams"][human.side]["default_pistol"]], {})
        else:
            inv.has_bomb = False
            inv.refill_ammo()
        if human.side != "defend":
            human.has_kit = False
        spawns = [s for s in g.level.spawns if s["team"] == human.side]
        self.rng.shuffle(spawns)
        sp = spawns[0] if spawns else g.level.spawn_point()
        player.spawn(sp["pos"], sp.get("heading", 0.0))
        player.char.vel = Vec3(0, 0, 0)
        player.char.prev_pos = Point3(player.char.pos)
        # ---- stand-ins hold positions
        positions = {s: list(g.level.data.get("practice_positions", {}).get(s, [])) for s in ("attack", "defend")}
        for s in positions.values():
            self.rng.shuffle(s)
        for a in self.standins:
            a.reset_for_round(keep_armor=True)
            pool = positions.get(a.side) or []
            if pool:
                x, y, z, h, crouch = pool.pop()
            else:
                sp = self.rng.choice([s for s in g.level.spawns if s["team"] == a.side])
                (x, y, z), h, crouch = sp["pos"], sp.get("heading", 0.0), 0
            a.set_weapon("r7" if a.side == "attack" else "c9")
            a.place((x, y, z), h, bool(crouch))
        # ---- the bomb goes to the human attacker (stand-ins cannot plant)
        if human.side == "attack":
            self.bomb.give(human)
            inv.has_bomb = True
        else:
            carriers = [a for a in self.standins if a.side == "attack"]
            if carriers:
                self.bomb.give(self.rng.choice(carriers))
        g.weapons.select(inv.best_slot(), force=True)
        g.renderer.post.reset_adaptation()

    # ------------------------------------------------------- damage
    def _filter(self, damageable, info) -> bool:
        if self.god and damageable is self.player_agent.damageable:
            return False
        victim = next((a for a in self.match.participants if a.damageable is damageable), None)
        attacker = self.agent_of(info.attacker)
        return self.match.allow_damage(victim, attacker)

    def _damaged(self, agent: Participant, res) -> None:
        attacker = self.agent_of(res.info.attacker)
        self.match.on_damage(agent, attacker, res.health)
        if res.killed:
            weapon = res.info.weapon or ""
            reward = self._kill_reward(weapon)
            if agent.is_human and self.bomb.state == "carried" and self.bomb.carrier is agent:
                self.drop_bomb(agent)
            if not agent.is_human and self.bomb.state == "carried" and self.bomb.carrier is agent:
                self.bomb.drop(agent.position())
            self.match.on_kill(agent, attacker, weapon, reward, res.info.headshot, res.info.kind)

    def _kill_reward(self, weapon: str) -> int:
        db = self.game.weapon_db
        if weapon in db.weapons:
            return int(db.weapons[weapon].kill_reward)
        if weapon in db.grenades:
            return int(db.grenades[weapon].get("kill_reward", 300))
        return int(self.rules["economy"]["kill_reward_default"])

    # ---------------------------------------------------------- bomb
    def drop_bomb(self, agent) -> None:
        if self.bomb.state != "carried" or self.bomb.carrier is not agent:
            return
        g = self.game
        if agent.is_human:
            inv = g.weapons.inv
            inv.has_bomb = False
            fwd = Vec3(*angles_to_dir(g.player.yaw, 0))
            pos = g.player.char.pos + fwd * (1.2 if agent.alive else 0.0)
            self.bomb.drop(pos)
            if agent.alive:
                g.weapons.select(inv.best_slot(), force=True)
        else:
            self.bomb.drop(agent.position())

    def _feet_zone(self, kind: str = "bombsite"):
        c = self.game.player.char.pos
        return self.game.level.zone_at((c.x, c.y, c.z + 0.1), kind)

    def in_buy_zone(self, agent) -> bool:
        zones = [z for z in self.game.level.zones if z["kind"] == "buyzone"]
        if not zones:
            return True
        p = agent.position()
        for z in zones:
            if z.get("team") == agent.side and all(z["min"][i] <= p[i] <= z["max"][i] for i in range(3)):
                return True
        return False

    def _update_plant(self, dt: float) -> bool:
        g = self.game
        human = self.player_agent
        inv = g.weapons.inv
        holding = (inv.slot == "bomb" and inv.has_bomb and human.alive and self.match.phase == "live"
                   and g.input.is_down("fire"))
        if not holding:
            if self._plant_t > 0.0:
                self._plant_t = 0.0
                g.weapons.vm.stop_track()
            if inv.slot == "bomb" and inv.has_bomb and self.match.phase == "live" and not self._feet_zone():
                self.hint = "Plant at bomb site A or B"
            return False
        site = self._feet_zone()
        if site is None or not g.player.char.on_ground:
            self.hint = "You must be standing in a bomb site to plant"
            return False
        plant_time = float(self.rules["timers"]["plant_time"])
        if self._plant_t == 0.0:
            g.weapons.vm.play("bomb_plant", plant_time, hold=True)
        before = int(self._plant_t * 3)
        self._plant_t += dt
        if int(self._plant_t * 3) != before:
            g.audio.play_ui("plant_tap", 0.5)
        self.progress = ("plant", min(self._plant_t / plant_time, 1.0))
        if self._plant_t >= plant_time:
            self._plant_t = 0.0
            inv.has_bomb = False
            fwd = Vec3(*angles_to_dir(g.player.yaw, 0))
            self.bomb.plant(g.player.char.pos + fwd * 0.45, g.player.yaw, site["name"])
            g.weapons.vm.stop_track()
            g.weapons.select(inv.best_slot(), force=True)
            self.match.on_bomb_planted(human)
            return False
        return True

    def _update_defuse(self, dt: float) -> bool:
        g = self.game
        human = self.player_agent
        if human.side != "defend" or not human.alive or self.bomb.state != "planted":
            self._defuse_t = 0.0
            return False
        eye = g.player.camera_pos
        look = g.camera.getQuat(g.render).getForward()
        on_target = self.bomb.defuse_target(eye, look)
        if on_target and not g.input.is_down("use"):
            self.hint = "Hold [F] to defuse" + ("" if human.has_kit else "  (no kit: 10 s)")
        if not (on_target and g.input.is_down("use")):
            self._defuse_t = 0.0
            return False
        total = float(self.rules["timers"]["defuse_time_kit" if human.has_kit else "defuse_time"])
        if self._defuse_t == 0.0:
            g.audio.play_at("defuse_start", self.bomb.pos, volume=0.8)
        self._defuse_t += dt
        self.progress = ("defuse", min(self._defuse_t / total, 1.0))
        if self._defuse_t >= total:
            self._defuse_t = 0.0
            self.bomb.defused()
            self.match.on_bomb_defused(human)
            return False
        return True

    # --------------------------------------------------------- events
    def _event(self, kind: str, **data) -> None:
        g = self.game
        if kind == "kill":
            self.feed.append({"t": g.loop.time, **data})
            self.feed = self.feed[-6:]
        elif kind == "round_end":
            won = data["result"].winner_side == self.player_agent.side
            g.audio.play_ui("round_win" if won else "round_lose", 0.7)
        elif kind == "bomb_planted":
            g.audio.play_ui("bomb_planted", 0.6)
        for cb in list(self.listeners):
            cb(kind, data)
        g.log(f"[match] {kind} " + ", ".join(f"{k}={_short(v)}" for k, v in data.items()))

    # ---------------------------------------------------------- console
    def plant_at_site(self, site_name: str) -> bool:
        """Developer console: plant the bomb at a site (for defuse practice)."""
        site = next((z for z in self.game.level.zones if z["kind"] == "bombsite" and z["name"] == site_name), None)
        if site is None:
            return False
        if self.match.phase == "freeze":
            self.match.update(float(self.rules["timers"]["freeze_time"]) + 0.01)
        if self.match.phase != "live":
            return False
        planter = next((a for a in self.match.participants if a.side == "attack"), None)
        if planter is None:
            return False
        c = [(site["min"][i] + site["max"][i]) / 2 for i in range(3)]
        if self.bomb.state == "carried" and self.bomb.carrier is self.player_agent:
            self.game.weapons.inv.has_bomb = False
            self.game.weapons.select(self.game.weapons.inv.best_slot(), force=True)
        self.bomb.plant(Point3(c[0], c[1], site["min"][2] + 1.0), 0.0, site_name)
        self.match.on_bomb_planted(planter)
        return True


def _short(v) -> str:
    if isinstance(v, Participant):
        return v.name
    if hasattr(v, "winner_side"):
        return f"{v.winner_side}/{v.reason}"
    if hasattr(v, "side") and hasattr(v, "score"):
        return f"team{v.index}"
    return str(v)
