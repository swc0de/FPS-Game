"""Milestone 9 skeleton and skinning: rotation conventions, forward kinematics,
the skinning palette, the linear blend, and the body's hit boxes agreeing with
the skinned pose (what you see is what you hit)."""
import random
import unittest
from types import SimpleNamespace

import numpy as np
from panda3d.core import NodePath, Point3, TransformState, Vec3

from engine.geometry import MeshBuilder, build_skinned, skinned_vertex_format
from engine.physics import PhysicsWorld
from gameplay import skeleton as sk


def panda_rot(h, p, r):
    m = TransformState.makeHpr(Vec3(h, p, r)).getMat()
    return np.array([[m.getCell(i, j) for j in range(3)] for i in range(3)])


def node_mat(np_, other):
    m = np_.getMat(other)
    return np.array([[m.getCell(i, j) for j in range(4)] for i in range(4)])


class RotationTests(unittest.TestCase):
    def test_hpr_matches_panda(self):
        rng = random.Random(1)
        hprs = [(rng.uniform(-180, 180), rng.uniform(-89, 89), rng.uniform(-180, 180)) for _ in range(40)]
        many = sk.hpr_matrices(np.array(hprs))
        for k, hpr in enumerate(hprs):
            want = panda_rot(*hpr)
            np.testing.assert_allclose(sk.hpr_matrix(*hpr), want, atol=1e-6)
            np.testing.assert_allclose(many[k], want, atol=1e-6)

    def test_matrix_hpr_and_relative_hpr(self):
        rng = random.Random(2)
        for _ in range(40):
            a = (rng.uniform(-180, 180), rng.uniform(-80, 80), rng.uniform(-180, 180))
            np.testing.assert_allclose(sk.hpr_matrix(*sk.matrix_hpr(sk.hpr_matrix(*a))), sk.hpr_matrix(*a),
                                       atol=1e-9)
            b = (rng.uniform(-180, 180), rng.uniform(-80, 80), 0.0)
            rel = sk.hpr_matrix(*sk.relative_hpr(b, a))
            np.testing.assert_allclose(rel @ sk.hpr_matrix(*b), sk.hpr_matrix(*a), atol=1e-9)

    def test_aim_hpr_points_y_along_the_direction(self):
        for d in ((0.3, 0.1, -0.9), (-0.5, 0.5, 0.2), (0.0, 1.0, 0.0)):
            y = sk.hpr_matrix(*sk.aim_hpr(Vec3(*d)))[1]
            np.testing.assert_allclose(y, np.array(d) / np.linalg.norm(d), atol=1e-9)


class SkeletonTests(unittest.TestCase):
    def test_layout(self):
        self.assertTrue(40 <= sk.N_BONES <= sk.MAX_BONES)
        self.assertEqual(len(sk.INDEX), sk.N_BONES)
        self.assertTrue(all(p < i for i, p in enumerate(sk.PARENT)))
        for side in ("l", "r"):
            for name in ("clavicle", "upperarm", "upperarm_twist", "lowerarm", "lowerarm_twist", "hand",
                         "thumb_03", "index_03", "fingers_03", "thigh", "calf", "foot", "toe"):
                self.assertIn(f"{name}_{side}", sk.INDEX)
        for name in ("pelvis", "spine_01", "spine_02", "spine_03", "neck", "head", "weapon", "pack"):
            self.assertIn(name, sk.INDEX)

    def test_rest_pose_is_a_standing_a_pose(self):
        p = {b.name: sk.REST_WORLD[i, 3, :3] for i, b in enumerate(sk.BONES)}
        self.assertGreater(p["head"][2], p["neck"][2])
        self.assertAlmostEqual(p["head"][2] + 0.065, 1.635, places=6)       # the head hit sphere's centre
        for side, sx in (("l", -1), ("r", 1)):
            self.assertAlmostEqual(p[f"upperarm_{side}"][0], 0.21 * sx, places=6)
            # arms hang down and out
            self.assertLess(p[f"hand_{side}"][2], p[f"lowerarm_{side}"][2])
            self.assertGreater(p[f"hand_{side}"][0] * sx, p[f"upperarm_{side}"][0] * sx)
            # the thumb is on the front of the hand
            self.assertGreater(p[f"thumb_01_{side}"][1], p[f"fingers_01_{side}"][1])
            self.assertTrue(0.0 < p[f"foot_{side}"][2] < 0.12)
            self.assertTrue(0.0 <= p[f"toe_{side}"][2] < p[f"foot_{side}"][2])
            self.assertGreater(p[f"toe_{side}"][1], p[f"foot_{side}"][1])

    def test_solve_matches_a_scene_graph(self):
        rng = random.Random(3)
        pose = sk.Pose()
        root = NodePath("root")
        nodes = []
        for i, b in enumerate(sk.BONES):
            pose.set_hpr(i, rng.uniform(-60, 60), rng.uniform(-60, 60), rng.uniform(-60, 60))
            pose.set_pos(i, *(np.array(b.pos) + [rng.uniform(-0.05, 0.05) for _ in range(3)]))
            n = (root if sk.PARENT[i] < 0 else nodes[sk.PARENT[i]]).attachNewNode(b.name)
            n.setPosHpr(*pose.pos[i], *pose.hpr[i])
            nodes.append(n)
        world = pose.solve()
        for i, n in enumerate(nodes):
            np.testing.assert_allclose(world[i], node_mat(n, root), atol=1e-5)


