"""Milestone 7: radar / compass / damage-arc maths and the audio mixing rules (no window needed)."""
import math
import unittest

import numpy as np

from audio import synth
from audio.system import AMBIENCE, FAR_DISTANCE, approach, environment, shot_variant
from ui.hud import arc_angle
from ui.radar import PANEL, SITE, WALL, bearing, bearing_to, radar_image, tex_transform, world_to_radar


class RadarMathTests(unittest.TestCase):
    def test_north_up_when_not_rotating(self):
        u, v = world_to_radar(0.0, 10.0, 0.0, 20.0)
        self.assertAlmostEqual(u, 0.0)
        self.assertAlmostEqual(v, 0.5)

    def test_heading_points_up_when_rotating(self):
        # Panda heading 90 looks west (-X): a point to the west is straight ahead (up)
        u, v = world_to_radar(-10.0, 0.0, 90.0, 10.0)
        self.assertAlmostEqual(u, 0.0, places=6)
        self.assertAlmostEqual(v, 1.0, places=6)
        # and north is then to the right
        u, v = world_to_radar(0.0, 10.0, 90.0, 10.0)
        self.assertAlmostEqual(u, 1.0, places=6)
        self.assertAlmostEqual(v, 0.0, places=6)

    def test_texture_transform_matches_marker_mapping(self):
        origin, dims = (-50.0, -40.0), (100.0, 80.0)
        px, py, yaw, hw = 12.0, -7.0, 33.0, 25.0
        a00, a01, b0, a10, a11, b1 = tex_transform(px, py, yaw, hw, origin, dims)

        def tex(s, t):
            return a00 * s + a01 * t + b0, a10 * s + a11 * t + b1

        # card centre shows the player
        cx, cy = tex(0.5, 0.5)
        self.assertAlmostEqual(cx, (px - origin[0]) / dims[0])
        self.assertAlmostEqual(cy, (py - origin[1]) / dims[1])
        # any card point shows the world point that world_to_radar puts there
        for wx, wy in ((20.0, -3.0), (0.0, 5.0), (-8.0, -20.0)):
            u, v = world_to_radar(wx - px, wy - py, yaw, hw)
            tx, ty = tex((u + 1) / 2, (v + 1) / 2)
            self.assertAlmostEqual(tx, (wx - origin[0]) / dims[0], places=6)
            self.assertAlmostEqual(ty, (wy - origin[1]) / dims[1], places=6)

    def test_compass_bearings(self):
        self.assertAlmostEqual(bearing(0.0), 0.0)
        self.assertAlmostEqual(bearing(90.0), 270.0)     # heading 90 = west
        self.assertAlmostEqual(bearing(-90.0), 90.0)     # east
        self.assertAlmostEqual(bearing_to(1.0, 0.0), 90.0)
        self.assertAlmostEqual(bearing_to(0.0, -1.0), 180.0)

    def test_damage_arc_points_at_source(self):
        self.assertAlmostEqual(arc_angle(0.0, 5.0, 0.0), 0.0)       # ahead
        self.assertAlmostEqual(arc_angle(5.0, 0.0, 0.0), 90.0)      # right (clockwise)
        self.assertAlmostEqual(abs(arc_angle(0.0, -5.0, 0.0)), 180.0)
        # turn left 90 degrees: something east is now behind
        self.assertAlmostEqual(abs(arc_angle(5.0, 0.0, 90.0)), 180.0, places=5)


class _Cfg:
    cell = 1.0


class _Panel:
    def __init__(self, center, size, kind="wall"):
        self.center, self.size, self.hpr, self.kind = center, size, (0, 0, 0), kind


class _Nav:
    """5x5 cells, a 3x3 floor in the middle, one floor cell with a tunnel below."""

    def __init__(self):
        self.cfg = _Cfg()
        self.nx = self.ny = 5
        self.x0 = self.y0 = 0.0
        cols, z = [], []
        for j in range(1, 4):
            for i in range(1, 4):
                cols.append(j * 5 + i)
                z.append(0.0)
        cols.append(2 * 5 + 2)
        z.append(-4.0)
        order = np.argsort(cols, kind="stable")
        self.node_col = np.array(cols)[order]
        self.node_z = np.array(z)[order]


