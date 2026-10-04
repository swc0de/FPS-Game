# Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Player controller, movement, test level with PBR + shadows | **done** |
| 2 | Weapons, recoil, hit detection, impact effects | **done** |
| 3 | Full map, post-processing pipeline, graphics settings | **done** |
| 4 | Rounds, economy, buy menu, bomb objective | next |
| 5 | AI bots | |
| 6 | Destructible walls, lean, gadgets, specialists | |
| 7 | HUD polish, audio, menus, performance pass | |

## Milestone 3 - delivered

* **Kestrel Compound** (`maps/data/compound.json`, now the default map), a
  5v5 bomb-defusal layout built from modular prefabs:
  * Attackers start outside the south wall and enter through three gates:
    * **West gate:** A Long alley or the **Barracks** (corridor and bunk
      rooms) into the **West Yard**.
    * **Main gate:** a checkpoint chicane into **Mid** (parade ground,
      containers, flagpole post). From Mid, connectors lead to A and B,
      and **Mid Doors** lead to CT spawn.
    * **East gate:** **B Long** road, or down the ramp into the **Bunker**
      and through a 16 m **tunnel** that comes up by stairs in the **B Yard**.
  * **Site A** is the **Armory**: a concrete-block warehouse with a loading
    door, an office, shelving aisles and an outdoor loading apron.
  * **Site B** is the **Motor Pool**: an open steel hangar with trucks,
    workbenches and a yard.
  * Defenders spawn in front of the **HQ**, which they can also cut through
    to rotate between sites. The **comms tower** has a 4.5 m platform
    overlooking A and CT. A **fuel depot** sits on the B side.
  * Bomb-site zones, 23 callout areas (shown bottom-left on the HUD), and
    5 attacker and 5 defender spawns.
  * 11 walking **test routes** (`python main.py --demo routes`) prove every
    lane is passable. Attackers reach a site in about 20-27 s; defenders
    reach either site in about 7-8 s, like CS.
  * New prefabs (`maps/prefabs_military.py`):
    * building shells with door and window openings, parapets and
      two-material walls
    * T-walls, jersey barriers, gabions and a checkpoint boom gate
    * procedural trucks and utility vehicles
    * a hangar canopy and a lattice comms mast
    * furniture: bunks, lockers, shelving, desks, workbenches, fuel tanks
    * a height-field **backdrop terrain** of hills beyond the perimeter
    * bomb-site zones, callouts and paint lines
  * 17 new procedural PBR materials, each with Poly Haven / ambientCG
    download candidates.
* **Post-processing pipeline** (`render/post.py`; details in
  [RENDERING.md](RENDERING.md)), with custom render targets in place of
  FilterManager:
  * depth/normal pre-pass
  * **GTAO** ambient occlusion with a bilateral blur, applied to ambient
    light only
  * soft particles
  * **bloom** (13-tap / tent chain with a Karis-average prefilter)
  * **eye adaptation** (log-luminance pyramid, ping-pong adaptation)
  * ACES with white balance, saturation, contrast, lift/gamma/gain and
    vignette, plus optional chromatic aberration and film grain
  * **FXAA and/or MSAA 2x/4x/8x**
  * render scale 50-100% with **CAS** sharpening
  * **F3** debug views
* **Pause menu (ESC) and settings screen** with Graphics, Display, Gameplay
  and Audio tabs:
  * Presets, plus every graphics option individually.
  * Changes **apply live**: only the affected part of the renderer is
    rebuilt. Settings are saved to `user/settings.json`. Only texture
    quality and V-Sync need a restart.
  * Display options: resolution, display mode, FPS limit, FOV with the
    horizontal equivalent shown, weapon FOV and brightness.
  * Gameplay options: mouse and zoom sensitivity, invert mouse, and a
    crosshair editor. Audio: volumes.
* Fixed a frame-order bug: shadow maps now render before the scene in the
  same frame (before, they lagged one frame behind).
* 65 unit tests. New ones cover the graphics settings normalisation, map
  integrity (known prefabs and materials, both sites, spawns, no
  overlapping floor slabs) and the settings menu model.

### Milestone 3 decisions to confirm

1. **Anti-aliasing: MSAA + FXAA instead of SMAA.** In a forward renderer,
   MSAA is the natural high-quality option (CS2 uses it), and FXAA already
   covers the cheap post-process case. SMAA needs precomputed area and
   search textures plus three more passes, for a result between the two.
   I can still add SMAA 1x if you want it.
2. **Eye adaptation is partial and clamped** (55% strength, -1..+1.25 EV),
   so interiors are brighter than without it but still darker than
   outside. Siege adapts more strongly; CS2 adapts less. You can turn it
   off in the settings.
3. **Map name and layout.** I designed the layout CS-style: three lanes,
   sites near the defender spawn, an underground tunnel as the Siege-like
   flank. Interior partition walls are plaster and plywood. They are
   already bullet-penetrable, and they will become destructible soft walls
   in Milestone 6.
4. **Default map** is now the compound. The test range is still there:
   `--map test_range`. The weapon demos select it automatically.
5. **ESC pauses the game** (single player against bots). Online play would
   not pause; that can change if multiplayer is ever added.

## Milestone 2 - delivered

* **Data-driven weapons** (`data/weapons.json`). There are 7 weapons:
  * P9 Warden pistol
  * MX-5 Hornet SMG
  * R7 Halberd and C9 Lancer rifles
  * SR-90 Longbow bolt-action sniper
  * S12 Breacher pump shotgun
  * K-3 knife

  There are also 3 grenades: the M-40 frag, the SG-2 smoke and the FB-1
  flashbang. Each gun sets its price, fire rate, magazine, damage, armour
  penetration, range modifier, wall penetration power, head multiplier,
  inaccuracy model, recoil pattern, reload timings, movement speed, ADS or
  scope, and tracer cadence.
