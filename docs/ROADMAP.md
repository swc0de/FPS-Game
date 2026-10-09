# Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Player controller, movement, test level with PBR + shadows | **done** |
| 2 | Weapons, recoil, hit detection, impact effects | **done** |
| 3 | Full map, post-processing pipeline, graphics settings | **done** |
| 4 | Rounds, economy, buy menu, bomb objective | **done** |
| 5 | AI bots | **done** |
| 6 | Destructible walls, lean, gadgets, specialists | **done** |
| 7 | HUD polish, audio, menus, performance pass | **done** |
| 8 | Bot intelligence overhaul (v2 brain, fairness audit, tactical map) | **done**, with the follow-up below; win rate 46 % against legacy, kept as is (your decision) |
| 9 | Realistic soldiers (skinned bodies, materials, animation, variety) | **done** |

## Milestone 9 - delivered

Soldiers instead of mannequins (workstream B of [OVERHAUL_PLAN.md](OVERHAUL_PLAN.md); results and
sheet: [docs/results_m9/](results_m9/README.md)).

* **Skeleton and skinning** (`gameplay/skeleton.py`, `render/shaders/skinning.glsl`): 47 bones (spine,
  neck, clavicles, twist bones, three finger chains, weapon and pack), forward kinematics by pointer
  jumping in numpy, linear blend skinning with 4 weights and a `mat3x4` palette in every pass.
* **Soldiers** (`characters/`): bodies, clothing and gear as distance fields meshed with surface nets and
  simplified to three LODs (about 12k / 4k / 1.2k triangles); MakeHuman / MPFB2 (CC0) faces when fetched,
  procedural faces offline; one draw call per soldier (plus the rifle), colours from a per-team palette, skin, fabric and
  hard-surface shading (`render/shaders/character.*`). Variety from the bot's name and the match seed:
  faces, five skin tones and more, three builds, men and women, hair and facial hair, helmet / cap /
  balaclava, pouch layouts. Built once per soldier and cached.
* **Animation** (`gameplay/body.py`, `data/character_anims.json`): gait, crouch, aim, lean, arm IK onto the
  weapon; reload, switch, throw, knife, plant and defuse clips from the game's own events; a hit flinch
  that does not depend on health; animation LOD; Bullet ragdolls on death (`gameplay/ragdoll.py`).
* **Hit boxes** (`gameplay/hitboxes.py`): capsules on the bones fitted to the visible soldier (your
  choice), a sphere on the visible head; console `hitboxes` draws them. Rays on capsules are
  re-tested exactly (`engine/physics.py`): Bullet's own capsule test is up to 6 mm generous.
* **The charge on the carrier's back** (B-8), shown only to attackers and omniscient spectators.
* **Tools**: `tools/soldier_sheet.py` (sheet, statistics, silhouettes, 40 m team contrast),
  `tools/fit_hitboxes.py`.

### Milestone 9 results

Full tables, sheet and statistics: [docs/results_m9/](results_m9/README.md).

* **Draw calls per soldier**, the rifle included: 5 at LOD0, 2 far (budget 6 / 3; the mannequin had 10).
* **Triangles**: 11.6-11.7k / 3.8-3.9k / 1.2k per full kit (budget about 12k / 4k / 1.2k).
* **Variety**: 20 different faces per match roster, at least five skin tones, three builds, men and
  women, two to three kinds of headgear and several gear layouts per team (tests).
* **No candy-wrapper**: twisted 90 degrees, forearm and upper-arm rings keep 85-95 % of their area.
* **Hit boxes**: fitted to the soldier, total exposed area 8-9 % below the mannequin's in every view;
  by group from -25 % to +24 % as the game's ray tests see them (table in the results); head
  centres within 1 cm. The B5 table you chose from projected the capsules and split overlapping
  groups approximately (up to -28 % / +47 % there); the outline is the same.
* **Animation CPU**: 1.48 ms per tick for 10 running soldiers near the camera (the mannequin 0.69 ms),
  before the animation LOD.
* **AI balance with the new hit boxes**: head-to-head subset 38 % (16/42, CI 25-53) against 50 %
  (19/38, CI 35-65) for the same seeds with the Milestone 8 hit boxes, within the interval. Hits per
  shot 50 % → 44 % and deaths while reloading 8.4 % → 12.7 % (legacy 20.3 %): smaller targets,
  longer fights.

### Milestone 9 known issues

* The first match builds every soldier once (about 1.5-2 minutes on 4 cores).
* Shots test the pose of the last physics step (at most one tick behind), as before; kept as you chose.
* Animation costs about twice the mannequin's per soldier near the camera (above).
* Hair lines and brows follow the head's vertex spacing (about 1 cm at LOD0): soft up close.
* In the backlit 40 m shot the teams separate by hue more than by value.

