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
    parser.add_argument("--map", default="test_range", help="map name in maps/data or path to a .json")
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
    parser.add_argument("--save-settings", action="store_true", help="persist CLI overrides to user/settings.json")
    args = parser.parse_args(argv)

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