* **CS-style gunplay** (`weapons/weapon.py`):
  * Fixed per-weapon recoil patterns, indexed by shot number. The camera
    follows part of the pattern and the rest shows as bullet drift. The
    pattern recovers over time when you stop firing.
  * First-shot accuracy. Inaccuracy is built from a base value plus
    movement (only above 34% of max speed, so slow walking stays accurate),
    air, crouch, and an accumulated firing term. ADS tightens it.
  * Fire modes: auto and semi-auto with trigger reset; bolt and pump
    cycling; shell-by-shell shotgun reload that firing interrupts;
    tactical vs. empty reloads.
* **Hit detection** (`weapons/ballistics.py`, `gameplay/hitboxes.py`):
  * Hitscan against per-body-part kinematic Bullet hitboxes: head, neck,
    chest, stomach, pelvis, arms, thighs and calves, posed for standing
    and crouching.
  * Hit-group multipliers, distance falloff (`damage * rm^(d/10)`), and
    CS-style armour and helmet absorption.
  * **Wall penetration:** the solid thickness of each wall is measured
    with forward and backward ray casts. Each surface type
    (`data/surfaces.json`) costs penetration power per cm and keeps part
    of the damage. Rifles go through plywood, plaster and sheet steel, but
    not brick or concrete.
* **Viewmodel** with a separate FOV and procedural, data-driven gun models
  (`data/weapon_models.json`). It has:
  * sway, bob, crouch and landing motion
  * ADS on every gun, plus a 2-level sniper scope with a scope overlay
  * keyframed reload, empty reload, bolt, pump, shell, inspect, knife and
    grenade animations, with moving magazine, bolt, slide and pump parts
* **Impact effects:**
  * a batched particle system (1 draw call per blend mode)
  * PBR-lit decals in a ring buffer (1 draw call), with per-surface bullet
    holes, scorches and blood
  * a muzzle flash with a light pulse, tracers and bouncing shell casings
  * explosions, a smoke volume, a flashbang whiteout and camera shake
* **Grenades:** Bullet rigid bodies with continuous collision detection,
  bounces and fuses. Frag damage needs line of sight and falls off with
  distance. Flash blindness depends on distance and viewing angle. Smoke
  lasts 18 s and exposes a density value for AI line-of-sight checks.
* **Inventory and pickups:**
  * slots for primary, secondary, melee and grenades
  * weapon switching with keys 1-4, the mouse wheel and last weapon (Z)
  * drop (G), use-key pickup (F) from respawning racks, and auto-pickup by
    walking over a dropped gun
  * ammo, armour and grenade boxes
* **HUD:**
  * health and armour
  * ammo and reserve, with the weapon name
  * a dynamic crosshair that shows the real spread
  * hit markers: head, body and kill
  * a damage vignette, the scope overlay, the flash whiteout and pickup
    prompts
* **Audio (placeholder):** every gunshot, impact, reload stage, footstep,
  explosion and UI sound is synthesised with numpy on first start and
  cached as WAV. Sounds play through 3D positional audio. Footsteps and
  shots raise "noise events" that bots will use in Milestone 5.
* **Shooting range** in the test level:
  * benches and weapon racks
  * 6 target dummies: armoured, helmeted, moving, and at 10, 20 and 30 m.
    Each shows its damage, toppling at 0 HP and respawning.
  * a spray wall
  * 5 penetration panels with dummies behind them
* **Scripted demo** `python main.py --demo weapons`. It exercises everything
  above with virtual input, prints damage results and saves screenshots.
  The output is used as a regression check.
* 51 unit tests: gunplay, damage, ballistics against real Bullet hitboxes,
  penetration, inventory and grenade maths.

### Milestone 2 decisions to confirm

1. **Weapon names and roster.** The names are all original placeholders:
   * pistol: P9 Warden
   * SMG: MX-5 Hornet
   * rifles: R7 Halberd and C9 Lancer
   * sniper: SR-90 Longbow
   * shotgun: S12 Breacher
   * knife: K-3
   * grenades: M-40 frag, SG-2 smoke, FB-1 flash

   The two rifles follow the CS attacker/defender rifle split: the R7 kills
   through a helmet with one headshot, while the C9 fires faster with easier
   recoil. Milestone 4 will tie them to sides and prices.
2. **ADS on every gun.** CS has no iron-sight ADS. Siege does. I added
   light ADS: about 0.8× zoom, a tighter spread and slower movement. The sniper uses
   a real scope overlay. If you prefer pure CS hip-fire, delete a weapon's
   `ads` block in `data/weapons.json` and that gun goes back to hip fire.
3. **Armour model** follows CS: armour absorbs part of the damage and loses
   half of what it absorbs, and legs are never armoured. A helmet only
   matters for headshots.
4. **Procedural placeholder audio** now, rather than waiting for
   Milestone 7. The game is very hard to judge silently. A CC0 sound pack
   can replace it later. The code only needs WAV/OGG files with the same
   names.
5. **Viewmodel art.** The guns and arms are procedural, chamfered
   low-poly models. They are readable but not detailed. They can be swapped
   for real CC0 or authored meshes later (glTF/egg) without code changes,
   because anchors and animated groups are just named nodes.

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

## Milestone 1 design decisions

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