## Milestone 8 - delivered

Bots that play by decisions, not better aim (`ai/v2/`, selectable per team with `--ai`; the Milestone 5
brain is unchanged and still the default until you confirm). Plan, measurements and running log:
[OVERHAUL_PLAN.md](OVERHAUL_PLAN.md).

* **What a bot knows** (`ai/v2/knowledge.py`, `comms.py`, `belief.py`): facts with a source and a
  precision - sight (exact), sound (fuzzier with distance), damage from an unseen shooter (a direction
  only), radio callouts (0.3-1.2 s late, snapped to the area and fuzzed, batched into lines such as
  "Two B Long, one tagged"; "tagged" only from the speaker's own hit markers), the radar (a glance every
  few seconds, never mid-fight), pings, the kill feed, the defuse sound. A dying bot gets its last call
  out. Each team keeps a **possibility field** over the tactical points: earliest arrival times from the
  enemy spawn, cleared by what the team sees (one bot's view per tick), refilled from the sides at running
  speed, tracks for enemies seen or heard recently, and a danger weight per point with priors learned from
  earlier rounds.
* **Fairness audit** (`ai/audit.py`, `--audit`): every read of an enemy's position, head, velocity, the
  hidden charge carrier or an unseen gadget by AI code is checked against what the reader's team can see.
  v2: 0 violations in every run; the legacy brain's leaks are listed by call site.
* **Tactical map** (`ai/v2/tactical_map.py`): about 1,800 points on the compound with standing and crouched
  visibility (bitsets), cover in 16 directions, a walking graph, spawn arrival times, and per site the
  entries, hold spots (scored by how much of the entries they see and how exposed they are, off-angles,
  crouch spots), crossfire pairs and forward information spots. Built once (~15 s), cached; breakable walls
  update it.
* **The brain** (`ai/v2/brain.py`): utility-scored actions (fight, fall back, reload, trade, investigate,
  reposition, avoid, throw, the team's task) with commitment and hysteresis; controllers for movement
  (spacing, doorways, walking near likely enemies), aim (likely-point pre-aim from the field), shooting
  (bursts, counter-strafe, jiggle between bursts at range, break off stale duels) and peeking; own utility
  (pop-flash a corner and swing, frag a held spot); budgeted A* (`ai/v2/pathing.py`, both portal scorings,
  cached chains).
* **Team strategy** (`ai/v2/strategy.py`): attack plans default / execute / split / fake / contact / rush with
  an anti-repetition decay and weights that follow results; roles entry, trade, support, lurker, AWPer;
  defence setups 2-1-2 / stack / aggro / retake, crossfire holds, rotations on credible information, anchors
  that fall back, spots that died twice used less; post-plant hiding and a synchronised swing on the defuse;
  retakes with utility.
* **Humanisation and difficulty** (`ai/v2/humanize.py`, `personality.py`): lognormal reactions around the
  profile's mean (aim and reaction otherwise exactly the Milestone 5 profile), stress, per-difficulty mistakes, stable
  per-bot traits.
* **Tools**: `botinfo <name>`, `overlay`, `belief`; the bot demo reports the behaviour metrics per AI, the
  head-to-head with a Wilson interval and the audit; `BOT_DEMO_NORENDER` / `TICKS` / `CONSOLE` / `JSON`.

### Milestone 8 results

Full tables and files: [docs/results/](results/README.md). Measured after the performance pass
you chose (the deeper pass, with the spike rule as "AI p99 ≤ 4 ms").

* **Against legacy: 42 % of rounds** (32 of 77, 95 % CI 31-53; four full matches with sides
  swapped, same aim and reaction numbers). The target was 70 %. Before the performance pass the
  same four seeds gave 43 % (CI 33-54). Attack 21 of 36, defence 11 of 41.
* **Behaviour** (v2 against v2, same seeds as the baseline):
  * deaths while reloading 20 % → 8 %;
  * stacking 3.4 → 1.4 incidents per round (target 0.1);
  * no attack plan above 29 % of rounds;
  * on the wrong side of legacy in the final matches: trades 13.6 % → 11.9 % of deaths (16.1 %
    before the pass) and unseen deaths 8.4 % → 9.2 % (8.0 % before the pass). Re-runs of the same
    two seeds gave 15.0-15.2 % and 8.1-8.4 %, so single matches vary by about as much as the gap
    (decision 2).
* **Fairness audit**: 0 violations for v2 in every run. Legacy reads hidden state about 10,000
  times a match.
* **Performance**:
  * live tick on the final code 4.42 ms mean, 7.31 ms p95; the legacy control in the same session
    3.15 / 5.55 ms (1.40 × / 1.32 ×). An earlier session measured 3.95 / 6.52 against 2.91 / 4.95.
    So within 1.3 × the Phase 0 numbers in one session and over in the other, and over 1.3 × the
    same-session legacy in both (budget 1.3 ×);
  * AI p99 3.90 ms (3.51 in the earlier session; rule ≤ 4 ms: met). The AI's own cost fell
    6-13 %;
  * all gun, grenade, gadget and charge models are built at match load: their first use cost up
    to 264 ms in one tick, for both brains.

### Milestone 8 decisions (settled)

How they were settled after the merge, with the follow-up above:

* **1. Win rate**: kept as it is ("keep it as is, don't try to get to 70 %"), after one tuning
  round on defence (42 → 46 %). Legacy stays the default brain for now (A-6).
* **2. Path search budget**: 140 kept (a).
* **3. Tick budget**: being measured on the final code.
* **4. Head-to-head method**: as run (full matches, four seeds, sides swapped).
* **5. The radar**: a legitimate channel, as agreed.
* **Human errors** (asked during the follow-up): equal mechanics (no second-enemy reaction
  penalty, no flick error) and lighter mistakes (Normal at the old Expert rates), your choices.

The options as they were put:

1. **The win rate.** v2 is fair by construction and plays more like a team, but it does not beat
   the Milestone 5 bots 70 % of the time; it loses most defence rounds. Options:
   * a) accept it and make v2 the default now (legacy stays behind `--ai legacy`);
   * b) keep legacy as the default and spend another tuning round on defence (retake and
     aggressive setups lose most; anchors give up sites without a trade);
   * c) both: v2 default now, defence tuning as a follow-up.
   I recommend c): the measured gap is on one side, and v2 is better than legacy on most
   behaviour metrics (reloading deaths, deaths to an unseen enemy, exposure, hits per shot,
   utility). It is worse on clumping.
