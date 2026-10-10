"""Military compound prefabs: building shells, concrete barriers, gabions,
vehicles, hangar canopies, the comms mast, furniture and gameplay zones.

Coordinates in the JSON are absolute map metres unless a piece is inside a
``group`` (which offsets/rotates its children). Every piece only emits
boxes / cylinders through the ``LevelContext`` API, so geometry stays
batched per material and collision stays a handful of Bullet boxes.
"""
from __future__ import annotations

import math
import random

from maps.prefabs import _local, prefab, wall

# ------------------------------------------------------------------ helpers


def _line(p0, p1):
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    return dx, dy, length, math.degrees(math.atan2(dy, dx))


def _along(p0, dx, dy, length, s, lateral=0.0):
    ux, uy = dx / length, dy / length
    return p0[0] + ux * s - uy * lateral, p0[1] + uy * s + ux * lateral


def _slab(ctx, x0, y0, x1, y1, z_top, thickness, top_mat, bottom_mat=None, side_mat=None, collide=True):
    """Horizontal slab whose top, underside and edges may use different materials."""
    c = ((x0 + x1) / 2, (y0 + y1) / 2, z_top - thickness / 2)
    size = (x1 - x0, y1 - y0, thickness)
    bottom_mat = bottom_mat or top_mat
    side_mat = side_mat or top_mat
    if top_mat == bottom_mat == side_mat:
        ctx.box(c, size, top_mat, collide=collide, occlude=True)
        return
    ctx.box(c, size, top_mat, skip_faces=("-z", "+x", "-x", "+y", "-y"), collide=collide, occlude=True)
    ctx.box(c, size, bottom_mat, skip_faces=("+z", "+x", "-x", "+y", "-y"), collide=False, occlude=False)
    ctx.box(c, size, side_mat, skip_faces=("+z", "-z"), collide=False, occlude=False)


