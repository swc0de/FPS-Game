"""Post-processing configuration helpers (no GPU needed)."""
import unittest

from render.post import AA_MODES, AO_QUALITY, ENV_DEFAULTS, _merge_env, normalize_graphics
from render.renderer import defines_from_graphics


class GraphicsNormalisationTests(unittest.TestCase):
    def test_legacy_booleans_are_converted(self):
        g = normalize_graphics({"ssao": True, "fxaa": False})
        self.assertEqual(g["ssao"], "medium")
        self.assertEqual(g["antialiasing"], "off")
        g = normalize_graphics({"ssao": False, "fxaa": True})
        self.assertEqual(g["ssao"], "off")
        self.assertEqual(g["antialiasing"], "fxaa")

    def test_explicit_modes_survive(self):
        for aa in AA_MODES:
            self.assertEqual(normalize_graphics({"antialiasing": aa})["antialiasing"], aa)
        for q in AO_QUALITY:
            self.assertEqual(normalize_graphics({"ssao": q})["ssao"], q)

    def test_render_scale_is_clamped(self):
        self.assertEqual(normalize_graphics({"render_scale": 2.0})["render_scale"], 1.0)
        self.assertEqual(normalize_graphics({"render_scale": 0.1})["render_scale"], 0.5)

    def test_ssao_define_follows_setting(self):
        self.assertTrue(defines_from_graphics({"ssao": "low"})["SSAO"])
        self.assertFalse(defines_from_graphics({"ssao": "off"})["SSAO"])

    def test_environment_merge_keeps_defaults(self):
        env = _merge_env({"bloom": {"intensity": 0.2}, "exposure": 1.3})
        self.assertEqual(env["bloom"]["intensity"], 0.2)
        self.assertEqual(env["bloom"]["threshold"], ENV_DEFAULTS["bloom"]["threshold"])
        self.assertEqual(env["exposure"], 1.3)
        self.assertIn("auto_exposure", env)


if __name__ == "__main__":
    unittest.main()
