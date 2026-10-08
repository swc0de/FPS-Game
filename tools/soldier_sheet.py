#!/usr/bin/env python3
"""Soldier screenshot sheet, render statistics and hitbox areas.

The before/after record for the character overhaul (docs/OVERHAUL_PLAN.md,
workstream B). It builds soldiers with the game's own ``CharacterBody`` in the
compound's parade ground, poses them for a fixed list of shots, renders each
shot through the normal renderer (shadows, post-processing) and pastes the
shots into one labelled sheet. The same script renders the "after" sheet, so
the two show exactly the same views.

    python tools/soldier_sheet.py --out docs/images/soldiers_before.jpg \
        --stats docs/baseline/soldier_stats.json
    xvfb-run -a python tools/soldier_sheet.py ...     # headless Linux

Shots: close up at 2 m (front, side, back), 10 m, 40 m (colour and
greyscale: team readability by value only; rendered at the pixel size a
1920x1080 screen shows, as a centre crop), crouching, aiming at +60 and -60
degrees, leaning both ways, running, reloading and dead. Vanguard (attack) is
always on the left of a pair, Bastion (defence) on the right.

Statistics written with ``--stats``:
* per soldier: geometry nodes, Geoms (each Geom is one draw call in each pass
  it is drawn in: main, depth pre-pass, every shadow cascade it overlaps),
  triangles and vertices, for the body and for the held rifle;
* skinning: bones and vertex-shader uniform components used by the palette;
* hitbox areas: the area of every hit group seen from the front and the side,
  standing and crouched, measured by casting a 5 mm grid of rays at the
  Bullet hitboxes. These are the "exposed sizes" that must stay within 10 %.
* CPU cost of ``CharacterBody.animate`` per call (running, so nothing is
  skipped by the pose cache).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ANCHOR = (-4.0, 0.0)            # parade ground ("Mid"), open sky, flat asphalt
ROSTER = {"attack": ("Rook", "Harrow", "Sable", "Dagger", "Mako", "Brine", "Cinder", "Wolfe", "Kestrel", "Jager"),
          "defend": ("Anvil", "Bastille", "Grail", "Halyard", "Osprey", "Pike", "Thorn", "Vigil", "Rampart", "Tarn")}
FACING_CAM = 180.0              # soldiers face -Y (towards the camera south of them)
PANEL = (640, 360)
COLS = 4

# (name, caption, soldiers [(team, dx, dy, yaw, pose)], camera (dx, dy, z, look_dz)[, options])
# dx/dy are relative to ANCHOR; the camera looks at ANCHOR + (0, 0, look_dz). Options: "anchor"
# moves the scene, "rot" turns the whole set-up (offsets and headings) about it.
SHOTS = [
    ("front_2m", "2 m front", [("attack", -0.45, 0, 180, {}), ("defend", 0.45, 0, 180, {})], (0, -2.0, 1.55, 1.1)),
    ("side_2m", "2 m side", [("attack", -0.45, 0, 90, {}), ("defend", 0.45, 0, 90, {})], (0, -2.0, 1.55, 1.1)),
    ("back_2m", "2 m back", [("attack", -0.45, 0, 0, {}), ("defend", 0.45, 0, 0, {})], (0, -2.0, 1.55, 1.1)),
    ("dist_10m", "10 m", [("attack", -0.7, 0, 200, {}), ("defend", 0.7, 0, 160, {})], (0, -10.0, 1.7, 1.0)),
    ("dist_40m", "40 m (pixel size as on a 1080p screen)", [("attack", -2.4, 0, 180, {}), ("attack", -1.4, 0.5, 150, {"crouch": 1.0}),
                          ("defend", 1.4, 0.5, 210, {}), ("defend", 2.4, 0, 180, {"crouch": 1.0})],
     (0, -40.0, 1.7, 0.9), {"anchor": (15.0, -15.0), "rot": -90.0, "zoom": 1 / 3}),  # a clear line across mid
    ("dist_40m_grey", "40 m, greyscale (value only)", "dist_40m", None),
    ("dist_40m_empty", "40 m (pixel size as on a 1080p screen)", "empty:dist_40m", None),   # stats only
    ("crouch", "crouching", [("attack", -0.5, 0, 210, {"crouch": 1.0}), ("defend", 0.5, 0, 150, {"crouch": 1.0})],
     (0, -2.6, 1.3, 0.7)),
    ("aim_pitch", "aiming +60 / -60", [("attack", -0.5, 0, 90, {"pitch": 60.0}),
                                       ("defend", 0.5, 0, 90, {"pitch": -60.0})], (0, -2.8, 1.4, 1.1)),
    ("lean", "leaning left / right", [("attack", -0.55, 0, 180, {"lean": -1.0}),
                                      ("defend", 0.55, 0, 180, {"lean": 1.0})], (0, -2.6, 1.5, 1.1)),
    ("run", "running", [("attack", -0.6, 0, 90, {"run": 0.35}), ("defend", 0.6, 0, 90, {"run": 0.6})],
     (0, -3.2, 1.4, 1.0)),
    ("reload", "reloading", [("attack", -0.5, 0, 120, {"event": "reload"}),
                             ("defend", 0.5, 0, 240, {"event": "reload"})], (0, -2.6, 1.5, 1.1)),
    ("death", "dead", [("attack", -0.8, 0.2, 180, {"die": True}), ("defend", 0.9, 0.0, 120, {"die": True})],
     (0, -3.6, 1.9, 0.3)),
]


class _Owner:
    """Stand-in owner for a posed body (only used as the hitboxes' python tag)."""
    def __init__(self, side: str):
        self.side = side


class SheetDemo:
    """Drives the game loop: builds, poses and photographs the soldiers."""

    def __init__(self, game, opts):
        self.game = game
        self.opts = opts
        self.frame_dt = 1.0 / 30.0
        self.bodies: list = []
        self._roster_i: dict = {}
        self.queue = [s for s in SHOTS]
        self.wait = 0
        self.files: list[tuple[str, str]] = []
        self.stats: dict = {}
        self.done = False
        game.player.noclip = True
        game.weapons.force_hide_vm = True
        game.hud.set_visible(False)
        game.debug_hud.set_mode(False)
        self.ground = self._ground(*ANCHOR)
        self.out_dir = Path(opts.shots_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stats()
        if getattr(opts, "stats_only", False):
            self.queue = []
        else:
            self._next()

    # ------------------------------------------------------------- setup
    def _ground(self, x: float, y: float) -> float:
        from engine.physics import MASK_SIGHT
        hit = self.game.physics.ray_cast((x, y, 20.0), (x, y, -10.0), MASK_SIGHT)
        return hit.pos.z if hit is not None else 0.0

    def _body(self, side: str):
        from gameplay.body import CharacterBody
        from gameplay.match import load_rules
        uniform = load_rules()["teams"][side].get("uniform", "uniform_tan")
        helmet = "metal_tan" if side == "attack" else "metal_olive"
        character = None
        if getattr(self.opts, "procedural", False):
            # a different soldier each time, from a fixed roster, so sheets are comparable
            roster = ROSTER[side]
            k = self._roster_i.get(side, 0)
            self._roster_i[side] = k + 1
            character = (roster[k % len(roster)], self.opts.seed, side)
        b = CharacterBody(self.game, _Owner(side), uniform, helmet, name=f"sheet:{side}", character=character)
        rifle = self.game.weapon_db.weapons["r7" if side == "attack" else "c9"]
        b.set_weapon(rifle.model, rifle.cls)
        b.reset()
        return b

    def _clear(self) -> None:
        for b in self.bodies:
            b.destroy()
        self.bodies = []

    def _pose(self, body, x: float, y: float, yaw: float, pose: dict) -> None:
        from panda3d.core import Point3, Vec3
        z = self._ground(x, y)
        pos = Point3(x, y, z)
        crouch = float(pose.get("crouch", 0.0))
        pitch = float(pose.get("pitch", 0.0))
        lean = float(pose.get("lean", 0.0))
        dt = 1.0 / 64.0
        if "event" in pose and hasattr(body, "event"):
            body.event(pose["event"])                  # workstream B adds animation events
        if pose.get("run"):
            # run for a moment so the gait is mid-stride (phase given as a fraction of a cycle)
            h = math.radians(yaw)
            vel = Vec3(-math.sin(h), math.cos(h), 0) * 5.4
            ticks = int(pose["run"] * 64 * 0.6) + 64
            for _ in range(ticks):
                body.animate(dt, pos, yaw, pitch, crouch, vel, True, False, lean)
            return
        for _ in range(30):
            body.animate(dt, pos, yaw, pitch, crouch, Vec3(0, 0, 0), True, False, lean)
        if pose.get("die"):
            h = math.radians(yaw)
            body.die(Vec3(math.sin(h), -math.cos(h), 0))   # shot from the front
            for _ in range(160):
                self.game.physics.step(dt)                # the ragdoll falls (Milestone 9)
                body.animate(dt, pos, yaw, pitch, crouch, Vec3(0, 0, 0), True)

    # ------------------------------------------------------------- shots
    def _next(self) -> None:
        if not self.queue:
            return
        shot = self.queue[0]
        name, caption, soldiers, cam = shot[:4]
        if isinstance(soldiers, str):               # same scene as another shot (the greyscale panel)
            empty = soldiers.startswith("empty:")
            shot = next(s for s in SHOTS if s[0] == soldiers.split(":")[-1])
            soldiers, cam = ([] if empty else shot[2]), shot[3]
        opt = shot[4] if len(shot) > 4 else {}
        self._clear()
        ax, ay = opt.get("anchor", ANCHOR)
        rot = float(opt.get("rot", 0.0))
        cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))

        def turn(dx, dy):
            return dx * cr - dy * sr, dx * sr + dy * cr
        for side, dx, dy, yaw, pose in soldiers:
            b = self._body(side)
            ox, oy = turn(dx, dy)
            self._pose(b, ax + ox, ay + oy, yaw + rot, pose)
            self.bodies.append(b)
            b.sheet_side = side
        cx, cy, cz, look_dz = cam
        cx, cy = turn(cx, cy)
        g = self._ground(ax, ay)
        eye = (ax + cx, ay + cy, g + cz)
        look = (ax, ay, g + look_dz)
        dx, dy, dz = look[0] - eye[0], look[1] - eye[1], look[2] - eye[2]
        heading = math.degrees(math.atan2(-dx, dy))
        pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
        self.game.player.set_pose(eye, (heading, pitch))
        self.game.player.noclip = True
        # "zoom" < 1 narrows the view: 1/3 shows a 640x360 crop of what a 1920x1080 screen shows
        self.zoom = float(opt.get("zoom", 1.0))
        self.game.renderer.post.reset_adaptation()
        self._label(caption)
        self.wait = 8

    def _label(self, text: str) -> None:
        from direct.gui.OnscreenText import OnscreenText
        from panda3d.core import TextNode
        if getattr(self, "_text", None) is not None:
            self._text.destroy()
        self._text = OnscreenText(text=text, pos=(-1.72, 0.88), scale=0.085, fg=(1, 1, 1, 1),
                                  shadow=(0, 0, 0, 1), align=TextNode.ALeft, mayChange=True)

    def tick(self, dt: float) -> None:
        pass

    def frame(self) -> bool:
        if self.done:
            return True
        self.game.set_zoom(getattr(self, "zoom", 1.0))     # after the weapons' own ADS zoom this frame
        if not self.queue:
            if self.files:
                self._compose()
            self._write_stats()
            self.done = True
            return True
        if self.wait > 0:
            self.wait -= 1
            return False
        name, caption, *_ = self.queue.pop(0)
        if name == "dist_40m":
            self._boxes = self._project_boxes()
        path = self.out_dir / f"sheet_{name}.png"
        self.game.screenshot(str(path))
        self.files.append((name, caption))
        self._next()
        return False

    # ----------------------------------------------------------- compose
    def _compose(self) -> None:
        from panda3d.core import Filename, PNMImage
        pw, ph = PANEL
        rows = math.ceil(len([f for f in self.files if not f[0].endswith("_empty")]) / COLS)
        sheet = PNMImage(pw * COLS, ph * rows, 3)
        sheet.fill(0.08, 0.08, 0.08)
        shown = [(n, c) for n, c in self.files if not n.endswith("_empty")]
        for k, (name, caption) in enumerate(shown):
            img = PNMImage()
            if not img.read(Filename.fromOsSpecific(str(self.out_dir / f"sheet_{name}.png"))):
                continue
            if name.endswith("_grey"):
                img.makeGrayscale()
                img.makeRgb()
            small = PNMImage(pw, ph, 3)
            small.quickFilterFrom(img)
            sheet.copySubImage(small, (k % COLS) * pw, (k // COLS) * ph)
        out = Path(self.opts.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.write(Filename.fromOsSpecific(str(out)))
        self.game.log(f"[sheet] {len(self.files)} panels -> {out}")

    def _project_boxes(self) -> list:
        """Each soldier's box on screen (side, x0, x1, y0, y1 in pixels), from its feet and the
        top of its head, a little wider than the body."""
        from panda3d.core import Point2, Point3
        cam = self.game.cam if hasattr(self.game, "cam") else self.game.camera
        lens = cam.node().getLens()
        w, h = PANEL
        out = []
        for b in self.bodies:
            pts = []
            for z in (0.0, 1.85):
                p = cam.getRelativePoint(self.game.render, b.root.getPos(self.game.render) + Point3(0, 0, z))
                p2 = Point2()
                if lens.project(p, p2):
                    pts.append(((p2.x + 1) * 0.5 * w, (1 - p2.y) * 0.5 * h))
            if len(pts) == 2:
                (xa, ya), (xb, yb) = pts
                half = abs(ya - yb) * 0.3
                out.append((getattr(b, "sheet_side", "attack"), min(xa, xb) - half, max(xa, xb) + half,
                            min(ya, yb) - 2, max(ya, yb) + 2))
        return out

    def _team_contrast(self) -> dict | None:
        """The 40 m shot with and without the soldiers: within each soldier's box on screen, the
        pixels that differ are the soldier; their mean luminance per team (sRGB values)."""
        from panda3d.core import Filename, PNMImage
        imgs = []
        for name in ("dist_40m", "dist_40m_empty"):
            img = PNMImage()
            if not img.read(Filename.fromOsSpecific(str(self.out_dir / f"sheet_{name}.png"))):
                return None
            imgs.append(img)
        a, b = imgs
        w, h = a.getXSize(), a.getYSize()
        lum = lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        sums = {"attack": [0.0, 0], "defend": [0.0, 0]}
        for side, x0, x1, y0, y1 in getattr(self, "_boxes", []):
            for y in range(max(int(y0), 0), min(int(y1) + 1, h)):
                for x in range(max(int(x0), 0), min(int(x1) + 1, w)):
                    ca, cb = a.getXel(x, y), b.getXel(x, y)
                    if abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) + abs(ca[2] - cb[2]) > 0.06:
                        t = sums[side]
                        t[0] += lum(ca)
                        t[1] += 1
        out = {k: {"pixels": n, "mean_luminance": round(v / max(n, 1), 3)} for k, (v, n) in sums.items()}
        out["difference"] = round(abs(out["attack"]["mean_luminance"] - out["defend"]["mean_luminance"]), 3)
        return out

    def _write_stats(self) -> None:
        if self.files:
            contrast = self._team_contrast()
            if contrast is not None:
                self.stats["team_contrast_40m"] = contrast
        if getattr(self.opts, "masks", ""):
            import numpy as np
            np.savez_compressed(self.opts.masks, **{f"{a}__{b}": m for (a, b), m in MASKS.items()})
            self.game.log(f"[sheet] silhouettes and poses -> {self.opts.masks}")
        if self.opts.stats:
            Path(self.opts.stats).parent.mkdir(parents=True, exist_ok=True)
            Path(self.opts.stats).write_text(json.dumps(self.stats, indent=1))
            self.game.log(f"[sheet] statistics -> {self.opts.stats}")

    # ------------------------------------------------------------- stats
    def _stats(self) -> None:
        from panda3d.core import LODNode, Point3, Vec3
        b = self._body("attack")
        self.stats["body"] = _geom_stats(b.skin) if getattr(b, "skin", None) is not None else {}
        self.stats["weapon"] = _geom_stats(b.weapon_model.root) if b.weapon_model is not None else {}
        # draw calls per pass at the nearest and the farthest level of detail, weapon included
        lods = b.skin.findAllMatches("**/+LODNode") if getattr(b, "skin", None) is not None else []
        wlods = []
        if b.weapon_model is not None:
            wroot = b.weapon_model.root
            wlods = [wroot] if wroot.node().isOfType(LODNode.getClassType()) else list(wroot.findAllMatches("**/+LODNode"))
        if lods:
            levels = [_geom_stats(c)["geoms"] for c in lods[0].getChildren()]
            wl = [_geom_stats(c)["geoms"] for c in wlods[0].getChildren()] if wlods else \
                [self.stats["weapon"].get("geoms", 0)] * 2
            self.stats["geoms_per_soldier"] = {"lod0": levels[0] + wl[0], "far": levels[-1] + wl[-1],
                                               "body_per_lod": levels, "weapon_near_far": [wl[0], wl[-1]]}
        pose = getattr(b, "pose", None)
        if pose is not None:                     # Milestone 9: the game skeleton, linear blend skinning
            from gameplay import skeleton as sk
            self.stats["skinning"] = {"palette_bones": sk.N_BONES, "matrix": "mat3x4 (3 vec4 rows)",
                                      "uniform_components": sk.MAX_BONES * 12, "weights_per_vertex": 4}
        else:
            bones = getattr(b, "_bones", None)
            n_bones = len(bones) if bones is not None else 0
            self.stats["skinning"] = {"palette_bones": n_bones, "matrix": "mat4",
                                      "uniform_components": n_bones * 16, "weights_per_vertex": 1}
        self.stats["draw_calls_note"] = ("one draw call per Geom per pass; a soldier is drawn in the main pass, the "
                                         "depth pre-pass and every shadow cascade it overlaps (2-4 by preset), and "
                                         "in local-light shadow maps when a shadowed lamp is near")
        # CPU cost of animate() while running (the pose cache cannot skip it)
        pos = Point3(ANCHOR[0], ANCHOR[1], self.ground)
        vel = Vec3(0, 5.4, 0)
        n = 2000
        t0 = time.perf_counter()
        for _ in range(n):
            b.animate(1 / 64, pos, 0.0, 5.0, 0.0, vel, True, False, 0.0)
        self.stats["animate_us_per_call_running"] = (time.perf_counter() - t0) / n * 1e6
        self.stats["animate_ms_per_tick_10_soldiers"] = self.stats["animate_us_per_call_running"] * 10 / 1000
        b.destroy()
        self.stats["hitbox_areas_cm2"] = _hitbox_areas(self.game, self._body, self.ground)
        mesh = _mesh_areas(self.game, self._body, self.ground)
        if mesh:
            self.stats["visible_areas_cm2"] = mesh


def _geom_stats(root) -> dict:
    nodes = list(root.findAllMatches("**/+GeomNode"))
    if root.node().isGeomNode():
        nodes.insert(0, root)
    geoms = tris = verts = 0
    per_node = []
    for np_ in nodes:
        gn = np_.node()
        for i in range(gn.getNumGeoms()):
            g = gn.getGeom(i)
            geoms += 1
            verts += g.getVertexData().getNumRows()
            t = 0
            for p in range(g.getNumPrimitives()):
                t += g.getPrimitive(p).decompose().getNumPrimitives()
            tris += t
            per_node.append({"node": gn.getName(), "triangles": t})
    return {"geom_nodes": len(nodes), "geoms": geoms, "triangles": tris, "vertices": verts, "parts": per_node}


MASKS: dict = {}          # (what, pose_view) -> (400, 261) bool silhouette on the 5 mm grid


def _hitbox_areas(game, make_body, ground: float) -> dict:
    """Projected area per hit group (front and side, standing and crouched)."""
    import numpy as np
    from panda3d.core import Point3, Vec3
    from engine.physics import GROUP_HITBOX
    out = {}
    x0, y0 = 30.0, -60.0            # anywhere: rays only test hitboxes
    for crouch in (0.0, 1.0):
        b = make_body("attack")
        pos = Point3(x0, y0, ground)
        for _ in range(4):
            b.animate(1 / 64, pos, 0.0, 0.0, crouch, Vec3(0, 0, 0), True)
        game.physics.step(1 / 64)               # Bullet picks up the parented hitbox transforms
        if getattr(b, "pose", None) is not None:
            MASKS[("pose", "crouch" if crouch else "stand")] = b.pose.solve().copy()
        for view, (ax, ay) in (("front", (0.0, 1.0)), ("side", (1.0, 0.0))):
            step = 0.005
            areas: dict[str, float] = {}
            rx, ry = ay, -ax                    # horizontal axis across the view
            key = f"{'crouch' if crouch else 'stand'}_{view}"
            mask = np.zeros((400, 261), bool)
            for i in range(-130, 131):
                u = i * step
                for k in range(0, 400):
                    z = ground + k * step
                    a = Point3(x0 + rx * u + ax * 2.0, y0 + ry * u + ay * 2.0, z)
                    c = Point3(x0 + rx * u - ax * 2.0, y0 + ry * u - ay * 2.0, z)
                    hit = game.physics.ray_cast(a, c, GROUP_HITBOX)
                    if hit is not None and hit.node is not None:
                        hg = hit.node.getTag("hitgroup") or "?"
                        areas[hg] = areas.get(hg, 0.0) + step * step
                        mask[k, i + 130] = True
            MASKS[("hitbox", key)] = mask
            out[key] = {k: round(v * 1e4, 1) for k, v in sorted(areas.items())}
            out[key]["total"] = round(sum(areas.values()) * 1e4, 1)
        b.destroy()
    return out


# the hit group a vertex belongs to, by its strongest bone (gameplay/hitboxes.py's groups)
_GROUP_PREFIX = (("head", "head"), ("neck", "chest"), ("spine_03", "chest"), ("clavicle", "chest"),
                 ("spine_02", "stomach"), ("spine_01", "stomach"), ("pelvis", "stomach"), ("root", "stomach"),
                 ("upperarm", "arm"), ("lowerarm", "arm"), ("hand", "arm"), ("thumb", "arm"), ("index", "arm"),
                 ("fingers", "arm"), ("thigh", "leg"), ("calf", "leg"), ("foot", "leg"), ("toe", "leg"),
                 ("pack", "chest"), ("weapon", "arm"))


def _mesh_areas(game, make_body, ground: float) -> dict:
    """Projected area per hit group of the visible soldier (LOD0, skinned on the CPU, z-buffered
    on the same 5 mm grid and views as ``_hitbox_areas``): what the hit boxes should cover."""
    import numpy as np
    from panda3d.core import Point3, Vec3
    from gameplay import skeleton as sk
    groups = ["head", "chest", "stomach", "arm", "leg"]
    bone_group = np.array([groups.index(next(g for pre, g in _GROUP_PREFIX if b.name.startswith(pre)))
                           for b in sk.BONES])
    out = {}
    step = 0.005
    for crouch in (0.0, 1.0):
        b = make_body("attack")
        mesh = getattr(b, "mesh_lod0", None)
        if mesh is None:
            b.destroy()
            return {}
        pos = Point3(30.0, -60.0, ground)
        for _ in range(4):
            b.animate(1 / 64, pos, 0.0, 0.0, crouch, Vec3(0, 0, 0), True)
        p = sk.skin_points(mesh.pos, mesh.joints, mesh.weights, b._rows[:sk.N_BONES].astype(np.float64))
        strongest = mesh.joints[np.arange(len(p)), np.argmax(mesh.weights, axis=1)].astype(np.int64)
        vg = bone_group[strongest]
        tg = vg[mesh.tris[:, 0]]
        b.destroy()
        for view in ("front", "side"):
            # the rays' grid: u across the view (front: x; side, seen from +x: -y), depth towards
            # the viewer (front: +y, side: +x); pixel (k, i) is centred at z = k step, u = (i - 130) step
            u = p[:, 0] if view == "front" else -p[:, 1]
            z = p[:, 2]
            depth = p[:, 1] if view == "front" else p[:, 0]
            nu, nz = 261, 400
            zbuf = np.full((nz, nu), -np.inf)
            gbuf = np.full((nz, nu), -1)
            for t, g in zip(mesh.tris, tg):
                tu, tz, td = u[t] / step + 130.0, z[t] / step, depth[t]
                i0, i1 = max(int(np.floor(tz.min())), 0), min(int(np.ceil(tz.max())), nz - 1)
                j0, j1 = max(int(np.floor(tu.min())), 0), min(int(np.ceil(tu.max())), nu - 1)
                if i1 < i0 or j1 < j0:
                    continue
                jj, ii = np.meshgrid(np.arange(j0, j1 + 1, dtype=float), np.arange(i0, i1 + 1, dtype=float))
                (x0, x1, x2), (y0, y1, y2) = tu, tz
                den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
                if abs(den) < 1e-12:
                    continue
                a = ((y1 - y2) * (jj - x2) + (x2 - x1) * (ii - y2)) / den
                bb = ((y2 - y0) * (jj - x2) + (x0 - x2) * (ii - y2)) / den
                c = 1.0 - a - bb
                inside = (a >= 0) & (bb >= 0) & (c >= 0)
                if not inside.any():
                    continue
                d = a * td[0] + bb * td[1] + c * td[2]
                sub_z = zbuf[i0:i1 + 1, j0:j1 + 1]
                sub_g = gbuf[i0:i1 + 1, j0:j1 + 1]
                win = inside & (d > sub_z)
                sub_z[win] = d[win]
                sub_g[win] = g
            key = f"{'crouch' if crouch else 'stand'}_{view}"
            out[key] = {groups[k]: round(float((gbuf == k).sum()) * step * step * 1e4, 1) for k in range(len(groups))}
            out[key]["total"] = round(float((gbuf >= 0).sum()) * step * step * 1e4, 1)
            MASKS[("mesh", key)] = gbuf >= 0
            hit = MASKS.get(("hitbox", key))
            if hit is not None:
                # what you see is what you hit: the visible soldier with no hit box behind it, and
                # hit box with no soldier in front of it
                seen = gbuf >= 0
                out[key]["visible_not_hittable"] = round(float((seen & ~hit).sum()) * step * step * 1e4, 1)
                out[key]["hittable_not_visible"] = round(float((hit & ~seen).sum()) * step * step * 1e4, 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="soldier screenshot sheet and statistics")
    ap.add_argument("--out", default="user/screenshots/soldiers_sheet.jpg")
    ap.add_argument("--stats", default="")
    ap.add_argument("--shots-dir", default="user/screenshots/sheet")
    ap.add_argument("--preset", default="high")
    ap.add_argument("--procedural", action="store_true", help="Milestone 9 soldiers instead of the mannequin")
    ap.add_argument("--seed", type=int, default=1, help="appearance seed for --procedural")
    ap.add_argument("--stats-only", action="store_true", help="write --stats and quit (no shots, no sheet)")
    ap.add_argument("--masks", default="", help="also save the silhouettes and the poses (.npz, hit box fitting)")
    opts, rest = ap.parse_known_args(argv)

    from panda3d.core import loadPrcFileData
    loadPrcFileData("soldier-sheet", "jpeg-quality 85")       # keeps the committed sheet small
    import engine.demo as demo_mod
    orig = demo_mod.make_demo

    def make_demo(game, name):
        if name == "sheet":
            return SheetDemo(game, opts)
        return orig(game, name)
    demo_mod.make_demo = make_demo
    from engine.app import main as game_main
    w, h = PANEL
    return game_main(["--map", "compound", "--mode", "sandbox", "--demo", "sheet", "--preset", opts.preset,
                      "--res", f"{w}x{h}", "--windowed"] + rest)


if __name__ == "__main__":
    sys.exit(main())
