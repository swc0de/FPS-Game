#!/usr/bin/env python3
"""COLD SECTOR - tactical FPS (Panda3D).

    python main.py                      # play the test range
    python main.py --preset ultra --res 1920x1080 --fullscreen
    python main.py --shots              # render the map's camera shots and exit
    python main.py --help
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from engine.app import main
    sys.exit(main())