# ---------------------------------------------------------------- buildings
@prefab("building")
def building(ctx, e):
    """Rectangular building shell.

    min/max   outer footprint corners [x, y]
    z         floor level (default 0)
    height    floor to ceiling (default 3.2)
    wall      wall thickness (0.3)
    mat       exterior finish; mat_in interior finish
    floor_mat interior floor (None = no floor slab, e.g. over a stairwell)
    roof_mat  roof top; ceiling_mat underside (defaults to mat_in)
    parapet   height of the roof parapet (0 = flat roof edge)
    openings  {"s": [...], "n": [...], "e": [...], "w": [...]}; each opening is
              {"x" (south/north walls) or "y" (east/west walls), "width",
               "bottom" (0 = door), "top"} in absolute map coordinates
    frame_mat door/window trim material
    soft      {"e": {"reinforce": true, "breach": true}, ...}: destructible sides
    hatches   [{"x", "y", "size": [sx, sy], "mat"}]: destructible roof hatches
              (reinforceable; attackers blow them open to drop in)
    parapet_gaps [{"side": "w", "from": y0, "to": y1}] (x for s/n sides)
    """
    (x0, y0), (x1, y1) = e["min"], e["max"]
    z = e.get("z", 0.0)
    h = e.get("height", 3.2)
    t = e.get("wall", 0.3)
    roof_t = e.get("roof_thickness", 0.3)
    ext = e.get("mat", "plaster_tan")
    inn = e.get("mat_in", "plaster")
    ops = e.get("openings", {})
    frame = e.get("frame_mat")

    def conv(side, start, sign):
        out = []
        for op in ops.get(side, []):
            key = "x" if side in ("s", "n") else "y"
            out.append(dict(op, at=(op[key] - start) * sign))
        return out

    # counter-clockwise, so the left side (``mat`` of the wall prefab) is inside;
    # east/west walls fit between the south/north walls (or run to the edge
    # when that side is open)
    open_sides = e.get("open_sides", ())
    ys = y0 if "s" in open_sides else y0 + t
    yn = y1 if "n" in open_sides else y1 - t
    sides = [
        ("s", (x0, y0 + t / 2), (x1, y0 + t / 2), conv("s", x0, 1)),
        ("e", (x1 - t / 2, ys), (x1 - t / 2, yn), conv("e", ys, 1)),
        ("n", (x1, y1 - t / 2), (x0, y1 - t / 2), conv("n", x1, -1)),
        ("w", (x0 + t / 2, yn), (x0 + t / 2, ys), conv("w", yn, -1)),
    ]
    soft = e.get("soft", {})
    for side, p0, p1, openings in sides:
        if side in open_sides:
            continue
        w = {"p0": p0, "p1": p1, "height": h, "thickness": t, "z": z, "mat": inn, "mat2": ext,
             "openings": openings, "frame_mat": frame}
        if side in soft:
            w.update(soft=True, reinforce=soft[side].get("reinforce", False),
                     breach=soft[side].get("breach", False), name=soft[side].get("name", side),
                     surface=soft[side].get("surface", ctx.surface(ext)))
        wall(ctx, w)
    if e.get("floor_mat", "concrete_floor"):
        ctx.box(((x0 + x1) / 2, (y0 + y1) / 2, z - 0.03), (x1 - x0 - 2 * t + 0.02, y1 - y0 - 2 * t + 0.02, 0.1),
                e.get("floor_mat", "concrete_floor"), collide=True, occlude=False)
        if z != 0.0:
            # off the ground nothing covers the floor under a door: the slab stops at the walls'
            # inner faces and the ground floor is not there, so a 0.3 m strip under each doorway
            # had no floor. The bunker room was cut off from the navmesh by it (bots walked in
            # anyway, with no mesh to follow). A threshold under every door opening
            for side, openings in ops.items():
                for op in openings:
                    if op.get("bottom", 0.0) > 0.0:
                        continue                                    # a window
                    w = op["width"]
                    if side in ("s", "n"):
                        yc = y0 + t / 2 if side == "s" else y1 - t / 2
                        c, size = (op["x"], yc, z - 0.03), (w, t + 0.1, 0.1)
                    else:
                        xc = x0 + t / 2 if side == "w" else x1 - t / 2
                        c, size = (xc, op["y"], z - 0.03), (t + 0.1, w, 0.1)
                    ctx.box(c, size, e.get("floor_mat", "concrete_floor"), collide=True, occlude=False)
    if e.get("roof", True):
        roof_mat, ceil_mat = e.get("roof_mat", "roof_membrane"), e.get("ceiling_mat", inn)
        top = z + h + roof_t
        holes = []
        for hd in e.get("hatches", []):
            sx, sy = hd.get("size", (1.2, 1.2))
            hx0, hy0 = hd["x"] - sx / 2, hd["y"] - sy / 2
            holes.append((hx0, hy0, hx0 + sx, hy0 + sy))
            ctx.panel((hd["x"], hd["y"], top - roof_t / 2), (sx, sy, roof_t), hd.get("mat", roof_mat), ceil_mat,
                      reinforce=hd.get("reinforce", True), breach=hd.get("breach", False), kind="floor",
                      name=hd.get("name", "hatch"), surface=hd.get("surface", "wood"))
            # steel rim around the opening
            for (cx, cy, rx, ry) in ((hd["x"], hy0 - 0.04, sx + 0.16, 0.08), (hd["x"], hy0 + sy + 0.04, sx + 0.16, 0.08),
                                     (hx0 - 0.04, hd["y"], 0.08, sy), (hx0 + sx + 0.04, hd["y"], 0.08, sy)):
                ctx.box((cx, cy, top + 0.03), (rx, ry, 0.06), e.get("frame_mat") or "steel_painted",
                        collide=False, occlude=False)
        for (rx0, ry0, rx1, ry1) in _subtract_rects((x0, y0, x1, y1), holes):
            _slab(ctx, rx0, ry0, rx1, ry1, top, roof_t, roof_mat, ceil_mat, e.get("edge_mat", ext))
        par = e.get("parapet", 0.0)
        if par > 0:
            pt = 0.2
            gaps = e.get("parapet_gaps", [])
            for side, (cx, cy, sx, sy) in (("s", ((x0 + x1) / 2, y0 + pt / 2, x1 - x0, pt)),
                                           ("n", ((x0 + x1) / 2, y1 - pt / 2, x1 - x0, pt)),
                                           ("w", (x0 + pt / 2, (y0 + y1) / 2, pt, y1 - y0 - 2 * pt)),
                                           ("e", (x1 - pt / 2, (y0 + y1) / 2, pt, y1 - y0 - 2 * pt))):
                along_x = side in ("s", "n")
                lo = cx - sx / 2 if along_x else cy - sy / 2
                hi = cx + sx / 2 if along_x else cy + sy / 2
                for (a, b) in _subtract_spans(lo, hi, [(g["from"], g["to"]) for g in gaps if g["side"] == side]):
                    if along_x:
                        ctx.box(((a + b) / 2, cy, top + par / 2), (b - a, sy, par), ext, collide=True, occlude=True)
                    else:
                        ctx.box((cx, (a + b) / 2, top + par / 2), (sx, b - a, par), ext, collide=True, occlude=True)


def _subtract_spans(lo: float, hi: float, gaps) -> list[tuple[float, float]]:
    spans = [(lo, hi)]
    for g0, g1 in gaps:
        out = []
        for a, b in spans:
            if g1 <= a or g0 >= b:
                out.append((a, b))
                continue
            if g0 > a:
                out.append((a, g0))
            if g1 < b:
                out.append((g1, b))
        spans = out
    return [(a, b) for a, b in spans if b - a > 1e-3]


