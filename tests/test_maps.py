"""Map data integrity: every map builds with known prefabs and materials,
the compound has both bomb sites, both spawn groups and no z-fighting pads."""
import json
import unittest
from types import SimpleNamespace

from engine import paths
from maps.level import LevelContext
from maps.prefabs import PREFABS
from render.materials import load_definitions

DEFS = load_definitions()


class FakeMaterials:
    def get(self, name):
        d = DEFS[name]
        return SimpleNamespace(uv_scale=float(d.get("uv_scale", 2.0)), surface=d.get("surface", "default"))


def build(name):
    with open(paths.MAPS_DIR / f"{name}.json", encoding="utf-8") as f:
        data = json.load(f)
    ctx = LevelContext(FakeMaterials())
    for piece in data["pieces"]:
        ctx.build_piece(piece)
    return data, ctx


class MapTests(unittest.TestCase):
    def test_all_maps_use_known_prefabs_and_materials(self):
        for path in paths.MAPS_DIR.glob("*.json"):
            data, ctx = build(path.stem)
            for p in data["pieces"]:
                self.assertIn(p["type"], PREFABS, f"{path.stem}: {p['type']}")
            for mat in ctx.builders:
                self.assertIn(mat, DEFS, f"{path.stem}: material {mat}")

    def test_compound_gameplay_data(self):
        data, ctx = build("compound")
        self.assertEqual(sorted(z["name"] for z in ctx.zones if z["kind"] == "bombsite"), ["A", "B"])
        teams = [s["team"] for s in ctx.spawns]
        self.assertEqual(teams.count("attack"), 5)
        self.assertEqual(teams.count("defend"), 5)
        self.assertGreater(len(ctx.callouts), 15)
        self.assertGreater(len(ctx.colliders), 300)
        self.assertGreater(len(data["test_routes"]), 8)
        # spawns sit inside the map bounds
        (x0, y0, _), (x1, y1, _) = data["bounds"]
        for s in ctx.spawns:
            self.assertTrue(x0 < s["pos"][0] < x1 and y0 < s["pos"][1] < y1)

    def test_compound_floor_pads_do_not_overlap(self):
        """Coplanar overlapping floor slabs would z-fight."""
        data, _ = build("compound")
        floors = [p for p in data["pieces"] if p["type"] == "floor"]
        for i, a in enumerate(floors):
            for b in floors[i + 1:]:
                if abs(a.get("z", 0) - b.get("z", 0)) > 1e-3:
                    continue
                ox = min(a["max"][0], b["max"][0]) - max(a["min"][0], b["min"][0])
                oy = min(a["max"][1], b["max"][1]) - max(a["min"][1], b["min"][1])
                self.assertFalse(ox > 1e-3 and oy > 1e-3, f"overlapping floors {a} / {b}")

    def test_callout_lookup(self):
        from maps.level import Level
        lvl = Level.__new__(Level)
        _, ctx = build("compound")
        lvl.callouts = ctx.callouts
        lvl.zones = ctx.zones
        self.assertEqual(lvl.callout_at(0, -55), "T Spawn")
        self.assertEqual(lvl.callout_at(33.5, 0), "Tunnel")       # smaller area wins over "Bunker"
        self.assertEqual(lvl.zone_at((-40, 30, 0.5))["name"], "A")
        self.assertIsNone(lvl.zone_at((0, 0, 0.5)))


if __name__ == "__main__":
    unittest.main()
