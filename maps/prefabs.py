"""Modular level pieces.

Each prefab turns one JSON entry into:
  * visual geometry appended to per-material ``MeshBuilder`` batches,
  * static collision boxes (Bullet),
  * sky-visibility occluders,
  * optionally lights / emissive fixtures / spawn points.

Prefabs only talk to the ``LevelContext`` API, so new piece types are easy
to add and every map is just data.
"""
from __future__ import annotations

import math
import random
from typing import Callable

PREFABS: dict[str, Callable] = {}


def prefab(name: str):
    def deco(fn):
        PREFABS[name] = fn
        return fn
    return deco


def _rot2d(x: float, y: float, heading_deg: float):
    a = math.radians(heading_deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def _local(origin, heading, lx, ly, lz):
    """Local (x right, y forward, z up) offset -> world point for a heading."""
    wx, wy = _rot2d(lx, ly, heading)
    return (origin[0] + wx, origin[1] + wy, origin[2] + lz)


# ------------------------------------------------------------------ basics
@prefab("box")
def box(ctx, e):
    """Generic box. pos = centre (or 'base' = bottom centre), size = full extents."""
    size = e["size"]
    if "base" in e:
        b = e["base"]
        pos = (b[0], b[1], b[2] + size[2] / 2)
    else:
        pos = e["pos"]
    hpr = e.get("hpr", (e.get("heading", 0), 0, 0))
    ctx.box(pos, size, e.get("mat", "concrete_wall"), hpr=hpr, uv=e.get("uv", "world"),
            collide=e.get("collide", True), occlude=e.get("occlude", True),
            skip_faces=tuple(e.get("skip_faces", ())), surface=e.get("surface"))


@prefab("floor")
def floor(ctx, e):
    """Horizontal slab from 2D min/max with top at z."""
    (x0, y0), (x1, y1) = e["min"], e["max"]
    t = e.get("thickness", 0.3)
    z = e.get("z", 0.0)
    ctx.box(((x0 + x1) / 2, (y0 + y1) / 2, z - t / 2), (x1 - x0, y1 - y0, t), e.get("mat", "concrete_floor"),
            collide=e.get("collide", True), occlude=e.get("occlude", True))


@prefab("wall")
def wall(ctx, e):
    """Straight wall p0 -> p1 with optional openings (doors/windows).

    openings: [{"at": distance from p0 to opening centre, "width": w,
                "bottom": z0 (relative), "top": z1 (relative)}]
    mat = +left side (and edges), mat2 = right side (e.g. interior plaster).
    """
    p0, p1 = e["p0"], e["p1"]
    h = e.get("height", 3.0)
    t = e.get("thickness", 0.25)
    z0 = e.get("z", 0.0)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    heading = math.degrees(math.atan2(dy, dx))
    shift = 0.0
    if e.get("extend"):
        # grow both ends by half the thickness so corners close cleanly
        ux, uy = dx / length, dy / length
        p0 = (p0[0] - ux * t / 2, p0[1] - uy * t / 2)
        p1 = (p1[0] + ux * t / 2, p1[1] + uy * t / 2)
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        length += t
        shift = t / 2
    e = dict(e, openings=[dict(op, at=op["at"] + shift) for op in e.get("openings", [])])
    mat = e.get("mat", "concrete_wall")
    mat2 = e.get("mat2", mat)
    # segments: list of (s0, s1, zb, zt) rectangles in wall space
    rects = [(0.0, length, 0.0, h)]
    for op in e.get("openings", []):
        c, w = op["at"], op["width"]
        ob, ot = op.get("bottom", 0.0), op.get("top", min(2.2, h))
        s0, s1 = c - w / 2, c + w / 2
        new = []
        for (a, b, zb, zt) in rects:
            if s1 <= a or s0 >= b or ot <= zb or ob >= zt:
                new.append((a, b, zb, zt))
                continue
            if s0 > a:
                new.append((a, s0, zb, zt))
            if s1 < b:
                new.append((s1, b, zb, zt))
            lo_a, lo_b = max(a, s0), min(b, s1)
            if ob > zb:
                new.append((lo_a, lo_b, zb, ob))
            if ot < zt:
                new.append((lo_a, lo_b, ot, zt))
        rects = new
    ux, uy = dx / length, dy / length
    for (a, b, zb, zt) in rects:
        if b - a < 1e-3 or zt - zb < 1e-3:
            continue
        mid = (a + b) / 2
        cx, cy = p0[0] + ux * mid, p0[1] + uy * mid
        center = (cx, cy, z0 + (zb + zt) / 2)
        size = (b - a, t, zt - zb)
        hpr = (heading, 0, 0)
        if mat2 != mat:
            ctx.box(center, size, mat, hpr=hpr, collide=True, occlude=True, skip_faces=("-y",))
            ctx.box(center, size, mat2, hpr=hpr, collide=False, occlude=False,
                    skip_faces=("+x", "-x", "+y", "+z", "-z"))
        else:
            ctx.box(center, size, mat, hpr=hpr, collide=True, occlude=True)
    # trim around openings (frames)
    frame = e.get("frame_mat")
    if frame:
        for op in e.get("openings", []):
            c, w = op["at"], op["width"]
            ob, ot = op.get("bottom", 0.0), op.get("top", min(2.2, h))
            for side in (-1, 1):
                s = c + side * (w / 2 - 0.04)
                cx, cy = p0[0] + ux * s, p0[1] + uy * s
                ctx.box((cx, cy, z0 + (ob + ot) / 2), (0.08, t + 0.04, ot - ob), frame,
                        hpr=(heading, 0, 0), collide=False, occlude=False)
            cx, cy = p0[0] + ux * c, p0[1] + uy * c
            ctx.box((cx, cy, z0 + ot - 0.04), (w, t + 0.04, 0.08), frame, hpr=(heading, 0, 0),
                    collide=False, occlude=False)
            if ob > 0.05:
                ctx.box((cx, cy, z0 + ob + 0.03), (w + 0.1, t + 0.12, 0.06), frame, hpr=(heading, 0, 0),
                        collide=False, occlude=False)


@prefab("stairs")
def stairs(ctx, e):
    """Straight staircase. base = bottom-front-centre, heading = climb direction."""
    base = e["base"]
    heading = e.get("heading", 0.0)
    steps = int(e["steps"])
    rise = e["rise"] / steps
    run = e.get("run", 0.28)
    width = e.get("width", 1.4)
    mat = e.get("mat", "concrete_floor")
    solid = e.get("solid", True)
    for i in range(steps):
        top = rise * (i + 1)
        if solid:
            hgt, zc = top, top / 2
        else:
            hgt, zc = 0.12, top - 0.06
        c = _local(base, heading, 0, run * (i + 0.5), zc)
        ctx.box(c, (width, run, hgt), mat, hpr=(heading, 0, 0), collide=True, occlude=True)
    if e.get("rails"):
        rail_mat = e.get("rail_mat", "steel")
        total = run * steps
        for side in (-1, 1):
            for k in range(0, steps + 1, max(steps // 3, 1)):
                c = _local(base, heading, side * (width / 2 - 0.03), run * k, rise * k + 0.5)
                ctx.box(c, (0.05, 0.05, 1.0), rail_mat, hpr=(heading, 0, 0), collide=False, occlude=False)
            ang = math.degrees(math.atan2(rise * steps, total))
            c = _local(base, heading, side * (width / 2 - 0.03), total / 2, rise * steps / 2 + 1.0)
            ctx.box(c, (0.06, math.hypot(total, rise * steps), 0.06), rail_mat, hpr=(heading, ang, 0),
                    collide=False, occlude=False)


@prefab("ramp")
def ramp(ctx, e):
    """Solid ramp (wedge). base = bottom-front-centre of the walking surface,
    heading = climb direction, rises 'height' over horizontal 'length'."""
    base = e["base"]
    heading = e.get("heading", 0.0)
    length, height, width = e["length"], e["height"], e.get("width", 2.0)
    t = 0.3
    a = math.atan2(height, length)
    mat = e.get("mat", "concrete_floor")
    ctx.wedge(base, width, length, height, heading, mat)
    # collision: the sloped slab plus blocks underneath (never above the slope)
    center = _local(base, heading, 0, length / 2 + math.sin(a) * t / 2, height / 2 - math.cos(a) * t / 2)
    ctx.collider(center, (width, math.hypot(length, height), t), hpr=(heading, math.degrees(a), 0),
                 surface=ctx.surface(mat))
    steps = 8
    for i in range(1, steps):
        y0 = length * i / steps
        hh = height * i / steps - t / math.cos(a) - 0.02
        if hh <= 0.05:
            continue
        cc = _local(base, heading, 0, y0 + length / steps / 2, hh / 2)
        ctx.collider(cc, (width, length / steps, hh), hpr=(heading, 0, 0), surface=ctx.surface(mat))
    ctx.occluder(_local(base, heading, 0, length * 0.6, height * 0.3), (width, length * 0.6, height * 0.6),
                 hpr=(heading, 0, 0))


# ------------------------------------------------------------------- props
@prefab("crate")
def crate(ctx, e):
    """Wooden/metal crate with protruding edge battens. base = bottom centre."""
    base = e["base"]
    s = e.get("size", 1.0)
    size = e.get("dims", [s, s, s])
    heading = e.get("heading", 0.0)
    mat = e.get("mat", "plywood")
    frame = e.get("frame_mat", "wood_planks")
    c = (base[0], base[1], base[2] + size[2] / 2)
    ctx.box(c, size, mat, hpr=(heading, 0, 0), uv="fit", collide=True, occlude=True)
    b = 0.07
    hx, hy, hz = size[0] / 2, size[1] / 2, size[2] / 2
    for sx in (-1, 1):
        for sy in (-1, 1):
            ctx.box(_local(c, heading, sx * (hx - b / 2 + 0.01), sy * (hy - b / 2 + 0.01), 0),
                    (b, b, size[2] + 0.02), frame, hpr=(heading, 0, 0), collide=False, occlude=False)
    for sz in (-1, 1):
        for sy in (-1, 1):
            ctx.box(_local(c, heading, 0, sy * (hy - b / 2 + 0.01), sz * (hz - b / 2 + 0.01)),
                    (size[0], b, b), frame, hpr=(heading, 0, 0), collide=False, occlude=False)
        for sx in (-1, 1):
            ctx.box(_local(c, heading, sx * (hx - b / 2 + 0.01), 0, sz * (hz - b / 2 + 0.01)),
                    (b, size[1], b), frame, hpr=(heading, 0, 0), collide=False, occlude=False)


@prefab("crate_stack")
def crate_stack(ctx, e):
    base = e["base"]
    s = e.get("size", 1.0)
    rng = random.Random(e.get("seed", 1))
    for (ix, iy, iz) in e.get("layout", [[0, 0, 0], [1, 0, 0], [0, 0, 1]]):
        jitter = rng.uniform(-4, 4)
        crate(ctx, {"base": [base[0] + ix * (s + 0.04), base[1] + iy * (s + 0.04), base[2] + iz * s],
                    "size": s, "heading": e.get("heading", 0) + jitter, "mat": e.get("mat", "plywood"),
                    "frame_mat": e.get("frame_mat", "wood_planks")})


@prefab("container")
def container(ctx, e):
    """Shipping container (6.06 x 2.44 x 2.59). base = bottom centre.

    open: true makes the door end open (enterable, interior is lit by the map)."""
    base = e["base"]
    heading = e.get("heading", 0.0)
    L = e.get("length", 6.06)
    W, H = 2.44, 2.59
    t = 0.08
    mat = e.get("mat", "container_red")
    steel = e.get("frame_mat", "metal_olive")
    hd = heading

    def part(lx, ly, lz, size, m, collide=True, uv="world"):
        ctx.box(_local(base, hd, lx, ly, lz), size, m, hpr=(hd, 0, 0), collide=collide, occlude=True, uv=uv)

    # walls are along local y (length)
    part(-W / 2 + t / 2, 0, H / 2, (t, L, H), mat)
    part(W / 2 - t / 2, 0, H / 2, (t, L, H), mat)
    part(0, 0, H - t / 2, (W, L, t), mat)
    part(0, 0, 0.1, (W, L, 0.2), e.get("floor_mat", "wood_planks"))
    part(0, -L / 2 + t / 2, H / 2, (W, t, H), mat)  # closed back end
    if not e.get("open", False):
        part(0, L / 2 - t / 2, H / 2, (W, t, H), mat)
    else:
        # doors swung open against the side walls
        for side in (-1, 1):
            c = _local(base, hd, side * (W / 2 + 0.06), L / 2 + W / 4, H / 2)
            ctx.box(c, (0.06, W / 2, H - 0.1), mat, hpr=(hd, 0, 0), collide=True, occlude=True)
    # corner posts & rails (steel frame)
    for sx in (-1, 1):
        for sy in (-1, 1):
            part(sx * (W / 2 - 0.08), sy * (L / 2 - 0.08), H / 2, (0.17, 0.17, H + 0.01), steel, collide=False)
        part(sx * (W / 2 - 0.05), 0, H - 0.08, (0.12, L, 0.16), steel, collide=False)
        part(sx * (W / 2 - 0.05), 0, 0.08, (0.12, L, 0.16), steel, collide=False)


@prefab("sandbags")
def sandbags(ctx, e):
    """Sandbag wall from p0 to p1, 'layers' high (each bag ~0.6x0.32x0.14)."""
    p0, p1 = e["p0"], e["p1"]
    layers = int(e.get("layers", 5))
    z0 = e.get("z", 0.0)
    rng = random.Random(e.get("seed", 3))
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    heading = math.degrees(math.atan2(dy, dx))
    bag_l, bag_w, bag_h = 0.58, 0.34, 0.15
    depth = e.get("depth", 2)
    for layer in range(layers):
        n = max(int(length / bag_l), 1)
        off = (layer % 2) * bag_l / 2
        for d in range(depth if layer < layers - 1 else max(depth - 1, 1)):
            for i in range(n):
                s = off + bag_l * (i + 0.5)
                if s > length - bag_l * 0.25:
                    continue
                lat = (d - (depth - 1) / 2) * bag_w
                cx = p0[0] + dx / length * s - dy / length * lat
                cy = p0[1] + dy / length * s + dx / length * lat
                cz = z0 + layer * bag_h * 0.92 + bag_h / 2
                jh = heading + rng.uniform(-5, 5)
                sz = (bag_l * rng.uniform(0.94, 1.02), bag_w * rng.uniform(0.92, 1.0), bag_h * rng.uniform(0.9, 1.05))
                ctx.box((cx, cy, cz), sz, e.get("mat", "sandbag"), hpr=(jh, rng.uniform(-3, 3), rng.uniform(-3, 3)),
                        collide=False, occlude=False, uv="world")
    total_h = layers * bag_h * 0.92 + bag_h * 0.08
    mid = (p0[0] + dx / 2, p0[1] + dy / 2, z0 + total_h / 2)
    ctx.collider(mid, (length, bag_w * depth, total_h), hpr=(heading, 0, 0), surface="fabric")
    ctx.occluder(mid, (length, bag_w * depth, total_h), hpr=(heading, 0, 0))


@prefab("barrel")
def barrel(ctx, e):
    base = e["base"]
    r, h = 0.29, 0.88
    mat = e.get("mat", "metal_olive")
    c = (base[0], base[1], base[2] + h / 2)
    ctx.cylinder(c, r, h, mat, segments=20)
    for z in (0.28, 0.6):
        ctx.cylinder((base[0], base[1], base[2] + z), r + 0.012, 0.035, mat, segments=20, caps=False)
    ctx.collider(c, (r * 2, r * 2, h), surface="metal")


@prefab("pillar")
def pillar(ctx, e):
    base = e["base"]
    h = e.get("height", 3.0)
    if e.get("round"):
        r = e.get("radius", 0.25)
        ctx.cylinder((base[0], base[1], base[2] + h / 2), r, h, e.get("mat", "concrete_wall"), segments=24)
        ctx.collider((base[0], base[1], base[2] + h / 2), (r * 2, r * 2, h), surface="concrete")
        ctx.occluder((base[0], base[1], base[2] + h / 2), (r * 2, r * 2, h))
    else:
        s = e.get("size", 0.4)
        ctx.box((base[0], base[1], base[2] + h / 2), (s, s, h), e.get("mat", "concrete_wall"))


@prefab("railing")
def railing(ctx, e):
    p0, p1 = e["p0"], e["p1"]
    z = e.get("z", 0.0)
    h = e.get("height", 1.05)
    mat = e.get("mat", "steel")
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    heading = math.degrees(math.atan2(dy, dx))
    n = max(int(length / 1.5), 1)
    for i in range(n + 1):
        s = i / n
        ctx.box((p0[0] + dx * s, p0[1] + dy * s, z + h / 2), (0.05, 0.05, h), mat, hpr=(heading, 0, 0),
                collide=False, occlude=False)
    for zz in (h, h * 0.5):
        ctx.box((p0[0] + dx / 2, p0[1] + dy / 2, z + zz), (length, 0.05, 0.05), mat, hpr=(heading, 0, 0),
                collide=False, occlude=False)
    ctx.collider((p0[0] + dx / 2, p0[1] + dy / 2, z + h / 2), (length, 0.08, h), hpr=(heading, 0, 0),
                 surface="metal")


@prefab("catwalk")
def catwalk(ctx, e):
    """Elevated walkway: deck between p0 and p1 at height z, legs, railings."""
    p0, p1 = e["p0"], e["p1"]
    z = e["z"]
    w = e.get("width", 1.6)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    heading = math.degrees(math.atan2(dy, dx))
    nx, ny = -dy / length, dx / length
    mid = (p0[0] + dx / 2, p0[1] + dy / 2, z - 0.06)
    ctx.box(mid, (length, w, 0.12), e.get("mat", "diamond_plate"), hpr=(heading, 0, 0))
    leg_mat = e.get("leg_mat", "steel")
    n = max(int(length / 3.0), 1)
    for i in range(n + 1):
        s = i / n
        for side in (-1, 1):
            x = p0[0] + dx * s + nx * side * (w / 2 - 0.08)
            y = p0[1] + dy * s + ny * side * (w / 2 - 0.08)
            if e.get("legs", True):
                ctx.box((x, y, z / 2 - 0.06), (0.12, 0.12, z - 0.12), leg_mat, collide=True, occlude=False)
    if e.get("rails", True):
        for side in e.get("rail_sides", (-1, 1)):
            q0 = (p0[0] + nx * side * (w / 2 - 0.03), p0[1] + ny * side * (w / 2 - 0.03))
            q1 = (p1[0] + nx * side * (w / 2 - 0.03), p1[1] + ny * side * (w / 2 - 0.03))
            railing(ctx, {"p0": q0, "p1": q1, "z": z, "mat": leg_mat})


@prefab("pbr_gallery")
def pbr_gallery(ctx, e):
    """Row of calibration spheres on pedestals."""
    base = e["base"]
    heading = e.get("heading", 0.0)
    mats = e.get("mats", [])
    spacing = e.get("spacing", 1.6)
    for i, m in enumerate(mats):
        x = (i - (len(mats) - 1) / 2) * spacing
        p = _local(base, heading, x, 0, 0)
        ctx.box((p[0], p[1], p[2] + 0.45), (0.6, 0.6, 0.9), e.get("pedestal_mat", "concrete_dark"))
        ctx.sphere((p[0], p[1], p[2] + 0.9 + 0.42), 0.42, m)
        ctx.collider((p[0], p[1], p[2] + 1.32), (0.84, 0.84, 0.84))


@prefab("light")
def light(ctx, e):
    ctx.light(e)


@prefab("spawn")
def spawn(ctx, e):
    ctx.spawns.append({"team": e.get("team", "attack"), "pos": e["pos"], "heading": e.get("heading", 0.0)})


@prefab("weapon_display")
def weapon_display(ctx, e):
    """A weapon model resting on a surface (static prop; pickups are spawned by the game)."""
    ctx.prop("weapon_model", e)


@prefab("dummy")
def dummy(ctx, e):
    """Shooting-range target dummy (spawned by the game, not baked into geometry)."""
    ctx.prop("dummy", e)


@prefab("pickup")
def pickup(ctx, e):
    """Weapon / ammo / grenade pickup spawned by the game."""
    ctx.prop("pickup", e)


@prefab("group")
def group(ctx, e):
    """Offset/rotate a list of child pieces (reusable building blocks)."""
    ctx.push_transform(e.get("offset", (0, 0, 0)), e.get("heading", 0.0))
    for child in e.get("pieces", []):
        ctx.build_piece(child)
    ctx.pop_transform()
