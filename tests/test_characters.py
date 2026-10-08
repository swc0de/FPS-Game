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


    def test_decimation_reaches_the_target_on_thin_plates(self):
        # a plate a few cells thick: many collapses across its rim would flip a face; refused
        # ones must not hold up their neighbourhood round after round
        sh = S.Shape().add(S.box((0, 0, 0), (0.15, 0.012, 0.2), 0.004, "a"))
        v, f, _ = surface_nets(sh, *sh.bounds(0.01), cell=0.006)
        v2, f2, _ = decimate(v, f, 200, max_rounds=200)
        self.assertLessEqual(len(f2), 230)
        self.assertTrue(closed(f2))

    def test_helmet_pieces_mesh_closed_and_manifold(self):
        # a step in a distance field cracks the mesh, a wall thinner than two cells pinches it;
        # either one locks the decimation (the goggle strap stayed at 760 triangles at every LOD)
        from characters import gear as G
        for style in ("vanguard", "bastion"):
            for p in G.helmet(style, True, True, True):
                v, f, _ = surface_nets(p.shape, *p.shape.bounds(), cell=p.cell)
                e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
                _, cnt = np.unique(e[:, 0] * len(v) + e[:, 1], return_counts=True)
                self.assertEqual(int((cnt == 1).sum()), 0, f"{style} {p.name} open edges")
                self.assertLessEqual(int((cnt > 2).sum()), 4, f"{style} {p.name} non-manifold edges")

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


class ClipTests(unittest.TestCase):
    def test_channel_interpolation(self):
        from gameplay.body import _channel
        keys = [{"t": 0.0, "a": 0.0, "h": None}, {"t": 0.5, "a": 10.0}, {"t": 1.0, "a": 0.0, "h": ["chest", 1, 2, 3]}]
        self.assertAlmostEqual(_channel(keys, "a", 0.0), 0.0)
        self.assertAlmostEqual(_channel(keys, "a", 0.5), 10.0)
        self.assertAlmostEqual(_channel(keys, "a", 0.25), 5.0)            # smoothstep at the midpoint
        before, after, w = _channel(keys, "h", 0.5)
        self.assertIsNone(before)
        self.assertEqual(after, ["chest", 1, 2, 3])
        self.assertAlmostEqual(w, 0.5)
        self.assertIsNone(_channel(keys, "missing", 0.5))

    def test_clips_are_well_formed(self):
        from gameplay.body import clips
        for name, c in clips().items():
            ts = [k["t"] for k in c["keys"]]
            self.assertEqual(ts, sorted(ts), name)
            self.assertEqual((ts[0], ts[-1]), (0.0, 1.0), name)
            self.assertGreater(c["duration"], 0.0, name)

    def test_events_keep_hitboxes_on_the_pose_and_end(self):
        from panda3d.core import NodePath, Vec3
        from types import SimpleNamespace
        from engine.physics import PhysicsWorld
        from gameplay.body import PART_MOUNT, CharacterBody
        from panda3d.core import Point3, TransformState
        game = SimpleNamespace(render=NodePath("render"), physics=PhysicsWorld(NodePath("world")))
        body = CharacterBody(game, object(), visible=False)
        body.set_weapon("rifle_r7", "rifle")
        body.event("reload", duration=1.0)
        for _ in range(40):
            body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        self.assertIn("reload", body.active_clips)
        world = body.pose.solve()
        for name, (bone, off, hpr) in PART_MOUNT.items():
            m = TransformState.makePosHpr(Point3(*off), Vec3(*hpr)).getMat()
            m = np.array([[m.getCell(i, j) for j in range(4)] for i in range(4)])
            got = body.parts[name].getMat(body.root)
            got = np.array([[got.getCell(i, j) for j in range(4)] for i in range(4)])
            np.testing.assert_allclose(got, m @ world[sk.INDEX[bone]], atol=1e-5)
        for _ in range(40):
            body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        self.assertNotIn("reload", body.active_clips)
        body.event("plant")
        for _ in range(80):
            body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        self.assertIn("plant", body.active_clips)                        # loops until stopped
        self.assertGreater(body.crouch, 0.9)
        body.stop("plant")
        for _ in range(80):
            body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        self.assertNotIn("plant", body.active_clips)

    def test_flinch_does_not_depend_on_health(self):
        """No hidden-state leak: the flinch is the same whatever the health (OVERHAUL_PLAN 4.2)."""
        from panda3d.core import NodePath, Vec3
        from types import SimpleNamespace
        from engine.physics import PhysicsWorld
        from gameplay.body import CharacterBody
        game = SimpleNamespace(render=NodePath("render"), physics=PhysicsWorld(NodePath("world")))
        flinches = []
        for hp in (100, 7):
            owner = SimpleNamespace(damageable=SimpleNamespace(health=hp))
            body = CharacterBody(game, owner, visible=False)
            body.event("hit", direction=Vec3(0, 1, 0))
            flinches.append(body.flinch)
        self.assertEqual(flinches[0], flinches[1])



