# Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Player controller, movement, test level with PBR + shadows | **done** |
| 2 | Weapons, recoil, hit detection, impact effects | next |
| 3 | Full map, post-processing pipeline, graphics settings | |
| 4 | Rounds, economy, buy menu, bomb objective | |
| 5 | AI bots | |
| 6 | Destructible walls, lean, gadgets, specialists | |
| 7 | HUD polish, audio, menus, performance pass | |

## Milestone 1 - delivered

* Fixed-timestep simulation (64 Hz) with interpolated rendering.
* Kinematic character controller on Bullet convex sweeps: collide-and-slide,
  stairs, slopes, ledges, crouch with headroom check, crouch-jump, depenetration,
  Source-style friction and acceleration, landing slowdown, footstep events with
  per-surface type and loudness (bots will "hear" these in Milestone 5).
* Data-driven movement tuning (`data/movement.json`).
* Custom PBR forward renderer: GGX/Smith/Schlick BRDF, normal maps, parallax occlusion,
  specular AA, AO micro-shadowing.
* Sun: 2-4 stabilised cascaded shadow maps in one atlas, Vogel-disk PCF soft shadows.
* Shadow-casting point lights (6 faces) and spot lights in a budgeted shadow atlas.
* IBL from an HDRI or a procedural sky: SH9 diffuse and a GGX-prefiltered specular cube map.
  The sun direction is extracted from the HDRI.
* Baked sky-visibility volume so interiors are not lit by the sky.
* Exponential height fog, HDR RGBA16F target, ACES tone mapping, dithering, FXAA.
* 26 materials: CC0 downloader (Poly Haven, ambientCG) with procedural fallback textures.
* Modular JSON level format with prefabs, plus the "Range 07" test level.
* Graphics presets Low/Medium/High/Ultra (`data/graphics_presets.json`), CLI overrides,
  settings persisted to `user/settings.json`.

## Design decisions to confirm (please tell me if you want something different)

1. **Working title "COLD SECTOR"** and test level "Range 07". Proposed names for later milestones:
   the sides are *Vanguard* (attackers) and *Bastion* (defenders), the bomb is a "breach charge"
   (*demo charge*), and the full map is *Kestrel Compound*. All are placeholders and easy to rename.
2. **Character controller:** I used my own kinematic controller built on Bullet sweeps instead of
   Panda3D's `BulletCharacterControllerNode`. The stock node is known to jitter on stairs and has no
   clean crouch. It still uses Bullet for every collision query. The hull is an axis-aligned box
   (like CS) rather than a capsule, because capsules "ride" stair edges and lose speed. Hitboxes for
   bullets will be separate per-bone shapes in Milestone 2.
3. **Renderer:** custom GLSL instead of `simplepbr`. simplepbr has no cascaded shadows, no
   shadow-casting point lights and no interior sky occlusion, which this look needs.
4. **Movement numbers:** run 5.4 m/s, walk 2.75, crouch 1.85, jump about 1 m, 64 Hz tick.
   Shift = quiet walk (CS-style), not sprint. Tell me if you prefer Siege-style sprinting.
5. **FOV setting is vertical** (default 74°, about 106° horizontal at 16:9). It stays consistent
   across aspect ratios.