2. **The path search budget.** The performance pass halved it (260 → 140 node expansions per
   team per tick) to keep AI spikes down; routes now take about twice as long to arrive. The
   final v2-against-v2 matches traded less (16.1 % → 11.9 %) and died unseen more (8.0 % →
   9.2 %) than before the pass; the head-to-head is unchanged at 42 %. Measured since, on the
   same two seeds:
   * at 260 on the final code: trades 15.2 %, unseen deaths 8.4 %, stuck 2 + 0, and the AI p99
     4.15 ms (over the rule you chose; AI mean 1.42 → 1.46 ms);
   * at 140 with Milestone 9's hit boxes (PR B), the only other change: trades 15.0 %, unseen
     deaths 8.1 %.
   So the budget does not explain the drop: one 24-round match differs from the next by about as
   much. Options:
   * a) keep 140 (spike rule met);
   * b) go back to 260 (over the spike rule, no measurable gain);
   * c) keep 140 and make trading cheaper anyway (a trade route from the path cache, not a new
     search), as part of the defence tuning round.
   I recommend a).
3. **The tick budget.** The spike rule you chose is met, but the whole tick is about 1.32-1.40 ×
   legacy's (budget 1.3 ×). Going further needs a structural change (batching the per-bot
   queries across the team), not more trimming. Options: a) accept about 1.35-1.4 × for v2 (game
   logic stays under a third of the 15.6 ms tick on this slow VM); b) the structural change as a
   follow-up. I recommend a).
4. **How the head-to-head was run.** Full matches through `--demo bots` with
   `BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64` (same 64 Hz game logic, no drawing), four seeds instead
   of the planned three seeds × two starting sides, because a full match is about 22 rounds.
5. **The radar is a legitimate channel** for bots (glances, never mid-fight), as agreed (A-1).

### Milestone 8 known issues

As merged; the follow-up's known issues above replace them.

* **Win rate 42 %**, defence 27 % (above).
* **Trades and unseen deaths** on the wrong side of legacy in the final matches, within the
  spread between single matches (decision 2).
* **Stacking**: 1.38 incidents per round (target ≤ 0.1; legacy 3.4). Executes and regroups move
  as a group.
* **Stuck**: one bot in 48 rounds (target 0; legacy had 2). Path-follower micro-stucks rose from
  about 80 to 290-350 a match: bots in groups block each other for under a second.
* **Performance**: about 1.32-1.40 × legacy's tick measured in the same session (budget 1.3 ×);
  the AI p99 rule is met by a small margin. Timings on this VM vary by about ±10 % between
  sessions.

## Milestone 7 - delivered

* **Main menu** (`ui/main_menu.py`): title over a slow camera tour of the
  map's shots, Play (side, difficulty, 0-4 teammates, 1-5 opponents), Watch
  bot match, How to play (lists your current binds), Settings, Quit. The
  pause menu has "Quit to main menu", which ends the match
  (`Director.stop`) and brings the menu back; any setup can be started
  again without restarting the game.
