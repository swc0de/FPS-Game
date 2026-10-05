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
        b = CharacterBody(self.game, _Owner(side), uniform, helmet, name=f"sheet:{side}")
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
            for _ in range(80):
                body.animate(dt, pos, yaw, pitch, crouch, Vec3(0, 0, 0), True)

    # ------------------------------------------------------------- shots
    def _next(self) -> None:
        if not self.queue:
            return
        shot = self.queue[0]
        name, caption, soldiers, cam = shot[:4]
        if isinstance(soldiers, str):               # same scene as another shot (the greyscale panel)
            shot = next(s for s in SHOTS if s[0] == soldiers)
            soldiers, cam = shot[2], shot[3]
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
            self._compose()
            self.done = True
            return True
        if self.wait > 0:
            self.wait -= 1
            return False
        name, caption, *_ = self.queue.pop(0)
        path = self.out_dir / f"sheet_{name}.png"
        self.game.screenshot(str(path))
        self.files.append((name, caption))
        self._next()
        return False

    # ----------------------------------------------------------- compose
    def _compose(self) -> None:
        from panda3d.core import Filename, PNMImage
        pw, ph = PANEL
        rows = math.ceil(len(self.files) / COLS)
        sheet = PNMImage(pw * COLS, ph * rows, 3)
        sheet.fill(0.08, 0.08, 0.08)
        for k, (name, caption) in enumerate(self.files):
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
        if self.opts.stats:
            Path(self.opts.stats).parent.mkdir(parents=True, exist_ok=True)
            Path(self.opts.stats).write_text(json.dumps(self.stats, indent=1))
            self.game.log(f"[sheet] statistics -> {self.opts.stats}")

    # ------------------------------------------------------------- stats
    def _stats(self) -> None:
        from panda3d.core import Point3, Vec3
        b = self._body("attack")
        self.stats["body"] = _geom_stats(b.skin) if getattr(b, "skin", None) is not None else {}
        self.stats["weapon"] = _geom_stats(b.weapon_model.root) if b.weapon_model is not None else {}
        bones = getattr(b, "_bones", None)
        n_bones = len(bones) if bones is not None else 0
        self.stats["skinning"] = {"palette_bones": n_bones, "matrix": "mat4",
                                  "uniform_components": n_bones * 16,
                                  "weights_per_vertex": 1}
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


def _geom_stats(root) -> dict:
    nodes = root.findAllMatches("**/+GeomNode")
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


def _hitbox_areas(game, make_body, ground: float) -> dict:
    """Projected area per hit group (front and side, standing and crouched)."""
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
        for view, (ax, ay) in (("front", (0.0, 1.0)), ("side", (1.0, 0.0))):
            step = 0.005
            areas: dict[str, float] = {}
            rx, ry = ay, -ax                    # horizontal axis across the view
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
            key = f"{'crouch' if crouch else 'stand'}_{view}"
            out[key] = {k: round(v * 1e4, 1) for k, v in sorted(areas.items())}
            out[key]["total"] = round(sum(areas.values()) * 1e4, 1)
        b.destroy()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="soldier screenshot sheet and statistics")
    ap.add_argument("--out", default="user/screenshots/soldiers_sheet.jpg")
    ap.add_argument("--stats", default="")
    ap.add_argument("--shots-dir", default="user/screenshots/sheet")
    ap.add_argument("--preset", default="high")
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
