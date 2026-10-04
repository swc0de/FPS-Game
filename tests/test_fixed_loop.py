import unittest

from engine.fixed_loop import FixedTimestep


class FixedTimestepTests(unittest.TestCase):
    def test_tick_count_matches_time(self):
        loop = FixedTimestep(64)
        ticks = []
        for _ in range(100):
            loop.advance(1 / 144, ticks.append)
        self.assertAlmostEqual(len(ticks) / 64, 100 / 144, delta=1 / 64)
        self.assertTrue(all(abs(t - 1 / 64) < 1e-12 for t in ticks))

    def test_alpha_range(self):
        loop = FixedTimestep(64)
        for _ in range(50):
            a = loop.advance(0.007, lambda dt: None)
            self.assertGreaterEqual(a, 0.0)
            self.assertLess(a, 1.0)

    def test_spiral_of_death_clamped(self):
        loop = FixedTimestep(64, max_ticks_per_frame=5)
        n = []
        loop.advance(10.0, n.append)
        self.assertEqual(len(n), 5)


if __name__ == "__main__":
    unittest.main()
