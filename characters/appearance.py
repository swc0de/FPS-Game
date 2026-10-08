"""Who a soldier looks like: drawn once per bot from ``hash(seed, name)``.

The appearance seed is the bot's name (and the match seed), never its side,
so a bot keeps its face, build and hair every round and across halftime;
only the team's uniform and gear change with the side (Vanguard attacks,
Bastion defends).

Variety (OVERHAUL_PLAN 4.2, checked by tests/test_characters.py):
* men and women (about one in four);
* three builds (slim, average, heavy: soft volume only);
* six skin tones from a pigment model (characters/palette.py), jittered;
* face shape: a dozen proportions, each a small random departure from
  average (no two faces alike);
* hair and facial hair styles and colours that go with the skin tone;
* eye colour;
* per team: headgear (Vanguard: covered helmet with goggles, or a cap;
  Bastion: high-cut helmet with ear protection, or a cap with ear
  protection, or a balaclava), eyewear, sleeves, and the gear layout
  (radio side, magazine pouches, admin pouch, holster).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

import numpy as np

from characters.human import Build, Face
from characters.palette import skin_tone

MELANIN = (0.04, 0.18, 0.36, 0.55, 0.74, 0.92)        # six base tones, very fair .. very dark

HAIR_COLOURS = {          # sRGB
    "black": (0.05, 0.045, 0.04), "dark_brown": (0.13, 0.09, 0.06), "brown": (0.26, 0.17, 0.10),
    "light_brown": (0.42, 0.30, 0.18), "blond": (0.62, 0.50, 0.32), "red": (0.45, 0.20, 0.09),
    "grey": (0.48, 0.47, 0.45),
}
EYE_COLOURS = {"brown": (0.25, 0.14, 0.07), "dark": (0.1, 0.06, 0.04), "hazel": (0.35, 0.27, 0.12),
               "green": (0.22, 0.32, 0.18), "blue": (0.25, 0.38, 0.52), "grey": (0.38, 0.42, 0.44)}


@dataclass
class Appearance:
    name: str
    build: Build
    face: Face
    melanin: float
    haemoglobin: float
    hair_colour: str
    hair: str                 # "shaved" | "buzz" | "crew" | "short" | "bun"
    facial_hair: str          # "none" | "stubble" | "short_beard" | "full_beard" | "moustache"
    eye_colour: str
    seed: int
    team: dict = field(default_factory=dict)     # style -> TeamLook

    def skin_rgb(self) -> np.ndarray:
        return skin_tone(self.melanin, self.haemoglobin)

    @property
    def makehuman(self) -> bool:
        """Whether the face comes from the fetched MakeHuman files (characters/makehuman.py);
        without them, or with COLD_SECTOR_OFFLINE=1, it is the procedural head."""
        from characters import makehuman
        return makehuman.data_dir() is not None

    def key(self, style: str) -> str:
        """A stable cache key for this appearance in one team's kit (and the head's source)."""
        src = "mh" if self.makehuman else "proc"
        return hashlib.sha1(repr((self.name, self.seed, style, CHARACTER_VERSION, src)).encode()).hexdigest()[:16]


@dataclass
class TeamLook:
    style: str                # "vanguard" | "bastion"
    headgear: str             # "helmet" | "cap" | "balaclava"
    cover: bool
    ear_pro: bool
    goggles: bool
    glasses: bool
    sleeves: str              # "full" | "rolled"
    gloves: bool
    layout: dict


CHARACTER_VERSION = 4         # bump to rebuild every cached body


def _rng(seed: int, name: str, salt: str = "") -> random.Random:
    h = hashlib.sha256(f"{seed}:{name}:{salt}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "little"))


def appearance(name: str, seed: int = 0) -> Appearance:
    r = _rng(seed, name)
    female = r.random() < 0.25
    build = Build(female=female, mass=r.choice((-1.0, 0.0, 1.0)), muscle=r.uniform(0.1, 0.8))

    def j(sd: float) -> float:
        return float(np.clip(r.gauss(1.0, sd), 1.0 - 2.5 * sd, 1.0 + 2.5 * sd))

    face = Face(width=j(0.04), jaw=j(0.08), chin=j(0.1), chin_forward=r.gauss(0.0, 0.004),
                cheekbones=j(0.12), brow=j(0.15), nose_length=j(0.08), nose_width=j(0.1), nose_bridge=j(0.12),
                nose_hook=r.gauss(0.0, 0.002), lips=j(0.12), mouth_width=j(0.06), eye_spacing=j(0.04),
                eye_depth=j(0.15), ears=j(0.08), cranium_length=j(0.04), forehead_slope=abs(r.gauss(0.0, 0.006)),
                neck=j(0.06))
    tone = r.randrange(len(MELANIN))
    melanin = float(np.clip(MELANIN[tone] + r.gauss(0.0, 0.03), 0.0, 1.0))
    haemo = r.uniform(0.3, 0.7)
    if melanin > 0.5:
        hair_colour = r.choice(("black", "black", "dark_brown"))
    else:
        hair_colour = r.choice(("black", "dark_brown", "brown", "brown", "light_brown", "blond", "red"))
    if r.random() < 0.1:
        hair_colour = "grey"
    if female:
        hair = r.choice(("bun", "bun", "short"))
        facial = "none"
    else:
        hair = r.choice(("shaved", "buzz", "buzz", "crew", "crew", "short"))
        facial = r.choice(("none", "none", "stubble", "stubble", "short_beard", "full_beard", "moustache"))
    if melanin > 0.5:
        eye = r.choice(("dark", "brown", "brown"))
    else:
        eye = r.choice(("brown", "brown", "hazel", "green", "blue", "blue", "grey"))
    a = Appearance(name, build, face, melanin, haemo, hair_colour, hair, facial, eye, seed)
    for style in ("vanguard", "bastion"):
        a.team[style] = _team_look(name, seed, style)
    return a


def _team_look(name: str, seed: int, style: str) -> TeamLook:
    r = _rng(seed, name, style)
    if style == "vanguard":
        headgear = r.choices(("helmet", "cap"), (0.8, 0.2))[0]
        ear_pro = False
        cover = True
        goggles = headgear == "helmet" and r.random() < 0.75
    else:
        headgear = r.choices(("helmet", "cap", "balaclava"), (0.7, 0.15, 0.15))[0]
        ear_pro = headgear in ("helmet", "cap")
        cover = r.random() < 0.25
        goggles = False
    glasses = not goggles and r.random() < 0.45
    layout = {"radio": r.choice(("l", "r")), "mags": r.choice((2, 3, 3, 4)), "admin": r.random() < 0.6,
              "holster": r.random() < 0.5}
    return TeamLook(style, headgear, cover, ear_pro, goggles, glasses,
                    sleeves=r.choices(("full", "rolled"), (0.8, 0.2))[0], gloves=r.random() < 0.75, layout=layout)


def style_for_side(side: str) -> str:
    return "vanguard" if side == "attack" else "bastion"