def _subtract_rects(rect, holes) -> list[tuple[float, float, float, float]]:
    """Axis-aligned rectangle minus holes, as a few non-overlapping rectangles."""
    rects = [rect]
    for hx0, hy0, hx1, hy1 in holes:
        out = []
        for (x0, y0, x1, y1) in rects:
            if hx1 <= x0 or hx0 >= x1 or hy1 <= y0 or hy0 >= y1:
                out.append((x0, y0, x1, y1))
                continue
            if hy0 > y0:
                out.append((x0, y0, x1, hy0))
            if hy1 < y1:
                out.append((x0, hy1, x1, y1))
            my0, my1 = max(y0, hy0), min(y1, hy1)
            if hx0 > x0:
                out.append((x0, my0, hx0, my1))
            if hx1 < x1:
                out.append((hx1, my0, x1, my1))
        rects = out
    return [r for r in rects if r[2] - r[0] > 1e-3 and r[3] - r[1] > 1e-3]


# ------------------------------------------------------------------ barriers
@prefab("twall")
def twall(ctx, e):
    """Line of 3.6 m concrete T-wall barrier segments (stem + wide foot)."""
    p0, p1 = e["p0"], e["p1"]
    z0 = e.get("z", 0.0)
    h = e.get("height", 3.6)
    mat = e.get("mat", "concrete_barrier")
    dx, dy, length, heading = _line(p0, p1)
    seg = e.get("segment", 1.5)
    n = max(int(round(length / seg)), 1)
    seg_len = length / n
    rng = random.Random(e.get("seed", int(abs(p0[0] * 7 + p0[1] * 13)) + 1))
    foot = e.get("foot", 1.0)
    for i in range(n):
        s = (i + 0.5) * seg_len
        cx, cy = _along(p0, dx, dy, length, s)
        lean = rng.uniform(-0.4, 0.4)
        hh = h + rng.uniform(-0.03, 0.03)
        ctx.box((cx, cy, z0 + 0.5 + (hh - 0.5) / 2), (seg_len - 0.04, 0.26, hh - 0.5), mat,
                hpr=(heading, 0, lean), collide=False, occlude=False)
        ctx.box((cx, cy, z0 + 0.22), (seg_len - 0.04, foot, 0.44), mat, hpr=(heading, 0, 0), collide=False,
                occlude=False)
        # tapered shoulder between foot and stem
        ctx.box((cx, cy, z0 + 0.5), (seg_len - 0.05, 0.5, 0.16), mat, hpr=(heading, 0, 0), collide=False,
                occlude=False)
    mid = (p0[0] + dx / 2, p0[1] + dy / 2)
    ctx.collider((mid[0], mid[1], z0 + h / 2), (length, 0.32, h), hpr=(heading, 0, 0), surface="concrete")
    ctx.collider((mid[0], mid[1], z0 + 0.22), (length, foot, 0.44), hpr=(heading, 0, 0), surface="concrete")
    ctx.occluder((mid[0], mid[1], z0 + h / 2), (length, 0.3, h), hpr=(heading, 0, 0))


@prefab("jersey")
def jersey(ctx, e):
    """Line of 0.8 m jersey barriers (waist-high cover)."""
    p0, p1 = e["p0"], e["p1"]
    z0 = e.get("z", 0.0)
    mat = e.get("mat", "concrete_barrier")
    dx, dy, length, heading = _line(p0, p1)
    n = max(int(round(length / 3.0)), 1)
    seg_len = length / n
    for i in range(n):
        cx, cy = _along(p0, dx, dy, length, (i + 0.5) * seg_len)
        L = seg_len - 0.06
        ctx.box((cx, cy, z0 + 0.13), (L, 0.62, 0.26), mat, hpr=(heading, 0, 0), collide=False, occlude=False)
        ctx.box((cx, cy, z0 + 0.38), (L, 0.44, 0.24), mat, hpr=(heading, 0, 0), collide=False, occlude=False)
        ctx.box((cx, cy, z0 + 0.65), (L, 0.24, 0.3), mat, hpr=(heading, 0, 0), collide=False, occlude=False)
    mid = (p0[0] + dx / 2, p0[1] + dy / 2)
    ctx.collider((mid[0], mid[1], z0 + 0.4), (length, 0.5, 0.8), hpr=(heading, 0, 0), surface="concrete")
    ctx.occluder((mid[0], mid[1], z0 + 0.4), (length, 0.5, 0.8), hpr=(heading, 0, 0))


