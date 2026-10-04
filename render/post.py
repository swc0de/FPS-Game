"""Post-processing chain.

    scene (RGBA16F HDR + depth)  ->  tonemap (exposure, ACES, sRGB, dither)
                                 ->  FXAA  ->  window

Milestone 3 extends this with bloom, SSAO, vignette and colour grading;
the scene is already rendered to a floating point target so those passes
operate on real HDR values.
"""
from __future__ import annotations

from direct.filter.FilterManager import FilterManager
from panda3d.core import FrameBufferProperties, LVecBase2f, LVecBase4f, SamplerState, Texture

from render import shader_loader


class PostPipeline:
    def __init__(self, base, graphics: dict, defines: dict):
        self.base = base
        self.graphics = graphics
        self.manager = FilterManager(base.win, base.cam)
        fbprops = FrameBufferProperties()
        fbprops.setFloatColor(True)
        fbprops.setRgbaBits(16, 16, 16, 16)
        fbprops.setDepthBits(24)
        self.scene_tex = Texture("scene_hdr")
        self.depth_tex = Texture("scene_depth")
        self.final_quad = self.manager.renderSceneInto(colortex=self.scene_tex, depthtex=self.depth_tex,
                                                       fbprops=fbprops)
        if self.final_quad is None:
            raise RuntimeError("could not create the HDR scene buffer (float render targets unsupported?)")
        self.scene_tex.setMinfilter(SamplerState.FT_linear)
        self.scene_tex.setMagfilter(SamplerState.FT_linear)
        self.scene_tex.setWrapU(SamplerState.WM_clamp)
        self.scene_tex.setWrapV(SamplerState.WM_clamp)
        self.ldr_tex = Texture("ldr")
        self.tonemap_quad = self.manager.renderQuadInto("tonemap", colortex=self.ldr_tex)
        self.ldr_tex.setMinfilter(SamplerState.FT_linear)
        self.ldr_tex.setMagfilter(SamplerState.FT_linear)
        self.ldr_tex.setWrapU(SamplerState.WM_clamp)
        self.ldr_tex.setWrapV(SamplerState.WM_clamp)

        self.exposure = 1.0
        self.tonemap_quad.setShader(shader_loader.load("post_quad.vert", "tonemap.frag", defines))
        self.tonemap_quad.setShaderInput("u_scene", self.scene_tex)
        self.tonemap_quad.setShaderInput("u_tonemapParams", LVecBase4f(self.exposure, 1.0, 0, 0))
        self.final_quad.setShader(shader_loader.load("post_quad.vert", "fxaa.frag", defines))
        self.final_quad.setShaderInput("u_ldr", self.ldr_tex)
        self.final_quad.setShaderInput("u_enabled", 1.0 if graphics.get("fxaa", True) else 0.0)
        self._size = (0, 0)
        self.update()

    def set_exposure(self, exposure: float) -> None:
        self.exposure = exposure
        self.tonemap_quad.setShaderInput("u_tonemapParams", LVecBase4f(exposure, 1.0, 0, 0))

    def set_fxaa(self, enabled: bool) -> None:
        self.final_quad.setShaderInput("u_enabled", 1.0 if enabled else 0.0)

    def update(self) -> None:
        w, h = self.ldr_tex.getXSize(), self.ldr_tex.getYSize()
        if (w, h) != self._size and w > 0 and h > 0:
            self._size = (w, h)
            self.final_quad.setShaderInput("u_texel", LVecBase2f(1.0 / w, 1.0 / h))

    def cleanup(self) -> None:
        self.manager.cleanup()
