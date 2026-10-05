# Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Player controller, movement, test level with PBR + shadows | **done** |
| 2 | Weapons, recoil, hit detection, impact effects | **done** |
| 3 | Full map, post-processing pipeline, graphics settings | **done** |
| 4 | Rounds, economy, buy menu, bomb objective | **done** |
| 5 | AI bots | **done** |
| 6 | Destructible walls, lean, gadgets, specialists | **done** |
| 7 | HUD polish, audio, menus, performance pass | next |

## Milestone 6 - delivered

* **Destructible panels** (`gameplay/destruction.py`, `data/destruction.json`).
  * Soft walls, sheet walls and hatches are cut into 25 cm chunks, each with
    hit points from its surface: plaster, wood, sheet metal, block.
  * Bullets chip the chunk they hit (a shotgun's pellets open a hole at once).
    Melee and the hammer smash an area. Explosions destroy everything within a
    radius that scales with their power: frags blow holes, the bomb levels
    walls. Wall charges and the lance cut a door-sized opening from the floor.
  * A damaged panel rebuilds once per tick: its mesh (numpy, both faces plus
    the edges of every hole) and its Bullet collision (surviving chunks merged
    into a few boxes). Bullets, sight, grenades and movement all go through
    the holes.
  * Dust, fragments and up to 48 tumbling debris pieces fly out. Bullet decals
    over removed chunks disappear.
  * A new round restores every wall.
* **Reinforcement**: defenders hold F (2.5 s) on marked walls and the hatch,
  two per player per round (three for a Recruit). Steel plates make the
  panel bullet-proof and immune to everything but the thermal lance.
* **Kestrel Compound** gained destructible pieces: the barracks and HQ
  interior walls; the armory (site A) office walls; its east wall (a breach
  wall facing mid) and west wall in block; a roof hatch above the site with
  steel stairs up the armory's west side; and the motor pool's (site B) sheet
  walls, the east one reinforceable. Six security cameras cover the approaches
  and both sites.
* **Navmesh links through holes** (`ai/navlinks.py`). The navmesh is still
  baked with every panel intact. When a wall opens from the floor to crouch
  height over at least 0.8 m, a two-way off-mesh link joins the floors on
  either side. A hatch open over 0.8 m square adds a one-way drop link. A* and
  the funnel treat links like portals, so bots path through breaches and drop
  into the site.
* **Prep phase**: freeze time (8 s) -> preparation (20 s) -> live. Defenders
  move, reinforce, shoot murder holes and place gadgets; attackers stay at
  spawn and drone. Nobody can be hurt before the round goes live.
* **Lean** (`gameplay/lean.py`), Q/E for the player and decided by the bots:
  * the eye moves 36 cm sideways and the view rolls 12°, limited by a ray so
    you cannot lean through a wall;
  * the spine rolls so the head hit box follows the camera;
  * bullets leave from the leaned eye;
  * bots lean when what they watch is hidden from their upright eye but
    visible from one side, and keep peeking while they shoot.
* **Specialists and gadgets** (`gameplay/tactical.py`, `gameplay/gadgets.py`,
  `data/specialists.json`, models in `data/weapon_models.json`):
  * Vanguard: Kiln (thermal lance), Vesper (pulse scanner), Maul (breaching
    hammer), Static (EMP grenades).
  * Bastion: Bramble (razor wire), Lantern (motion sensors), Bulwark
    (deployable shield), Ember (signal jammer).
  * The fifth player on a side is a Recruit, with an extra charge or
    reinforcement instead of a gadget.
  * Attackers get one wall charge and can buy two more ($300).
  * The specialist is picked on the buy menu's last tab; bots take the rest.
  * Placed gadgets are world objects. Electronics have a one-bullet hit box
    and EMPs switch them off. The shield is solid cover. Wire slows and
    rattles. The jammer pauses charges and lances and cuts drone signal.
* **Drones and cameras** (`gameplay/observation.py`):
  * drones are tiny kinematic characters: they drive, hop, fall through
    hatches and die to one bullet; attackers have two each per round;
  * map cameras pan within ±55°;
  * the view overlay shows REC, the view name, help and jam/EMP static;
  * a left click in a view (or middle mouse in first person) pings an enemy,
    a gadget or a spot. Pings show as markers through walls and feed the bot
    team's knowledge.
* **Bots use all of it** (`ai/gadget_ai.py`):
  * Defenders: in prep they reinforce their site (breach walls first), lay
    wire across the attack lanes, mount sensors facing an entrance and put
    shields and jammers at their hold spots. Dead defenders watch the cameras
    and call out what they see.
  * Attackers: they drive drones down a lane in prep. Before an execute one
    of them breaches a site wall (charge, lance on a reinforced wall, or
    hammer) and the team pushes through. Vesper pulses near the site and
    Static EMPs electronics there.
  * Both sides shoot enemy cameras, sensors, jammers and drones in sight when
    there is no fight.
* **HUD**: specialist kit (bottom right), ping markers, view overlay,
  reinforcement progress, prep hints; console `specialist`, `gadgets`,
  `walls`, `prep`.
* `--demo m6` tour with screenshots; the bot match demo reports gadget use.

### Milestone 6 decisions to confirm

1. **A separate prep phase** (20 s) after an 8 s freeze time, instead of
   Siege's 45 s or making the whole freeze time a prep phase. Rounds start
   about 16 s later than in Milestone 5.
2. **One specialist per player, picked in the buy menu, kept until you
   change it.** Picks are unique per team: bots take the free ones and swap
   with you. The fifth player is a Recruit. A change applies at once during
   freeze/prep if your gadget is unused, otherwise next round.
3. **Wall charges are standard kit plus buyable** (one free, up to two more at
   $300), so the economy matters for breaching. They blow on a 3 s fuse
   rather than remote detonation (simpler for players and bots). Gadgets are
   free.
4. **Gadget names and numbers** (`data/specialists.json`) are first guesses:
   the lance takes 4 s, the pulse reaches 15 m, the jammer 5 m, the EMP
   lasts 12 s.
5. **Off-mesh links instead of rebuilding navmesh cells** when walls break
   (the Milestone 5 plan): links are cheap, exact for doors cut in walls,
   and they vanish with the round reset.
6. **Destruction granularity**: 25 cm chunks. Holes are blocky by design, so
   they are readable and their collision is exact. Bullet holes are one or
   two chunks.

## Milestone 5 - delivered

* **Navigation mesh** (`ai/navmesh.py`), generated from the level's collision
  boxes with numpy (a small Recast-style pipeline), with no new dependency:
  * 0.25 m columns; thin walls are rasterised conservatively, so a 10 cm wall
    is never missed
  * several floors per column (tunnel under the yard, open stairs), linked
    when the step is climbable
  * erosion by the 0.30 m hull radius; low ceilings marked crouch-only
  * only areas reachable from the spawns are kept
  * the result is merged into about 1,900 rectangles with portals
  * A* over the rectangles, then "simple stupid funnel" string pulling
    gives shortest corner-to-corner paths
  * built in about 1 s and cached in `assets/cache/nav`
  * the tests drive the real character controller along navmesh paths
    through doors, up stairs and under a crouch-only beam, and check that
    every compound lane and hold spot is on the mesh
* **Path following** (`ai/steering.py`) with corner skipping, crouching for
  low ceilings, stuck detection (a hop and sidestep, then a new path) and
  per-bot route variety.
* **Soldier bodies** (`gameplay/body.py`): a jointed, procedurally animated
  mannequin with team uniform, helmet, goggles, vest, gloves and boots.
  * Animations: walk and run cycles that follow the direction of travel,
    crouch, aim pitch spread over spine, chest and weapon, two-bone arm IK
    onto each weapon's grips, recoil kick, and a death fall away from the
    killing shot.
  * Characters block each other: overlapping bots are pushed apart, and
    bots are pushed off the player.
  * Hitboxes are children of the body parts, so they follow the pose (Bullet
    syncs them in C++). The first-person player gets the same hitboxes.
* **Bots play by the player's rules** (`ai/bot.py`): the same character
  controller, inventory, gunplay model (recoil patterns, movement
  inaccuracy, reloads, ammo) and ballistics, including wall penetration.
  Effects and sounds play in 3D: muzzle flashes, tracers, impacts,
  gunshots, footsteps and reloads.
* **Perception** (`ai/perception.py`):
  * Sight: a view cone (fov by difficulty) and a peripheral cone up close,
    line-of-sight rays to head and chest, blocked by smoke; flashbangs blind
    bots too.
  * Hearing: footsteps (not when walking), gunshots and grenades, each tagged
    with its source.
  * Damage awareness, teammate callouts, and a memory of last known
    positions.
* **Aim** (`ai/aim.py`): turn-speed-limited view control. Aim error starts
  with distance and movement and shrinks while the bot tracks the target.
  Recoil control by difficulty, a reaction delay, and target leading.
* **Brain** (`ai/brain.py`) with five modes: engage, seek, alert, retreat
  and task.
  * Combat micro: burst length by range, counter-strafing before shooting,
    crouch-firing at range, strafing between bursts, switching to the pistol
    when the primary is empty, falling back to cover to reload or when hurt
    (back-pedalling, still facing the threat, and firing back if the enemy
    shows while the gun is loaded), and holding fire with a teammate in the
    way.
  * Tasks: move along a lane, hold an angle, plant, defuse, pick up the
    charge, guard, hunt, save.
  * Pre-aim: toward a sound, a bot aims at the first doorway or corner on
    the path to it, not through the wall.
  * Grenades: solved ballistic arcs with an obstruction check.
* **Team tactics** (`ai/tactics.py`), one brain per side:
  * Attackers: execute (one or two lanes, staging, a synced entry with flash
    and smoke), rush, or default (map control, then commit to the quieter
    site). Carriers plant, the others clear the defenders' spots. After the
    plant they guard with a view of the charge, then run before it blows.
    The nearest bot fetches a dropped charge, and the bots follow a human
    carrier's choice of site.
  * Defenders: an A/B/Mid setup on the map's hold spots, and rotations when
    two or more enemies are reported at a site (walking the last 12 m in).
    Occasional repositioning and late hunting.
  * Retakes: defenders regroup about 16 m out on their own way to the
    charge and go in together, then choose a defuser (a kit first). They
    save when the clock makes a defuse impossible.
  * The bots announce what they do on the team radio.
* **Bot economy** (`ai/buy.py`): pistol, eco, force and full buys decided by
  the team, plus a designated sniper, defuse kits and grenades.
* **Match integration**:
  * a 5v5 default roster (`data/match.json` "bots") and four difficulty
    profiles (`data/bots.json`)
  * a difficulty choice on the side selection, saved in settings
  * spectating teammates after death (over-the-shoulder chase camera, cycle
    players, free camera); the HUD shows the spectated bot
  * `--spectate` / "watch a bot match", `--bots off` for the stand-in
    practice mode
  * console `difficulty`, `botinfo`, `spectate`
  * a dead player drops their gun, and anyone who falls out of the map dies
* **Character controller fix** (affects the player too): depenetration used
  to add up the four identical manifold points of a box-box contact, pushing
  four times too far. With the head in a low ceiling that could shove a
  character through a thin floor. It now uses the deepest point per object,
  caps the step and never pushes a grounded character down.
* **`--demo bots`**: a fast-forwarded spectated match that logs every kill
  with context (distance, both bots' modes, whether the victim saw the
  killer) and stuck detection, with a summary. A 6-round run went 3:3
  (3 eliminations, 3 defuses), with 42 kills, 33% headshots and 39% of shots
  hitting, and nobody stuck.
* **Performance**: about 0.3 ms per bot per 64 Hz tick (vision at about
  10 Hz staggered, decisions at about 7 Hz, team brains at 4 Hz, hearing
  only on new noises, poses skipped when a bot stands still).
* 121 unit tests. The new ones cover the navmesh and path following, aiming,
  grenade arcs, buying, lanes, perception and the controller fix.

### Milestone 5 decisions to confirm

1. **Own navmesh generator** instead of a Recast binding. It is about 800
   lines of numpy and Python, it needs no new dependency and is built from
   the same boxes as the collision, so the map format did not change.
   Destructible walls in Milestone 6 will need it to be updated when a wall
   breaks; that is a local rebuild of the affected cells.
2. **No cheating**: bots know where you are only from what they see, hear,
   are shot by or are told by teammates. They obey the same recoil, spread,
   movement and ammo rules. Difficulty only changes reaction time, aim
   speed and error, recoil control, field of view, hearing range, strafing
   and grenade use.
3. **The bomb goes to a random attacker** (bot or human), as in CS. In
   Milestone 4 it always went to the human. Bots follow a human carrier's
   choice of site.
4. **Third-person spectating** (over the shoulder) rather than CS's
   first-person view: it shows the animated bodies and the situation
   better. A first-person mode can be added in Milestone 7.
5. **Bodies are procedural mannequins** (box limbs, helmet, vest,
   balaclava). They read clearly at range and have exact hitboxes. Rigged
   CC0 character models could replace them later.
6. **Normal is the default difficulty.** Expert bots react in 0.14-0.22 s
   and control sprays well; tell me if Normal is too hard or too easy.

### Known issue

**Bot-vs-bot balance leans towards the attackers.** In seeded 8-round
spectated matches the attackers usually win about 6 rounds in 8. The
results vary: one 6-round run went 3:3, and rounds end by detonation,
elimination, defuse and time. With you on a team you decide many rounds
yourself, but bot defenders still lose too many 2-vs-5 site fights. Next
steps I would try:
* defenders falling back from an executed site to wait for rotators;
* defenders using utility;
* tuning the defender setup.

## Milestone 4 - delivered

* **Match rules** (`gameplay/match.py`, all values in `data/match.json`): a
  pure-Python state machine with no engine dependency, so it is unit
  tested and the bots in Milestone 5 can use the same `Participant`
  interface as the player.
  * Phases: freeze (12 s) → live (1:55) → planted (40 s bomb timer) →
    round end (6 s), with halftime after round 12 and the match ending
    when a side reaches 13 of 24 rounds.
  * Win conditions: elimination, time, detonation and defuse. An
    elimination of the attackers after a plant does not end the round:
    the bomb still has to be defused, as in CS.
  * Per-player stats (kills, deaths, assists by damage, headshots, MVPs,
    score), MVP selection, round history, and friendly fire (off by
    default) through a damage filter.
* **CS-style economy:** start money $800, cap $16,000; win rewards per
  reason; a loss bonus streak ($1400 → $3400) that a win lowers by one;
  the $800 planted-loss bonus; $300 plant and defuse rewards; per-weapon
  kill rewards; halftime money reset.
* **Buy menu** (`gameplay/shop.py`, `ui/buy_menu.py`):
  * categories: pistols, SMGs, rifles, heavy, grenades, gear
  * team-restricted items: the R7 for attackers; the C9 and the defuse
    kit for defenders
  * buy-zone and buy-time checks, and the CS 1.6-style number-key buying
  * refunds of this round's purchases while buy time lasts
  * grenade carry and total limits, and the $350 helmet upgrade
  * buying a primary drops the old one
  * the reason an item cannot be bought is shown
* **Breach charge** (`gameplay/bomb.py`), with a procedural model and plant
  animation:
  * one attacker carries it; it drops on death and can be picked up
  * a 3.2 s plant, only on the ground inside a site
  * beeps that speed up and a blinking LED and light, then a 40 s timer
  * a Gaussian-falloff explosion through walls (500 damage, sigma 14 m,
    45 m radius) with a large explosion effect
  * a 10 s defuse (5 s with a kit) while looking at the charge; you cannot
    move while planting or defusing
* **Match director** (`gameplay/director.py`) connects the rules to the
  world:
  * spawns and loadouts; survivors keep their equipment, the dead get a
    knife and pistol
  * buy zones, the freeze-time movement and combat lock, plant and
    defuse input, kill attribution and rewards, bomb drop and pickup
  * clearing decals, casings and dropped guns between rounds
  * a free spectator camera 2.5 s after death
* **Stand-in opponents** (`gameplay/agents.py`): uniformed mannequins with
  real hitboxes, armour and helmets, at 21 hand-placed
  `practice_positions` on the compound. They die, topple and drop their
  rifle. They are placeholders for the Milestone 5 bots.
* **UI:**
  * side selection
  * the match HUD: score bar with clock and alive pips, money and
    reward pop-ups, kill feed, round, halftime and match banners,
    plant and defuse progress, hints, buy indicator, bomb and kit icons
  * a scoreboard (Tab) with K/D/A, HS%, MVPs, score, money and round
    history
  * a developer console (` or F10) with practice commands
* **Map:** buy zones for both spawns and the stand-in positions on Kestrel
  Compound. A map test checks that every spawn and position is free of
  colliders.
* **Scripted demo** `python main.py --demo round --seed 3`:
  * a pistol round: buy armour, eliminate the 5 stand-ins
  * a rifle round with the helmet upgrade, a plant at A and a detonation
  * a defuse at B on the defending side
  * prints the money after each step (it must end at $4900 → $1850 →
    $2150 → $5650, and $4600 after the defuse) and saves screenshots
* 84 unit tests. The new ones cover the match state machine, the economy,
  the shop rules and the bomb maths.

### Milestone 4 decisions to confirm

1. **Stand-ins instead of opponents that fight back.** Real opponents need
   the bots from Milestone 5, so for now the enemy team stands still. The
   player also has no hitboxes yet; they come with the bots, which will
   need something to shoot at.
2. **CS economy values unchanged** (start $800, win $3250/$3500, loss bonus
   $1400-$3400 with the "a win lowers the streak by one" rule, $800
   planted-loss bonus, per-weapon kill rewards). They are all in
   `data/match.json` if you want a Siege-like flatter economy instead.
3. **Timers:** 12 s freeze time (CS2 uses 15-20 s), 20 s buy time, a 1:55
   round, a 40 s bomb timer, a 3.2 s plant, and a 10 s / 5 s defuse.
4. **Friendly fire is off** (`friendly_fire` in `data/match.json`). CS
   competitive has it on; with bot teammates it is mostly a source of
   frustration.
5. **The bomb carrier.** On attack it is always the human player; with
   bots the carrier will be random, as in CS, and bots will pass it.
6. **ESC still pauses the match**, as decided in Milestone 3. The buy menu,
   scoreboard and console are separate overlays that do not pause.

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
