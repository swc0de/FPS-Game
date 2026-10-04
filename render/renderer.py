"""Renderer facade: wires shaders, shadows, lights, sky and post together."""
from __future__ import annotations

from panda3d.core import (
    BitMask32,
    ColorWriteAttrib,
    CullFaceAttrib,
    LVecBase4f,
    RenderState,
    ShaderAttrib,
    Vec3,
)

from render import shader_loader, sky_visibility
from render.csm import CascadedShadows
from render.local_lights import LightManager
from render.post import PostPipeline
from render.sky import Sky

MAIN_CAMERA_MASK = BitMask32.bit(0)
SHADOW_CAMERA_MASK = BitMask32.bit(1)
VIEWMODEL_CAMERA_MASK = BitMask32.bit(2)


def defines_from_graphics(g: dict) -> dict:
    return {
        "CSM_CASCADES": int(g.get("shadow_cascades", 4)),
        "PCF_TAPS": int(g.get("shadow_pcf_taps", 12)),
        "LOCAL_PCF_TAPS": max(4, int(g.get("shadow_pcf_taps", 12)) // 2),
        "LOCAL_SHADOWS": bool(g.get("local_shadows", True)),
        "PARALLAX": bool(g.get("parallax", True)),
        "SPECULAR_AA": bool(g.get("specular_aa", True)),
        "FOG": bool(g.get("fog", True)),
    }


class Renderer:
    def __init__(self, base, graphics: dict, log=print):
        self.base = base
        self.graphics = graphics
        self.log = log
        self.defines = defines_from_graphics(graphics)
        base.cam.node().setCameraMask(MAIN_CAMERA_MASK)

        # depth-only state for every shadow camera: no colour, cull front faces
        # (storing back-face depth removes most self-shadowing acne on closed meshes)
        depth_shader = shader_loader.load("depth.vert", "depth.frag")
        self.caster_state = RenderState.make(
            ColorWriteAttrib.make(ColorWriteAttrib.COff),
            CullFaceAttrib.makeReverse(),
            ShaderAttrib.make(depth_shader, 1000),
        )

        self.post = PostPipeline(base, graphics, self.defines)
        self.csm = CascadedShadows(base, graphics, self.caster_state, SHADOW_CAMERA_MASK)
        self.lights = LightManager(base, graphics, self.caster_state, SHADOW_CAMERA_MASK)
        self.sky: Sky | None = None

        self.scene_shader = shader_loader.load("pbr.vert", "pbr.frag", self.defines)
        root = base.render
        root.setShader(self.scene_shader)
        root.setShaderInput("u_camPos", Vec3(0, 0, 0))
        root.setShaderInput("u_emission", LVecBase4f(0, 0, 0, 0))
        root.setShaderInput("u_matParams", LVecBase4f(0, 1, 1, 1))
        self.csm.apply_inputs(root)
        self.lights.apply_inputs(root)
        self.set_sky_visibility(None)

    # --------------------------------------------------------- environment
    def setup_environment(self, env: dict) -> Sky:
        self.sky = Sky(self.base, env, SHADOW_CAMERA_MASK, self.defines, log=self.log)
        self.sky.apply_inputs(self.base.render)
        self.csm.set_sun(self.sky.sun_dir)
        self.post.set_exposure(float(env.get("exposure", 1.0)))
        self.log(f"[render] environment: {self.sky.source}, sun dir {tuple(round(v, 2) for v in self.sky.sun_dir)}")
        return self.sky

    def set_sky_visibility(self, volume: dict | None) -> None:
        root = self.base.render
        if volume is None:
            root.setShaderInput("u_skyVis", sky_visibility.neutral_texture())
            root.setShaderInput("u_skyVisMin", Vec3(-1e4, -1e4, -1e4))
            root.setShaderInput("u_skyVisInvSize", Vec3(1e-5, 1e-5, 1e-5))
            return
        tex, vmin, inv = sky_visibility.make_texture(volume)
        root.setShaderInput("u_skyVis", tex)
        root.setShaderInput("u_skyVisMin", Vec3(*map(float, vmin)))
        root.setShaderInput("u_skyVisInvSize", Vec3(*map(float, inv)))

    # --------------------------------------------------------------- frame
    def update(self) -> None:
        base = self.base
        cam_pos = base.camera.getPos(base.render)
        base.render.setShaderInput("u_camPos", cam_pos)
        if self.sky is not None:
            self.sky.update(cam_pos)
        self.csm.update(base.camera, base.camLens)
        self.lights.update(cam_pos, base.render)
        self.post.update()
