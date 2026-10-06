"""Fairness audit: catches bot code that reads enemy state it could not know.

A bot may know where an enemy is only from what it sees (Perception), what
it hears (noise events carry their own, fuzzed positions), what teammates
call out on the radio, what the HUD radar shows, and pings from drones,
cameras and gadgets. It must never simply ask the enemy where it is.

In audit mode (``--audit`` or ``BOT_AUDIT=1``) every read of a
participant's live state - ``position``, ``head_pos``, ``center_of_mass``,
``velocity``, ``eye`` - made by code in the ``ai/`` package is checked:

* reads of teammates are fine (the radar shows them);
* the vision code (``Perception._scan`` / ``_clear``, the v2 vision
  helpers, ``GadgetAI._visible_gadget``) is where seeing happens, so its
  reads are the source of sight, not a use of it;
* a read of an enemy is fine while the reading bot currently sees it (its
  contact is ``seen`` in the last vision scan); for team-level code
  (team brains, gadget AI) while any teammate sees it;
* anything else is a violation, recorded with the reader, the accessor and
  the call site.

Two pieces of hidden state are checked as well:

* which attacker carries the charge: it is invisible on the carrier, so
  only attackers may read ``Bomb.carrier``;
* enemy gadgets: their position may be read once a teammate has had them
  in sight (``tick`` keeps that record); map cameras are public.

``report()`` groups violations by AI (legacy / v2), kind and call site.
v2 code is additionally checked action by action (``check_order``): every
order to aim at, fire at, pre-aim or path to a point because of an enemy
names the knowledge it acts on (ai/v2/knowledge.py).
"""
from __future__ import annotations

import math
import os
import sys
from collections import defaultdict

AI_ROOT = os.path.dirname(os.path.abspath(__file__))
ACCESSORS = ("position", "head_pos", "center_of_mass", "velocity", "eye")
VISION_FUNCS = {"_scan", "_clear", "_visible_gadget", "_sight_test", "_vision_scan", "_gadget_in_sight"}
GADGET_SIGHT = 30.0