@prefab("gabion")
def gabion(ctx, e):
    """Wire-mesh sand barriers (1.05 m cubes, 1.25 m tall), 1-2 layers."""
    p0, p1 = e["p0"], e["p1"]
    z0 = e.get("z", 0.0)
    layers = int(e.get("layers", 1))
    fill = e.get("mat", "sandbag")
    wire = e.get("wire_mat", "steel")
    dx, dy, length, heading = _line(p0, p1)
    n = max(int(round(length / 1.05)), 1)
    cell = length / n
    H = 1.25
    for layer in range(layers):
        zc = z0 + layer * H
        for i in range(n):
            cx, cy = _along(p0, dx, dy, length, (i + 0.5) * cell)
            ctx.box((cx, cy, zc + H / 2), (cell - 0.03, 1.02, H - 0.02), fill, hpr=(heading, 0, 0), collide=False,
                    occlude=False)
            # wire frame: vertical posts on the corners and top/bottom rails
            for sx in (-1, 1):
                for sy in (-1, 1):
                    px, py = _along((cx, cy), dx, dy, length, sx * (cell / 2 - 0.02), sy * 0.52)
                    ctx.box((px, py, zc + H / 2), (0.035, 0.035, H), wire, hpr=(heading, 0, 0), collide=False,
                            occlude=False)
            for sy in (-1, 1):
                px, py = _along((cx, cy), dx, dy, length, 0, sy * 0.52)
                for zz in (0.02, H - 0.02, H / 2):
                    ctx.box((px, py, zc + zz), (cell, 0.03, 0.03), wire, hpr=(heading, 0, 0), collide=False,
                            occlude=False)
    mid = (p0[0] + dx / 2, p0[1] + dy / 2)
    ctx.collider((mid[0], mid[1], z0 + layers * H / 2), (length, 1.05, layers * H), hpr=(heading, 0, 0),
                 surface="dirt")
    ctx.occluder((mid[0], mid[1], z0 + layers * H / 2), (length, 1.05, layers * H), hpr=(heading, 0, 0))


@prefab("boom_gate")
def boom_gate(ctx, e):
    """Checkpoint barrier arm (raised or lowered)."""
    base = e["base"]
    heading = e.get("heading", 0.0)
    length = e.get("length", 4.0)
    ctx.box(_local(base, heading, 0, 0, 0.55), (0.4, 0.4, 1.1), "steel_painted", hpr=(heading, 0, 0))
    raised = e.get("raised", False)
    if raised:
        ctx.box(_local(base, heading, 0, 0, 1.15 + length / 2), (0.12, 0.12, length), "hazard", hpr=(heading, 0, 0),
                collide=False, occlude=False)
    else:
        ctx.box(_local(base, heading, length / 2, 0, 1.0), (length, 0.12, 0.12), "hazard", hpr=(heading, 0, 0),
                collide=False, occlude=False)
        ctx.box(_local(base, heading, length - 0.1, 0, 0.5), (0.1, 0.1, 1.0), "steel_painted",
                hpr=(heading, 0, 0), collide=False, occlude=False)


# ------------------------------------------------------------------ vehicles
@prefab("vehicle")
def vehicle(ctx, e):
    """Procedural military vehicles. kind: 'truck' (covered 6x6 cargo truck)
    or 'utility' (4x4 light utility vehicle). base = ground centre, heading
    = direction the front faces (0 = +y)."""
    base = e["base"]
    hd = e.get("heading", 0.0)
    paint = e.get("mat", "vehicle_od")
    kind = e.get("kind", "truck")

    def part(lx, ly, lz, size, m, collide=False):
        ctx.box(_local(base, hd, lx, ly, lz), size, m, hpr=(hd, 0, 0), collide=collide, occlude=False)

    def wheel(lx, ly, r, w):
        ctx.cylinder(_local(base, hd, lx, ly, r), r, w, "rubber", segments=16, hpr=(hd, 0, 90))
        ctx.cylinder(_local(base, hd, lx + math.copysign(w / 2 + 0.005, lx), ly, r), r * 0.55, 0.02, "steel",
                     segments=12, hpr=(hd, 0, 90))

    if kind == "truck":
        # chassis, cab, hood, bed with canvas cover
        part(0, 0, 0.95, (1.0, 7.0, 0.3), "steel")
        part(0, 2.55, 1.15, (2.2, 1.3, 0.75), paint)                 # engine hood
        part(0, 1.35, 1.95, (2.4, 1.6, 1.7), paint)                  # cab
        part(0, 2.16, 2.25, (2.1, 0.03, 0.62), "glass_dark")         # windscreen
        for sx in (-1, 1):
            part(sx * 1.205, 1.4, 2.3, (0.02, 1.0, 0.55), "glass_dark")
            part(sx * 1.35, 2.15, 2.25, (0.2, 0.06, 0.32), "steel")  # mirrors
        part(0, 3.25, 0.85, (2.4, 0.18, 0.3), "steel")               # bumper
        part(0, -1.7, 1.42, (2.5, 4.7, 0.16), "wood_planks")         # bed floor
        for sx in (-1, 1):
            part(sx * 1.2, -1.7, 1.85, (0.08, 4.7, 0.7), paint)
        part(0, -1.7, 2.75, (2.46, 4.6, 1.5), e.get("cover_mat", "canvas"))
        part(0, -4.0, 1.85, (2.5, 0.08, 0.7), paint)
        for sx in (-1, 1):
            part(sx * 0.95, 0.25, 1.0, (0.5, 1.1, 0.5), "steel")     # fuel tank / toolbox
        for (ly, r) in ((2.45, 0.55), (-1.15, 0.55), (-2.55, 0.55)):
            for sx in (-1, 1):
                wheel(sx * 1.05, ly, r, 0.4)
        ctx.collider(_local(base, hd, 0, 1.8, 1.45), (2.4, 3.0, 2.9), hpr=(hd, 0, 0), surface="metal")
        ctx.collider(_local(base, hd, 0, -1.7, 1.8), (2.5, 4.7, 3.0), hpr=(hd, 0, 0), surface="metal")
        ctx.occluder(_local(base, hd, 0, 0, 1.6), (2.4, 7.0, 3.0), hpr=(hd, 0, 0))
    else:
        part(0, 0, 0.95, (2.1, 4.6, 0.75), paint)                    # hull
        part(0, 1.55, 1.38, (2.0, 1.4, 0.14), paint)                 # hood top
        part(0, -0.45, 1.68, (2.0, 2.4, 0.66), paint)                # cabin
        part(0, 0.77, 1.68, (1.9, 0.04, 0.5), "glass_dark")          # windscreen
        for sx in (-1, 1):
            for ly in (0.15, -0.95):
                part(sx * 1.005, ly, 1.72, (0.02, 0.9, 0.42), "glass_dark")
        part(0, -1.66, 1.68, (1.9, 0.03, 0.42), "glass_dark")
        part(0, 2.33, 0.75, (2.15, 0.15, 0.32), "steel")             # bumper / winch
        part(0, -2.5, 1.2, (0.9, 0.3, 0.9), "rubber")                # spare wheel
        for ly in (1.55, -1.45):
            for sx in (-1, 1):
                wheel(sx * 0.98, ly, 0.45, 0.35)
        ctx.collider(_local(base, hd, 0, 0, 1.0), (2.15, 4.7, 2.0), hpr=(hd, 0, 0), surface="metal")
        ctx.occluder(_local(base, hd, 0, 0, 1.0), (2.1, 4.6, 2.0), hpr=(hd, 0, 0))


