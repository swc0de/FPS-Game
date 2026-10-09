"""Human error for the v2 bots: reaction spread, stress, mistakes.

None of this makes a bot better than the Milestone 5 brain mechanically -
it uses the same profile numbers (data/bots.json "difficulty") - it only
shapes the errors the way people make them:

* **Reaction time**: lognormal (sigma ``reaction_sigma``) instead of uniform,
  with the same mean as the profile's range: usually about average, now and
  then much slower, never much faster (clipped at 0.85 x the fastest).
* No other aim or reaction error: an earlier version also reacted late to a
  second enemy while busy with one and picked the side of the first-shot
  error from the flick; both were removed (your decision, "equal
  mechanics"), so v2 aims and reacts exactly as the Milestone 5 profile.
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
    # mistakes made rarer (your decision, "lighter mistakes"): Normal has the old Expert rates;
    # Easy keeps its own, Hard and Expert make fewer still
    "easy": {"stress_gain": 1.0, "decision_noise": 0.35,
             "over_peek": 0.35, "skip_corner": 0.3, "reload_open": 0.35, "late_trade": 0.5,
             "ignore_call": 0.25, "panic": 0.35, "radar_glance": [3.0, 6.0]},
    "normal": {"stress_gain": 0.45, "decision_noise": 0.08,
               "over_peek": 0.03, "skip_corner": 0.03, "reload_open": 0.02, "late_trade": 0.05,
               "ignore_call": 0.0, "panic": 0.02, "radar_glance": [1.5, 3.0]},
    "hard": {"stress_gain": 0.35, "decision_noise": 0.06,
             "over_peek": 0.015, "skip_corner": 0.015, "reload_open": 0.01, "late_trade": 0.025,
             "ignore_call": 0.0, "panic": 0.01, "radar_glance": [1.2, 2.5]},
    "expert": {"stress_gain": 0.25, "decision_noise": 0.04,
               "over_peek": 0.01, "skip_corner": 0.01, "reload_open": 0.005, "late_trade": 0.01,
               "ignore_call": 0.0, "panic": 0.005, "radar_glance": [1.0, 2.0]},
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