class SkinningTests(unittest.TestCase):
    def test_rest_palette_is_identity(self):
        rows = sk.palette_rows(sk.REST_WORLD)
        self.assertEqual(rows.shape, (sk.N_BONES, 3, 4))
        np.testing.assert_allclose(rows, np.broadcast_to(np.eye(4)[:3], rows.shape), atol=1e-5)

    def test_linear_blend(self):
        rng = random.Random(4)
        pose = sk.Pose()
        for i in range(sk.N_BONES):
            pose.set_hpr(i, rng.uniform(-40, 40), rng.uniform(-40, 40), rng.uniform(-40, 40))
        world = pose.solve()
        rows = sk.palette_rows(world)
        lo, up = sk.INDEX["lowerarm_r"], sk.INDEX["upperarm_r"]
        # a bind-pose point near the elbow, rigidly on one bone, follows that bone
        pt = sk.REST_WORLD[lo, 3, :3] + np.array([0.01, 0.02, -0.03])
        local = np.append(pt, 1.0) @ sk.INV_BIND[lo]
        rigid = sk.skin_points(pt[None], np.array([[lo, 0, 0, 0]]), np.array([[1.0, 0, 0, 0]]), rows)[0]
        np.testing.assert_allclose(rigid, (local @ world[lo])[:3], atol=1e-5)
        # blending is linear in the weights
        other = sk.skin_points(pt[None], np.array([[up, 0, 0, 0]]), np.array([[1.0, 0, 0, 0]]), rows)[0]
        half = sk.skin_points(pt[None], np.array([[lo, up, 0, 0]]), np.array([[0.5, 0.5, 0, 0]]), rows)[0]
        np.testing.assert_allclose(half, 0.5 * (rigid + other), atol=1e-5)

    def test_skinned_mesh_format(self):
        fmt = skinned_vertex_format()
        arr = fmt.getArray(0)
        for col in ("skin_joints", "skin_weights"):
            c = arr.getColumn(col)
            self.assertIsNotNone(c, col)
            self.assertEqual(c.getNumComponents(), 4)
        mb = MeshBuilder()
        mb.add_box((0, 0, 0), (0.1, 0.1, 0.1))
        node = build_skinned([(mb, 7, None)], "t")
        vdata = node.getGeom(0).getVertexData()
        data = np.frombuffer(memoryview(vdata.getArray(0)).tobytes(), np.float32).reshape(vdata.getNumRows(), -1)
        np.testing.assert_array_equal(data[:, -8], 7.0)
        np.testing.assert_allclose(data[:, -4:].sum(axis=1), 1.0)