# ----------------------------------------------------------- canopy / hangar
@prefab("canopy")
def canopy(ctx, e):
    """Open-sided hangar: steel columns, roof beams and a corrugated roof.

    walls: {"n": {...}, "e": {...}} adds full-height sheet walls on those
    sides (with optional "openings" like the wall prefab, absolute x/y, and
    the wall prefab's soft/reinforce/breach flags)."""
    (x0, y0), (x1, y1) = e["min"], e["max"]
    h = e.get("height", 6.0)
    spacing = e.get("spacing", 5.0)
    col = e.get("col_mat", "steel_painted")
    roof = e.get("roof_mat", "corrugated_grey")
    over = e.get("overhang", 0.6)
    nx = max(int(round((x1 - x0) / spacing)), 1)
    ny = max(int(round((y1 - y0) / spacing)), 1)
    for i in range(nx + 1):
        for j in range(ny + 1):
            if 0 < i < nx and 0 < j < ny:
                continue
            x = x0 + (x1 - x0) * i / nx
            y = y0 + (y1 - y0) * j / ny
            ctx.box((x, y, h / 2), (0.3, 0.3, h), col, collide=True, occlude=False)
    # beams across x every column line, purlins along x
    for i in range(nx + 1):
        x = x0 + (x1 - x0) * i / nx
        ctx.box((x, (y0 + y1) / 2, h - 0.25), (0.25, y1 - y0 + 0.3, 0.5), col, collide=False, occlude=False)
    for j in range(ny * 2 + 1):
        y = y0 + (y1 - y0) * j / (ny * 2)
        ctx.box(((x0 + x1) / 2, y, h + 0.06), (x1 - x0, 0.12, 0.12), col, collide=False, occlude=False)
    ctx.box(((x0 + x1) / 2, (y0 + y1) / 2, h + 0.17), (x1 - x0 + 2 * over, y1 - y0 + 2 * over, 0.1), roof,
            collide=True, occlude=True)
    walls = e.get("walls", {})
    wm = e.get("wall_mat", roof)
    defs = {"s": ((x0, y0), (x1, y0), "x", x0, 1), "n": ((x1, y1), (x0, y1), "x", x1, -1),
            "e": ((x1, y0), (x1, y1), "y", y0, 1), "w": ((x0, y1), (x0, y0), "y", y1, -1)}
    for side, spec in walls.items():
        p0, p1, key, start, sign = defs[side]
        ops = [dict(op, at=(op[key] - start) * sign) for op in spec.get("openings", [])]
        wall(ctx, {"p0": p0, "p1": p1, "height": h + 0.12, "thickness": 0.12, "mat": wm, "openings": ops,
                   "extend": True, "soft": spec.get("soft", False), "reinforce": spec.get("reinforce", False),
                   "breach": spec.get("breach", False), "name": spec.get("name", side)})


