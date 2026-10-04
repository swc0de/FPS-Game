"""Offline tests for tools/download_assets.py (network calls are mocked)."""
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from engine import paths
from render import materials
from tools import download_assets as dl


def _png_bytes(arr: np.ndarray) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.png"
        materials.write_png(arr, p)
        return p.read_bytes()


class DownloadParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = paths.TEXTURE_DIR
        paths.TEXTURE_DIR = self.tmp

    def tearDown(self):
        paths.TEXTURE_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_polyhaven_pick(self):
        files = {
            "Diffuse": {"2k": {"jpg": {"url": "D2"}, "png": {"url": "D2p"}}, "1k": {"jpg": {"url": "D1"}}},
            "nor_gl": {"2k": {"png": {"url": "N2"}}},
            "arm": {"4k": {"png": {"url": "A4"}}, "2k": {"png": {"url": "A2"}}},
            "Displacement": {"1k": {"png": {"url": "H1"}}},
        }
        self.assertEqual(dl.ph_pick(files, "albedo", "2k", ("jpg", "png")), ("D2", "jpg"))
        self.assertEqual(dl.ph_pick(files, "normal", "2k"), ("N2", "png"))
        self.assertEqual(dl.ph_pick(files, "arm", "4k"), ("A4", "png"))
        self.assertEqual(dl.ph_pick(files, "height", "2k"), ("H1", "png"))  # falls back to 1k
        self.assertEqual(dl.ph_pick(files, "metal", "2k"), (None, None))
        self.assertAlmostEqual(dl.ph_world_size({"dimensions": [2000, 1500]}), 2.0)
        self.assertEqual(dl.ph_hdri_url({"hdri": {"4k": {"hdr": {"url": "U4"}}}}, "2k"), "U4")

    def test_polyhaven_material_end_to_end(self):
        albedo = _png_bytes(np.full((8, 8, 3), 128, np.uint8))
        normal = _png_bytes(np.tile(np.array([128, 128, 255], np.uint8), (8, 8, 1)))
        arm = _png_bytes(np.tile(np.array([255, 200, 0], np.uint8), (8, 8, 1)))
        files = {"Diffuse": {"2k": {"png": {"url": "alb"}}}, "nor_gl": {"2k": {"png": {"url": "nrm"}}},
                 "arm": {"2k": {"png": {"url": "arm"}}}}
        blobs = {"alb": albedo, "nrm": normal, "arm": arm}

        def fake_json(url):
            if "/files/" in url:
                return files
            return {"dimensions": [2500, 2500], "authors": {"Someone": "All"}}
        orig_json, orig_get = dl.http_json, dl.http_get
        dl.http_json = fake_json
        dl.http_get = lambda url, retries=3: blobs[url]
        try:
            res = dl.download_material("concrete_wall", {"sources": {"polyhaven": ["x"]}}, "2k", True, log=lambda m: None)
        finally:
            dl.http_json, dl.http_get = orig_json, orig_get
        self.assertEqual(res, "polyhaven:x")
        folder, source = materials.material_folder("concrete_wall")
        self.assertEqual(source, "downloaded")
        meta = json.loads((folder / "meta.json").read_text())
        self.assertAlmostEqual(meta["world_size_m"], 2.5)
        orm = dl._load_rgba((folder / "orm.png").read_bytes(), ".png")
        self.assertAlmostEqual(float(orm[..., 1].mean()), 200 / 255, delta=0.01)

    def test_ambientcg_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Metal032_2K-JPG_Color.png", _png_bytes(np.full((4, 4, 3), 90, np.uint8)))
            zf.writestr("Metal032_2K-JPG_NormalGL.png", _png_bytes(np.full((4, 4, 3), 128, np.uint8)))
            zf.writestr("Metal032_2K-JPG_Roughness.png", _png_bytes(np.full((4, 4, 3), 60, np.uint8)))
            zf.writestr("Metal032_2K-JPG_Metalness.png", _png_bytes(np.full((4, 4, 3), 255, np.uint8)))
        orig = dl.http_get
        dl.http_get = lambda url, retries=3: buf.getvalue()
        try:
            res = dl.download_material("steel", {"sources": {"ambientcg": ["Metal032"]}}, "2k", True, log=lambda m: None)
        finally:
            dl.http_get = orig
        self.assertEqual(res, "ambientcg:Metal032")
        orm = dl._load_rgba((self.tmp / "steel" / "orm.png").read_bytes(), ".png")
        self.assertAlmostEqual(float(orm[..., 0].mean()), 1.0, delta=0.01)   # no AO map -> 1
        self.assertAlmostEqual(float(orm[..., 2].mean()), 1.0, delta=0.01)   # metalness

    def test_all_candidates_fail_gracefully(self):
        def boom(url, retries=3):
            raise OSError("offline")
        orig = dl.http_json, dl.http_get
        dl.http_json = boom
        dl.http_get = boom
        try:
            res = dl.download_material("brick", {"sources": {"polyhaven": ["a"], "ambientcg": ["B"]}}, "2k", True,
                                       log=lambda m: None)
        finally:
            dl.http_json, dl.http_get = orig
        self.assertIsNone(res)
        self.assertEqual(materials.material_folder("brick")[1] != "downloaded", True)


if __name__ == "__main__":
    unittest.main()
