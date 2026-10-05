"""Match director: connects the pure round logic (gameplay/match.py) to the
world - spawning, round resets, kill attribution, friendly fire, freeze
time, death spectating, the bomb and the plant/defuse interaction, and the
AI bots (ai/) that fill both teams.

Only maps with bomb-site zones and spawns for both teams run a match; other
maps (the test range) stay in sandbox mode.

Rosters: by default the human plays with bot teammates against a full
bot team (5v5, data/match.json "bots"). ``standins=True`` brings back the
Milestone 4 practice mode (stand-ins holding positions, no shooting back);
``spectate=True`` leaves the human out and lets ten bots play.
"""
from __future__ import annotations

import math
import random

from panda3d.core import Point3, Vec3

from engine.physics import MASK_SIGHT
from gameplay.agents import PlayerAgent, StandInAgent
from gameplay.body import CharacterBody
from gameplay.bomb import Bomb
from gameplay.match import SIDES, Match, Participant, load_rules, other_side
from gameplay.shop import Shop
from gameplay.spectator import Spectator
from weapons.weapon import angles_to_dir

SPECTATE_DELAY = 2.5
TEAM_THINK = 0.25


def map_supports_match(level) -> bool:
    sites = [z for z in level.zones if z["kind"] == "bombsite"]
    teams = {s["team"] for s in level.spawns}
    return len(sites) >= 1 and {"attack", "defend"} <= teams


