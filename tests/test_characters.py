"""Milestone 9 procedural soldiers: appearance variety and stability, distance fields,
meshing, simplification and skin weights."""
import unittest

import numpy as np

from characters import sdf as S
from characters import weights as W
from characters.appearance import appearance, style_for_side
from characters.decimate import decimate
from characters.mesher import surface_nets
from gameplay import skeleton as sk

ATTACK = ["Rook", "Harrow", "Sable", "Dagger", "Mako", "Brine", "Cinder", "Wolfe", "Kestrel", "Jager"]
DEFEND = ["Anvil", "Bastille", "Grail", "Halyard", "Osprey", "Pike", "Thorn", "Vigil", "Rampart", "Tarn"]


def closed(f: np.ndarray) -> bool:
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return bool((cnt == 2).all())


class AppearanceTests(unittest.TestCase):
    def test_variety_over_a_match_roster(self):
        apps = [appearance(n, 1) for n in ATTACK + DEFEND]
        faces = {tuple(round(v, 3) for v in vars(a.face).values() if isinstance(v, float)) for a in apps}
        self.assertGreaterEqual(len(faces), 8)
        tones = {round(a.melanin, 1) for a in apps}
        self.assertGreaterEqual(len(tones), 5, tones)
        self.assertEqual({a.build.mass for a in apps}, {-1.0, 0.0, 1.0})
        self.assertTrue(any(a.build.female for a in apps) and not all(a.build.female for a in apps))
        for names, side in ((ATTACK, "attack"), (DEFEND, "defend")):
            looks = [appearance(n, 1).team[style_for_side(side)] for n in names]
            self.assertGreaterEqual(len({lk.headgear for lk in looks}), 2, side)
            layouts = {(lk.layout["radio"], lk.layout["mags"], lk.layout["admin"]) for lk in looks}
            self.assertGreaterEqual(len(layouts), 2, side)

    def test_stable_and_the_face_survives_halftime(self):
        a, b = appearance("Kestrel", 4), appearance("Kestrel", 4)
        self.assertEqual(a.face, b.face)
        self.assertEqual(a.melanin, b.melanin)
        # the side only picks the kit; face, build and hair are the same object for both
        self.assertEqual(set(a.team), {"vanguard", "bastion"})
        self.assertNotEqual(a.key("vanguard"), a.key("bastion"))
        self.assertNotEqual(appearance("Kestrel", 5).face, a.face)


class MeshingTests(unittest.TestCase):
    def test_round_cone_distance(self):
        p = np.array([[0.0, 0.0, 0.5], [0.3, 0.0, 0.5], [0.0, 0.0, 1.3]])
        d = S.round_cone(p, (0, 0, 0), (0, 0, 1), 0.2, 0.1)
        self.assertAlmostEqual(d[1], 0.3 - 0.15, delta=0.01)
        self.assertAlmostEqual(d[2], 0.3 - 0.1, delta=1e-6)
        self.assertLess(d[0], 0)

    def test_culled_evaluation_matches_the_plain_union(self):
        sh = S.Shape().add(S.cone((0, 0, 0), (0, 0, 0.5), 0.1, 0.08, "a"),
                           S.ell((0.3, 0, 0.2), (0.1, 0.05, 0.08), "b", blend=0.04),
                           S.ell((0.0, 0.09, 0.3), (0.03, 0.03, 0.03), "a", blend=0.01, subtract=True))
        rng = np.random.default_rng(1)
        p = rng.uniform(-0.4, 0.7, (4000, 3))
        plain = S.round_cone(p, np.zeros(3), np.array([0, 0, 0.5]), 0.1, 0.08)
        plain = S.smin(plain, S.ellipsoid(p, (0.3, 0, 0.2), (0.1, 0.05, 0.08)), 0.04)
        plain = S.smax(plain, -S.ellipsoid(p, (0.0, 0.09, 0.3), (0.03, 0.03, 0.03)), 0.01)
        np.testing.assert_allclose(sh(p), plain, atol=1e-9)

    def test_surface_nets_are_closed_and_on_the_surface(self):
        sh = S.Shape().add(S.cone((0, 0, 0), (0, 0, 0.4), 0.1, 0.07, "a"),
                           S.cone((0, 0, 0.2), (0.25, 0, 0.3), 0.05, 0.04, "b", blend=0.03))
        v, f, n = surface_nets(sh, *sh.bounds(), cell=0.01)
        self.assertTrue(closed(f))
        self.assertLess(np.abs(sh(v)).max(), 1e-4)
        c = v[f].mean(axis=1)
        fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
        g = np.stack([sh(c + e * 1e-4) - sh(c - e * 1e-4) for e in np.eye(3)], axis=1)
        self.assertGreater(((fn * g).sum(axis=1) > 0).mean(), 0.99)     # wound outwards

    def test_decimation_keeps_the_shape_closed(self):
        sh = S.Shape().add(S.ell((0, 0, 0), (0.2, 0.12, 0.3), "a"))
        v, f, _ = surface_nets(sh, *sh.bounds(), cell=0.012)
        v2, f2, _ = decimate(v, f, len(f) // 6)
        self.assertLessEqual(len(f2), len(f) // 6 + 50)
        self.assertTrue(closed(f2))
        self.assertLess(np.abs(sh(v2)).max(), 0.004)


class WeightTests(unittest.TestCase):
    def test_weights_are_normalised_and_stay_in_their_region(self):
        from characters import human as H
        body = H.body()
        rng = np.random.default_rng(2)
        lo, hi = body.bounds()
        p = rng.uniform(lo, hi, (20000, 3))
        d, tag = body.evaluate(p, with_tags=True)
        near = np.abs(d) < 0.02
        p, tag = p[near], tag[near]
        j, w = W.compute(p, body.tags(), region_of=tag)
        np.testing.assert_allclose(w.sum(axis=1), 1.0, atol=1e-5)
        tags = body.tags()
        for ri, name in enumerate(tags):
            sel = tag == ri
            allowed = {sk.INDEX[b] for b in W.REGIONS[name]} | {sk.INDEX[b] for b in sk.INDEX if "twist" in b}
            used = set(np.unique(j[sel][w[sel] > 0]).astype(int).tolist())
            self.assertTrue(used <= allowed, (name, used - allowed))
        # a point on the left thigh never follows the right leg
        lt = tags.index("leg_l")
        sel = tag == lt
        self.assertFalse(np.isin(j[sel][w[sel] > 0], [sk.INDEX["thigh_r"], sk.INDEX["calf_r"]]).any())

    def test_forearm_twist_share_grows_towards_the_wrist(self):
        e, wr = sk.REST_WORLD[sk.INDEX["lowerarm_r"], 3, :3], sk.REST_WORLD[sk.INDEX["hand_r"], 3, :3]
        pts = np.array([e + (wr - e) * t for t in (0.1, 0.5, 0.9)]) + np.array([0, 0.035, 0])
        j, w = W.compute(pts, ["arm_r"], region_of=np.zeros(3, np.int64))
        twist = sk.INDEX["lowerarm_twist_r"]
        share = [(w[i][j[i] == twist].sum()) for i in range(3)]
        self.assertLess(share[0], share[1])
        self.assertLess(share[1], share[2])


if __name__ == "__main__":
    unittest.main()
