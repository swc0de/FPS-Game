"""Application: window/engine configuration and the main loop."""
from __future__ import annotations

import sys

from panda3d.core import loadPrcFileData

from engine import paths
from engine.settings import Settings

TICK_RATE = 64
GAME_TITLE = "COLD SECTOR"


def configure_engine(settings: Settings, args) -> None:
    """Panda3D PRC configuration; must run before ShowBase is created."""
    v = settings.video
    g = settings.graphics
    w, h = v["resolution"]
    prc = [
        f"window-title {GAME_TITLE}",
        f"win-size {w} {h}",
        f"fullscreen {'#t' if v.get('fullscreen') else '#f'}",
        f"sync-video {'#t' if v.get('vsync', True) else '#f'}",
        "framebuffer-srgb #f",
        "textures-power-2 none",
        "textures-auto-power-2 #f",
        f"max-texture-dimension {int(g.get('texture_size', 2048))}",
        "gl-coordinate-system default",
        "gl-check-errors #f",
        "show-frame-rate-meter #f",
        "notify-level-glgsg error",
        "notify-level-display error",
        "audio-library-name p3openal_audio",
        "cursor-hidden #f",
        "default-near 0.05",
        "default-far 2000",
        "texture-anisotropic-degree 1",
    ]
    if sys.platform == "darwin":
        # macOS only exposes modern OpenGL through a core profile
        prc.append("gl-version 3 2")
    if v.get("max_fps"):
        prc.append("clock-mode limited")
        prc.append(f"clock-frame-rate {int(v['max_fps'])}")
    if getattr(args, "offscreen", False):
        prc.append("window-type offscreen")
    loadPrcFileData("cold-sector", "\n".join(prc))


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=f"{GAME_TITLE} - tactical FPS")
    parser.add_argument("--map", default=None,
                        help="map name in maps/data (compound, test_range, showroom) or path to a .json")
    parser.add_argument("--preset", choices=["low", "medium", "high", "ultra"], help="graphics preset")
    parser.add_argument("--res", help="resolution, e.g. 1920x1080")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--fov", type=float, help="vertical field of view in degrees")
    parser.add_argument("--no-vsync", action="store_true")
    parser.add_argument("--gfx", action="append", metavar="KEY=VALUE",
                        help="override a graphics option, e.g. --gfx shadow_resolution=4096")
    parser.add_argument("--texture-res", type=int, default=1024,
                        help="resolution for procedurally generated fallback textures")
    parser.add_argument("--shots", nargs="?", const="all",
                        help="render the map's camera_shots (or a comma list of names) to user/screenshots and exit")
    parser.add_argument("--frames", type=int, default=0, help="exit after N frames (benchmark/testing)")
    parser.add_argument("--screenshot", help="save a screenshot to this path when exiting after --frames")
    parser.add_argument("--pose", help="start pose x,y,z,heading,pitch (eye position)")
    parser.add_argument("--offscreen", action="store_true", help="render without a window (testing)")
    parser.add_argument("--trace", action="store_true", help="print player state twice per second (testing)")
    parser.add_argument("--demo", help="run a scripted demo (e.g. 'weapons') that drives the player and "
                                       "saves screenshots to user/screenshots, then exits")
    parser.add_argument("--post-debug", type=int, default=0,
                        help="post-processing debug view: 1 AO, 2 bloom, 3 normals, 4 depth (also F3 in game)")
    parser.add_argument("--team", choices=["attack", "defend"], help="start the match on this side (skips team select)")
    parser.add_argument("--mode", choices=["auto", "match", "sandbox"], default="auto",
                        help="match (rounds/economy/bomb) or sandbox; auto = match on maps with bomb sites")
    parser.add_argument("--opponents", type=int, help="number of enemy bots (default 5, data/match.json)")
    parser.add_argument("--teammates", type=int, help="number of bot teammates (default 4)")
    parser.add_argument("--difficulty", choices=["easy", "normal", "hard", "expert"],
                        help="bot difficulty (default from data/bots.json)")
    parser.add_argument("--bots", choices=["on", "off"], default="on",
                        help="off = practice against stand-ins that do not shoot back (Milestone 4)")
    parser.add_argument("--spectate", action="store_true", help="watch a 5v5 bot match")
    parser.add_argument("--seed", type=int, help="random seed for spawns, bot decisions and stand-in positions")
    parser.add_argument("--save-settings", action="store_true", help="persist CLI overrides to user/settings.json")
    args = parser.parse_args(argv)
    if args.demo in ("routes",) and args.mode == "auto":
        args.mode = "sandbox"            # walking tests: no freeze time or round resets
    if args.demo in ("round", "m6"):
        args.bots = "off"                # the Milestone 4/6 demos work against stand-ins
    if args.demo == "m6" and args.team is None:
        args.team = "attack"
    if args.demo == "bots":
        args.spectate = True
    if args.map is None:
        # the weapon demos are scripted against the shooting range
        args.map = "test_range" if args.demo in ("weapons", "viewmodels", "impacts", "flash") else "compound"

    paths.ensure_dirs()
    settings = Settings.load()
    settings.apply_cli(args)
    if args.save_settings:
        settings.save()
    configure_engine(settings, args)
    from engine.game import Game  # imported after PRC configuration
    game = Game(settings, args)
    game.run()
    return game.exit_code