# --------------------------------------------------------------- comms mast
@prefab("mast")
def mast(ctx, e):
    """Lattice radio mast: tapered legs in sections with X bracing,
    antennas, a dish and an aviation light on top."""
    bx, by, bz = e["base"]
    height = e.get("height", 18.0)
    w0 = e.get("width", 3.0)
    w1 = e.get("top_width", 1.0)
    mat = e.get("mat", "steel")
    sections = int(e.get("sections", 8))
    sh = height / sections
    for k in range(sections):
        za, zb = bz + k * sh, bz + (k + 1) * sh
        wa = w0 + (w1 - w0) * k / sections
        wb = w0 + (w1 - w0) * (k + 1) / sections
        wm = (wa + wb) / 2
        lean = math.degrees(math.atan2((wa - wb) / 2, sh))
        for sx in (-1, 1):
            for sy in (-1, 1):
                ctx.box((bx + sx * wm / 2, by + sy * wm / 2, (za + zb) / 2), (0.1, 0.1, sh * 1.02), mat,
                        hpr=(0, sy * lean, -sx * lean), collide=False, occlude=False)
        # X braces on all four faces
        diag = math.hypot(wm, sh)
        ang = math.degrees(math.atan2(sh, wm))
        for side in (-1, 1):
            for a in (ang, -ang):
                ctx.box((bx, by + side * wm / 2, (za + zb) / 2), (diag, 0.05, 0.05), mat, hpr=(0, 0, a),
                        collide=False, occlude=False)
                ctx.box((bx + side * wm / 2, by, (za + zb) / 2), (diag, 0.05, 0.05), mat, hpr=(90, 0, a),
                        collide=False, occlude=False)
        ctx.box((bx, by, zb), (wb, wb, 0.06), mat, collide=False, occlude=False)
    for sx in (-1, 1):
        for sy in (-1, 1):
            ctx.collider((bx + sx * w0 / 2, by + sy * w0 / 2, bz + 3.0), (0.2, 0.2, 6.0), surface="metal")
    top = bz + height
    ctx.cylinder((bx, by, top + 2.0), 0.05, 4.0, mat, segments=8)
    ctx.cylinder((bx + 0.3, by, top + 1.2), 0.03, 2.4, mat, segments=6)
    ctx.cylinder((bx + w1 / 2 + 0.4, by, top - 2.5), 0.7, 0.18, "paint_white", segments=24, hpr=(90, 0, 70))
    ctx.cylinder((bx, by - w1 / 2 - 0.3, top - 4.5), 0.5, 0.15, "paint_white", segments=24, hpr=(0, 70, 0))
    if e.get("light", True):
        ctx.light({"pos": [bx, by, top + 4.15], "kind": "point", "color": [1.0, 0.08, 0.04], "intensity": 6.0,
                   "range": 6.0, "shadows": False})


