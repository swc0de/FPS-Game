"""IBL processing tests: HDRI orientation, sun detection, SH accuracy, cube faces."""
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from engine import paths
from render import ibl


def write_rgbe(path: Path, img_top_down: np.ndarray) -> None:
    """Minimal uncompressed Radiance .hdr writer (rows top to bottom)."""
    h, w = img_top_down.shape[:2]
    m = img_top_down.max(axis=2)
    e = np.where(m > 1e-32, np.floor(np.log2(np.maximum(m, 1e-32))) + 1, 0)
    scale = np.where(m > 1e-32, 256.0 / np.power(2.0, e), 0)
    rgb = np.clip(img_top_down * scale[..., None], 0, 255).astype(np.uint8)
    ex = np.where(m > 1e-32, e + 128, 0).astype(np.uint8)
    data = np.concatenate([rgb, ex[..., None]], -1)
    with open(path, "wb") as f:
        f.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n")
        f.write(f"-Y {h} +X {w}\n".encode())
        f.write(data.tobytes())


class IBLTests(unittest.TestCase):
    def test_cube_faces_cover_sphere(self):
        d = ibl.cube_face_dirs(16)
        # each face's centre looks along its axis, in GL order
        centres = d[:, 8, 8]
        expected = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        for c, e in zip(centres, expected):
            self.assertGreater(float(np.dot(c, e)), 0.99)
        total = ibl.cube_texel_solid_angle(16).sum() * 6
        self.assertAlmostEqual(total, 4 * math.pi, delta=0.05)

    def test_sh_matches_brute_force(self):
        eq = ibl.procedural_sky(256, 128, [0.3, 0.2, 0.9], {"clouds": 0.0})
        sh = ibl.sh9_irradiance(eq)
        d = ibl.equirect_dirs(256, 128)
        lat = ((np.arange(128) + 0.5) / 128 - 0.5) * math.pi
        dw = (2 * math.pi / 256) * (math.pi / 128) * np.cos(lat)[:, None]
        for n in ([0, 0, 1], [1, 0, 0], [0, -0.6, 0.8]):
            n = np.array(n) / np.linalg.norm(n)
            cos = np.clip(d @ n, 0, None)
            brute = (eq * (cos * dw)[..., None]).sum((0, 1)) / math.pi
            x, y, z = n
            basis = np.array([1, y, z, x, x * y, y * z, 3 * z * z - 1, x * z, x * x - y * y])
            approx = basis @ sh
            np.testing.assert_allclose(approx, brute, rtol=0.08, atol=0.01)

    def test_hdri_orientation_and_sun(self):
        tmp = Path(tempfile.mkdtemp())
        orig_h, orig_c = paths.HDRI_DIR, paths.CACHE_DIR
        paths.HDRI_DIR = tmp
        paths.CACHE_DIR = tmp
        try:
            w, h = 256, 128
            img = np.full((h, w, 3), 0.5, np.float32)
            # sun at elevation +40 deg, longitude +90 deg (=> direction +Y in our convention)
            elev, lon = math.radians(40), math.radians(90)
            col = int((lon / (2 * math.pi) + 0.5) * w)
            row_from_bottom = int((elev / math.pi + 0.5) * h)
            row_top_down = h - 1 - row_from_bottom
            img[row_top_down - 1:row_top_down + 2, col - 1:col + 2] = 5000.0
            img[h // 2 + 8:, :] = 0.05  # dark ground in the lower part of the file
            write_rgbe(tmp / "test_sky.hdr", img)
            eq = ibl.load_hdri(tmp / "test_sky.hdr")
            self.assertGreater(eq[-1].mean(), eq[0].mean())  # row 0 = bottom (dark ground)
            env = ibl.build_environment({"hdri": "missing", "hdri_candidates": ["test_sky"], "sun_clamp": 10},
                                        log=lambda m: None)
            self.assertTrue(env["source"].startswith("hdri:test_sky"))
            sun = env["sun_dir"]
            self.assertAlmostEqual(math.degrees(math.asin(sun[2])), 40, delta=3)
            self.assertGreater(sun[1], 0.7)
            self.assertLessEqual(float(env["equirect"].max()), 10.5)  # sun clamped out of the IBL
        finally:
            paths.HDRI_DIR, paths.CACHE_DIR = orig_h, orig_c
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
