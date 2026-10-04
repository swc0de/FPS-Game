"""Post-processing pipeline (custom render targets, no FilterManager).

Frame graph (sort order = execution order, all on the window's GSG):

    shadows (CSM atlas, local light atlas)                      sort -100/-99
    pre-pass  : depth + world normals  (render resolution)      -90
    GTAO      : ambient occlusion, then H + V bilateral blur    -89..-87
    scene     : forward PBR, RGBA16F HDR, optional MSAA         -80
                (+ viewmodel overlay camera, depth cleared)
    luminance : 64x64 log-lum -> 16 -> 4 -> 1, eye adaptation    -79..-75
    bloom     : 6 downsamples (1/2 .. 1/64) + 5 tent upsamples  -74..-64
    tonemap   : exposure, bloom, grading, vignette, ACES, sRGB  -60
    FXAA      : (render resolution)                             -59
    final     : upscale to the window + CAS sharpening          window

Every pass is a full-screen quad rendered into an off-screen buffer whose
colour attachment is a texture. Buffers follow the window size (times the
render scale) and are rebuilt when graphics settings change.
"""
from __future__ import annotations

import math

from panda3d.core import (
    BitMask32,
    Camera,
    CardMaker,
    FrameBufferProperties,
    GraphicsOutput,
    GraphicsPipe,
    LVecBase2f,
    LVecBase4f,
    NodePath,
    OrthographicLens,
    RenderState,
    SamplerState,
    ShaderAttrib,
    Texture,
    Vec3,
    WindowProperties,
)

from render import shader_loader

PREPASS_CAMERA_MASK = BitMask32.bit(3)

SORT_PREPASS = -90
SORT_AO = -89
SORT_SCENE = -80
SORT_LUM = -79
SORT_BLOOM = -74
SORT_TONEMAP = -60
SORT_FXAA = -59

BLOOM_LEVELS = 6

# ambient occlusion quality: (resolution scale, slices, steps)
AO_QUALITY = {
    "low": (0.5, 2, 4),
    "medium": (0.5, 3, 6),
    "high": (1.0, 3, 6),
    "ultra": (1.0, 4, 8),
}

# anti-aliasing modes: (MSAA samples, FXAA)
AA_MODES = {
    "off": (0, False),
    "fxaa": (0, True),
    "msaa2": (2, False),
    "msaa4": (4, False),
    "msaa8": (8, False),
    "msaa2+fxaa": (2, True),
    "msaa4+fxaa": (4, True),
}

ENV_DEFAULTS = {
    "exposure": 1.0,
    "bloom": {"intensity": 0.45, "threshold": 1.3, "knee": 0.6, "scatter": 0.7},
    "grading": {"saturation": 1.0, "contrast": 1.0, "temperature": 0.0, "tint": 0.0,
                "lift": [0.0, 0.0, 0.0], "gamma": [1.0, 1.0, 1.0], "gain": [1.0, 1.0, 1.0]},
    "vignette": {"strength": 0.32, "start": 0.45},
    "auto_exposure": {"strength": 0.55, "min_ev": -1.0, "max_ev": 1.2, "reference": -1.6,
                      "speed_up": 3.0, "speed_down": 1.1},
}


def normalize_graphics(g: dict) -> dict:
    """Fill in post-processing keys (and convert legacy values)."""
    g = dict(g)
    ssao = g.get("ssao", "medium")
    if ssao is True:
        ssao = "medium"
    elif ssao is False or ssao is None:
        ssao = "off"
    g["ssao"] = ssao if ssao in AO_QUALITY or ssao == "off" else "medium"
    aa = g.get("antialiasing")
    if aa not in AA_MODES:
        aa = "fxaa" if g.get("fxaa", True) else "off"
    g["antialiasing"] = aa
    g.setdefault("bloom", True)
    g.setdefault("auto_exposure", True)
    g.setdefault("vignette", True)
    g.setdefault("color_grading", True)
    g.setdefault("chromatic_aberration", False)
    g.setdefault("film_grain", False)
    g.setdefault("soft_particles", True)
    g.setdefault("sharpening", 0.25)
    g["render_scale"] = min(max(float(g.get("render_scale", 1.0)), 0.5), 1.0)
    return g


