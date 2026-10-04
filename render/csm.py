"""Cascaded shadow maps for the sun (see shaders/shadows.glsl for the theory).

All cascades render into one depth atlas (2x2 tiles for 3-4 cascades), each
from its own orthographic camera/display region. Per frame we:

  * split the view frustum (practical split scheme: blend of logarithmic and
    uniform distribution),
  * enclose each slice in a bounding sphere -> rotation-invariant cascade size,
  * snap the cascade centre to whole texels in light space -> no shimmering,
  * pull the shadow camera back toward the sun so off-screen casters (tall
    buildings) are still captured,
  * upload world->atlas matrices for the PBR shader.
"""
from __future__ import annotations

import math

from panda3d.core import (
    BitMask32,
    Camera,
    FrameBufferProperties,
    GraphicsOutput,
    GraphicsPipe,
    LMatrix4f,
    LVecBase4f,
    Mat4,
    NodePath,
    OrthographicLens,
    PTA_LMatrix4f,
    PTA_LVecBase4f,
    Point3,
    Quat,
    SamplerState,
    Texture,
    Vec3,
    WindowProperties,
    lookAt,
)


def make_depth_buffer(base, name: str, width: int, height: int, sort: int = -100):
    """Off-screen depth-only buffer whose depth attachment is a shadow texture."""
    fbp = FrameBufferProperties()
    fbp.setDepthBits(24)
    fbp.setRgbColor(False)
    fbp.setColorBits(0)
    fbp.setAlphaBits(0)
    winprops = WindowProperties.size(width, height)
    flags = GraphicsPipe.BF_refuse_window
    buf = base.graphicsEngine.makeOutput(base.pipe, name, sort, fbp, winprops, flags,
                                         base.win.getGsg(), base.win)
    if buf is None:
        raise RuntimeError(f"could not create shadow buffer {name} ({width}x{height})")
    tex = Texture(name)
    tex.setFormat(Texture.F_depth_component24)
    buf.addRenderTexture(tex, GraphicsOutput.RTMBindOrCopy, GraphicsOutput.RTPDepth)
    tex.setMinfilter(SamplerState.FT_shadow)
    tex.setMagfilter(SamplerState.FT_shadow)
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    buf.setClearColorActive(False)
    buf.setClearDepthActive(False)   # each display region clears its own tile
    buf.setClearStencilActive(False)
    # the default display region of a buffer covers everything; disable it
    for i in range(buf.getNumDisplayRegions()):
        buf.getDisplayRegion(i).setActive(False)
    return buf, tex


def tile_bias(u0: float, v0: float, du: float, dv: float) -> Mat4:
    """Clip space [-1,1] -> atlas tile UV (and depth -> [0,1])."""
    return Mat4.scaleMat(0.5 * du, 0.5 * dv, 0.5) * Mat4.translateMat(u0 + 0.5 * du, v0 + 0.5 * dv, 0.5)


