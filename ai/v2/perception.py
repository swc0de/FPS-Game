"""Perception for the v2 bots: the Milestone 5 senses with human reactions.

Sight, hearing and smoke are exactly ai/perception.py (same view cone,
rays, ranges and scan rate). What changes is how long a bot takes to react
to an enemy that appears (ai/v2/humanize.py):

* the base time is a lognormal draw with the profile's mean;
* a bot busy with another enemy reacts later, more so when the new one is
  far from its aim (attention);
* an enemy the bot expected - its own knowledge put an enemy near there in
  the last 4 s (a callout, a sound, a recent sighting) - is recognised
  faster, as before; a team callout no longer counts unless the bot itself
  has received it.

The legacy team callouts (``on_callout``) and exact damage positions
(``on_damaged``) are not used: v2 keeps that knowledge in
ai/v2/knowledge.py, with its real precision.
"""
from __future__ import annotations

import math

from ai.perception import Contact, Perception


class PerceptionV2(Perception):
    def __init__(self, bot, vision_cfg: dict, profile: dict, humanizer, knowledge):
        super().__init__(bot, vision_cfg, profile)
        self.human = humanizer
        self.knowledge = knowledge
        self.busy_with = None           # id of the enemy the brain is fighting

    def on_callout(self, enemy, pos, t) -> None:
        pass

    def on_damaged(self, attacker, now: float) -> None:
        pass

    def _scan(self, now: float, enemies) -> None:
        bot = self.bot
        eye = bot.eye()
        yaw = math.radians(bot.aim.yaw)
        fwd = (-math.sin(yaw), math.cos(yaw))
        half_fov = math.radians(float(self.p.get("fov", 110)) * 0.5)
        half_per = math.radians(float(self.cfg.get("peripheral", 160)) * 0.5)
        per_range = float(self.cfg.get("peripheral_range", 14.0))
        rng_max = float(self.cfg.get("range", 90.0))
        blind = self.blind
        game = bot.game
        for e in enemies:
            c = self.contacts.get(id(e))
            visible, head_vis, body_vis, point = False, False, False, None
            if e.alive and not blind:
                head = e.head_pos()
                chest = e.center_of_mass()
                d = chest - eye
                dist = d.length()
                if dist <= rng_max:
                    hd = math.hypot(d.x, d.y)
                    cosang = (d.x * fwd[0] + d.y * fwd[1]) / max(hd, 1e-4)
                    ang = math.acos(max(-1.0, min(1.0, cosang)))
                    if ang <= half_fov or (ang <= half_per and dist <= per_range):
                        head_vis = self._clear(eye, head)
                        body_vis = self._clear(eye, chest)
                        if head_vis or body_vis:
                            target = head if head_vis and not body_vis else chest
                            if game.effects.smoke_between(eye, target) < float(self.cfg.get("smoke_block", 0.55)):
                                visible = True
                                point = target
            if visible:
                if c is None or not c.seen:
                    lo, hi = self.p.get("reaction", [0.3, 0.5])
                    react = self.human.reaction(lo, hi)
                    pos = e.position()
                    if c is not None and now - c.time < 2.0 and c.source == "sight":
                        react *= 0.4                     # just lost sight of it: faster re-acquire
                    elif self._expected(pos, now):
                        react *= 0.75
                    busy = self.busy_with is not None and self.busy_with != id(e)
                    if busy:
                        to = math.degrees(math.atan2(-(pos.x - eye.x), pos.y - eye.y))
                        react *= self.human.attention(abs((to - bot.aim.yaw + 180.0) % 360.0 - 180.0), True)
                    c = Contact(e, pos, now, "sight", True, now, now + react)
                    self.contacts[id(e)] = c
                    bot.on_spotted(e)
                c.pos = e.position()
                c.time = now
                c.source = "sight"
                c.seen = True
                c.aim_point = point
                c.head_visible = head_vis
                c.body_visible = body_vis
            elif c is not None and c.seen:
                c.seen = False

    def _expected(self, pos, now: float) -> bool:
        for f in self.knowledge.recent(4.0, now):
            if math.hypot(f.pos[0] - pos.x, f.pos[1] - pos.y) <= f.radius + 6.0:
                return True
        return False