def _merge_env(env: dict) -> dict:
    out = {}
    for key, default in ENV_DEFAULTS.items():
        if isinstance(default, dict):
            d = dict(default)
            d.update(env.get(key, {}) if isinstance(env.get(key), dict) else {})
            out[key] = d
        else:
            out[key] = env.get(key, default)
    return out


def _texture(name: str, linear: bool = True) -> Texture:
    tex = Texture(name)
    f = SamplerState.FT_linear if linear else SamplerState.FT_nearest
    tex.setMinfilter(f)
    tex.setMagfilter(f)
    tex.setWrapU(SamplerState.WM_clamp)
    tex.setWrapV(SamplerState.WM_clamp)
    return tex


class Target:
    """An off-screen buffer that renders into one colour texture (and
    optionally a depth texture)."""

    def __init__(self, post: "PostPipeline", name: str, size: tuple[int, int], sort: int, fmt: str = "rgba16f",
                 depth_bits: int = 0, depth_tex: bool = False, msaa: int = 0, linear: bool = True):
        self.post = post
        self.name = name
        self.size = size
        base = post.base
        fbp = FrameBufferProperties()
        if fmt == "rgba16f":
            fbp.setFloatColor(True)
            fbp.setRgbaBits(16, 16, 16, 16)
        else:
            fbp.setRgbaBits(8, 8, 8, 8)
        fbp.setDepthBits(depth_bits)
        if msaa:
            fbp.setMultisamples(msaa)
        winprops = WindowProperties.size(max(size[0], 1), max(size[1], 1))
        flags = GraphicsPipe.BF_refuse_window | GraphicsPipe.BF_resizeable
        self.buffer = base.graphicsEngine.makeOutput(base.pipe, name, sort, fbp, winprops, flags,
                                                     base.win.getGsg(), base.win)
        if self.buffer is None:
            raise RuntimeError(f"could not create render target {name} ({fmt}, msaa {msaa})")
        self.tex = _texture(name, linear)
        self.buffer.addRenderTexture(self.tex, GraphicsOutput.RTMBindOrCopy, GraphicsOutput.RTPColor)
        self.depth = None
        if depth_tex:
            self.depth = _texture(name + "_depth", linear=False)
            self.depth.setFormat(Texture.F_depth_component24)
            self.buffer.addRenderTexture(self.depth, GraphicsOutput.RTMBindOrCopy, GraphicsOutput.RTPDepth)
        self.buffer.disableClears()
        for i in range(self.buffer.getNumDisplayRegions()):
            self.buffer.getDisplayRegion(i).setActive(False)

    def resize(self, size: tuple[int, int]) -> None:
        if size != self.size:
            self.size = size
            self.buffer.setSize(max(size[0], 1), max(size[1], 1))

    def destroy(self) -> None:
        self.post.base.graphicsEngine.removeWindow(self.buffer)


class QuadPass(Target):
    """Target + full-screen quad + orthographic camera running one shader."""

    def __init__(self, post, name, size, sort, frag: str, defines: dict | None = None, fmt: str = "rgba16f",
                 linear: bool = True):
        super().__init__(post, name, size, sort, fmt=fmt, linear=linear)
        self.root = NodePath(name + "-root")
        cm = CardMaker(name + "-quad")
        cm.setFrameFullscreenQuad()
        self.quad = self.root.attachNewNode(cm.generate())
        self.quad.setDepthTest(False)
        self.quad.setDepthWrite(False)
        self.quad.setShader(shader_loader.load("post_quad.vert", frag, defines or {}))
        cam = Camera(name + "-cam")
        lens = OrthographicLens()
        lens.setFilmSize(2, 2)
        lens.setNearFar(-1000, 1000)
        cam.setLens(lens)
        self.cam = self.root.attachNewNode(cam)
        dr = self.buffer.makeDisplayRegion()
        dr.disableClears()
        dr.setCamera(self.cam)
        self.region = dr

    def set(self, name: str, value) -> None:
        self.quad.setShaderInput(name, value)


