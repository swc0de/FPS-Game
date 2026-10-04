"""Settings menu model (no window needed)."""
import unittest

from engine.settings import Settings
from ui.menus import SHADOW_LEVELS, TABS, SettingsModel


def opt(key):
    for opts in TABS.values():
        for o in opts:
            if o.key == key:
                return o
    raise KeyError(key)


class SettingsModelTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings({"video": {"preset": "high"}})
        self.m = SettingsModel(self.settings)

    def test_graphics_override_and_preset_reset(self):
        self.m.set(opt("ssao"), "ultra")
        self.assertEqual(self.m.pending["graphics"], {"ssao": "ultra"})
        self.assertTrue(self.m.dirty())
        self.assertIn("custom", self.m.text(opt("preset")))
        # setting the preset's own value removes the override again
        self.m.set(opt("ssao"), self.settings.presets["high"]["ssao"])
        self.assertEqual(self.m.pending["graphics"], {})
        self.m.set(opt("ssao"), "low")
        self.m.set(opt("preset"), "low")
        self.assertEqual(self.m.pending["graphics"], {})
        self.assertEqual(self.m.get(opt("ssao")), self.settings.presets["low"]["ssao"])

    def test_shadow_quality_composite(self):
        self.assertEqual(self.m.get(opt("shadow_quality")), "high")
        self.m.step(opt("shadow_quality"), 1)
        self.assertEqual(self.m.get(opt("shadow_quality")), "ultra")
        g = self.m.graphics()
        for k, v in SHADOW_LEVELS["ultra"].items():
            self.assertEqual(g[k], v)

    def test_cycling_wraps(self):
        o = opt("antialiasing")
        first = self.m.get(o)
        for _ in range(len(o.choices)):
            self.m.step(o, 1)
        self.assertEqual(self.m.get(o), first)

    def test_restart_detection(self):
        self.assertEqual(self.m.restart_pending(), [])
        self.m.step(opt("texture_size"), 1)
        self.assertEqual(self.m.restart_pending(), ["Texture quality"])

    def test_fov_text_shows_horizontal(self):
        self.assertIn("h)", self.m.text(opt("fov")))


if __name__ == "__main__":
    unittest.main()