class RadarImageTests(unittest.TestCase):
    def test_floor_walls_sites_and_panels(self):
        zones = [{"name": "A", "kind": "bombsite", "min": [1, 1, 0], "max": [2, 2, 3]}]
        img = radar_image(_Nav(), zones, [_Panel((4.5, 2.5, 1.0), (0.1, 1.0, 2.0))])
        self.assertEqual(img.shape, (5, 5, 4))
        # the ring around the floor is wall, except where the panel is drawn
        np.testing.assert_allclose(img[0, 0, :3], WALL)
        np.testing.assert_allclose(img[2, 4, :3], PANEL)
        # floor cells are opaque and not wall coloured
        self.assertGreater(img[3, 3, 3], 0.9)
        self.assertFalse(np.allclose(img[3, 3, :3], WALL))
        # the site tint pulls (1, 1) towards red
        self.assertGreater(img[1, 1, 0] - img[1, 1, 2], img[3, 3, 0] - img[3, 3, 2])
        self.assertGreater(float(np.dot(img[1, 1, :3], SITE)), 0.0)
        # the tunnel under (2, 2) tints it blue
        self.assertGreater(img[2, 2, 2], img[3, 3, 2])


class AudioRuleTests(unittest.TestCase):
    def test_shot_variants(self):
        self.assertEqual(shot_variant(10.0, False), "")
        self.assertEqual(shot_variant(10.0, True), "_muffled")
        self.assertEqual(shot_variant(FAR_DISTANCE + 1, False), "_far")
        self.assertEqual(shot_variant(FAR_DISTANCE + 1, True), "_far_muffled")

    def test_environment(self):
        self.assertEqual(environment(None, 0.0), "outdoor")
        self.assertEqual(environment(3.0, 0.0), "indoor")
        self.assertEqual(environment(2.5, -3.0), "tunnel")
        for env in ("outdoor", "indoor", "tunnel"):
            self.assertIn(env, AMBIENCE)

    def test_approach(self):
        self.assertAlmostEqual(approach(0.0, 1.0, 0.25), 0.25)
        self.assertAlmostEqual(approach(0.9, 1.0, 0.25), 1.0)
        self.assertAlmostEqual(approach(0.5, 0.0, 0.25), 0.25)


class SynthTests(unittest.TestCase):
    def test_seamless_loop_has_no_click(self):
        rng = np.random.default_rng(1)
        x = synth.band(synth.noise(3.0, rng), 100, 2000)
        y = synth.seamless(x, 1.0)
        self.assertEqual(len(y), synth._n(2.0))
        # the loop point continues the waveform: the jump is no bigger than a typical step
        steps = np.abs(np.diff(y))
        self.assertLess(abs(float(y[0] - y[-1])), float(np.percentile(steps, 99.9)) * 1.5)

    def test_muffle_removes_highs(self):
        rng = np.random.default_rng(2)
        x = synth.gunshot("rifle_heavy", rng)
        m = synth.muffle(x)

        def high_ratio(s):
            spec = np.abs(np.fft.rfft(s)) ** 2
            f = np.fft.rfftfreq(len(s), 1.0 / synth.RATE)
            return spec[f > 2000].sum() / spec.sum()
        self.assertLess(high_ratio(m), high_ratio(x) * 0.05)

    def test_far_shot_is_duller_and_longer(self):
        rng = np.random.default_rng(3)
        near = synth.gunshot("pistol", rng)
        far = synth.gunshot_far("pistol", rng)
        self.assertGreater(len(far), len(near))

        def centroid(s):
            spec = np.abs(np.fft.rfft(s))
            f = np.fft.rfftfreq(len(s), 1.0 / synth.RATE)
            return float((spec * f).sum() / spec.sum())
        self.assertLess(centroid(far), centroid(near))

    def test_music_cues_render(self):
        rng = np.random.default_rng(4)
        for kind in ("round_start", "win", "lose", "planted"):
            x = synth.sting(kind, rng)
            self.assertGreater(len(x), synth.RATE)
            self.assertTrue(np.isfinite(x).all())
            self.assertLessEqual(float(np.abs(x).max()), 1.0)
        self.assertAlmostEqual(synth._note(69), 440.0)
        self.assertTrue(math.isclose(synth._note(81), 880.0))


if __name__ == "__main__":
    unittest.main()
