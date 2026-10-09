"""Human error for the v2 bots: reactions, attention, flicks, stress, mistakes.

None of this makes a bot better than the Milestone 5 brain mechanically -
it uses the same profile numbers (data/bots.json "difficulty") - it only
shapes the errors the way people make them:

* **Reaction time**: lognormal (sigma ``reaction_sigma``) instead of uniform,
  with the same mean as the profile's range: usually about average, now and
  then much slower, never much faster (clipped at 0.85 x the fastest).
* **Attention**: a bot fighting one enemy notices a second one late, and
  later still when it is far from where it is aiming (``tunnel``).
* **Flicks**: a big turn onto a new target overshoots or undershoots
  (``flick`` decides which, mostly over) and is corrected by the aim
  controller's error decay, like a hand. Only the side of the error changes,
  not its size: the aim profile stays the Milestone 5 one.
* **Stress** (0..1): rises when hit or flashed, decays in a few seconds;
  makes choices noisier and mistakes likelier (not the aim: that would
  change the profile).
* **Mistakes** by difficulty (probabilities per opportunity): over-peeking,
  skipping a corner while clearing, reloading in the open, trading late,
  ignoring a callout, freezing when flanked.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "easy": {"tunnel": 1.8, "flick": [0.18, 0.12], "stress_gain": 1.0, "decision_noise": 0.35,
             "over_peek": 0.35, "skip_corner": 0.3, "reload_open": 0.35, "late_trade": 0.5,
             "ignore_call": 0.25, "panic": 0.35, "radar_glance": [3.0, 6.0]},
    "normal": {"tunnel": 1.5, "flick": [0.12, 0.09], "stress_gain": 0.8, "decision_noise": 0.2,
               "over_peek": 0.15, "skip_corner": 0.15, "reload_open": 0.15, "late_trade": 0.25,
               "ignore_call": 0.1, "panic": 0.15, "radar_glance": [2.0, 4.0]},
    "hard": {"tunnel": 1.35, "flick": [0.09, 0.07], "stress_gain": 0.6, "decision_noise": 0.12,
             "over_peek": 0.07, "skip_corner": 0.07, "reload_open": 0.06, "late_trade": 0.12,
             "ignore_call": 0.05, "panic": 0.06, "radar_glance": [1.5, 3.0]},
    "expert": {"tunnel": 1.25, "flick": [0.06, 0.05], "stress_gain": 0.45, "decision_noise": 0.06,
               "over_peek": 0.03, "skip_corner": 0.03, "reload_open": 0.02, "late_trade": 0.05,
               "ignore_call": 0.0, "panic": 0.02, "radar_glance": [1.0, 2.5]},
}


class Humanizer:
    def __init__(self, bot, cfg: dict | None = None):
        self.bot = bot
        self.rng = bot.rng
        diff = getattr(bot, "difficulty", "normal")
        self.c = dict(DEFAULTS.get(diff, DEFAULTS["normal"]))
        if cfg:
            self.c.update(cfg.get(diff, {}))
        self.sigma = float(self.c.get("reaction_sigma", 0.25))
        self.stress = 0.0

    def reset(self) -> None:
        self.stress = 0.0

    # ------------------------------------------------------------ reaction
    def reaction(self, lo: float, hi: float) -> float:
        mean = 0.5 * (lo + hi)
        s = self.sigma
        mu = math.log(max(mean, 1e-3)) - 0.5 * s * s
        r = math.exp(self.rng.gauss(mu, s))
        return min(max(r, 0.85 * lo), 2.5 * hi)

    def attention(self, angle_from_aim: float, busy: bool) -> float:
        """Reaction multiplier for a newly seen enemy (angle in degrees from where the bot aims)."""
        if not busy:
            return 1.0
        t = float(self.c["tunnel"])
        return t if angle_from_aim > 40.0 else 1.0 + (t - 1.0) * 0.4

    # --------------------------------------------------------------- flick
    def flick(self, turn_deg: float) -> float:
        """Overshoot (+) or undershoot (-) in degrees for a turn of turn_deg onto a target."""
        if turn_deg < 12.0:
            return 0.0
        mean, sd = self.c["flick"]
        frac = self.rng.gauss(mean, sd) * (1 if self.rng.random() < 0.7 else -1)
        return turn_deg * frac

    # -------------------------------------------------------------- stress
    def add_stress(self, amount: float) -> None:
        self.stress = min(1.0, self.stress + amount * float(self.c["stress_gain"]))

    def update(self, dt: float) -> None:
        self.stress = max(0.0, self.stress - dt / 4.0)


    def noise(self) -> float:
        return float(self.c["decision_noise"]) * (1.0 + self.stress)

    # ------------------------------------------------------------ mistakes
    def mistake(self, kind: str) -> bool:
        p = float(self.c.get(kind, 0.0)) * (1.0 + 0.5 * self.stress)
        return self.rng.random() < p

    def radar_interval(self) -> float:
        lo, hi = self.c["radar_glance"]
        return self.rng.uniform(lo, hi)
