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

    def test_rebinding_swaps_conflicts(self):
        binds = self.m.pending["input"]["binds"]
        self.assertEqual(binds["lean_right"], "e")
        swapped = self.m.bind("reload", "e")
        self.assertEqual(swapped, "lean_right")
        self.assertEqual(binds["reload"], "e")
        self.assertEqual(binds["lean_right"], "r")
        self.assertEqual(self.m.text(opt("reload")), "E")
        self.assertIsNone(self.m.bind("reload", "f5"))
        self.assertTrue(self.m.dirty())

    def test_every_action_has_a_controls_row(self):
        from engine.settings import DEFAULT_KEYBINDS
        self.assertEqual({o.key for o in TABS["CONTROLS"]}, set(DEFAULT_KEYBINDS))
        self.assertEqual(len(set(DEFAULT_KEYBINDS.values())), len(DEFAULT_KEYBINDS))   # no default conflicts

    def test_reset_tab(self):
        self.m.bind("jump", "f6")
        self.m.set(opt("music"), 0.1)
        self.m.set(opt("minimap"), False)
        self.m.reset_tab("CONTROLS")
        self.assertEqual(self.m.pending["input"]["binds"]["jump"], "space")
        self.assertEqual(self.m.get(opt("music")), 0.1)
        self.m.reset_tab("AUDIO")
        self.assertEqual(self.m.get(opt("music")), 0.5)
        self.assertFalse(self.m.get(opt("minimap")))
        self.m.reset_tab("GAMEPLAY")
        self.assertTrue(self.m.get(opt("minimap")))

    def test_fov_text_shows_horizontal(self):
        self.assertIn("h)", self.m.text(opt("fov")))


if __name__ == "__main__":
    unittest.main()
