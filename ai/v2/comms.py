"""The v2 bots' team radio: callouts arrive late and only as precise as words.

A human hears "Two, B Long, one tagged" a moment after a teammate saw them,
and knows the area, not the exact spot. The radio models that:

* A sighting becomes a callout after a per-message delay (lognormal around
  ``delay`` seconds, clipped to ``delay_range``; longer when the speaker is
  fighting or stressed).
* Its position is snapped to the callout area: the nearest tactical point,
  fuzzed by ``fuzz`` metres; the fact's radius says so.
* Sightings in the same area within ``batch`` seconds are spoken as one line
  ("Two B Long, one tagged"); "tagged" and "low" only come from damage the
  speaker itself dealt (its hit markers), never from the enemy's real health.
* Listeners can miss a call (``miss`` by difficulty); a dead bot can still
  get one last call out ("He's in B Long!") if it saw its killer.
* Footsteps and shots a bot hears are called the same way, twice as vague
  ("Footsteps A Ramp").
* Plain calls ("Falling back", "I'll trade you", "Rotating B", "Lurking mid",
  "Flash going A") go out as text, rate-limited per speaker and kind.

Delivery is ``update(now)``: every due message is handed to the team, which
adds the fact to its possibility field and to every living teammate's
knowledge (ai/v2/knowledge.py).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from ai.v2.knowledge import Fact

NUMBERS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


@dataclass(order=True)
class Message:
    deliver: float
    seq: int
    fact: Fact = field(compare=False)
    speaker: str = field(compare=False, default="")


class Radio:
    def __init__(self, team, tm, rng, cfg: dict | None = None):
        self.team = team
        self.tm = tm
        self.rng = rng
        cfg = cfg or {}
        self.delay = float(cfg.get("delay", 0.55))
        self.delay_range = tuple(cfg.get("delay_range", (0.3, 1.2)))
        self.fuzz = float(cfg.get("fuzz", 2.5))
        self.batch = float(cfg.get("batch", 0.6))
        self.miss = float(cfg.get("miss", 0.0))
        self.queue: list[Message] = []
        self._seq = 0
        self._last_call: dict[tuple, float] = {}
        self._pending_lines: dict[tuple, dict] = {}    # (area, heard) -> {"t", "enemies", "hurt", "speaker"}
        self._said: dict[str, float] = {}

    def reset(self) -> None:
        self.queue = []
        self._last_call = {}
        self._pending_lines = {}

    # ------------------------------------------------------------- calling
    def draw_delay(self, stressed: bool = False) -> float:
        lo, hi = self.delay_range
        d = self.delay * math.exp(self.rng.gauss(0.0, 0.35))
        if stressed:
            d *= 1.4
        return max(lo, min(hi * (1.4 if stressed else 1.0), d))

    def sighting(self, speaker, enemy_key: int, pos, now: float, hurt: bool = False,
                 stressed: bool = False, every: float = 1.5, heard: bool = False) -> bool:
        """``speaker`` saw (or ``heard``) an enemy at ``pos``: a callout later reaches the team."""
        k = (speaker.name, enemy_key)
        if now - self._last_call.get(k, -99.0) < every:
            return False
        self._last_call[k] = now
        i = self.tm.nearest(pos)
        area = self.tm.area(i) if i >= 0 else ""
        base = self.tm.pos[i] if i >= 0 else pos
        a = self.rng.uniform(0, 2 * math.pi)
        r = self.fuzz * math.sqrt(self.rng.random())
        snapped = (float(base[0]) + math.cos(a) * r, float(base[1]) + math.sin(a) * r, float(base[2]))
        fuzz = self.fuzz * (2.0 if heard else 1.0)
        f = Fact(enemy_key, snapped, fuzz + 1.0, now, "radio", by=speaker.name, hurt=hurt, area=area)
        self._push(f, now + self.draw_delay(stressed), speaker.name)
        line = self._pending_lines.setdefault((area, heard), {"t": now, "enemies": set(), "hurt": 0,
                                                              "speaker": speaker})
        line["enemies"].add(enemy_key)
        line["hurt"] += int(hurt)
        return True

    def last_words(self, speaker, killer_key: int, pos, now: float, hurt: bool) -> None:
        """A dying bot that saw its killer gets one call out."""
        self._last_call.pop((speaker.name, killer_key), None)
        self.sighting(speaker, killer_key, pos, now, hurt=hurt, stressed=True, every=0.0)

    def say(self, speaker, text: str, key: str | None = None, every: float = 4.0) -> bool:
        k = f"{speaker.name}:{key or text}"
        now = self.team.game.loop.time
        if now - self._said.get(k, -99.0) < every:
            return False
        self._said[k] = now
        self.team.radio(speaker, text, key=key, every=0.5)
        return True

    def _push(self, f: Fact, when: float, speaker: str) -> None:
        self._seq += 1
        heapq.heappush(self.queue, Message(when, self._seq, f, speaker))

    # ------------------------------------------------------------ delivery
    def update(self, now: float) -> list[Fact]:
        out = []
        while self.queue and self.queue[0].deliver <= now:
            m = heapq.heappop(self.queue)
            if self.miss > 0.0 and self.rng.random() < self.miss:
                continue
            out.append(m.fact)
            self.team.on_radio_fact(m.fact, m.speaker)
        # spoken summary lines, once the batch window has passed
        for key in [a for a, ln in self._pending_lines.items() if now - ln["t"] >= self.batch]:
            ln = self._pending_lines.pop(key)
            area, heard = key
            n = len(ln["enemies"])
            if heard:
                self.team.radio(ln["speaker"], f"Footsteps {area or 'close'}", key=f"steps:{area}", every=8.0)
                continue
            text = f"{NUMBERS.get(n, str(n))} {area or 'here'}"
            if ln["hurt"]:
                text += ", one tagged" if ln["hurt"] == 1 else f", {ln['hurt']} tagged"
            alive = getattr(self.team, "enemies_alive", 5)
            if alive == 1 and n == 1:
                text = f"Last one {area or 'here'}" + (", he's low" if ln["hurt"] else "")
            self.team.radio(ln["speaker"], text, key=f"call:{area}", every=3.0)
        return out