class CascadedShadows:
    def __init__(self, base, graphics: dict, caster_state, shadow_mask: BitMask32):
        self.base = base
        self.count = int(graphics.get("shadow_cascades", 4))
        self.res = int(graphics.get("shadow_resolution", 2048))
        self.distance = float(graphics.get("shadow_distance", 90.0))
        self.softness = float(graphics.get("shadow_softness", 1.0))
        self.split_lambda = 0.78
        self.back_off = 120.0       # metres the camera is pulled toward the sun
        self.cols = 1 if self.count == 1 else 2
        self.rows = int(math.ceil(self.count / self.cols))
        self.atlas_w = self.cols * self.res
        self.atlas_h = self.rows * self.res
        self.buffer, self.texture = make_depth_buffer(base, "csm_atlas", self.atlas_w, self.atlas_h)
        self.cams: list[NodePath] = []
        self.lenses: list[OrthographicLens] = []
        self.rects = []
        self.biases = []
        root = base.render
        for i in range(self.count):
            cx, cy = i % self.cols, i // self.cols
            du, dv = 1.0 / self.cols, 1.0 / self.rows
            u0, v0 = cx * du, cy * dv
            dr = self.buffer.makeDisplayRegion(u0, u0 + du, v0, v0 + dv)
            dr.setClearDepthActive(True)
            dr.setClearDepth(1.0)
            dr.setSort(i)
            lens = OrthographicLens()
            lens.setFilmSize(10, 10)
            lens.setNearFar(0.5, 100)
            cam = Camera(f"csm_cam{i}", lens)
            cam.setCameraMask(shadow_mask)
            cam.setInitialState(caster_state)
            cam_np = root.attachNewNode(cam)
            dr.setCamera(cam_np)
            self.cams.append(cam_np)
            self.lenses.append(lens)
            self.rects.append(LVecBase4f(u0, v0, u0 + du, v0 + dv))
            self.biases.append(tile_bias(u0, v0, du, dv))
        self.mats = PTA_LMatrix4f.emptyArray(self.count)
        self.rect_arr = PTA_LVecBase4f.emptyArray(self.count)
        self.param_arr = PTA_LVecBase4f.emptyArray(self.count)
        for i, r in enumerate(self.rects):
            self.rect_arr[i] = r
        self.sun_dir = Vec3(0.4, -0.5, 0.75).normalized()
        self._light_quat = Quat()
        self._compute_light_rotation()

    def destroy(self) -> None:
        for cam in self.cams:
            cam.removeNode()
        self.cams = []
        self.base.graphicsEngine.removeWindow(self.buffer)

    # ------------------------------------------------------------------
    def set_sun(self, direction: Vec3) -> None:
        self.sun_dir = Vec3(direction).normalized()
        self._compute_light_rotation()

    def _compute_light_rotation(self) -> None:
        fwd = -self.sun_dir
        up = Vec3(0, 0, 1) if abs(fwd.z) < 0.99 else Vec3(0, 1, 0)
        q = Quat()
        lookAt(q, fwd, up)
        self._light_quat = q
        self._light_quat_inv = Quat(q)
        self._light_quat_inv.invertInPlace()

    def splits(self, near: float) -> list[float]:
        n, f = max(near, 0.1), self.distance
        out = []
        for i in range(1, self.count + 1):
            p = i / self.count
            log_s = n * (f / n) ** p
            uni = n + (f - n) * p
            out.append(self.split_lambda * log_s + (1 - self.split_lambda) * uni)
        return out

    def apply_inputs(self, np_: NodePath) -> None:
        np_.setShaderInput("u_csmAtlas", self.texture)
        np_.setShaderInput("u_csmMat", self.mats)
        np_.setShaderInput("u_csmRect", self.rect_arr)
        np_.setShaderInput("u_csmParams", self.param_arr)
        np_.setShaderInput("u_csmInfo", LVecBase4f(1.0 / self.atlas_w, 1.5 * self.softness,
                                                  self.distance, self.distance * 0.12))

    def update(self, cam_np: NodePath, lens) -> None:
        render = self.base.render
        cam_pos = cam_np.getPos(render)
        quat = cam_np.getQuat(render)
        fwd = quat.getForward()
        fov = lens.getFov()
        tx = math.tan(math.radians(fov[0]) * 0.5)
        ty = math.tan(math.radians(fov[1]) * 0.5)
        k2 = tx * tx + ty * ty
        near = 0.1
        prev = near
        for i, far in enumerate(self.splits(lens.getNear())):
            n = prev
            # bounding sphere of the slice (centre along the view axis)
            z = 0.5 * (far + n) * (1 + k2)
            if z >= far:
                z = far
                radius = far * math.sqrt(k2)
            else:
                radius = math.sqrt((far - z) ** 2 + far * far * k2)
            radius = math.ceil(radius * 16.0) / 16.0  # quantise -> stable size
            center = cam_pos + fwd * z
            texel = 2.0 * radius / self.res
            # snap in light space
            ls = self._light_quat_inv.xform(Vec3(center))
            ls.x = math.floor(ls.x / texel) * texel
            ls.z = math.floor(ls.z / texel) * texel
            snapped = Point3(self._light_quat.xform(ls))
            cam = self.cams[i]
            cam.setQuat(render, self._light_quat)
            cam.setPos(render, snapped + self.sun_dir * (radius + self.back_off))
            lensi = self.lenses[i]
            lensi.setFilmSize(2 * radius, 2 * radius)
            depth_range = 2 * radius + self.back_off
            lensi.setNearFar(0.0, depth_range)
            view = Mat4(cam.getMat(render))
            view.invertInPlace()
            m = view * lensi.getProjectionMat() * self.biases[i]
            self.mats[i] = LMatrix4f(m)
            self.param_arr[i] = LVecBase4f(texel, 1.0 / depth_range, far, 0)
            prev = far
