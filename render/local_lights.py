"""Local (point/spot) lights with an atlas of shadow maps.

* Up to ``max_lights`` lights closest to the camera are packed into a small
  float texture each frame (see shaders/lights.glsl for the layout).
* Shadow-casting lights get tiles in a shared depth atlas: a spot light uses
  one perspective tile, a point light six (one per cube face).
* Shadow tiles are re-rendered on a per-frame budget (closest lights first,
  then round-robin), so static lights cost almost nothing after the first
  frames while characters walking through a light still update it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from panda3d.core import (
    BitMask32,
    Camera,
    LVecBase4f,
    Mat4,
    NodePath,
    PerspectiveLens,
    Point3,
    SamplerState,
    Texture,
    Vec3,
)

from render.csm import make_depth_buffer, tile_bias

CUBE_DIRS = (
    (Vec3(1, 0, 0), Vec3(0, 0, 1)),
    (Vec3(-1, 0, 0), Vec3(0, 0, 1)),
    (Vec3(0, 1, 0), Vec3(0, 0, 1)),
    (Vec3(0, -1, 0), Vec3(0, 0, 1)),
    (Vec3(0, 0, 1), Vec3(0, 1, 0)),
    (Vec3(0, 0, -1), Vec3(0, 1, 0)),
)


def kelvin_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """Approximate blackbody colour (Tanner Helland fit), linear-ish 0..1."""
    t = kelvin / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    rgb = [min(max(c, 0.0), 255.0) / 255.0 for c in (r, g, b)]
    return tuple(c ** 2.2 for c in rgb)  # sRGB-ish -> linear


@dataclass
class LocalLight:
    pos: Point3
    color: Vec3                 # linear colour * intensity
    range: float = 10.0
    kind: str = "point"         # "point" | "spot"
    direction: Vec3 = field(default_factory=lambda: Vec3(0, 0, -1))
    inner_deg: float = 30.0
    outer_deg: float = 45.0
    shadows: bool = True
    enabled: bool = True
    shadow_tile: int = -1       # first atlas tile (assigned by the manager)
    shadow_near: float = 0.05
    dirty: bool = True          # shadow map needs re-render

    def tiles(self) -> int:
        return 6 if self.kind == "point" else 1


class LightManager:
    def __init__(self, base, graphics: dict, caster_state, shadow_mask: BitMask32):
        self.base = base
        self.max_lights = int(graphics.get("max_lights", 16))
        self.lights: list[LocalLight] = []
        self.data = np.zeros((self.max_lights, 4, 4), np.float32)
        self.data_tex = Texture("light_data")
        self.data_tex.setup2dTexture(4, self.max_lights, Texture.T_float, Texture.F_rgba32)
        self._nearest(self.data_tex)
        self.data_tex.setRamImage(bytes(self.data.nbytes))
        self.num_visible = 0

        self.shadows_enabled = bool(graphics.get("local_shadows", True))
        self.atlas_size = int(graphics.get("local_shadow_atlas", 4096))
        self.tile_size = int(graphics.get("local_shadow_tile", 512))
        self.updates_per_frame = int(graphics.get("local_shadow_updates", 4))
        self.tiles_per_row = max(self.atlas_size // self.tile_size, 1)
        self.max_tiles = self.tiles_per_row * self.tiles_per_row
        self.caster_state = caster_state
        self.shadow_mask = shadow_mask
        self.tile_cams: list[NodePath] = []
        self.tile_regions = []
        self.tile_owner: list[tuple[LocalLight, int]] = []
        self.mat_data = np.zeros((self.max_tiles, 5, 4), np.float32)
        self.mat_tex = Texture("shadow_mats")
        self.mat_tex.setup2dTexture(5, self.max_tiles, Texture.T_float, Texture.F_rgba32)
        self._nearest(self.mat_tex)
        self.mat_tex.setRamImage(bytes(self.mat_data.nbytes))
        self.buffer = None
        self.atlas = None
        self._active_regions = []
        self._rr = 0
        if self.shadows_enabled:
            self.buffer, self.atlas = make_depth_buffer(base, "local_shadow_atlas", self.atlas_size,
                                                        self.atlas_size, sort=-19)
        else:
            self.atlas = Texture("no_local_shadows")
            self.atlas.setup2dTexture(1, 1, Texture.T_float, Texture.F_depth_component)
            self.atlas.setRamImage(np.ones(1, np.float32).tobytes())
            self.atlas.setMinfilter(SamplerState.FT_shadow)
            self.atlas.setMagfilter(SamplerState.FT_shadow)

    @staticmethod
    def _nearest(tex: Texture) -> None:
        tex.setMinfilter(SamplerState.FT_nearest)
        tex.setMagfilter(SamplerState.FT_nearest)
        tex.setWrapU(SamplerState.WM_clamp)
        tex.setWrapV(SamplerState.WM_clamp)

    @staticmethod
    def _upload(tex: Texture, arr: np.ndarray) -> None:
        # Panda stores RGBA RAM images as BGRA
        bgra = arr[..., [2, 1, 0, 3]]
        tex.setRamImage(np.ascontiguousarray(bgra, dtype=np.float32).tobytes())

    # -------------------------------------------------------------- setup
    def add(self, light: LocalLight) -> LocalLight:
        self.lights.append(light)
        return light

    def finalize(self) -> None:
        """Assign shadow tiles and create shadow cameras."""
        if not self.shadows_enabled:
            for l in self.lights:
                l.shadow_tile = -1
            return
        next_tile = 0
        # spots first (cheaper), then points, in declaration order
        order = sorted((l for l in self.lights if l.shadows), key=lambda l: l.kind != "spot")
        for light in order:
            n = light.tiles()
            if next_tile + n > self.max_tiles:
                light.shadow_tile = -1
                continue
            light.shadow_tile = next_tile
            for face in range(n):
                self._make_tile(next_tile + face, light, face)
            next_tile += n
        self._upload(self.mat_tex, self.mat_data)

    def _make_tile(self, tile: int, light: LocalLight, face: int) -> None:
        tx, ty = tile % self.tiles_per_row, tile // self.tiles_per_row
        du = 1.0 / self.tiles_per_row
        u0, v0 = tx * du, ty * du
        dr = self.buffer.makeDisplayRegion(u0, u0 + du, v0, v0 + du)
        dr.setClearDepthActive(True)
        dr.setClearDepth(1.0)
        dr.setActive(False)
        lens = PerspectiveLens()
        if light.kind == "point":
            # slightly wider than 90 deg so PCF taps near face edges stay valid
            guard = 1.0 + 6.0 / self.tile_size
            fov = math.degrees(2 * math.atan(guard))
            lens.setFov(fov, fov)
            fwd, up = CUBE_DIRS[face]
        else:
            fov = min(light.outer_deg * 2 + 6, 170)
            lens.setFov(fov, fov)
            fwd = Vec3(light.direction).normalized()
            up = Vec3(0, 0, 1) if abs(fwd.z) < 0.95 else Vec3(0, 1, 0)
        lens.setNearFar(light.shadow_near, light.range)
        cam = Camera(f"lshadow{tile}", lens)
        cam.setCameraMask(self.shadow_mask)
        cam.setInitialState(self.caster_state)
        cam_np = self.base.render.attachNewNode(cam)
        cam_np.setPos(light.pos)
        cam_np.lookAt(light.pos + fwd, up)
        dr.setCamera(cam_np)
        view = Mat4(cam_np.getMat(self.base.render))
        view.invertInPlace()
        m = view * lens.getProjectionMat() * tile_bias(u0, v0, du, du)
        for c in range(4):
            col = m.getCol(c)
            self.mat_data[tile, c] = (col[0], col[1], col[2], col[3])
        self.mat_data[tile, 4] = (u0, v0, u0 + du, v0 + du)
        self.tile_cams.append(cam_np)
        self.tile_regions.append(dr)
        self.tile_owner.append((light, tile))

    def apply_inputs(self, np_: NodePath) -> None:
        np_.setShaderInput("u_lightData", self.data_tex)
        np_.setShaderInput("u_numLights", 0)
        np_.setShaderInput("u_localShadowAtlas", self.atlas)
        np_.setShaderInput("u_localShadowMats", self.mat_tex)
        np_.setShaderInput("u_localShadowInfo", LVecBase4f(1.0 / max(self.atlas_size, 1), 1.25, 0, 0))

    # ------------------------------------------------------------- frame
    def update(self, cam_pos: Point3, np_: NodePath) -> None:
        # deactivate last frame's shadow renders
        for dr in self._active_regions:
            dr.setActive(False)
        self._active_regions = []

        candidates = []
        for light in self.lights:
            if not light.enabled:
                continue
            d = (light.pos - cam_pos).length() - light.range
            candidates.append((d, light))
        candidates.sort(key=lambda x: x[0])
        visible = [l for _, l in candidates[: self.max_lights]]
        self.data[:] = 0
        for i, l in enumerate(visible):
            d = l.direction.normalized()
            cos_outer = math.cos(math.radians(l.outer_deg))
            cos_inner = math.cos(math.radians(min(l.inner_deg, l.outer_deg - 0.5)))
            tile_fov = 2.0 * (1.0 + 6.0 / self.tile_size) if l.kind == "point" else \
                2.0 * math.tan(math.radians(min(l.outer_deg * 2 + 6, 170)) * 0.5)
            normal_scale = 1.5 * tile_fov / self.tile_size   # ~1.5 texels at distance 1 m
            self.data[i, 0] = (l.pos.x, l.pos.y, l.pos.z, l.range)
            self.data[i, 1] = (l.color.x, l.color.y, l.color.z, 1.0 if l.kind == "spot" else 0.0)
            self.data[i, 2] = (d.x, d.y, d.z, cos_outer)
            self.data[i, 3] = (cos_inner, float(l.shadow_tile), l.shadow_near, normal_scale)
        self._upload(self.data_tex, self.data)
        self.num_visible = len(visible)
        np_.setShaderInput("u_numLights", self.num_visible)

        if not self.shadows_enabled or not self.tile_regions:
            return
        # schedule shadow renders: dirty lights first (closest first), then round-robin
        budget = self.updates_per_frame
        by_light = {}
        for idx, (light, tile) in enumerate(self.tile_owner):
            by_light.setdefault(id(light), []).append(idx)
        scheduled = []
        for _, light in candidates:
            if light.dirty and light.shadow_tile >= 0:
                idxs = by_light.get(id(light), [])
                if len(scheduled) + len(idxs) > max(budget, len(idxs)) and scheduled:
                    break
                scheduled += idxs
                light.dirty = False
        if not scheduled:
            n = len(self.tile_regions)
            for _ in range(min(budget, n)):
                scheduled.append(self._rr % n)
                self._rr += 1
        for idx in scheduled:
            dr = self.tile_regions[idx]
            dr.setActive(True)
            self._active_regions.append(dr)

    def mark_dirty_near(self, pos: Point3, radius: float = 1.0) -> None:
        for l in self.lights:
            if (l.pos - pos).length() < l.range + radius:
                l.dirty = True