def _hull_area(p2: np.ndarray) -> float:
    pts = sorted(set(map(tuple, np.round(p2, 6))))
    if len(pts) < 3:
        return 0.0

    def half(ps):
        out = []
        for q in ps:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (q[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(q)
        return out
    h = np.array(half(pts)[:-1] + half(pts[::-1])[:-1])
    x, y = h[:, 0], h[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


class TwistTests(unittest.TestCase):
    """No candy-wrapper (OVERHAUL_PLAN 7): twisting a limb 90 degrees either way, with the twist
    bone taking half of the roll, every ring of the limb keeps at least 70 % of its area."""

    def _rings(self, start: str, end: str, pose_fn, ts=(0.3, 0.6, 0.85)) -> np.ndarray:
        from characters import human as H
        B = sk.INDEX
        if not hasattr(self, "_mesh"):
            self._mesh = {}
        if start not in self._mesh:
            body = H.body()
            a, b = sk.REST_WORLD[B[start], 3, :3], sk.REST_WORLD[B[end], 3, :3]
            v, f, _ = surface_nets(body, np.minimum(a, b) - 0.07, np.maximum(a, b) + 0.07, cell=0.005)
            _, tag = body.evaluate(v, with_tags=True)
            self._mesh[start] = (v, *W.compute(v, body.tags(), region_of=tag))
        v, joints, weights = self._mesh[start]
        pose = sk.Pose()
        pose_fn(pose)
        world = pose.solve()
        rows = sk.palette_rows(world, np.zeros((sk.N_BONES, 3, 4), np.float32)).astype(np.float64)
        p = sk.skin_points(v, joints, weights, rows)
        a, b = world[B[start], 3, :3], world[B[end], 3, :3]
        ax = (b - a) / np.linalg.norm(b - a)
        t = (p - a) @ ax / np.linalg.norm(b - a)
        u = np.cross(ax, [0.0, 0.0, 1.0])
        u /= np.linalg.norm(u)
        w = np.cross(ax, u)
        out = []
        for t0 in ts:
            q = p[(t > t0 - 0.04) & (t < t0 + 0.04)] - a
            out.append(_hull_area(np.stack([q @ u, q @ w], axis=1)))
        return np.array(out)

    def _roll(self, bone: str, twist: str, roll: float):
        def fn(P):
            for name, share in ((bone, 1.0), (twist, 0.5)):
                h, p, r = P.hpr[sk.INDEX[name]]
                P.set_hpr(sk.INDEX[name], h, p, r + roll * share)
        return fn

    def test_forearm_rings_keep_their_area(self):
        rest = self._rings("lowerarm_r", "hand_r", lambda P: None)
        for roll in (90.0, -90.0):
            got = self._rings("lowerarm_r", "hand_r", self._roll("hand_r", "lowerarm_twist_r", roll))
            self.assertTrue((got / rest >= 0.7).all(), (roll, got / rest))

    def test_upper_arm_rings_keep_their_area(self):
        rest = self._rings("upperarm_r", "lowerarm_r", lambda P: None)
        for roll in (90.0, -90.0):
            got = self._rings("upperarm_r", "lowerarm_r", self._roll("lowerarm_r", "upperarm_twist_r", roll))
            self.assertTrue((got / rest >= 0.7).all(), (roll, got / rest))


class HitCapsuleAlignmentTests(unittest.TestCase):
    """OVERHAUL_PLAN 7, B hitboxes: the visual and hit box head centres coincide within 1 cm, and
    every capsule's axis runs inside the body it belongs to."""

    def _head_capsule(self):
        from gameplay.hitboxes import CAPSULES
        return np.array(next(c for c in CAPSULES if c.name == "head").a)

    def test_procedural_head_centre(self):
        from characters import human as H
        sh, lm = H.head()
        p = np.random.default_rng(3).uniform(*sh.bounds(), (60000, 3))
        inside = p[sh(p) < 0]
        skull = inside[(inside[:, 2] > 1.50) & (np.abs(inside[:, 0]) < 0.07)]
        front = skull[(np.abs(skull[:, 0]) < 0.03) & (skull[:, 2] > lm["eyes"][0][2] + 0.01)]
        centre = np.array([0.0, 0.5 * (skull[:, 1].min() + front[:, 1].max()),
                           0.5 * (skull[:, 2].max() + 1.503)])        # crown to chin (human.head)
        self.assertLess(np.linalg.norm(centre - self._head_capsule()), 0.01, centre)

    def test_makehuman_head_centres(self):
        from characters import makehuman as MH
        data = MH.data_dir()
        if data is None:
            self.skipTest("MPFB2 files not downloaded")
        for name in ("Kestrel", "Sable", "Anvil", "Thorn"):
            head = MH.head_region(data, appearance(name, 1))
            v = head["verts"][head["weights"][:, sk.INDEX["head"]] > 0.95]
            brow = v[(np.abs(v[:, 0]) < 0.03) & (v[:, 2] > head["landmarks"]["eyes"][0][2] + 0.01)]
            centre = np.array([0.0, 0.5 * (v[:, 1].min() + brow[:, 1].max()), 0.5 * (v[:, 2].max() + v[:, 2].min())])
            self.assertLess(np.linalg.norm(centre - self._head_capsule()), 0.01, (name, centre))

    def test_capsule_axes_run_inside_the_body(self):
        from characters import human as H
        from gameplay.hitboxes import CAPSULES
        body = H.body()
        head, _ = H.head()
        for c in CAPSULES:
            a = np.array(c.a)
            b = np.array(c.b) if c.b is not None else a
            pts = a + np.linspace(0.0, 1.0, 9)[:, None] * (b - a)
            d = np.minimum(body(pts), head(pts))
            self.assertTrue((d < 0.0).all(), (c.name, d.round(3)))

class MakeHumanTests(unittest.TestCase):
    def setUp(self):
        from characters import makehuman as MH
        self.MH = MH
        self.data = MH.data_dir()
        if self.data is None:
            self.skipTest("MPFB2 files not downloaded (tools/download_assets.py --only characters)")

    def test_head_sits_upright_on_the_game_neck(self):
        from characters import appearance as A
        for name in ("Kestrel", "Sable", "Anvil"):
            head = self.MH.head_region(self.data, A.appearance(name, 1))
            v, lm = head["verts"], head["landmarks"]
            np.testing.assert_allclose(head["weights"].sum(axis=1), 1.0, atol=1e-9)
            hv = v[head["weights"][:, sk.INDEX["head"]] > 0.95]
            # the head's middle where the procedural head's is (hit sphere, helmet)
            self.assertAlmostEqual(0.5 * (hv[:, 2].max() + hv[:, 2].min()), self.MH.HEAD_MID_Z, delta=0.002)
            self.assertTrue(0.20 < hv[:, 2].max() - hv[:, 2].min() < 0.26)
            # upright and facing +y: eyes level, in front of the skull, the mouth below them
            le, re = lm["eyes"]
            self.assertLess(abs(le[2] - re[2]), 0.002)
            self.assertLess(le[0], -0.02)
            self.assertGreater(re[0], 0.02)
            self.assertGreater(le[1], 0.06)
            self.assertTrue(0.04 < le[2] - lm["mouth_z"] < 0.09)
            self.assertLess(v[:, 2].min(), 1.46)                          # the neck goes under the collar

    def test_offline_switch(self):
        import os
        os.environ["COLD_SECTOR_OFFLINE"] = "1"
        try:
            self.assertIsNone(self.MH.data_dir())
        finally:
            del os.environ["COLD_SECTOR_OFFLINE"]
