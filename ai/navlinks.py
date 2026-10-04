"""Keeps the navmesh in step with destruction.

The navmesh is baked with every destructible panel intact. When a panel
changes shape (gameplay/destruction.py calls its listeners once per tick),
this module looks for openings a character fits through and adds off-mesh
links (``NavMesh.add_link``) that A* and the funnel treat like portals:

* a wall with a hole from the floor up to crouch height, at least
  ``MIN_WIDTH`` wide, links the floor on both sides (two-way);
* a floor or roof hatch with an opening of ``MIN_WIDTH`` square links the
  roof to the floor below (one-way: the bot walks onto the hole and drops).

A new round restores every panel and removes all links.
"""
from __future__ import annotations

import math

import numpy as np

HULL = 0.30          # character radius (data/movement.json)
MIN_WIDTH = 0.8      # opening a character fits through
MIN_HEIGHT = 1.3     # crouch height + margin
SIDE = 0.55          # probe distance from the wall face to find the floor on each side


class NavLinks:
    def __init__(self, nav, destruction):
        self.nav = nav
        self.destruction = destruction
        self.links: dict[int, list] = {}           # panel index -> link descriptions (for tests / debug)
        destruction.listeners.append(self.on_panel)
        for p in destruction.panels:
            if p.version > 0:
                self.on_panel(p)

    def on_panel(self, panel) -> None:
        if panel is None:
            self.nav.clear_links()
            self.links.clear()
            return
        self.nav.remove_links(panel.index)
        self.links.pop(panel.index, None)
        if panel.reinforced or panel.alive.all():
            return
        if panel.spec.kind == "floor":
            self._floor(panel)
        else:
            self._wall(panel)

    # ---------------------------------------------------------------- walls
    def _wall(self, panel) -> None:
        a, b = panel.ax
        R = panel.R
        if abs(R[b][2]) > 0.9:
            vert, horiz = b, a
            alive = panel.alive                      # (u, v) = (horizontal, vertical)
            cu, cv = panel.cu, panel.cv
        elif abs(R[a][2]) > 0.9:
            vert, horiz = a, b
            alive = panel.alive.T
            cu, cv = panel.cv, panel.cu
        else:
            return
        height = panel.size[vert]
        if height < MIN_HEIGHT:
            return
        sign = 1.0 if R[vert][2] > 0 else -1.0       # local +vert points up or down
        rows = min(int(math.ceil(MIN_HEIGHT / cv)), alive.shape[1])
        bottom = alive[:, :rows] if sign > 0 else alive[:, ::-1][:, :rows]
        open_col = ~bottom.any(axis=1)
        nu = len(open_col)
        bottom_z = float(panel.center[2]) - height / 2
        i = 0
        while i < nu:
            if not open_col[i]:
                i += 1
                continue
            j = i
            while j + 1 < nu and open_col[j + 1]:
                j += 1
            width = (j - i + 1) * cu
            if width >= MIN_WIDTH:
                u0 = -panel.size[horiz] / 2 + i * cu
                u1 = -panel.size[horiz] / 2 + (j + 1) * cu
                self._wall_link(panel, horiz, vert, sign, u0, u1, bottom_z)
            i = j + 1

    def _wall_link(self, panel, horiz, vert, sign, u0, u1, bottom_z) -> None:
        nav = self.nav
        half_t = panel.size[panel.thin] / 2
        uc = (u0 + u1) / 2

        def world(u, off):
            loc = np.zeros(3)
            loc[horiz] = u
            loc[vert] = -sign * panel.size[vert] / 2
            loc[panel.thin] = off
            return panel.to_world(loc)

        pa = world(uc, half_t + SIDE)
        pb = world(uc, -(half_t + SIDE))
        ra = nav.rect_at((pa.x, pa.y, bottom_z), search=3, max_dz=0.5)
        rb = nav.rect_at((pb.x, pb.y, bottom_z), search=3, max_dz=0.5)
        if ra < 0 or rb < 0 or ra == rb:
            return
        m = max((u1 - u0) / 2 - HULL, 0.05)
        e0 = world(uc - m, 0.0)
        e1 = world(uc + m, 0.0)
        nav.add_link(panel.index, ra, rb, (e0.x, e0.y), (e1.x, e1.y), two_way=True)
        self.links.setdefault(panel.index, []).append(("wall", ra, rb, (e0.x, e0.y), (e1.x, e1.y)))

    # --------------------------------------------------------------- floors
    def _floor(self, panel) -> None:
        a, b = panel.ax
        dead = ~panel.alive
        w_a = int(math.ceil(MIN_WIDTH / panel.cu - 1e-6))
        w_b = int(math.ceil(MIN_WIDTH / panel.cv - 1e-6))
        nu, nv = dead.shape
        if w_a > nu or w_b > nv:
            return
        # windows of w_a x w_b chunks that are completely open (summed-area table)
        sat = np.zeros((nu + 1, nv + 1), np.int32)
        sat[1:, 1:] = np.cumsum(np.cumsum(dead, axis=0), axis=1)
        win = sat[w_a:, w_b:] - sat[:-w_a, w_b:] - sat[w_a:, :-w_b] + sat[:-w_a, :-w_b]
        ok = np.argwhere(win == w_a * w_b)
        if len(ok) == 0:
            return
        ii, jj = np.nonzero(dead)
        ci, cj = ii.mean(), jj.mean()
        k = int(np.argmin((ok[:, 0] + (w_a - 1) / 2 - ci) ** 2 + (ok[:, 1] + (w_b - 1) / 2 - cj) ** 2))
        i0, j0 = ok[k]
        loc = np.zeros(3)
        loc[a] = -panel.size[a] / 2 + (i0 + w_a / 2) * panel.cu
        loc[b] = -panel.size[b] / 2 + (j0 + w_b / 2) * panel.cv
        c = panel.to_world(loc)
        top = float(panel.center[2]) + panel.size[panel.thin] / 2
        bottom = float(panel.center[2]) - panel.size[panel.thin] / 2
        nav = self.nav
        ra = nav.rect_at((c.x, c.y, top), search=2, max_dz=0.3)
        rb = nav.rect_below(c.x, c.y, bottom - 0.2)
        if ra < 0 or rb < 0 or ra == rb:
            return
        nav.add_link(panel.index, ra, rb, (c.x - 0.1, c.y), (c.x + 0.1, c.y), two_way=False)
        self.links.setdefault(panel.index, []).append(("drop", ra, rb, (c.x, c.y), (c.x, c.y)))