class MatchDirector:
    def __init__(self, game, side: str = "attack", opponents: int | None = None, teammates: int | None = None,
                 seed: int | None = None, difficulty: str | None = None, standins: bool = False,
                 spectate: bool = False):
        from ai.bot import load_bot_config
        self.game = game
        self.rules = load_rules()
        self.bot_config = load_bot_config()
        self.rng = random.Random(seed)
        self.seed = seed
        self.use_bots = not standins
        self.spectate_only = spectate and self.use_bots
        roster = self.rules.get("bots", {}) if self.use_bots else self.rules.get("practice", {})
        self.difficulty = difficulty or roster.get("difficulty") or self.bot_config.get("default_difficulty", "normal")
        if self.spectate_only:
            teammates = 5 if teammates is None else teammates
        self.n_opponents = int(roster.get("opponents", 5) if opponents is None else opponents)
        self.n_teammates = int(roster.get("teammates", 0) if teammates is None else teammates)
        self.player_agent = PlayerAgent(game)
        self.standins: list[StandInAgent] = []
        self.bots: list = []
        self.nav = game.navmesh() if self.use_bots else None
        self.match = Match(self.rules, [], side, on_event=self._event, on_round_reset=self._round_reset)
        if not self.spectate_only:
            self.match.add(self.player_agent, 0)
        else:
            self.player_agent.team = None
        self.bomb = Bomb(game, self.rules)
        self.shop = Shop(self)
        from gameplay.tactical import Tactical
        self.tactical = Tactical(game, self)
        self.team_brains: dict = {}
        if self.use_bots:
            from ai.tactics import TeamBrain
            self.team_brains = {s: TeamBrain(self, s) for s in SIDES}
        self._build_roster()
        self.player_agent.damageable.damage_filter = self._filter
        self.player_agent.damageable.on_damage.append(lambda res: self._damaged(self.player_agent, res))
        # the first-person player needs hit boxes for the bots to shoot at
        self.player_body = CharacterBody(game, game.player, visible=False, name="player_hitboxes")
        self.player_body.set_weapon(None)
        self.spectator = Spectator(game, self)
        # until the first round starts the player only has the starting gear
        game.weapons.inv.clear()
        game.weapons.give_loadout(["knife", self.rules["teams"][side]["default_pistol"]], {})
        self.listeners: list = []             # UI: callback(kind, data)
        self.progress: tuple[str, float] | None = None   # ("plant"|"defuse", 0..1) for the HUD
        self.hint = ""
        self._plant_t = 0.0
        self._defuse_t = 0.0
        self._dead_t = 0.0
        self._team_t = 0.0
        self._bots_buy = False
        self.god = False
        self._kill_z = float(game.level.data.get("bounds", [[0, 0, -50]])[0][2]) - 3.0
        self.feed: list[dict] = []
        self.radio_log: list[dict] = []

    # ------------------------------------------------------------ roster
    def _build_roster(self) -> None:
        side = self.match.teams[0].side
        names = {s: list(self.bot_config["names"][s]) for s in SIDES}
        for s in names.values():
            self.rng.shuffle(s)
        for i in range(self.n_teammates):
            self._add_agent(names[side][i % len(names[side])], side, 0)
        enemy = other_side(side)
        for i in range(self.n_opponents):
            self._add_agent(names[enemy][i % len(names[enemy])], enemy, 1)

    def _add_agent(self, name: str, side: str, team_index: int) -> None:
        if self.use_bots:
            from ai.bot import BotAgent
            from ai.brain import Brain
            a = BotAgent(self.game, name, side, self.difficulty, self.nav, seed=self.rng.randrange(1 << 30))
            self.match.add(a, team_index)
            a.brain = Brain(a, _TeamProxy(self, a))
            self.bots.append(a)
        else:
            cfg = self.rules["teams"][side]
            weapon = "r7" if side == "attack" else "c9"
            a = StandInAgent(self.game, name, side, cfg.get("uniform", "dummy_polymer"), weapon)
            self.match.add(a, team_index)
            self.standins.append(a)
        a.damageable.damage_filter = self._filter
        a.damageable.on_damage.append(lambda res, a=a: self._damaged(a, res))

    def set_opponents(self, opponents: int, teammates: int = 0) -> None:
        """Console: change the roster and restart the match."""
        for a in self.standins + self.bots:
            a.destroy()
        self.standins, self.bots = [], []
        human = [] if self.spectate_only else [self.player_agent]
        self.match.teams[0].members = list(human)
        self.match.teams[1].members = []
        self.match.participants = list(human)
        self.n_opponents, self.n_teammates = opponents, teammates
        self._build_roster()
        self.start()

    def set_spectate(self) -> None:
        """Leave the match and watch ten bots play (restarts the match)."""
        if self.spectate_only or not self.use_bots:
            return
        self.spectate_only = True
        self.player_agent.team = None
        self.set_opponents(5, 5)

    def set_difficulty(self, difficulty: str) -> None:
        from ai.bot import load_bot_config
        profiles = load_bot_config()["difficulty"]
        if difficulty not in profiles:
            return
        self.difficulty = difficulty
        for b in self.bots:
            b.difficulty = difficulty
            b.profile.clear()
            b.profile.update(profiles[difficulty])

    def agents(self) -> list[Participant]:
        return list(self.match.participants)

    def agent_of(self, obj) -> Participant | None:
        if obj is None:
            return None
        if isinstance(obj, Participant):
            return obj
        return getattr(obj, "agent", None)

    def brain_of(self, side: str):
        return self.team_brains.get(side)

    # ------------------------------------------------------------- flow
    def start(self) -> None:
        self.feed = []
        self.radio_log = []
        self.match.start()

    def configure(self, side: str, opponents: int, teammates: int, difficulty: str | None = None) -> None:
        """Main menu PLAY: your side, team sizes and bot difficulty, then a new match."""
        if difficulty:
            self.set_difficulty(difficulty)
        if self.spectate_only:
            self.spectate_only = False
            self.player_agent.team = self.match.teams[0]
            self.n_opponents = -1                     # force a roster rebuild with you in it
        if side != self.match.teams[0].side:
            for t in self.match.teams:
                t.side = other_side(t.side)
        if (opponents, teammates) != (self.n_opponents, self.n_teammates) or not self.use_bots:
            self.set_opponents(opponents, teammates if self.use_bots else 0)
        else:
            self.start()

    def stop(self) -> None:
        """Back to the main menu: the match stops and the world is cleaned up."""
        g = self.game
        m = self.match
        m.phase = "waiting"
        m.phase_time = 0.0
        g.clear_world()
        self.tactical.clear()
        self.bomb.reset()
        self.spectator.stop()
        for b in self.bots:
            b.remove()
        for a in self.standins:
            a.root.hide()
        self.feed, self.radio_log = [], []
        self.progress, self.hint = None, ""
        self._plant_t = self._defuse_t = self._dead_t = 0.0
        g.player.noclip = False
        g.player.move_lock = True
        self.player_body.hide()

    def restart(self, side: str | None = None) -> None:
        if side is not None and side != self.player_agent.side and not self.spectate_only:
            for t in self.match.teams:
                t.side = other_side(t.side)
        self.start()

    def combat_locked(self, agent=None) -> bool:
        """No shooting in freeze time; during prep only the defenders may
        (murder holes - nobody can be hurt before the round goes live)."""
        phase = self.match.phase
        if phase == "prep":
            return agent is None or agent.side != "defend"
        return phase in ("freeze", "match_end")

    def move_locked(self, agent) -> bool:
        phase = self.match.phase
        return phase == "freeze" or (phase == "prep" and agent.side != "defend")

    def fixed_update(self, dt: float) -> None:
        g = self.game
        m = self.match
        m.update(dt)
        now = g.loop.time
        player = g.player
        human = self.player_agent
        self.hint = ""
        self.progress = None
        if self._bots_buy and m.phase == "freeze":
            self._bots_buy = False
            for tb in self.team_brains.values():
                tb.buy()
                tb.round_start(now)
        planting = self._update_plant(dt)
        defusing = self._update_defuse(dt)
        tac = self.tactical
        player.move_lock = self.move_locked(human) or planting or defusing or tac.human_busy
        player.lean_lock = tac.human_busy
        if m.phase == "planted" and m.bomb_time_left() <= 0.0 and self.bomb.state == "planted":
            self.bomb.explode()
            m.on_bomb_exploded()
        # bomb pickup by walking over it
        if self.bomb.state == "dropped":
            if human.side == "attack" and human.alive and self.bomb.try_pickup(human, player.char.pos):
                g.weapons.inv.has_bomb = True
                g.hud.flash_msg("picked up the breach charge", 1.5)
                g.audio.play_ui("pickup", 0.6)
            for b in self.bots:
                if b.active and b.alive and self.bomb.try_pickup(b, b.char.pos):
                    b.weapons.inv.has_bomb = True
                    break
        # bots and their team brains
        for b in self.bots:
            b.fixed_update(dt, now, self.move_locked(b) if b.active else True)
            if b.active and b.alive and b.char.pos.z < self._kill_z:
                self._out_of_world(b)
        if human.alive and not self.spectate_only and not player.noclip and player.char.pos.z < self._kill_z:
            self._out_of_world(human)
        self._separate_characters()
        tac.fixed_update(dt)
        if now >= self._team_t:
            self._team_t = now + TEAM_THINK
            for tb in self.team_brains.values():
                tb.gadgets.update(now)
                tb.update(now)
        # the human's hit boxes follow the first-person character
        if human.alive and not self.spectate_only:
            c = player.char
            crouch = (c.stand_height - c.height) / max(c.stand_height - c.crouch_height, 1e-3)
            self.player_body.animate(dt, c.pos, player.yaw, player.pitch, crouch, c.vel, c.on_ground,
                                     lean=player.lean.amount)
        # death: spectate teammates after a moment
        if not human.alive:
            self._dead_t += dt
            if self._dead_t >= SPECTATE_DELAY and not self.spectator.active:
                if self.use_bots:
                    self.spectator.start()
                elif not player.noclip:
                    player.noclip = True
                    c = player.char
                    c.teleport((c.pos.x, c.pos.y, c.pos.z + 1.2))
        else:
            self._dead_t = 0.0

    def frame_update(self, dt: float, alpha: float = 1.0) -> None:
        self.bomb.update(dt, self.match.bomb_time_left())
        for a in self.standins:
            a.frame_update(dt)
        for b in self.bots:
            b.frame_update(dt, alpha)
        self.spectator.frame_update(dt)
        self.tactical.frame_update(dt, alpha)
        now = self.game.loop.time
        self.feed = [f for f in self.feed if now - f["t"] < 7.0]
        self.radio_log = [r for r in self.radio_log if now - r["t"] < 6.0]

    # --------------------------------------------------- round reset
    def _round_reset(self, match: Match, swapped: bool) -> None:
        g = self.game
        g.clear_world()
        self.shop.new_round()
        self.bomb.reset()
        self._plant_t = self._defuse_t = 0.0
        self._dead_t = 0.0
        self.spectator.stop()
        human = self.player_agent
        survived = {id(a): a.alive for a in match.participants}
        if match.round == 1:
            survived = {k: False for k in survived}
        spawns = {s: [sp for sp in g.level.spawns if sp["team"] == s] for s in SIDES}
        for s in spawns.values():
            self.rng.shuffle(s)
        # ---- the human
        player = g.player
        player.noclip = False
        inv = g.weapons.inv
        if self.spectate_only:
            player.damageable.health = 0.0
            player.damageable.alive = False
            inv.clear()
            sp = spawns["attack"][0] if spawns["attack"] else g.level.spawn_point()
            player.spawn(sp["pos"], sp.get("heading", 0.0))
            self.player_body.hide()
        else:
            kept = survived.get(id(human), False) and not swapped
            armor = player.damageable.armor if kept else 0.0
            helmet = player.damageable.helmet if kept else False
            player.damageable.reset(armor=armor, helmet=helmet)
            human.alive_flag = True
            if not kept:
                inv.clear()
                human.has_kit = False
                g.weapons.give_loadout(["knife", self.rules["teams"][human.side]["default_pistol"]], {})
            else:
                inv.has_bomb = False
                inv.refill_ammo()
            if human.side != "defend":
                human.has_kit = False
            pool = spawns[human.side]
            sp = pool.pop(0) if pool else g.level.spawn_point()
            player.spawn(sp["pos"], sp.get("heading", 0.0))
            player.char.vel = Vec3(0, 0, 0)
            player.char.prev_pos = Point3(player.char.pos)
            self.player_body.reset()
        # ---- bots
        for b in self.bots:
            kept = survived.get(id(b), False) and not swapped
            w = b.weapons
            armor = b.damageable.armor if kept else 0.0
            helmet = b.damageable.helmet if kept else False
            b.damageable.reset(armor=armor, helmet=helmet)
            b.alive_flag = True
            if not kept:
                w.inv.clear()
                b.has_kit = False
                w.give_loadout(["knife", self.rules["teams"][b.side]["default_pistol"]], {})
            else:
                w.inv.has_bomb = False
                w.inv.refill_ammo()
                w.select(w.inv.best_slot(), force=True)
            if b.side != "defend":
                b.has_kit = False
            b.restyle()
            pos, heading = self._spawn_spot(spawns[b.side], b.side)
            b.spawn(pos, heading)
        # ---- practice stand-ins hold positions
        positions = {s: list(g.level.data.get("practice_positions", {}).get(s, [])) for s in SIDES}
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
        # ---- the bomb goes to a random attacker (stand-ins cannot plant)
        carriers = [a for a in match.participants if a.side == "attack" and not isinstance(a, StandInAgent)]
        if not self.use_bots and human.side == "attack":
            carriers = [human]
        if carriers:
            c = self.rng.choice(carriers)
            self.bomb.give(c)
            if c is human:
                inv.has_bomb = True
            else:
                c.weapons.inv.has_bomb = True
        elif self.standins:
            self.bomb.give(self.rng.choice([a for a in self.standins if a.side == "attack"] or self.standins))
        if not self.spectate_only:
            g.weapons.select(inv.best_slot(), force=True)
        self._bots_buy = bool(self.bots)
        self.tactical.round_reset()
        if self.spectate_only:
            self.spectator.start()
        g.renderer.post.reset_adaptation()

    def _spawn_spot(self, pool: list, side: str):
        if pool:
            sp = pool.pop(0)
            return sp["pos"], sp.get("heading", 0.0)
        spots = [s for s in self.game.level.spawns if s["team"] == side]
        sp = self.rng.choice(spots) if spots else self.game.level.spawn_point()
        p = sp["pos"]
        q = self.nav.random_point(self.rng, (p[0], p[1], p[2]), 3.0) if self.nav is not None else None
        return (q if q is not None else p), sp.get("heading", 0.0)

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
            if self.bomb.state == "carried" and self.bomb.carrier is agent:
                if agent.is_human:
                    self.drop_bomb(agent)
                else:
                    self.bomb.drop(agent.position())
                    if hasattr(agent, "weapons"):
                        agent.weapons.inv.has_bomb = False
            if agent.is_human:
                self.player_body.die()
                self._drop_human_weapon()
            tb = self.team_brains.get(agent.side)
            if tb is not None:
                tb.on_teammate_killed(agent, attacker, self.game.loop.time)
            self.match.on_kill(agent, attacker, weapon, reward, res.info.headshot, res.info.kind)

    def _separate_characters(self) -> None:
        """Characters block each other: overlapping bots are pushed apart (and off
        the human, who is never pushed), each push swept against the level."""
        chars = [b.char for b in self.bots if b.active and b.alive]
        player = self.game.player
        human = None
        if self.player_agent.alive and not self.spectate_only and not player.noclip:
            human = player.char
        if not chars:
            return
        min_d = chars[0].radius * 2.0 * 0.95
        others = chars + ([human] if human is not None else [])
        for i, a in enumerate(chars):
            for b in others[i + 1:]:
                dz = a.pos.z - b.pos.z
                if abs(dz) > 1.6:
                    continue
                dx, dy = a.pos.x - b.pos.x, a.pos.y - b.pos.y
                d2 = dx * dx + dy * dy
                if d2 >= min_d * min_d:
                    continue
                d = math.sqrt(d2)
                if d < 1e-4:
                    ang = self.rng.uniform(0, 2 * math.pi)
                    dx, dy, d = math.cos(ang), math.sin(ang), 1.0
                push = (min_d - math.sqrt(d2)) / d
                if b is human:
                    _nudge(a, Vec3(dx, dy, 0) * push)
                else:
                    _nudge(a, Vec3(dx, dy, 0) * (push * 0.5))
                    _nudge(b, Vec3(-dx, -dy, 0) * (push * 0.5))

    def _out_of_world(self, agent) -> None:
        """Safety net: anyone who falls out of the map dies (like a trigger_hurt)."""
        from gameplay.damage import DamageInfo
        self.game.log(f"[match] {agent.name} fell out of the world at {tuple(round(v, 1) for v in agent.position())}")
        agent.damageable.take_damage(DamageInfo(1000.0, 1.0, "chest", "fall", None, "world"))
        if hasattr(agent, "body"):
            agent.body.die()

    def _drop_human_weapon(self) -> None:
        """Like the bots, a dead player drops the best gun for anyone to pick up."""
        w = self.game.weapons
        inv = w.inv
        ws = inv.weapons.get("primary") or inv.weapons.get("secondary")
        if ws is None:
            return
        inv.weapons[ws.d.slot] = None
        w.drop_weapon_state(ws, throw=1.0)

    def _kill_reward(self, weapon: str) -> int:
        db = self.game.weapon_db
        if weapon in db.weapons:
            return int(db.weapons[weapon].kill_reward)
        if weapon in db.grenades:
            return int(db.grenades[weapon].get("kill_reward", 300))
        return int(self.rules["economy"]["kill_reward_default"])

    def apply_flash(self, pos: Point3, radius: float, max_blind: float) -> None:
        """Flashbang: blind the bots that can see it."""
        from weapons.grenades import flash_duration
        phys = self.game.physics
        for b in self.bots:
            if not (b.active and b.alive):
                continue
            eye = b.eye()
            to = pos - eye
            dist = to.length()
            if dist >= radius:
                continue
            if phys.ray_cast(eye, pos + (eye - pos).normalized() * 0.1, MASK_SIGHT) is not None:
                continue
            if self.game.effects.smoke_between(eye, pos) > 0.8:
                continue
            cos_a = b.view_dir().dot(to / max(dist, 1e-3))
            dur = flash_duration(dist, radius, cos_a, max_blind)
            b.flashed(dur, dur / max_blind)

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
            if hasattr(agent, "weapons"):
                agent.weapons.inv.has_bomb = False

    def site_at(self, pos):
        return self.game.level.zone_at((pos[0], pos[1], pos[2] + 0.1), "bombsite")

    def bot_plant(self, bot, site) -> bool:
        if self.match.phase != "live" or self.bomb.state != "carried" or self.bomb.carrier is not bot:
            return False
        w = bot.weapons
        w.inv.has_bomb = False
        w.select(w.inv.best_slot(), force=True)
        fwd = bot.forward()
        self.bomb.plant(bot.position() + fwd * 0.45, bot.aim.yaw, site["name"])
        self.match.on_bomb_planted(bot)
        return True

    def bot_defuse(self, bot) -> bool:
        if self.bomb.state != "planted" or self.match.phase != "planted":
            return False
        self.bomb.defused()
        self.match.on_bomb_defused(bot)
        return True

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
                   and g.input.is_down("fire") and not self.spectate_only)
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
    def radio(self, agent, text: str) -> None:
        """Bot radio messages, shown to the human's team (everyone when spectating)."""
        if not self.spectate_only and agent.side != self.player_agent.side:
            return
        now = self.game.loop.time
        self.radio_log.append({"t": now, "who": agent.name, "text": text, "side": agent.side})
        self.radio_log = self.radio_log[-5:]
        if agent.side == self.player_agent.side and not self.spectate_only:
            self.game.audio.play_ui("radio", 0.35)
        for cb in list(self.listeners):
            cb("radio", {"who": agent, "text": text})

    def _event(self, kind: str, **data) -> None:
        g = self.game
        if kind == "kill":
            self.feed.append({"t": g.loop.time, **data})
            self.feed = self.feed[-6:]
        elif kind == "round_end":
            won = data["result"].winner_side == self.player_agent.side or self.spectate_only
            g.audio.play_sting("win" if won else "lose")
        elif kind == "bomb_planted":
            g.audio.play_ui("bomb_planted", 0.6)
            g.audio.play_sting("planted", 0.6)
        elif kind == "phase" and data.get("phase") == "live":
            g.audio.play_sting("round_start", 0.6)
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
        if self.bomb.state == "carried":
            carrier = self.bomb.carrier
            if carrier is self.player_agent:
                self.game.weapons.inv.has_bomb = False
                self.game.weapons.select(self.game.weapons.inv.best_slot(), force=True)
            elif hasattr(carrier, "weapons"):
                carrier.weapons.inv.has_bomb = False
                carrier.weapons.select(carrier.weapons.inv.best_slot(), force=True)
        pos = Point3(c[0], c[1], site["min"][2] + 1.0)
        if self.nav is not None:
            q = self.nav.snap((c[0], c[1], site["min"][2] + 0.6), search=12)
            if q is not None:
                pos = Point3(*q)
        self.bomb.plant(pos, 0.0, site_name)
        self.match.on_bomb_planted(planter)
        return True


def _nudge(char, v: Vec3) -> None:
    """Move a character sideways by v, stopping at walls."""
    hit = char._trace(char.pos, char.pos + v)
    if hit is None:
        char.pos += v
    else:
        char.pos += v * max(hit[0] - 0.05, 0.0)


class _TeamProxy:
    """What a bot brain sees of its team: always the TeamBrain of the bot's
    *current* side (bots change sides at halftime)."""

    def __init__(self, director, bot):
        self.director = director
        self.bot = bot

    def _tb(self):
        return self.director.team_brains[self.bot.side]

    def __getattr__(self, name):
        return getattr(self._tb(), name)


def _short(v) -> str:
    if isinstance(v, Participant):
        return v.name
    if hasattr(v, "winner_side"):
        return f"{v.winner_side}/{v.reason}"
    if hasattr(v, "side") and hasattr(v, "score"):
        return f"team{v.index}"
    return str(v)
