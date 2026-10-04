"""Sky dome, sun and image based lighting inputs."""
from __future__ import annotations

import math

import numpy as np
from panda3d.core import (
    CPTA_uchar,
    PTA_uchar,
    BitMask32,
    LVecBase4f,
    NodePath,
    PTA_LVecBase3f,
    SamplerState,
    Texture,
    TransparencyAttrib,
    Vec3,
)

from engine.geometry import MeshBuilder
from render import ibl, shader_loader


def _float_texture_2d(name: str, rgb: np.ndarray) -> Texture:
    h, w = rgb.shape[:2]
    tex = Texture(name)
    tex.setup2dTexture(w, h, Texture.T_float, Texture.F_rgb16)
    tex.setRamImage(np.ascontiguousarray(rgb[..., ::-1], dtype=np.float32).tobytes())  # BGR
    return tex


def _cube_texture(name: str, mips: list) -> Texture:
    size = mips[0].shape[1]
    tex = Texture(name)
    tex.setupCubeMap(size, Texture.T_float, Texture.F_rgb16)
    for level, faces in enumerate(mips):
        data = np.ascontiguousarray(faces[..., ::-1], dtype=np.float32).tobytes()  # BGR, 6 pages
        page = faces.shape[1] * faces.shape[2] * 3 * 4
        if level == 0:
            tex.setRamImage(data, Texture.CM_off, page)
        else:
            pta = PTA_uchar.emptyArray(len(data))
            memoryview(pta)[:] = data
            tex.setRamMipmapImage(level, CPTA_uchar(pta), page)
    tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    tex.setMagfilter(SamplerState.FT_linear)
    return tex


class Sky:
    """Builds the environment (HDRI or procedural) and owns the sky dome."""

    def __init__(self, base, env: dict, shadow_mask: BitMask32, defines: dict, log=print):
        self.base = base
        self.env = env
        data = ibl.build_environment(env, log=log)
        self.source = data["source"]
        self.sun_dir = Vec3(*map(float, data["sun_dir"])).normalized()
        if env.get("sun_dir_override"):
            self.sun_dir = Vec3(*env["sun_dir_override"]).normalized()
        sun_rgb = env.get("sun_color", [1.0, 0.93, 0.82])
        self.sun_intensity = float(env.get("sun_intensity", 3.2))
        self.sun_color = Vec3(*sun_rgb) * self.sun_intensity

        self.sky_tex = _float_texture_2d("sky_equirect", data["equirect"])
        self.sky_tex.setMinfilter(SamplerState.FT_linear)
        self.sky_tex.setMagfilter(SamplerState.FT_linear)
        self.sky_tex.setWrapU(SamplerState.WM_repeat)
        self.sky_tex.setWrapV(SamplerState.WM_clamp)
        self.env_tex = _cube_texture("env_specular", data["mips"])
        self.max_lod = len(data["mips"]) - 1
        self.sh = PTA_LVecBase3f.emptyArray(9)
        for i, c in enumerate(data["sh"]):
            self.sh[i] = Vec3(*map(float, c))

        # fog colour = average horizon radiance from the SH (cheap & consistent)
        horizon = np.zeros(3)
        for a in range(8):
            ang = a / 8 * 2 * math.pi
            n = (math.cos(ang), math.sin(ang), 0.15)
            x, y, z = n
            basis = np.array([1, y, z, x, x * y, y * z, 3 * z * z - 1, x * z, x * x - y * y])
            horizon += basis @ data["sh"]
        self.fog_color = Vec3(*(horizon / 8.0 * float(env.get("fog_color_scale", 1.0))))

        # sky dome
        mb = MeshBuilder()
        mb.add_sphere((0, 0, 0), 1.0, rings=24, segments=48)
        node = mb.build("sky_dome")
        self.dome = base.render.attachNewNode(node)
        self.dome.setScale(500)
        self.dome.setBin("background", 0)
        self.dome.setDepthWrite(False)
        self.dome.setDepthTest(False)
        self.dome.setTwoSided(True)
        self.dome.setLightOff(1)
        self.dome.hide(shadow_mask)
        self.dome.setTransparency(TransparencyAttrib.MNone)
        self.set_defines(defines)
        self.dome.setShaderInput("u_skyTex", self.sky_tex)
        self.dome.setShaderInput("u_skyParams", LVecBase4f(
            float(env.get("sky_intensity", 1.0)), math.radians(float(env.get("sun_disc_deg", 0.6))),
            float(env.get("sun_disc_intensity", 120.0)), float(env.get("horizon_fog", 1.0))))

    def set_defines(self, defines: dict) -> None:
        self.dome.setShader(shader_loader.load("sky.vert", "sky.frag", defines), 10)

    def apply_inputs(self, np_: NodePath) -> None:
        e = self.env
        np_.setShaderInput("u_sunDir", self.sun_dir)
        np_.setShaderInput("u_sunColor", self.sun_color)
        np_.setShaderInput("u_sh", self.sh)
        np_.setShaderInput("u_envSpec", self.env_tex)
        np_.setShaderInput("u_iblParams", LVecBase4f(float(e.get("ibl_diffuse", 1.0)),
                                                     float(e.get("ibl_specular", 1.0)), float(self.max_lod), 0))
        np_.setShaderInput("u_ambientFloor", Vec3(*e.get("ambient_floor", [0.004, 0.004, 0.005])))
        fog = e.get("fog", {})
        np_.setShaderInput("u_fogParams", LVecBase4f(float(fog.get("density", 0.004)),
                                                     float(fog.get("height_falloff", 0.06)),
                                                     float(fog.get("base_height", 0.0)),
                                                     float(fog.get("max_opacity", 0.85))))
        np_.setShaderInput("u_fogColor", self.fog_color)
        np_.setShaderInput("u_fogSun", self.sun_color * float(fog.get("sun_scatter", 0.25)))

    def update(self, cam_pos) -> None:
        self.dome.setPos(cam_pos)
