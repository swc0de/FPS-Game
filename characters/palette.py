"""Palette slots: every character vertex names a slot, and a body's slot table
(colour, roughness, surface kind, ...) is a shader uniform.

One body is one mesh and one draw call: skin, hair, eyes, clothing and
gear all share it, and the shader (render/shaders/character.frag) picks the
shading model per slot. Swapping a team's uniform at halftime only rewrites
the table (the face stays, the geometry is untouched), and both teams share
the same meshes.

Kinds: 0 skin (wrap diffuse, curvature tint, pores), 1 fabric (sheen,
weave), 2 hard (polymer, painted metal), 3 eye (cornea gloss), 4 hair
(strand noise, anisotropic sheen), 5 lens (dark, glossy), 6 metal.
"""
from __future__ import annotations

import numpy as np

SLOTS = ("skin", "lips", "hair", "eye", "shirt", "trousers", "gloves", "boots", "carrier", "pouch", "webbing",
         "helmet", "helmet_cover", "hard", "lens", "cap", "balaclava", "charge")
SLOT = {name: i for i, name in enumerate(SLOTS)}
N_SLOTS = 24                       # uniform array size (render/shaders/character.frag)

KIND = {"skin": 0, "lips": 0, "hair": 4, "eye": 3, "shirt": 1, "trousers": 1, "gloves": 1, "boots": 2,
        "carrier": 1, "pouch": 1, "webbing": 1, "helmet": 2, "helmet_cover": 1, "hard": 2, "lens": 5, "cap": 1,
        "balaclava": 1, "charge": 2}

# per-team gear colours (sRGB, converted to linear in table()); plain solids, no camouflage
TEAMS = {
    "vanguard": {   # lighter, warm
        "shirt": (0.60, 0.53, 0.40), "trousers": (0.55, 0.49, 0.37), "gloves": (0.36, 0.32, 0.26),
        "boots": (0.42, 0.34, 0.24), "carrier": (0.62, 0.55, 0.42), "pouch": (0.58, 0.51, 0.38),
        "webbing": (0.45, 0.40, 0.31), "helmet": (0.52, 0.47, 0.36), "helmet_cover": (0.63, 0.56, 0.43),
        "hard": (0.16, 0.15, 0.13), "cap": (0.57, 0.50, 0.38), "balaclava": (0.48, 0.43, 0.34),
    },
    "bastion": {    # darker, cool
        "shirt": (0.27, 0.30, 0.31), "trousers": (0.24, 0.27, 0.28), "gloves": (0.12, 0.12, 0.12),
        "boots": (0.10, 0.10, 0.10), "carrier": (0.22, 0.25, 0.27), "pouch": (0.20, 0.23, 0.25),
        "webbing": (0.15, 0.17, 0.18), "helmet": (0.19, 0.21, 0.22), "helmet_cover": (0.25, 0.28, 0.29),
        "hard": (0.09, 0.09, 0.09), "cap": (0.23, 0.26, 0.27), "balaclava": (0.12, 0.12, 0.13),
    },
}

ROUGH = {"skin": 0.5, "lips": 0.42, "hair": 0.55, "eye": 0.08, "shirt": 0.9, "trousers": 0.9, "gloves": 0.8,
         "boots": 0.6, "carrier": 0.88, "pouch": 0.88, "webbing": 0.85, "helmet": 0.55, "helmet_cover": 0.92,
         "hard": 0.45, "lens": 0.1, "cap": 0.9, "balaclava": 0.95, "charge": 0.5}


def skin_tone(melanin: float, haemoglobin: float = 0.5) -> np.ndarray:
    """Skin albedo from two pigments (a simplified two-layer model): melanin in the epidermis
    absorbs strongly towards blue and darkens the whole tone, haemoglobin in the dermis
    absorbs green and blue and adds red. ``melanin`` 0 (very fair) .. 1 (very dark)."""
    m = np.clip(melanin, 0.0, 1.0)
    h = np.clip(haemoglobin, 0.0, 1.0)
    # absorption per channel (R, G, B), roughly following the pigments' spectra
    mel = np.array([0.55, 1.0, 1.6]) * (0.25 + 3.2 * m ** 1.4)
    hb = np.array([0.08, 0.55, 0.45]) * (0.4 + 0.8 * h)
    return np.exp(-(mel + hb) * 0.55) * np.array([1.0, 0.93, 0.86])


def table(team: str, skin: np.ndarray, hair: np.ndarray, eyes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(N_SLOTS, 4) linear colour + roughness and (N_SLOTS, 4) kind, metallic, sheen, wear for one
    body. ``skin``, ``hair`` and ``eyes`` are linear."""
    col = np.zeros((N_SLOTS, 4), np.float32)
    par = np.zeros((N_SLOTS, 4), np.float32)
    team_cols = TEAMS[team]
    for name, i in SLOT.items():
        if name == "skin":
            c = skin
        elif name == "lips":
            c = skin * np.array([0.92, 0.72, 0.72])
        elif name == "hair":
            c = hair
        elif name == "eye":
            c = eyes
        elif name == "lens":
            c = np.array([0.03, 0.035, 0.04])
        elif name == "charge":
            c = np.array([0.25, 0.24, 0.2])
        else:
            c = np.array(team_cols[name]) ** 2.2
        col[i, :3] = c
        col[i, 3] = ROUGH[name]
        par[i, 0] = KIND[name]
        par[i, 1] = 0.0
        par[i, 2] = 0.5 if KIND[name] == 1 else 0.0        # fabric sheen
        par[i, 3] = 0.6 if KIND[name] in (1, 2) else 0.0   # wear and grime
    return col, par
