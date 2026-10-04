"""Procedural sound synthesis (numpy -> 16-bit mono WAV).

Every sound in the game is generated from noise, filtered noise and damped
oscillators, so the project ships no third-party audio. Building blocks:

* ``band(noise, lo, hi)``  - FFT band-pass with soft edges
* ``env(t, attack, decay)`` - exponential attack/decay envelopes
* ``modes(freqs, decays)``  - sums of damped sinusoids (metal pings, clicks)
* gunshot = sharp broadband crack + low "thump" + filtered decaying tail
  (a cheap stand-in for the room/outdoor reverb that sells a gunshot)
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

RATE = 44100


def _n(dur: float) -> int:
    return int(round(dur * RATE))


def _t(dur: float) -> np.ndarray:
    return np.arange(_n(dur), dtype=np.float32) / RATE


def noise(dur: float, rng) -> np.ndarray:
    return rng.standard_normal(_n(dur)).astype(np.float32)


def band(x: np.ndarray, lo: float, hi: float, soft: float = 0.25) -> np.ndarray:
    n = len(x)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    w_lo = 1.0 / (1.0 + np.exp(-(f - lo) / max(lo * soft, 1.0)))
    w_hi = 1.0 / (1.0 + np.exp((f - hi) / max(hi * soft, 1.0)))
    return np.fft.irfft(spec * w_lo * w_hi, n).astype(np.float32)


def env(t: np.ndarray, attack: float, decay: float, start: float = 0.0) -> np.ndarray:
    tt = t - start
    a = np.clip(tt / max(attack, 1e-4), 0, 1)
    d = np.exp(-np.maximum(tt - attack, 0) / max(decay, 1e-4))
    return np.where(tt < 0, 0.0, a * d).astype(np.float32)


def modes(t: np.ndarray, freqs, decays, amps, start: float = 0.0) -> np.ndarray:
    out = np.zeros_like(t)
    tt = t - start
    for f, d, a in zip(freqs, decays, amps):
        out += a * np.sin(2 * math.pi * f * tt) * np.exp(-np.maximum(tt, 0) / d)
    out[tt < 0] = 0
    return out


def click(t: np.ndarray, start: float, rng, hi=6000.0, decay=0.006, amp=1.0, body=1800.0) -> np.ndarray:
    n = band(noise(len(t) / RATE, rng), 800, hi) * env(t, 0.0005, decay, start)
    m = modes(t, [body, body * 2.3, body * 3.9], [0.01, 0.006, 0.004], [0.5, 0.3, 0.2], start)
    return (n * 0.8 + m) * amp


def normalize(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return (x / m * peak).astype(np.float32)


def write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


# ------------------------------------------------------------------ recipes
GUN_PROFILES = {
    # crack band, thump freq, tail length, tail band, body weight
    "rifle_heavy": dict(crack=(700, 9000), thump=72, tail=0.9, tail_band=(180, 2400), body=1.0, dur=1.3),
    "rifle_light": dict(crack=(900, 11000), thump=88, tail=0.75, tail_band=(220, 3000), body=0.85, dur=1.1),
    "smg": dict(crack=(1100, 10000), thump=110, tail=0.45, tail_band=(300, 3500), body=0.6, dur=0.7),
    "pistol": dict(crack=(1000, 9000), thump=120, tail=0.5, tail_band=(300, 3200), body=0.55, dur=0.8),
    "sniper": dict(crack=(500, 12000), thump=55, tail=1.6, tail_band=(120, 2000), body=1.4, dur=2.2),
    "shotgun": dict(crack=(400, 7000), thump=60, tail=1.1, tail_band=(120, 1800), body=1.3, dur=1.5),
}


def gunshot(profile: str, rng) -> np.ndarray:
    p = GUN_PROFILES[profile]
    t = _t(p["dur"])
    crack = band(noise(p["dur"], rng), *p["crack"]) * env(t, 0.0003, 0.018)
    thump = np.sin(2 * math.pi * p["thump"] * t * (1 + 0.6 * np.exp(-t / 0.03))) * env(t, 0.001, 0.07) * p["body"]
    mech = click(t, 0.0, rng, hi=7000, decay=0.01, amp=0.3)
    tail = band(noise(p["dur"], rng), *p["tail_band"]) * env(t, 0.01, p["tail"] * 0.35) * 0.35
    echo = band(noise(p["dur"], rng), 150, 1500) * env(t, 0.03, p["tail"] * 0.5, start=0.08) * 0.18
    x = crack * 1.0 + thump * 0.9 + mech + tail + echo
    return normalize(np.tanh(normalize(x, 1.0) * 1.6), 0.95)


def impact(kind: str, rng) -> np.ndarray:
    t = _t(0.45)
    n = noise(0.45, rng)
    if kind == "metal":
        x = modes(t, rng.uniform([1800, 3100, 4700, 6900], [2300, 3800, 5400, 7600]), [0.12, 0.08, 0.05, 0.03],
                  [0.6, 0.4, 0.3, 0.2]) + band(n, 2000, 12000) * env(t, 0.0005, 0.01) * 0.6
    elif kind == "wood":
        x = modes(t, rng.uniform([380, 900], [460, 1100]), [0.04, 0.02], [0.8, 0.4]) + \
            band(n, 500, 5000) * env(t, 0.0005, 0.02) * 0.6
    elif kind in ("dirt", "gravel"):
        x = band(n, 80, 1500) * env(t, 0.001, 0.05) + band(n, 2000, 8000) * env(t, 0.01, 0.08, 0.02) * \
            (0.25 if kind == "gravel" else 0.08)
    elif kind == "glass":
        x = modes(t, rng.uniform([2500, 4200, 6100], [3200, 5000, 7300]), [0.2, 0.15, 0.1], [0.4, 0.3, 0.3]) + \
            band(n, 3000, 14000) * env(t, 0.0005, 0.08) * 0.5
    elif kind in ("flesh", "dummy"):
        x = band(n, 60, 900) * env(t, 0.001, 0.04) + (modes(t, [520], [0.03], [0.4]) if kind == "dummy" else 0)
    elif kind == "fabric":
        x = band(n, 100, 2000) * env(t, 0.001, 0.03)
    else:  # concrete, plaster, tile, brick
        x = band(n, 900, 9000) * env(t, 0.0003, 0.012) + band(n, 200, 4000) * env(t, 0.002, 0.06) * 0.5 + \
            band(n, 3000, 9000) * env(t, 0.02, 0.1, 0.03) * 0.12
    return normalize(x, 0.8)


def footstep(kind: str, rng) -> np.ndarray:
    t = _t(0.25)
    n = noise(0.25, rng)
    heel = band(n, 60, 700) * env(t, 0.002, 0.025)
    if kind == "metal" or kind == "metal_grate":
        x = heel * 0.6 + modes(t, rng.uniform([600, 1500, 2600], [800, 1800, 3000]), [0.08, 0.05, 0.03],
                               [0.4, 0.25, 0.15]) * 0.5
    elif kind == "wood":
        x = heel + modes(t, rng.uniform([180, 420], [220, 500]), [0.05, 0.03], [0.4, 0.2])
    elif kind in ("dirt", "gravel"):
        x = heel * 0.7 + band(n, 1500, 9000) * env(t, 0.005, 0.06, 0.01) * (0.6 if kind == "gravel" else 0.3)
    elif kind == "fabric":
        x = band(n, 80, 1500) * env(t, 0.004, 0.05)
    else:  # concrete, tile, default
        x = heel + band(n, 2000, 8000) * env(t, 0.001, 0.015) * 0.4
    return normalize(x, 0.7)


def explosion(rng) -> np.ndarray:
    t = _t(3.0)
    n = noise(3.0, rng)
    boom = band(n, 25, 400) * env(t, 0.002, 0.35)
    crack = band(n, 600, 10000) * env(t, 0.0005, 0.03)
    rumble = band(n, 30, 200) * env(t, 0.05, 0.9) * 0.6
    debris = band(n, 1500, 7000) * env(t, 0.2, 0.5, 0.25) * 0.15
    return normalize(np.tanh(normalize(boom + crack * 0.7 + rumble + debris, 1.0) * 2.0), 0.95)


def flashbang(rng) -> np.ndarray:
    t = _t(1.8)
    n = noise(1.8, rng)
    x = band(n, 200, 12000) * env(t, 0.0003, 0.05) + band(n, 60, 600) * env(t, 0.002, 0.25) * 0.8 + \
        band(n, 500, 4000) * env(t, 0.02, 0.4, 0.02) * 0.2
    return normalize(np.tanh(normalize(x, 1.0) * 2.2), 0.95)


def smoke_pop(rng) -> np.ndarray:
    t = _t(3.5)
    n = noise(3.5, rng)
    pop = band(n, 100, 3000) * env(t, 0.001, 0.04)
    hiss = band(n, 2500, 10000) * env(t, 0.15, 1.6, 0.05) * 0.35
    return normalize(pop + hiss, 0.8)


def whoosh(rng, dur=0.3, lo=300, hi=3000) -> np.ndarray:
    t = _t(dur)
    n = noise(dur, rng)
    x = band(n, lo, hi) * np.sin(np.clip(t / dur, 0, 1) * math.pi) ** 2
    return normalize(x, 0.6)


def ring(rng) -> np.ndarray:
    t = _t(4.0)
    x = np.sin(2 * math.pi * 3400 * t) * env(t, 0.2, 1.4) * 0.5 + np.sin(2 * math.pi * 3410 * t) * env(t, 0.3, 1.2) * 0.3
    return normalize(x, 0.35)


def ui_tick(freq: float, dur: float = 0.06) -> np.ndarray:
    t = _t(dur)
    x = np.sin(2 * math.pi * freq * t) * env(t, 0.001, dur / 4) + np.sin(2 * math.pi * freq * 2.01 * t) * env(t, 0.001, dur / 6) * 0.3
    return normalize(x, 0.5)


def tone(freqs, dur: float, attack: float = 0.005, decay: float = 0.2, square: float = 0.0) -> np.ndarray:
    """Sum of sine (optionally squared-off) partials with an attack/decay envelope."""
    t = _t(dur)
    x = np.zeros_like(t)
    for i, f in enumerate(freqs):
        w = np.sin(2 * math.pi * f * t)
        if square > 0:
            w = np.tanh(w * (1.0 + 6.0 * square))
        x += w / (1.0 + i * 0.4)
    return x * env(t, attack, decay)


def bomb_beep() -> np.ndarray:
    return normalize(tone([2900, 5810], 0.09, 0.002, 0.03, square=0.2), 0.7)


def chord(notes, dur: float, step: float = 0.0, decay: float = 0.6) -> np.ndarray:
    t = _t(dur)
    x = np.zeros_like(t)
    for i, f in enumerate(notes):
        start = i * step
        tt = np.clip(t - start, 0, None)
        e = env(t, 0.03, decay, start)
        x += (np.sin(2 * math.pi * f * tt) + 0.3 * np.sin(2 * math.pi * 2 * f * tt)) * e
    return normalize(x, 0.6)


def alert(rng) -> np.ndarray:
    """Two-tone radio alert for 'bomb planted'."""
    parts = [tone([880, 1760], 0.18, 0.005, 0.12, square=0.4), tone([660, 1320], 0.18, 0.005, 0.12, square=0.4)]
    x = np.concatenate(parts * 2)
    return normalize(x + band(noise(len(x) / RATE, rng), 1500, 4000) * 0.03, 0.6)


def sequence(dur: float, events, rng) -> np.ndarray:
    """events: list of (time, kind) with kind in click/clack/slide/thump/cloth."""
    t = _t(dur)
    x = np.zeros_like(t)
    for at, kind in events:
        if at >= dur:
            continue
        if kind == "click":
            x += click(t, at, rng, decay=0.004, amp=0.7, body=2400)
        elif kind == "clack":
            x += click(t, at, rng, decay=0.012, amp=1.0, body=1100)
            x += click(t, at + 0.05, rng, decay=0.008, amp=0.6, body=1500)
        elif kind == "slide":
            n = band(noise(dur, rng), 1500, 7000) * env(t, 0.02, 0.06, at) * 0.4
            x += n
        elif kind == "thump":
            x += band(noise(dur, rng), 80, 900) * env(t, 0.001, 0.03, at) * 0.8
        elif kind == "cloth":
            x += band(noise(dur, rng), 400, 4000) * env(t, 0.05, 0.12, at) * 0.25
    return normalize(x, 0.6)


def reload_events(cls: str, kind: str, duration: float) -> list:
    """Clicks timed to the viewmodel reload tracks (weapons/anim_data.py)."""
    if cls == "pistol":
        ev = [(0.05, "cloth"), (0.28 * duration, "click"), (0.7 * duration, "thump"), (0.72 * duration, "click")]
        if kind == "empty":
            ev.append((0.82 * duration, "clack"))
    elif cls == "sniper":
        ev = [(0.05, "cloth"), (0.26 * duration, "click"), (0.64 * duration, "click"),
              (0.8 * duration, "clack"), (0.9 * duration, "clack")]
    else:
        out_t = 0.3 if kind != "empty" else 0.25
        in_t = 0.72 if kind != "empty" else 0.63
        ev = [(0.05, "cloth"), (out_t * duration, "click"), (out_t * duration + 0.05, "slide"),
              (in_t * duration, "thump"), (in_t * duration + 0.01, "click")]
        if kind == "empty":
            ev += [(0.83 * duration, "clack")]
    return ev


def build_library(out_dir: Path, weapons: dict, seed: int = 21) -> dict[str, list[Path]]:
    """Synthesise every sound (if missing). Returns name -> variant paths."""
    rng = np.random.default_rng(seed)
    lib: dict[str, list[Path]] = {}

    def add(name: str, gen, variants: int = 1):
        paths = []
        for v in range(variants):
            p = out_dir / f"{name}_{v}.wav"
            if not p.exists():
                write_wav(p, gen())
            paths.append(p)
        lib[name] = paths

    for prof in GUN_PROFILES:
        add(f"shot_{prof}", lambda prof=prof: gunshot(prof, rng), 3)
    for k in ("concrete", "metal", "wood", "dirt", "gravel", "glass", "flesh", "dummy", "fabric", "plaster", "tile"):
        add(f"impact_{k}", lambda k=k: impact(k, rng), 3)
    for k in ("concrete", "metal", "metal_grate", "wood", "dirt", "gravel", "tile", "fabric", "default", "brick",
              "plaster"):
        add(f"step_{k}", lambda k=k: footstep(k, rng), 4)
    add("explosion", lambda: explosion(rng), 2)
    add("flashbang", lambda: flashbang(rng))
    add("smoke_pop", lambda: smoke_pop(rng))
    add("ear_ring", lambda: ring(rng))
    add("grenade_bounce", lambda: impact("metal", rng) * 0.6, 3)
    add("knife_swing", lambda: whoosh(rng, 0.25, 600, 5000), 2)
    add("throw", lambda: whoosh(rng, 0.35, 300, 2500))
    add("knife_hit_body", lambda: impact("flesh", rng), 2)
    add("knife_hit_wall", lambda: impact("metal", rng), 2)
    add("dry_fire", lambda: sequence(0.15, [(0.0, "click")], rng))
    add("draw", lambda: sequence(0.4, [(0.0, "cloth"), (0.18, "click")], rng))
    add("pump", lambda: sequence(0.5, [(0.05, "clack"), (0.25, "clack")], rng))
    add("bolt", lambda: sequence(0.9, [(0.15, "click"), (0.3, "clack"), (0.55, "clack"), (0.7, "click")], rng))
    add("shell_insert", lambda: sequence(0.25, [(0.0, "slide"), (0.08, "click")], rng), 2)
    add("scope", lambda: sequence(0.15, [(0.0, "click")], rng))
    add("pin", lambda: sequence(0.3, [(0.0, "click"), (0.05, "slide")], rng))
    add("pickup", lambda: sequence(0.3, [(0.0, "cloth"), (0.1, "click")], rng))
    add("land", lambda: footstep("concrete", rng) * 1.0)
    add("hit", lambda: ui_tick(1800, 0.05))
    add("bomb_beep", bomb_beep)
    add("bomb_planted", lambda: alert(rng))
    add("bomb_defused", lambda: chord([1046, 784, 523], 1.2, 0.12, 0.35))
    add("plant_tap", lambda: ui_tick(2200, 0.04))
    add("defuse_start", lambda: sequence(0.5, [(0.0, "click"), (0.1, "slide"), (0.3, "click")], rng))
    add("round_win", lambda: chord([523, 659, 784, 1046], 1.8, 0.09, 0.8))
    add("round_lose", lambda: chord([440, 349, 294, 220], 1.8, 0.12, 0.8))
    add("buy", lambda: sequence(0.3, [(0.0, "cloth"), (0.12, "click")], rng))
    add("hit_head", lambda: ui_tick(2600, 0.12))
    for key, w in weapons.items():
        if w.magazine <= 0:
            continue
        if w.fire_mode == "pump":
            continue
        for kind, dur in (("tactical", w.reload_time), ("empty", w.reload_empty_time)):
            add(f"reload_{key}_{kind}", lambda w=w, kind=kind, dur=dur: sequence(dur + 0.2, reload_events(w.cls, kind, dur), rng))
    add("radio", lambda: radio_squelch(rng))
    # Milestone 6: destruction and gadgets
    add("wall_break", lambda: crumble(rng, "plaster"), 3)
    add("wood_break", lambda: crumble(rng, "wood"), 2)
    add("charge_place", lambda: sequence(0.4, [(0.0, "thump"), (0.08, "click"), (0.2, "click")], rng))
    add("charge_beep", lambda: normalize(tone([1900, 3800], 0.07, 0.002, 0.03, square=0.3), 0.6))
    add("thermal_burn", lambda: burn(rng))
    add("hammer_hit", lambda: normalize(mix(impact("concrete", rng), crumble(rng, "plaster") * 0.6), 0.9), 2)
    add("reinforce", lambda: clanks(rng))
    add("pulse", lambda: normalize(mix(chord([660, 990], 0.9, 0.0, 0.5) * 0.6, tone([1320], 0.9, 0.3, 0.4) * 0.3), 0.6))
    add("emp", lambda: emp_burst(rng))
    add("wire_place", lambda: sequence(0.6, [(0.0, "cloth"), (0.1, "slide"), (0.3, "clack")], rng))
    add("wire_rattle", lambda: rattle(rng), 2)
    add("sensor_alert", lambda: normalize(tone([1500, 3000], 0.12, 0.002, 0.05, square=0.5), 0.5))
    add("shield_deploy", lambda: normalize(mix(impact("metal", rng), sequence(0.45, [(0.1, "clack")], rng)), 0.8))
    add("jammer", lambda: normalize(mix(tone([120, 240, 360], 1.0, 0.1, 0.8) * 0.5, band(noise(1.0, rng), 800, 3000)
                                        * 0.1), 0.4))
    add("drone_motor", lambda: drone_whine(rng))
    add("camera_switch", lambda: normalize(mix(sequence(0.2, [(0.0, "click")], rng), band(noise(0.2, rng), 1500, 6000)
                                               * env(_t(0.2), 0.001, 0.08) * 0.4), 0.6))
    add("ping", lambda: normalize(tone([1760, 2640], 0.25, 0.002, 0.15), 0.55))
    return lib


def mix(*parts) -> np.ndarray:
    """Sum of sounds of different lengths (shorter ones are zero-padded)."""
    n = max(len(p) for p in parts)
    out = np.zeros(n, np.float64)
    for p in parts:
        out[:len(p)] += p
    return out


def crumble(rng, kind: str) -> np.ndarray:
    """A chunk of wall breaking: a crack, then pieces hitting the floor."""
    t = _t(0.9)
    n = noise(0.9, rng)
    x = band(n, 300, 6000) * env(t, 0.0005, 0.05)
    for k in range(7):
        at = 0.08 + rng.uniform(0.0, 0.6)
        if kind == "wood":
            x += modes(t, rng.uniform([300, 800], [450, 1100]), [0.03, 0.02], [0.5, 0.25], at) * rng.uniform(0.2, 0.6)
        else:
            x += band(n, 500, 5000) * env(t, 0.001, 0.02, at) * rng.uniform(0.2, 0.6)
    x += band(n, 200, 1500) * env(t, 0.05, 0.4, 0.05) * 0.25
    return normalize(x, 0.85)


def burn(rng) -> np.ndarray:
    t = _t(3.0)
    n = noise(3.0, rng)
    hiss = band(n, 1500, 9000) * env(t, 0.1, 2.2) * 0.6
    crackle = np.zeros_like(t)
    for _ in range(40):
        crackle += band(n, 2000, 8000) * env(t, 0.0005, 0.01, rng.uniform(0.1, 2.8)) * rng.uniform(0.2, 0.8)
    return normalize(hiss + crackle, 0.8)


def clanks(rng) -> np.ndarray:
    t = _t(2.4)
    x = np.zeros_like(t)
    for at in (0.0, 0.55, 1.1, 1.65, 2.1):
        x += modes(t, rng.uniform([300, 700, 1400], [380, 820, 1600]), [0.2, 0.12, 0.06], [0.7, 0.4, 0.2], at)
    return normalize(x, 0.8)


def emp_burst(rng) -> np.ndarray:
    t = _t(1.2)
    n = noise(1.2, rng)
    sweep = np.sin(2 * math.pi * (200 + 1800 * t) * t) * env(t, 0.005, 0.5) * 0.5
    zap = band(n, 2000, 12000) * env(t, 0.0005, 0.15)
    return normalize(sweep + zap, 0.85)


def rattle(rng) -> np.ndarray:
    t = _t(0.6)
    x = np.zeros_like(t)
    for _ in range(14):
        x += modes(t, rng.uniform([2200, 3700], [2800, 4600]), [0.03, 0.02], [0.3, 0.2], rng.uniform(0.0, 0.45))
    return normalize(x, 0.7)


def drone_whine(rng) -> np.ndarray:
    t = _t(1.0)
    n = noise(1.0, rng)
    whine = np.sin(2 * math.pi * 410 * t) * 0.3 + np.sin(2 * math.pi * 820 * t) * 0.15
    return normalize(whine + band(n, 300, 2500) * 0.25, 0.4)


def radio_squelch(rng) -> np.ndarray:
    """Short team-radio squelch: a band-limited static burst and a key click."""
    t = _t(0.16)
    x = band(noise(0.16, rng), 900, 3200) * env(t, 0.004, 0.05) * 0.5
    x += tone([1250], 0.16, 0.002, 0.03) * 0.25
    return normalize(x, 0.4)