* **Settings** (`ui/menus.py`):
  * a CONTROLS tab: every action in two columns, click and press a key or
    mouse button; a key that is in use moves to the old key of the changed
    action; Esc, F1, F3, F10, F12, V and the console key are reserved;
  * GAMEPLAY: crosshair options with a live preview, HUD options (radar,
    rotate, zoom, compass, first-person spectating);
  * AUDIO: master, effects, ambience, music and interface volumes;
  * DISPLAY: the debug overlay default (off / FPS / full); a Defaults button
    per tab.
* **HUD**:
  * radar (`ui/radar.py`): the image is rasterised from the navmesh once per
    map (floors shaded by height, walls, breakable panels, sites, tunnels)
    and shown through a texture transform centred on the viewed player;
    markers for teammates, spotted enemies (pings and the team's sightings
    of the last 3 s), the bomb and the sites;
  * a compass strip with site, ping and bomb markers;
  * damage direction arcs (`ui/hud.py`), health and armour bars;
  * the FPS counter moved to the top right (F1 cycles off / FPS / full).
* **First-person spectating** (option): the camera sits at the bot's eyes and
  its head is collapsed in the skinning palette.
* **Audio** (`audio/system.py`, `audio/synth.py`):
  * occlusion: a ray from the listener; blocked sounds play a low-passed
    "_muffled" recording at 60 % volume (Panda's OpenAL has no live
    filters, so the variants are synthesised offline like everything else);
  * gunshots beyond 45 m play a "_far" recording (dull boom and echo);
    shots under a roof add a room or hall tail;
  * ambience loops (wind, room tone, tunnel rumble) crossfade by the
    listener's surroundings (a ray up finds the roof), plus random distant
    creaks, birds and clanks outdoors;
  * music: a menu loop and stings for round start, win, loss and the plant;
  * 270 synthesised files, about 7 s to build on the first start (cached).
* **Performance**:
  * character bodies are GPU-skinned: one mesh per material with a 24-bone
    rigid palette (`render/shaders/skinning.glsl`) instead of a node per
    part;
  * static props, weapon models and gadget bodies are flattened, and intact
    destructible panels are drawn from one batch per material; a panel
    gets its own mesh only once it is damaged;
  * about 506 -> 190 geometry nodes in a full 5v5 match;
  * HUD text only rebuilds when it changes; the render-state cache is swept
    at a fifth of the default rate;
  * `--benchmark [SECONDS]` reports average FPS, 1 % / 0.1 % lows,
    frame-time percentiles and the game-logic time, and writes
    `user/benchmark.json`.
* `--demo m7` tour with screenshots and an ambience report; 13 new unit
  tests (radar, compass and arc maths, audio rules, synthesis), 4 more for
  rebinding and destruction batching.

### Milestone 7 decisions to confirm

1. **A square radar that rotates with you**, 28 m from the centre to the
   edge (zoom changes it). CS2 uses a rotating radar by default, too.
2. **Enemies on the radar only when spotted** by a teammate, a camera, a
   drone or a gadget, for 3 s (pings last their own time). Your own sight
   doesn't add markers: you can see them anyway.
3. **First-person spectating is off by default.** The bots' third-person
   pose holds the gun lower than a real viewmodel, so the over-the-shoulder
   camera reads better.
4. **Music only in the menu and as short stings.** No music during rounds,
   so footsteps stay audible.
5. **The audio is still fully synthesised** (no third-party sounds). A CC0
   pack could replace the gunshots and footsteps later.

### Milestone 7 known issues

* I could not listen to any of the audio: the container has no sound
  device. I checked it with OpenAL's null device (every path plays without
  errors), unit tests on the signals, and the logged mixer state. Levels
  and the ambience mix probably need tuning by ear.
* All performance numbers come from software OpenGL in a headless
  container (2-3 FPS), so they don't say anything about real GPUs. The
  draw-node count and the CPU-side profile are the reliable parts: about
  5-6 ms per 64 Hz tick for a 10-bot match on this slow CPU, dominated by bot
  AI, character movement and Bullet. Please run `--benchmark` on your
  machine.
* The radar image is static: holes blown in walls don't show on it.

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

### Milestone 6 known issues

* **Bot balance swings a lot from seed to seed.** Three spectated runs after
  the Milestone 6 changes went 2:2, 0:4 and 5:1 (attack:defence), 7:7 over
  14 rounds. That is about even overall, but individual matches are lopsided.
* **Bots breach only from a staged group, and only door-sized walls.** Once
  defenders have reinforced a breach wall, only Kiln can open it. In the
  last run bots planned 2 breaches and found no way in 4 times.
* Deployable shields do not change the navmesh: bots walk around them using
  the stuck handling.
* I measured everything with software OpenGL in a headless container, so I
  have no real FPS numbers. The destructible panels add about 110 small
  geometry nodes; a damaged panel rebuilds in about a millisecond.

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