# ----------------------------------------------------------------- furniture
@prefab("furniture")
def furniture(ctx, e):
    """Interior props. kind: bunk, locker, shelf, table, desk, workbench,
    rack, generator, fuel_tank, flagpole, ammo_boxes, cabinet."""
    kind = e.get("kind", "table")
    base = e["base"]
    hd = e.get("heading", 0.0)
    n = int(e.get("count", 1))

    def part(lx, ly, lz, size, m, collide=False):
        ctx.box(_local(base, hd, lx, ly, lz), size, m, hpr=(hd, 0, 0), collide=collide, occlude=False)

    def coll(lx, ly, lz, size, surface="metal"):
        ctx.collider(_local(base, hd, lx, ly, lz), size, hpr=(hd, 0, 0), surface=surface)

    if kind == "bunk":
        for sx in (-1, 1):
            for sy in (-1, 1):
                part(sx * 0.42, sy * 0.95, 0.9, (0.05, 0.05, 1.8), "steel_painted")
        for z in (0.38, 1.38):
            part(0, 0, z, (0.9, 1.95, 0.06), "steel_painted")
            part(0, 0, z + 0.1, (0.82, 1.85, 0.14), "mattress")
        coll(0, 0, 0.9, (0.9, 2.0, 1.8), "fabric")
    elif kind == "locker":
        w = 0.42
        for i in range(n):
            lx = (i - (n - 1) / 2) * w
            part(lx, 0, 0.93, (w - 0.01, 0.5, 1.86), "steel_painted")
            part(lx, -0.252, 1.5, (0.2, 0.01, 0.05), "steel")        # vents / handle
            part(lx + w * 0.3, -0.252, 1.0, (0.03, 0.02, 0.15), "steel")
        coll(0, 0, 0.93, (w * n, 0.5, 1.86))
    elif kind in ("shelf", "rack"):
        L = e.get("length", 2.0)
        for sx in (-1, 1):
            for sy in (-1, 1):
                part(sx * (L / 2 - 0.03), sy * 0.27, 1.0, (0.05, 0.05, 2.0), "steel_painted")
        rng = random.Random(e.get("seed", 5))
        for z in (0.15, 0.75, 1.35, 1.95):
            part(0, 0, z, (L, 0.6, 0.04), "steel_painted")
            if z < 1.9:
                x = -L / 2 + 0.1
                while x < L / 2 - 0.35:
                    bw = rng.uniform(0.3, 0.6)
                    bh = rng.uniform(0.18, 0.42)
                    m = rng.choice(["metal_olive", "plywood", "metal_olive", "canvas"])
                    part(x + bw / 2, rng.uniform(-0.05, 0.05), z + 0.02 + bh / 2, (bw, 0.45, bh), m)
                    x += bw + rng.uniform(0.04, 0.2)
        coll(0, 0, 1.0, (L, 0.6, 2.0))
    elif kind in ("table", "desk", "workbench"):
        L, W = e.get("length", 1.8), e.get("width", 0.8)
        top = "steel" if kind == "workbench" else ("wood_planks" if kind == "table" else "steel_painted")
        hgt = 0.9 if kind == "workbench" else 0.76
        part(0, 0, hgt - 0.025, (L, W, 0.05), top)
        for sx in (-1, 1):
            for sy in (-1, 1):
                part(sx * (L / 2 - 0.06), sy * (W / 2 - 0.06), (hgt - 0.05) / 2, (0.05, 0.05, hgt - 0.05),
                     "steel_painted")
        if kind == "desk":
            part(L / 2 - 0.25, 0, 0.4, (0.4, W - 0.06, 0.66), "steel_painted")
        if kind == "workbench":
            part(0, 0, 0.2, (L - 0.1, W - 0.1, 0.03), "steel_painted")
            part(-L / 2 + 0.2, -W / 2 + 0.1, hgt + 0.08, (0.2, 0.12, 0.12), "steel")
        coll(0, 0, hgt / 2, (L, W, hgt))
    elif kind == "generator":
        part(0, 0, 0.55, (1.0, 2.0, 1.1), "vehicle_od")
        part(0, 0, 1.12, (0.9, 1.8, 0.05), "steel")
        part(0, 0.9, 1.3, (0.12, 0.12, 0.5), "steel")
        coll(0, 0, 0.6, (1.0, 2.0, 1.2))
    elif kind == "fuel_tank":
        L = e.get("length", 6.0)
        r = e.get("radius", 1.2)
        ctx.cylinder(_local(base, hd, 0, 0, r + 0.5), r, L, e.get("mat", "paint_white"), segments=24,
                     hpr=(hd, 90, 0))
        for sy in (-1, 1):
            part(0, sy * (L / 2 - 0.8), 0.45, (r * 1.6, 0.3, 0.9), "concrete_barrier")
        part(0, 0, 2 * r + 0.55, (0.6, 0.6, 0.12), "steel")
        coll(0, 0, r + 0.5, (2 * r, L, 2 * r + 0.2))
    elif kind == "flagpole":
        h = e.get("height", 9.0)
        part(0, 0, 0.2, (1.2, 1.2, 0.4), "concrete_barrier", collide=True)
        ctx.cylinder(_local(base, hd, 0, 0, 0.4 + h / 2), 0.06, h, "steel", segments=10)
        ctx.box(_local(base, hd, 0.65, 0, h - 0.4), (1.2, 0.02, 0.8), e.get("flag_mat", "paint_red"),
                hpr=(hd, 0, 0), collide=False, occlude=False)
        coll(0, 0, 0.4 + h / 2, (0.15, 0.15, h))
    elif kind == "ammo_boxes":
        rng = random.Random(e.get("seed", 9))
        for i in range(n):
            lx = (i % 3) * 0.62 - 0.62
            lz = (i // 3) * 0.32
            part(lx + rng.uniform(-0.03, 0.03), rng.uniform(-0.03, 0.03), lz + 0.16, (0.58, 0.32, 0.3), "metal_olive")
        rows = (n + 2) // 3
        coll(0, 0, rows * 0.16, (1.9, 0.35, rows * 0.32))
    elif kind == "cabinet":
        part(0, 0, 0.7, (0.9, 0.5, 1.4), "steel_painted")
        part(0, -0.252, 1.05, (0.85, 0.01, 0.02), "steel")
        coll(0, 0, 0.7, (0.9, 0.5, 1.4))


# --------------------------------------------------------------- gameplay
@prefab("camera")
def camera(ctx, e):
    """Defender security camera: pos (lens), heading (view direction), pitch.
    Built and run by gameplay/observation.py (it can be shot out)."""
    c, r = ctx._xf(e["pos"], (e.get("heading", 0.0), 0.0, 0.0))
    ctx.cameras.append({"name": e.get("name", ""), "pos": tuple(c), "heading": float(r[0]),
                        "pitch": float(e.get("pitch", -15.0))})


@prefab("zone")
def zone(ctx, e):
    """Gameplay volume: {"name": "A", "kind": "bombsite" | "buyzone", "team": "attack"
    (buy zones), "min": [x, y, z], "max": [x, y, z]}."""
    ctx.zones.append({"name": e["name"], "kind": e.get("kind", "bombsite"), "team": e.get("team", ""),
                      "min": list(e["min"]), "max": list(e["max"])})


@prefab("callout")
def callout(ctx, e):
    """Named area used for radio callouts / HUD location / bot navigation."""
    ctx.callouts.append({"name": e["name"], "min": list(e["min"]), "max": list(e["max"])})


@prefab("paint_line")
def paint_line(ctx, e):
    """Painted ground marking strip from p0 to p1."""
    p0, p1 = e["p0"], e["p1"]
    dx, dy, length, heading = _line(p0, p1)
    z = e.get("z", 0.0)
    ctx.box((p0[0] + dx / 2, p0[1] + dy / 2, z + 0.004), (length, e.get("width", 0.12), 0.01),
            e.get("mat", "paint_white"), hpr=(heading, 0, 0), collide=False, occlude=False)


# ------------------------------------------------------------------ terrain
def _value_noise(x, y, seed: int, cell: float):
    """Smooth 2D value noise (bilinear on a random lattice, smoothstep weights)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    lattice = rng.random((257, 257))
    gx, gy = x / cell, y / cell
    ix, iy = np.floor(gx).astype(int), np.floor(gy).astype(int)
    fx, fy = gx - ix, gy - iy
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    ix, iy = ix % 256, iy % 256
    a = lattice[ix, iy]
    b = lattice[ix + 1, iy]
    c = lattice[ix, iy + 1]
    d = lattice[ix + 1, iy + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


@prefab("terrain")
def terrain(ctx, e):
    """Backdrop terrain around the playable area: a height-field grid that is
    flat (slightly below the map's ground slabs) inside 'inner' and rises into
    rolling hills beyond 'start' metres from it. Visual only (players cannot
    leave the perimeter), so no collision."""
    import numpy as np
    (x0, y0), (x1, y1) = e["inner"]
    extent = e.get("extent", 480.0)
    step = e.get("step", 8.0)
    start = e.get("start", 20.0)
    ramp = e.get("ramp", 120.0)
    hmax = e.get("height", 26.0)
    seed = e.get("seed", 7)
    mat = e.get("mat", "sand")
    uv = e.get("uv_scale", ctx.uv_scale(mat) * 2.5)    # coarser tiling reads better at a distance
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    xs = np.arange(cx - extent, cx + extent + step * 0.5, step)
    ys = np.arange(cy - extent, cy + extent + step * 0.5, step)
    X, Y = np.meshgrid(xs, ys, indexing="ij")

    def height(X, Y):
        dx = np.maximum(np.maximum(x0 - X, X - x1), 0.0)
        dy = np.maximum(np.maximum(y0 - Y, Y - y1), 0.0)
        d = np.hypot(dx, dy)
        t = np.clip((d - start) / ramp, 0.0, 1.0)
        t = t * t * (3 - 2 * t)
        n = (_value_noise(X + 1000, Y + 1000, seed, 90.0) * 0.6 + _value_noise(X + 1000, Y + 1000, seed + 1, 35.0) * 0.3
             + _value_noise(X + 1000, Y + 1000, seed + 2, 14.0) * 0.1)
        ridge = 0.35 + 0.65 * n
        return t * hmax * ridge - 0.08

    Z = height(X, Y)
    eps = 1.0
    dzdx = (height(X + eps, Y) - height(X - eps, Y)) / (2 * eps)
    dzdy = (height(X, Y + eps) - height(X, Y - eps)) / (2 * eps)
    T = np.stack([np.ones_like(Z), np.zeros_like(Z), dzdx], -1)
    B = np.stack([np.zeros_like(Z), np.ones_like(Z), dzdy], -1)
    T /= np.linalg.norm(T, axis=-1, keepdims=True)
    B /= np.linalg.norm(B, axis=-1, keepdims=True)
    N = np.cross(T, B)
    N /= np.linalg.norm(N, axis=-1, keepdims=True)
    nx, ny = X.shape
    verts = np.zeros((nx, ny, 14), np.float32)
    verts[..., 0], verts[..., 1], verts[..., 2] = X, Y, Z
    verts[..., 3:6] = N
    verts[..., 6:9] = T
    verts[..., 9:12] = B
    verts[..., 12] = X / uv
    verts[..., 13] = Y / uv
    # quads, skipping cells hidden under the playable ground
    i, j = np.meshgrid(np.arange(nx - 1), np.arange(ny - 1), indexing="ij")
    keep = ~((X[:-1, :-1] >= x0) & (X[1:, 1:] <= x1) & (Y[:-1, :-1] >= y0) & (Y[1:, 1:] <= y1))
    i, j = i[keep], j[keep]
    a = i * ny + j
    b = (i + 1) * ny + j
    c = (i + 1) * ny + j + 1
    d = i * ny + j + 1
    idx = np.stack([a, b, c, a, c, d], -1).reshape(-1)
    ctx.builder(mat).add(verts.reshape(-1, 14), idx)
