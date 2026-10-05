"""Renderer facade: wires shaders, shadows, lights, sky and post together."""
from __future__ import annotations

from panda3d.core import (
    BitMask32,
    ColorWriteAttrib,
    CullFaceAttrib,
    LMatrix4f,
    LVecBase4f,
    PTA_LMatrix4f,
    RenderState,
    ShaderAttrib,
    Vec3,
)

from render import shader_loader, sky_visibility
from render.csm import CascadedShadows
from render.local_lights import LightManager
from render.post import PREPASS_CAMERA_MASK, PostPipeline, normalize_graphics
from render.sky import Sky

MAIN_CAMERA_MASK = BitMask32.bit(0)
SHADOW_CAMERA_MASK = BitMask32.bit(1)
VIEWMODEL_CAMERA_MASK = BitMask32.bit(2)
# translucent / non-solid things (sky, particles, decals, text) stay out of
# the shadow maps and the depth/normal pre-pass
NO_DEPTH_PASSES = SHADOW_CAMERA_MASK | PREPASS_CAMERA_MASK


# graphics options grouped by what has to be rebuilt when they change
CSM_KEYS = {"shadow_cascades", "shadow_resolution", "shadow_distance", "shadow_softness"}
LOCAL_LIGHT_KEYS = {"local_shadows", "local_shadow_atlas", "local_shadow_tile", "local_shadow_updates",
                    "max_lights"}
POST_KEYS = {"render_scale", "antialiasing", "ssao", "bloom", "soft_particles"}
POST_LIVE_KEYS = {"auto_exposure", "vignette", "color_grading", "chromatic_aberration", "film_grain",
                  "sharpening", "ssao_strength", "ssao_radius", "ssao_power"}


def defines_from_graphics(g: dict) -> dict:
    g = normalize_graphics(g)
    return {
        "CSM_CASCADES": int(g.get("shadow_cascades", 4)),
        "PCF_TAPS": int(g.get("shadow_pcf_taps", 12)),
        "LOCAL_PCF_TAPS": max(4, int(g.get("shadow_pcf_taps", 12)) // 2),
        "LOCAL_SHADOWS": bool(g.get("local_shadows", True)),
        "PARALLAX": bool(g.get("parallax", True)),
        "SPECULAR_AA": bool(g.get("specular_aa", True)),
        "FOG": bool(g.get("fog", True)),
        "SSAO": g["ssao"] != "off",
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

        self.env: dict = {}
        self.overlays: list[tuple] = []
        self.shader_users: list = []
        self.post = PostPipeline(base, graphics, self.defines, log=log)
        self.csm = CascadedShadows(base, graphics, self.caster_state, SHADOW_CAMERA_MASK)
        self.lights = LightManager(base, graphics, self.caster_state, SHADOW_CAMERA_MASK)
        self.sky: Sky | None = None

        self.scene_shader = shader_loader.load("pbr.vert", "pbr.frag", self.defines)
        root = base.render
        root.setShader(self.scene_shader)
        root.setShaderInput("u_camPos", Vec3(0, 0, 0))
        root.setShaderInput("u_emission", LVecBase4f(0, 0, 0, 0))
        # rigid skinning (render/shaders/skinning.glsl): off for everything but soldier bodies
        bones = PTA_LMatrix4f.emptyArray(24)
        for k in range(24):
            bones[k] = LMatrix4f.identMat()
        root.setShaderInput("u_bones", bones)
        root.setShaderInput("u_skinned", 0.0)
        root.setShaderInput("u_matParams", LVecBase4f(0, 1, 1, 1))
        self.csm.apply_inputs(root)
        self.lights.apply_inputs(root)
        self.set_sky_visibility(None)

    # --------------------------------------------------------- environment
    def setup_environment(self, env: dict) -> Sky:
        self.env = env
        self.sky = Sky(self.base, env, NO_DEPTH_PASSES, self.defines, log=self.log)
        self.sky.apply_inputs(self.base.render)
        self.register_shader_user(self.sky.set_defines)
        self.csm.set_sun(self.sky.sun_dir)
        self.post.apply_environment(env.get("post", {}) | {"exposure": float(env.get("exposure", 1.0))})
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

    # ------------------------------------------------------------- post fx
    def add_scene_overlay(self, cam_np, sort: int) -> None:
        """Extra camera drawn into the HDR scene buffer (the viewmodel)."""
        self.overlays.append((cam_np, sort))
        self.post.attach_overlay(cam_np, sort)

    def register_shader_user(self, callback) -> None:
        """callback(defines) is called when the quality defines change."""
        self.shader_users.append(callback)

    def rebuild_post(self, graphics: dict) -> None:
        """Re-create the post pipeline (AA mode, render scale, AO quality...)."""
        brightness = self.post.brightness
        debug = self.post.debug_view
        self.post.cleanup()
        self.graphics = graphics
        self.post = PostPipeline(self.base, graphics, self.defines, overlays=self.overlays, log=self.log)
        self.post.apply_environment(self.env.get("post", {}) | {"exposure": float(self.env.get("exposure", 1.0))})
        self.post.set_brightness(brightness)
        self.post.set_debug_view(debug)

    def set_defines(self, graphics: dict) -> bool:
        """Recompile the scene shaders if the quality defines changed."""
        defines = defines_from_graphics(graphics)
        if defines == self.defines:
            return False
        self.defines = defines
        self.scene_shader = shader_loader.load("pbr.vert", "pbr.frag", defines)
        self.base.render.setShader(self.scene_shader)
        for cb in self.shader_users:
            cb(defines)
        return True

    def apply_graphics(self, graphics: dict, materials=None) -> list[str]:
        """Apply changed graphics options live. Returns the options that
        only take effect after a restart."""
        old = normalize_graphics(self.graphics)
        new = normalize_graphics(graphics)
        changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
        restart = []
        if not changed:
            return restart
        self.graphics = new
        self.set_defines(new)
        root = self.base.render
        if changed & CSM_KEYS:
            sun = self.csm.sun_dir
            self.csm.destroy()
            self.csm = CascadedShadows(self.base, new, self.caster_state, SHADOW_CAMERA_MASK)
            self.csm.set_sun(sun)
            self.csm.apply_inputs(root)
        if changed & LOCAL_LIGHT_KEYS:
            lights = list(self.lights.lights)
            self.lights.destroy()
            self.lights = LightManager(self.base, new, self.caster_state, SHADOW_CAMERA_MASK)
            for light in lights:
                light.dirty = True
                self.lights.add(light)
            self.lights.finalize()
            self.lights.apply_inputs(root)
        if changed & POST_KEYS:
            self.rebuild_post(new)
        elif changed & POST_LIVE_KEYS:
            self.post.g = normalize_graphics(new)
            self.post.apply_environment(self.post.env)
        if "anisotropy" in changed and materials is not None:
            materials.set_anisotropy(int(new["anisotropy"]))
        if "texture_size" in changed:
            restart.append("texture_size")
        return restart

    def on_window_resized(self) -> None:
        self.post.on_window_resized()

    # --------------------------------------------------------------- frame
    def update(self, dt: float = 1 / 60) -> None:
        base = self.base
        cam_pos = base.camera.getPos(base.render)
        base.render.setShaderInput("u_camPos", cam_pos)
        if self.sky is not None:
            self.sky.update(cam_pos)
        self.csm.update(base.camera, base.camLens)
        self.lights.update(cam_pos, base.render)
        self.post.update(dt)
