#!/usr/bin/env python3
"""Hit capsule areas and fitting (OVERHAUL_PLAN 4.1, step B5).

Reads the silhouettes and poses written by

    xvfb-run -a python tools/soldier_sheet.py --procedural --stats-only --masks masks.npz

and projects the capsules of gameplay/hitboxes.py (CAPSULES) exactly: seen along an axis, a
capsule is a 2D stadium and a sphere a disc, so the areas need no ray casts. For each pose
(standing, crouched) and view (front, side) it prints the area per hit group against the
baseline (docs/baseline/soldier_stats.json) and how well the capsules cover the visible
soldier: the visible soldier with no capsule behind it, and capsule with no soldier in front.

    python tools/fit_hitboxes.py masks.npz                   # report the current capsules
    python tools/fit_hitboxes.py masks.npz --fit visual      # fit the radii to the soldier
    python tools/fit_hitboxes.py masks.npz --fit areas       # fit them to the baseline areas
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gameplay import skeleton as sk  # noqa: E402
from gameplay.hitboxes import CAPSULES  # noqa: E402

STEP = 0.005                    # the 5 mm grid of tools/soldier_sheet.py
GROUPS = ["head", "chest", "stomach", "arm", "leg"]


def _posed(pt, bone: str, world: np.ndarray) -> np.ndarray:
    local = np.append(pt, 1.0) @ np.linalg.inv(sk.REST_WORLD[sk.INDEX[bone]])
    return (local @ world[sk.INDEX[bone]])[:3]


def raster(capsules, world: np.ndarray, view: str) -> np.ndarray:
    """Hit group index per pixel (-1: none), the front-most capsule winning, on the sheet's grid:
    pixel (k, i) at z = k STEP, u = (i - 130) STEP; front: u = x, seen from +y; side: u = -y,
    seen from +x."""
    k, i = np.mgrid[0:400, 0:261]
    z, u = k * STEP, (i - 130) * STEP
    best = np.full(z.shape, -np.inf)
    grp = np.full(z.shape, -1)
    for c in capsules:
        a = _posed(c.a, c.bone, world)
        b = _posed(c.b, c.bone, world) if c.b is not None else a
        if view == "front":
            pa, pb, da, db = (a[0], a[2]), (b[0], b[2]), a[1], b[1]
        else:
            pa, pb, da, db = (-a[1], a[2]), (-b[1], b[2]), a[0], b[0]
        ab = np.subtract(pb, pa)
        t = np.clip(((u - pa[0]) * ab[0] + (z - pa[1]) * ab[1]) / max(ab @ ab, 1e-12), 0.0, 1.0)
        d2 = (u - pa[0] - t * ab[0]) ** 2 + (z - pa[1] - t * ab[1]) ** 2
        inside = d2 <= c.radius ** 2
        depth = da + t * (db - da) + np.sqrt(np.maximum(c.radius ** 2 - d2, 0.0))
        win = inside & (depth > best)
        best[win] = depth[win]
        grp[win] = GROUPS.index(c.hitgroup)
    return grp


def measure(capsules, masks) -> dict:
    out = {}
    px = STEP * STEP * 1e4                      # cm² per pixel
    for pose in ("stand", "crouch"):
        world = masks[f"pose__{pose}"]
        for view in ("front", "side"):
            key = f"{pose}_{view}"
            g = raster(capsules, world, view)
            a = {name: float((g == k).sum()) * px for k, name in enumerate(GROUPS)}
            a["total"] = float((g >= 0).sum()) * px
            seen = masks.get(f"mesh__{key}")
            if seen is not None:
                a["visible_not_hittable"] = float((seen & (g < 0)).sum()) * px
                a["hittable_not_visible"] = float(((g >= 0) & ~seen).sum()) * px
            out[key] = a
    return out


def worst_area_error(m: dict, base: dict) -> float:
    return max(abs(m[k][g] / base[k][g] - 1.0) for k in m for g in GROUPS)


def mismatch(m: dict) -> float:
    return sum(a.get("visible_not_hittable", 0.0) + a.get("hittable_not_visible", 0.0) for a in m.values())


def fit(capsules, masks, base, goal: str, rounds: int = 6):
    """Coordinate descent on each capsule's radius (left and right sides together)."""
    cost = (lambda m: worst_area_error(m, base)) if goal == "areas" else mismatch
    caps = list(capsules)
    names = sorted({c.name.rstrip("_lr") if c.name[-2:] in ("_l", "_r") else c.name for c in caps})
    for _ in range(rounds):
        for name in names:
            idx = [k for k, c in enumerate(caps) if c.name == name or c.name in (f"{name}_l", f"{name}_r")]
            best, best_cost = None, cost(measure(caps, masks))
            for f in (0.85, 0.93, 0.97, 1.03, 1.07, 1.15):
                trial = list(caps)
                for k in idx:
                    trial[k] = replace(caps[k], radius=caps[k].radius * f)
                c = cost(measure(trial, masks))
                if c < best_cost - 1e-6:
                    best, best_cost = trial, c
            if best is not None:
                caps = best
    return caps


def report(m: dict, base: dict) -> None:
    for key, a in m.items():
        cells = ", ".join(f"{g} {a[g]:.0f} ({100 * (a[g] / base[key][g] - 1):+.0f} %)" for g in GROUPS)
        extra = ""
        if "visible_not_hittable" in a:
            extra = (f"; visible, no hit box {a['visible_not_hittable']:.0f}; "
                     f"hit box, nothing visible {a['hittable_not_visible']:.0f}")
        print(f"{key}: {cells}; total {a['total']:.0f} / {base[key]['total']:.0f}{extra}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("masks")
    ap.add_argument("--baseline", default="docs/baseline/soldier_stats.json")
    ap.add_argument("--fit", choices=("visual", "areas"))
    opts = ap.parse_args(argv)
    masks = dict(np.load(opts.masks))
    base = json.loads(Path(opts.baseline).read_text())["hitbox_areas_cm2"]
    caps = CAPSULES
    if opts.fit:
        caps = fit(caps, masks, base, opts.fit)
        for c in caps:
            print(f"{c.name}: radius {c.radius:.4f}")
    report(measure(caps, masks), base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