class FairnessAudit:
    def __init__(self, director=None, roots: list[str] | None = None, install: bool = True):
        self.director = director
        self.roots = [os.path.abspath(r) for r in (roots or [AI_ROOT])]
        self.counts: dict[tuple, int] = defaultdict(int)
        self.examples: dict[tuple, str] = {}
        self.orders = 0
        self.checked_reads = 0
        self._patched: list[tuple] = []
        self.seen_gadgets: dict[str, set[int]] = {"attack": set(), "defend": set()}
        self._next_tick = 0.0
        if install:
            self.install()

    # ------------------------------------------------------------ install
    def install(self) -> None:
        from ai.bot import BotAgent
        from gameplay.agents import PlayerAgent, StandInAgent
        for cls in (BotAgent, PlayerAgent, StandInAgent):
            self.watch_class(cls)
        self._watch_gadgets()
        self._watch_carrier()

    def uninstall(self) -> None:
        bomb = self.director.bomb if self.director is not None else None
        if bomb is not None and "_audit_carrier" in bomb.__dict__:
            carrier = bomb.__dict__.pop("_audit_carrier")
        else:
            carrier = None
        for cls, name, orig in reversed(self._patched):
            if orig is None:
                delattr(cls, name)
            else:
                setattr(cls, name, orig)
        self._patched = []
        if bomb is not None:
            bomb.carrier = carrier

    def watch_class(self, cls, accessors=ACCESSORS) -> None:
        """Wrap the state accessors of an agent class."""
        for name in accessors:
            orig = cls.__dict__.get(name)
            if orig is None or isinstance(orig, property):
                continue
            audit = self

            def wrapped(agent, *a, _orig=orig, _name=name, **kw):
                audit._check_read(agent, _name, sys._getframe(1))
                return _orig(agent, *a, **kw)
            wrapped.__name__ = name
            self._patched.append((cls, name, orig))
            setattr(cls, name, wrapped)

    def _watch_gadgets(self) -> None:
        import gameplay.gadgets as gm
        import gameplay.observation as om
        audit = self
        for mod in (gm, om):
            for obj in list(vars(mod).values()):
                if isinstance(obj, type) and "center" in obj.__dict__ and hasattr(obj, "kind"):
                    orig = obj.__dict__["center"]

                    def wrapped(g, *a, _orig=orig, **kw):
                        audit._check_gadget(g, sys._getframe(1))
                        return _orig(g, *a, **kw)
                    self._patched.append((obj, "center", orig))
                    setattr(obj, "center", wrapped)

    def _watch_carrier(self) -> None:
        from gameplay.bomb import Bomb
        if isinstance(Bomb.__dict__.get("carrier"), property):
            return
        audit = self
        bomb = self.director.bomb if self.director is not None else None
        if bomb is not None and "carrier" in bomb.__dict__:
            bomb.__dict__["_audit_carrier"] = bomb.__dict__.pop("carrier")

        def get(b):
            audit._check_carrier(sys._getframe(1))
            return b.__dict__.get("_audit_carrier")

        def put(b, v):
            b.__dict__["_audit_carrier"] = v
        self._patched.append((Bomb, "carrier", None))
        Bomb.carrier = property(get, put)

    # -------------------------------------------------------------- reader
    def _in_ai(self, frame) -> bool:
        f = frame.f_code.co_filename
        if f == __file__:
            return False                    # nested wrapper (an alias of a wrapped accessor)
        return any(f.startswith(r) for r in self.roots)

    def _reader(self, frame):
        """(reader bot or None, reader side, ai kind, frame of the AI call site)."""
        f = frame
        site = None
        for _ in range(6):
            if f is None or not self._in_ai(f):
                break
            site = site or f
            obj = f.f_locals.get("self")
            if obj is not None:
                bot = getattr(obj, "bot", None)
                if bot is not None and hasattr(bot, "side"):
                    return bot, bot.side, self._ai_of(bot), site
                side = getattr(obj, "side", None) or getattr(getattr(obj, "team", None), "side", None)
                if isinstance(side, str) and side:
                    kind = getattr(obj, "ai", None) or getattr(getattr(obj, "team", None), "ai", None) or "legacy"
                    if hasattr(obj, "perception") and hasattr(obj, "brain"):
                        return obj, side, self._ai_of(obj), site       # a BotAgent method
                    return None, side, kind, site
            f = f.f_back
        return None, "", "?", site

    def _ai_of(self, bot) -> str:
        brain = getattr(bot, "brain", None)
        return getattr(brain, "ai", "legacy") if brain is not None else "?"

    # ------------------------------------------------------------- checks
    def _check_read(self, agent, accessor: str, frame) -> None:
        if not self._in_ai(frame):
            return
        reader, side, kind, site = self._reader(frame)
        if reader is agent or not side:
            return
        enemy_side = getattr(agent, "side", "")
        if not enemy_side or enemy_side == side:
            return
        self.checked_reads += 1
        if frame.f_code.co_name in VISION_FUNCS:
            return
        if reader is not None:
            c = reader.perception.contacts.get(id(agent)) if hasattr(reader, "perception") else None
            if c is not None and c.seen:
                return
        elif self._team_sees(side, agent):
            return
        self._violation(kind, f"enemy {accessor}() without sight", site or frame,
                        f"{getattr(reader, 'name', side)} read {getattr(agent, 'name', '?')}")

    def _team_sees(self, side: str, agent) -> bool:
        d = self.director
        if d is None:
            return False
        for b in d.bots:
            if b.side == side and b.active and b.alive:
                c = b.perception.contacts.get(id(agent))
                if c is not None and c.seen:
                    return True
        return False

    def _check_carrier(self, frame) -> None:
        if not self._in_ai(frame):
            return
        reader, side, kind, site = self._reader(frame)
        if side == "defend":
            self._violation(kind, "charge carrier identity (hidden)", site or frame,
                            getattr(reader, "name", "defenders"))

    def _check_gadget(self, g, frame) -> None:
        if not self._in_ai(frame) or frame.f_code.co_name in VISION_FUNCS:
            return
        reader, side, kind, site = self._reader(frame)
        gside = getattr(g, "side", "")
        if not side or not gside or gside == side or getattr(g, "kind", "") == "camera":
            return
        if id(g) in self.seen_gadgets.get(side, ()):
            return
        self._violation(kind, f"unseen enemy gadget ({g.kind})", site or frame, getattr(reader, "name", side))

    def check_order(self, bot, what: str, point, fact, now: float, tolerance: float = 0.5) -> bool:
        """v2: an aim / fire / pre-aim / path order at ``point`` acting on ``fact``.
        The fact must exist, be known to the bot by now, and the point must lie
        within the fact's uncertainty plus how far the enemy could have moved."""
        self.orders += 1
        if fact is None:
            self._violation("v2", f"{what} order without knowledge", None, bot.name)
            return False
        if fact.time > now + 1e-6:
            self._violation("v2", f"{what} order on future knowledge", None, bot.name)
            return False
        return True

    def _violation(self, kind: str, what: str, frame, who: str) -> None:
        if frame is not None:
            site = f"{os.path.relpath(frame.f_code.co_filename, os.path.dirname(AI_ROOT))}:{frame.f_lineno} " \
                   f"{frame.f_code.co_name}"
        else:
            site = "-"
        key = (kind, what, site)
        self.counts[key] += 1
        if key not in self.examples:
            t = self.director.game.loop.time if self.director is not None else 0.0
            self.examples[key] = f"t={t:.1f} {who}"

    # ---------------------------------------------------------------- tick
    def tick(self, now: float) -> None:
        """Remember which enemy gadgets each side has had in sight."""
        if now < self._next_tick or self.director is None:
            return
        self._next_tick = now + 0.25
        d = self.director
        tac = d.tactical
        from engine.physics import MASK_SIGHT
        phys = d.game.physics
        gadgets = [g for g in list(tac.deployables) + list(tac.drones) if g.alive]
        for side in ("attack", "defend"):
            seen = self.seen_gadgets[side]
            eyes = [b for b in d.bots if b.side == side and b.active and b.alive]
            for g in gadgets:
                if g.side == side or id(g) in seen:
                    continue
                p = g.center()                  # audit.py has no reader side: not itself checked
                for b in eyes:
                    e = b.eye()
                    v = p - e
                    dist = v.length()
                    if dist > GADGET_SIGHT or dist < 1e-3:
                        continue
                    fwd = b.view_dir()
                    if v.dot(fwd) / dist < math.cos(math.radians(float(b.profile.get("fov", 110)) * 0.5)):
                        continue
                    hit = phys.ray_cast(e, p, MASK_SIGHT)
                    if hit is None or (hit.pos - p).length() < 0.3:
                        seen.add(id(g))
                        break

    def round_reset(self) -> None:
        for s in self.seen_gadgets.values():
            s.clear()

    # -------------------------------------------------------------- report
    def total(self, kind: str | None = None) -> int:
        return sum(n for (k, _, _), n in self.counts.items() if kind is None or k == kind)

    def report(self) -> list[str]:
        lines = [f"[audit] {self.checked_reads} enemy reads checked, {self.orders} v2 orders checked; "
                 f"violations: legacy {self.total('legacy')}, v2 {self.total('v2')}"]
        for (kind, what, site), n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"[audit]   {kind:6s} {n:5d}  {what}  @ {site}  (first: {self.examples[(kind, what, site)]})")
        return lines

    def summary(self) -> dict:
        return {"checked_reads": self.checked_reads, "orders": self.orders,
                "violations": {"legacy": self.total("legacy"), "v2": self.total("v2"), "other": self.total("?")},
                "by_site": [{"ai": k, "what": w, "site": s, "count": n, "first": self.examples[(k, w, s)]}
                            for (k, w, s), n in sorted(self.counts.items(), key=lambda kv: -kv[1])]}