def make_body(weapon="rifle_r7", cls="rifle"):
    from gameplay.body import CharacterBody
    game = SimpleNamespace(render=NodePath("render"), physics=PhysicsWorld(NodePath("world")))
    body = CharacterBody(game, object(), visible=False)
    body.set_weapon(weapon, cls)
    return game, body


class BodyTests(unittest.TestCase):
    def test_standing_hitboxes_where_the_dummy_has_them(self):
        from gameplay.hitboxes import PARTS
        game, body = make_body()
        body.animate(1 / 64, (0, 0, 0), 0.0, 0.0, 0.0, Vec3(0, 0, 0), True)
        for part in PARTS:
            if part.name.startswith(("upper_arm", "forearm")):
                continue                                  # the arms hold the weapon
            got = body.parts[part.name].getPos(game.render)
            # the jointed legs have always sat up to 2 cm off the dummy's table; the refit is step B5
            tol = 0.025 if part.hitgroup == "leg" else 1e-4
            self.assertLess((got - Point3(*part.stand[:3])).length(), tol, part.name)

    def test_hitboxes_follow_the_skinned_pose(self):
        """The hit box nodes (Panda's chain) and the palette (numpy's) agree in every pose."""
        from gameplay.body import PART_MOUNT
        rng = random.Random(5)
        for weapon, cls in (("rifle_r7", "rifle"), ("pistol_p9", "pistol"), ("knife_k3", "knife")):
            game, body = make_body(weapon, cls)
            for step in range(60):
                body.animate(1 / 64, (1.0, 2.0, 0.0), rng.uniform(-180, 180), rng.uniform(-70, 70),
                             rng.choice((0.0, 1.0)), Vec3(rng.uniform(-4, 4), rng.uniform(-4, 4), 0), True,
                             lean=rng.uniform(-1, 1))
            world = body.pose.solve()
            for name, (bone, off, hpr) in PART_MOUNT.items():
                mount = TransformState.makePosHpr(Point3(*off), Vec3(*hpr)).getMat()
                mount = np.array([[mount.getCell(i, j) for j in range(4)] for i in range(4)])
                np.testing.assert_allclose(node_mat(body.parts[name], body.root), mount @ world[sk.INDEX[bone]],
                                           atol=1e-5, err_msg=f"{weapon} {name}")
            np.testing.assert_allclose(node_mat(body.gun, body.root), world[sk.INDEX["weapon"]], atol=1e-5)

    def test_capsules_follow_their_bones(self):
        from gameplay.hitboxes import CAPSULES, capsule_mount
        rng = random.Random(9)
        game, body = make_body()
        for step in range(40):
            body.animate(1 / 64, (0.5, -1.0, 0.0), rng.uniform(-180, 180), rng.uniform(-70, 70),
                         rng.choice((0.0, 1.0)), Vec3(rng.uniform(-4, 4), rng.uniform(-4, 4), 0), True,
                         lean=rng.uniform(-1, 1))
        world = body.pose.solve()
        for c in CAPSULES:
            m = capsule_mount(c)
            mount = np.array([[m.getCell(i, j) for j in range(4)] for i in range(4)])
            np.testing.assert_allclose(node_mat(body.hit_mounts[c.name], body.root), mount @ world[sk.INDEX[c.bone]],
                                       atol=1e-5, err_msg=c.name)

    def test_grip_reached(self):
        """Two-bone IK puts the right hand's grip point on the weapon's grip."""
        game, body = make_body()
        body.animate(1 / 64, (0, 0, 0), 30.0, 20.0, 0.0, Vec3(0, 0, 0), True)
        world = body.pose.solve()
        lo = sk.INDEX["lowerarm_r"]
        palm = np.array([0.0, sk.LOWER_ARM + sk.PALM, 0.0, 1.0]) @ world[lo]
        grip = body.root.getRelativePoint(body.gun, body._grips[0])
        self.assertLess(np.linalg.norm(palm[:3] - np.array(grip)), 0.01)


if __name__ == "__main__":
    unittest.main()