class PostPipeline:
    def __init__(self, base, graphics: dict, defines: dict, env: dict | None = None, overlays=(), log=print):
        self.base = base
        self.g = normalize_graphics(graphics)
        self.defines = defines
        self.log = log
        self.env = _merge_env(env or {})
        self.passes: list[Target] = []
        self.debug_view = 0
        self.brightness = 0.0
        self.time = 0.0
        self._adapt_reset = 3
        self._frame = 0

        win_w, win_h = self._win_size()
        self.scale = self.g["render_scale"]
        self.rsize = self._render_size(win_w, win_h)
        self.msaa, self.fxaa = AA_MODES[self.g["antialiasing"]]
        self.ssao_mode = self.g["ssao"]
        self.prepass_enabled = self.ssao_mode != "off" or self.g["soft_particles"]

        # ------------------------------------------------ pre-pass (depth + normals)
        self.prepass = None
        self.prepass_cam = None
        if self.prepass_enabled:
            pp = Target(self, "prepass", self.rsize, SORT_PREPASS, fmt="rgba8", depth_bits=24, depth_tex=True,
                        linear=False)
            pp.buffer.setClearColorActive(True)
            pp.buffer.setClearColor((0.5, 0.5, 1.0, 0.0))
            pp.buffer.setClearDepthActive(True)
            pp.buffer.setClearDepth(1.0)
            cam = Camera("prepass_cam", base.camLens)
            cam.setCameraMask(PREPASS_CAMERA_MASK)
            shader = shader_loader.load("prepass.vert", "prepass.frag")
            cam.setInitialState(RenderState.make(ShaderAttrib.make(shader, 1000)))
            self.prepass_cam = base.cam.attachNewNode(cam)
            dr = pp.buffer.makeDisplayRegion()
            dr.disableClears()
            dr.setCamera(self.prepass_cam)
            self.prepass = pp
            self.passes.append(pp)

        # ------------------------------------------------ GTAO + blur
        self.ao = None
        self.ao_blur = []
        if self.ssao_mode != "off":
            scale, slices, steps = AO_QUALITY[self.ssao_mode]
            ao_size = self._scaled(self.rsize, scale)
            self.ao_scale = scale
            ao = QuadPass(self, "gtao", ao_size, SORT_AO, "gtao.frag", {"AO_SLICES": slices, "AO_STEPS": steps})
            ao.set("u_depth", self.prepass.depth)
            ao.set("u_normal", self.prepass.tex)
            self.ao = ao
            blur_h = QuadPass(self, "gtao_blur_h", ao_size, SORT_AO + 1, "ao_blur.frag")
            blur_h.set("u_src", ao.tex)
            blur_v = QuadPass(self, "gtao_blur_v", ao_size, SORT_AO + 2, "ao_blur.frag")
            blur_v.set("u_src", blur_h.tex)
            for b in (blur_h, blur_v):
                b.set("u_sharpness", 24.0)
            self.ao_blur = [blur_h, blur_v]
            self.passes += [ao, blur_h, blur_v]

        # ------------------------------------------------ scene (HDR)
        try:
            scene = Target(self, "scene_hdr", self.rsize, SORT_SCENE, fmt="rgba16f", depth_bits=24, msaa=self.msaa)
        except RuntimeError:
            if not self.msaa:
                raise
            log(f"[post] MSAA {self.msaa}x is not supported here; using no MSAA")
            self.msaa = 0
            scene = Target(self, "scene_hdr", self.rsize, SORT_SCENE, fmt="rgba16f", depth_bits=24)
        scene.buffer.setClearColorActive(True)
        scene.buffer.setClearColor((0.0, 0.0, 0.0, 1.0))
        scene.buffer.setClearDepthActive(True)
        scene.buffer.setClearDepth(1.0)
        dr = scene.buffer.makeDisplayRegion()
        dr.disableClears()
        dr.setCamera(base.cam)
        self.scene = scene
        self.scene_region = dr
        self.passes.append(scene)
        self.overlay_regions = []
        for cam_np, sort in overlays:
            self.attach_overlay(cam_np, sort)

        # ------------------------------------------------ eye adaptation
        self.lum = QuadPass(self, "lum64", (64, 64), SORT_LUM, "luminance.frag")
        self.lum.set("u_scene", scene.tex)
        self.lum.set("u_cell", LVecBase2f(1 / 64, 1 / 64))
        self.reduce = []
        src = self.lum
        for i, s in enumerate((16, 4, 1)):
            r = QuadPass(self, f"lum{s}", (s, s), SORT_LUM + 1 + i, "reduce.frag", linear=False)
            r.set("u_src", src.tex)
            self.reduce.append(r)
            src = r
        self.adapt = []
        for i in range(2):
            a = QuadPass(self, f"adapt{i}", (1, 1), SORT_LUM + 4, "adapt.frag", linear=False)
            a.set("u_avg", self.reduce[-1].tex)
            self.adapt.append(a)
        self.adapt[0].set("u_prev", self.adapt[1].tex)
        self.adapt[1].set("u_prev", self.adapt[0].tex)
        self.adapt[1].buffer.setActive(False)
        self._adapt_index = 0
        self.passes += [self.lum] + self.reduce + self.adapt

        # ------------------------------------------------ bloom
        self.bloom_down = []
        self.bloom_up = []
        self.bloom_on = bool(self.g["bloom"])
        if self.bloom_on:
            src_tex = scene.tex
            src_size = self.rsize
            sort = SORT_BLOOM
            for i in range(BLOOM_LEVELS):
                size = (max(src_size[0] // 2, 1), max(src_size[1] // 2, 1))
                p = QuadPass(self, f"bloom_down{i}", size, sort, "bloom_down.frag")
                p.set("u_src", src_tex)
                p.set("u_srcTexel", LVecBase2f(1 / src_size[0], 1 / src_size[1]))
                self.bloom_down.append(p)
                src_tex, src_size = p.tex, size
                sort += 1
            low = self.bloom_down[-1]
            for i in range(BLOOM_LEVELS - 2, -1, -1):
                high = self.bloom_down[i]
                p = QuadPass(self, f"bloom_up{i}", high.size, sort, "bloom_up.frag")
                p.set("u_low", low.tex)
                p.set("u_high", high.tex)
                p.set("u_lowTexel", LVecBase2f(1 / low.size[0], 1 / low.size[1]))
                self.bloom_up.append(p)
                low = p
                sort += 1
            self.passes += self.bloom_down + self.bloom_up
            self.bloom_tex = self.bloom_up[-1].tex
        else:
            self.bloom_tex = self._black_texture()

        # ------------------------------------------------ tonemap / grading
        self.tonemap = QuadPass(self, "tonemap", self.rsize, SORT_TONEMAP, "tonemap.frag", fmt="rgba8")
        self.tonemap.set("u_scene", scene.tex)
        self.tonemap.set("u_bloom", self.bloom_tex)
        self.passes.append(self.tonemap)

        # ------------------------------------------------ FXAA (render resolution)
        self.fxaa_pass = QuadPass(self, "fxaa", self.rsize, SORT_FXAA, "fxaa.frag", fmt="rgba8")
        self.fxaa_pass.set("u_ldr", self.tonemap.tex)
        self.fxaa_pass.set("u_enabled", 1.0 if self.fxaa else 0.0)
        self.passes.append(self.fxaa_pass)

        # ------------------------------------------------ final quad in the window
        self.final_root = NodePath("final-root")
        cm = CardMaker("final-quad")
        cm.setFrameFullscreenQuad()
        self.final_quad = self.final_root.attachNewNode(cm.generate())
        self.final_quad.setDepthTest(False)
        self.final_quad.setDepthWrite(False)
        self.final_quad.setShader(shader_loader.load("post_quad.vert", "final.frag", {}))
        self.final_quad.setShaderInput("u_src", self.fxaa_pass.tex)
        black = self._black_texture()
        self.final_quad.setShaderInput("u_dbgAO", self.ao_blur[-1].tex if self.ao_blur else black)
        self.final_quad.setShaderInput("u_dbgBloom", self.bloom_tex)
        self.final_quad.setShaderInput("u_dbgNormal", self.prepass.tex if self.prepass else black)
        self.final_quad.setShaderInput("u_dbgDepth", self.prepass.depth if self.prepass else black)
        cam = Camera("final-cam")
        lens = OrthographicLens()
        lens.setFilmSize(2, 2)
        lens.setNearFar(-1000, 1000)
        cam.setLens(lens)
        self.final_cam = self.final_root.attachNewNode(cam)
        self.window_region = None
        for i in range(base.win.getNumDisplayRegions()):
            dr = base.win.getDisplayRegion(i)
            if dr.getCamera() == base.cam:
                self.window_region = dr
                break
        if self.window_region is None:
            raise RuntimeError("main camera display region not found")
        self.window_region.setCamera(self.final_cam)

        self.apply_environment(env or {})
        self.apply_scene_inputs()
        self._update_size_inputs()
        self.update(0.0)    # per-frame inputs must exist before the first render
        self.log(f"[post] render {self.rsize[0]}x{self.rsize[1]} (scale {self.scale:.2f}), "
                 f"AA {self.g['antialiasing']}, AO {self.ssao_mode}, bloom {'on' if self.bloom_on else 'off'}, "
                 f"{len(self.passes)} targets")

    # ---------------------------------------------------------------- sizes
    def _win_size(self) -> tuple[int, int]:
        w = self.base.win
        return max(w.getXSize(), 1), max(w.getYSize(), 1)

    def _render_size(self, w: int, h: int) -> tuple[int, int]:
        return max(int(round(w * self.scale)), 1), max(int(round(h * self.scale)), 1)

    @staticmethod
    def _scaled(size, s: float) -> tuple[int, int]:
        return max(int(round(size[0] * s)), 1), max(int(round(size[1] * s)), 1)

    def _black_texture(self) -> Texture:
        tex = Texture("black")
        tex.setup2dTexture(1, 1, Texture.T_unsigned_byte, Texture.F_rgba8)
        tex.setRamImage(bytes([0, 0, 0, 255]))
        return tex

    def on_window_resized(self) -> None:
        w, h = self._win_size()
        rsize = self._render_size(w, h)
        if rsize == self.rsize:
            return
        self.rsize = rsize
        for t in (self.prepass, self.scene, self.tonemap, self.fxaa_pass):
            if t is not None:
                t.resize(rsize)
        if self.ao is not None:
            s = self._scaled(rsize, self.ao_scale)
            for t in [self.ao] + self.ao_blur:
                t.resize(s)
        src_size = rsize
        for p in self.bloom_down:
            size = (max(src_size[0] // 2, 1), max(src_size[1] // 2, 1))
            p.resize(size)
            p.set("u_srcTexel", LVecBase2f(1 / src_size[0], 1 / src_size[1]))
            src_size = size
        for i, p in enumerate(self.bloom_up):
            level = BLOOM_LEVELS - 2 - i
            p.resize(self.bloom_down[level].size)
            low = self.bloom_down[level + 1] if i == 0 else self.bloom_up[i - 1]
            p.set("u_lowTexel", LVecBase2f(1 / low.size[0], 1 / low.size[1]))
        self._update_size_inputs()

    def _update_size_inputs(self) -> None:
        w, h = self.rsize
        self.final_quad.setShaderInput("u_srcSize", LVecBase4f(w, h, 1 / w, 1 / h))
        self.fxaa_pass.set("u_texel", LVecBase2f(1 / w, 1 / h))
        if self.ao is not None:
            aw, ah = self.ao.size
            self.ao.set("u_aoTarget", LVecBase4f(w, h, 1 / w, 1 / h))
            self.ao_blur[0].set("u_dir", LVecBase2f(1 / aw, 0))
            self.ao_blur[1].set("u_dir", LVecBase2f(0, 1 / ah))
        root = self.base.render
        strength = float(self.g.get("ssao_strength", 1.0)) if self.ao is not None else 0.0
        root.setShaderInput("u_ssaoParams", LVecBase4f(1 / w, 1 / h, strength, 0.35))
        self.tonemap.set("u_misc", LVecBase4f(1.0, self.time, w / h, 0))

    # ------------------------------------------------------------ overlays
    def attach_overlay(self, cam_np, sort: int) -> None:
        """Another camera drawn into the HDR scene buffer after the world
        (the first-person viewmodel), with its own depth clear."""
        dr = self.scene.buffer.makeDisplayRegion()
        dr.setSort(sort)
        dr.setClearDepthActive(True)
        dr.setClearDepth(1.0)
        dr.setCamera(cam_np)
        self.overlay_regions.append(dr)

    # -------------------------------------------------------------- inputs
    def apply_scene_inputs(self) -> None:
        """Textures the forward pass and particles read (set on render)."""
        root = self.base.render
        lens = self.base.camLens
        near, far = lens.getNear(), lens.getFar()
        root.setShaderInput("u_depthRange", LVecBase2f(near, far))
        if self.ao is not None:
            root.setShaderInput("u_ssao", self.ao_blur[-1].tex)
        else:
            root.setShaderInput("u_ssao", self._black_texture())
        soft = self.prepass is not None and self.g["soft_particles"]
        root.setShaderInput("u_sceneDepth", self.prepass.depth if self.prepass else self._black_texture())
        root.setShaderInput("u_softParams", LVecBase4f(1 / 0.6, near, far, 1.0 if soft else 0.0))

    def apply_environment(self, env: dict) -> None:
        e = self.env = _merge_env(env)
        g = self.g
        ae = e["auto_exposure"]
        strength = float(ae["strength"]) if g["auto_exposure"] else 0.0
        exposure = LVecBase4f(float(e["exposure"]), strength, float(ae["min_ev"]), float(ae["max_ev"]))
        ae_params = LVecBase4f(float(ae["reference"]), self.brightness, 0, 0)
        b = e["bloom"]
        for p in [self.tonemap] + self.bloom_down:
            p.set("u_exposureParams", exposure)
            p.set("u_aeParams", ae_params)
        for i, p in enumerate(self.bloom_down):
            p.set("u_bloomParams", LVecBase4f(1.0 if i == 0 else 0.0, float(b["threshold"]), float(b["knee"]), 64.0))
        for p in self.bloom_up:
            p.set("u_scatter", float(b["scatter"]))
        self.tonemap.set("u_bloomMix", LVecBase4f(float(b["intensity"]) if self.bloom_on else 0.0, 0, 0, 0))
        gr = e["grading"] if g["color_grading"] else ENV_DEFAULTS["grading"]
        self.tonemap.set("u_grade", LVecBase4f(float(gr["saturation"]), float(gr["contrast"]),
                                               float(gr["temperature"]), float(gr["tint"])))
        self.tonemap.set("u_lift", LVecBase4f(*map(float, gr["lift"]), 0))
        self.tonemap.set("u_gamma", LVecBase4f(*map(float, gr["gamma"]), 1))
        self.tonemap.set("u_gain", LVecBase4f(*map(float, gr["gain"]), 1))
        v = e["vignette"]
        self.tonemap.set("u_lensFx", LVecBase4f(float(v["strength"]) if g["vignette"] else 0.0, float(v["start"]),
                                                1.0 if g["chromatic_aberration"] else 0.0,
                                                0.035 if g["film_grain"] else 0.0))
        self.final_quad.setShaderInput("u_finalParams", LVecBase4f(float(g["sharpening"]), self.debug_view, 0, 0))
        if self.ao is not None:
            self.ao.set("u_aoParams", LVecBase4f(float(g.get("ssao_radius", 0.9)), 0.4,
                                                 float(g.get("ssao_power", 1.4)), 140.0 * self.ao_scale * 2))
            self.ao.set("u_aoFade", LVecBase2f(55.0, 1 / 25.0))

    def set_brightness(self, ev: float) -> None:
        self.brightness = ev
        self.apply_environment(self.env)

    def set_exposure(self, exposure: float) -> None:
        self.env["exposure"] = exposure
        self.apply_environment(self.env)

    def set_debug_view(self, mode: int) -> None:
        self.debug_view = mode
        g = self.g
        self.final_quad.setShaderInput("u_finalParams", LVecBase4f(float(g["sharpening"]), mode, 0, 0))

    def reset_adaptation(self) -> None:
        self._adapt_reset = 3

    # --------------------------------------------------------------- frame
    def update(self, dt: float) -> None:
        base = self.base
        self.time += dt
        self._frame += 1
        lens = base.camLens
        hfov, vfov = lens.getFov()
        proj = LVecBase4f(math.tan(math.radians(hfov) * 0.5), math.tan(math.radians(vfov) * 0.5),
                          lens.getNear(), lens.getFar())
        if self.ao is not None:
            q = base.cam.getQuat(base.render)
            self.ao.set("u_proj", proj)
            self.ao.set("u_viewRight", Vec3(q.getRight()))
            self.ao.set("u_viewUp", Vec3(q.getUp()))
            self.ao.set("u_viewBack", -Vec3(q.getForward()))
        self.final_quad.setShaderInput("u_proj", proj)

        # eye adaptation: ping-pong between the two 1x1 targets
        cur = self._adapt_index
        nxt = 1 - cur
        self.adapt[cur].buffer.setActive(False)
        self.adapt[nxt].buffer.setActive(True)
        self._adapt_index = nxt
        ae = self.env["auto_exposure"]
        up = 1.0 - math.exp(-max(dt, 0.0) * float(ae["speed_up"]))
        down = 1.0 - math.exp(-max(dt, 0.0) * float(ae["speed_down"]))
        reset = 1.0 if self._adapt_reset > 0 else 0.0
        self._adapt_reset = max(self._adapt_reset - 1, 0)
        self.adapt[nxt].set("u_adaptRates", LVecBase4f(up, down, reset, 0))
        adapted = self.adapt[nxt].tex
        self.tonemap.set("u_adapt", adapted)
        for p in self.bloom_down:
            p.set("u_adapt", adapted)
        w, h = self.rsize
        self.tonemap.set("u_misc", LVecBase4f(1.0, self.time, w / h, 0))

    def adapted_luminance(self) -> float | None:
        """Read back the adapted log2 luminance (debug overlay only: stalls the GPU)."""
        tex = self.adapt[self._adapt_index].tex
        try:
            if not self.base.graphicsEngine.extractTextureData(tex, self.base.win.getGsg()):
                return None
            img = tex.getRamImageAs("R")
            import numpy as np
            if tex.getComponentType() == Texture.T_float:
                return float(np.frombuffer(bytes(img), np.float32)[0])
            if tex.getComponentType() == Texture.T_half_float:
                return float(np.frombuffer(bytes(img), np.float16)[0])
            return None
        except Exception:
            return None

    def exposure_ev(self, adapted_log: float) -> float:
        ae = self.env["auto_exposure"]
        strength = float(ae["strength"]) if self.g["auto_exposure"] else 0.0
        ev = (float(ae["reference"]) - adapted_log) * strength
        return min(max(ev, float(ae["min_ev"])), float(ae["max_ev"]))

    # ------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        if self.window_region is not None:
            self.window_region.setCamera(self.base.cam)
        for p in self.passes:
            p.destroy()
        self.passes = []
        if self.prepass_cam is not None:
            self.prepass_cam.removeNode()
            self.prepass_cam = None
