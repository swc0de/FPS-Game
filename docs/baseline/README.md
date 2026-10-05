# Phase-0 baseline (legacy bots, Milestone 7 soldiers)

The reference for the overhaul in [../OVERHAUL_PLAN.md](../OVERHAUL_PLAN.md). Workstreams A and B are
measured again with the same tools, seeds and machine.

## Machine and method

* Linux container, Intel Xeon @ 2.10 GHz, 4 cores, Python 3.11.15, Panda3D 1.10.16, numpy 2.4.6.
  Rendering is Mesa llvmpipe (software GL) under Xvfb, so **frame rates mean nothing here**. Only the
  CPU-side simulation numbers do.
* `tools/bot_metrics.py` runs the normal `--demo bots` match (spectated, 10 bots, fixed 0.5 s
  frames, 64 Hz ticks) and observes it from the outside. It wraps methods but changes no decision.
  `--full-match` plays every round (no early win at 13 rounds), so every run has 24 rounds and a
  halftime side swap.
* Behaviour runs (two in parallel):
  ```
  xvfb-run -a python tools/bot_metrics.py --seed 1 --rounds 24 --full-match --out docs/baseline/legacy_normal_seed1.json
  xvfb-run -a python tools/bot_metrics.py --seed 2 --rounds 24 --full-match --out docs/baseline/legacy_normal_seed2.json
  ```
* Timing runs, **alone on the machine**: whole ticks only (lowest overhead), then the same seed with
  the subsystem and spike breakdown:
  ```
  xvfb-run -a python tools/bot_metrics.py --seed 3 --rounds 8 --full-match --no-detail --out docs/baseline/legacy_normal_seed3_timing.json
  xvfb-run -a python tools/bot_metrics.py --seed 3 --rounds 8 --full-match --out docs/baseline/legacy_normal_seed3_detail.json
  ```
* Soldiers: `xvfb-run -a python tools/soldier_sheet.py --out docs/images/soldiers_before.jpg --stats docs/baseline/soldier_stats.json`
* Tables: `python tools/bot_metrics.py --compare <json files>`.

The metric definitions are in the docstring of `tools/bot_metrics.py`.

## Behaviour (legacy brain, Normal, 2 × 24 rounds)

| metric | seed 1 | seed 2 | seed 3 (timing run, 8 rounds) |
|---|---|---|---|
| rounds | 24 | 24 | 8 |
| attack round wins % | 62 | 62 | 62 |
| average round length s | 73 | 68 | 86 |
| kills | 173 | 167 | 49 |
| plants | 12 | 15 | 3 |
| defuses | 0 | 3 | 0 |
| headshot kills % | 37 | 33 | 27 |
| bullet hits / shots % | 38 | 41 | 37 |
| deaths while reloading % | 22.0 | 18.6 | 12.2 |
| unseen deaths % (killer not seen in last 5 s) | 13.9 | 3.0 | 8.2 |
| killer never seen that round % | 12.1 | 1.8 | 6.1 |
| deaths traded within 3 s % | 12.1 | 15.0 | 16.3 |
| traded, of deaths with a teammate within 15 m % | 19.3 | 25.3 | 23.1 |
| killer's sight of victim before the kill s | 3.15 | 3.45 | 2.58 |
| live time seen by an enemy % | 29.7 | 27.0 | 27.1 |
| stacking incidents per round | 3.58 | 3.17 | 1.75 |
| clumps (3+ within 2.5 m for 2 s) | 22 | 23 | 5 |
| stuck bots (5 s without progress) | 2 | 0 | 1 |
| path-follower micro-stucks | 83 | 80 | 25 |
| flashes that blinded an enemy % | 0 | 0 | 0 |
| smokes that blocked an enemy sighting % | 0 | 0 | 0 |
| frags that hurt an enemy % | 0 | 0 | 0 |
| leak: exact attacker position when shot unseen, per round | 3.21 | 1.71 | 1.38 |
| leak: exact killer position broadcast, per round | 1.00 | 0.33 | 0.50 |
| live tick mean ms | 3.54 | 3.62 | 3.06 |

**Pooled over all 56 rounds:**
* Attack won 35 of 56 rounds: 62 %, Wilson 95 % CI 49-74 %.
* 389 kills.
* Deaths while reloading: 19.3 %. Unseen deaths: 8.5 %.
* Trades: 13.9 % of deaths, and 22.3 % of the 197 deaths with a teammate within 15 m.
* Stacking: 3.1 incidents per round. 50 clumps. 3 stuck bots.
* Attack plans: execute 45 %, default 39 %, rush 16 %.
* Leaks per round: 2.3 exact attacker positions and 0.64 exact killer broadcasts.

The timing rows of the seed 1 and 2 runs are left out: those two runs shared the CPU. The seed 3
timing below was measured alone.

Notes from the per-death records (`deaths` in the JSON files, 340 kills between bots):

* **Where bots die**, by the victim's mode:
  * engage: 213;
  * **retreat: 77 (23 %)**;
  * alert: 37;
  * seek: 7;
  * task: 6.

  **60 of the 69 deaths while reloading happen in "retreat"**. A hurt or empty bot back-pedals in
  sight of its enemy while reloading.
* **Unseen deaths** happen mostly in "alert" (20 of 29): the bot faces a sound and dies to someone
  else.
* **Utility is almost absent.** One grenade (a frag) was thrown in 48 rounds.
  * The execute orders its flash at the site centre and its smoke at the defenders' rotation point
    while the team stands at the lane staging points, 19-35 m away.
  * With the game's gravity (20 m/s²) and throw speeds of 16-17 m/s, the longest flat throw is
    12.8-14.5 m.
  * `_do_throw` solves the arc once, at the moment of the order. It finds none and drops the order.

  A probe of all 12 lane / grenade pairs finds every one out of range, and obstructed too.
  Defenders never throw on purpose.
* **Attack plans**: execute 50 % / 42 %, default 33 % / 38 %, rush 17 % / 21 % of rounds
  (seed 1 / seed 2). The site choice drifts: seed 1 went B in 13 of 24 rounds, seed 2 went A in
  19 of 24. Each plant adds weight to that site (`site_history`).
* **Defender holds are predictable**: 11 distinct spots over 118 samples (one per defender,
  10 s into each round). The most used spot was taken in 16 of 24 rounds.
* **Stacking** happens mostly on the attack lanes and at spawns: Main Gate 32, West Yard 23,
  T Spawn 24, B Long 17, Mid 20. Bodies touch for over a second.
* **Stuck**: two bots on seed 1 at the same spot (50.0, 38.2) in the hangar, one guarding the
  post-plant spot and one moving to clear it. They block each other.
* **Information leaks** a player would not have, counted per round:
  * an exact attacker position when shot by an unseen enemy;
  * an exact killer position broadcast to the team after an unseen kill.

  Not counted, but present in the code: the defenders' rotation uses the charge carrier's
  identity, and Static's EMP picks targets from every enemy gadget near the site.
* **Side balance**: attack 15 : 9 on both seeds (62 %).
* **Kill distance**: median 19.5 m.

## Simulation time per 64 Hz tick (10 bots)

Seed 3, 8 rounds, all rounds played, **alone on the machine**. 43,921 live and
planted ticks (freeze, prep and round end excluded).

| ms per tick | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| **whole tick** (`--no-detail`, the reference) | **3.06** | 2.73 | **5.11** | 7.54 | 258 |
| whole tick (detailed run, +4 % overhead) | 3.17 | 2.84 | 5.21 | 7.62 | 331 |
| AI (brains + team brains + gadget AI) | 0.66 | 0.45 | 1.37 | 3.19 | 326 |
| AI decisions (without the mesh building, radio HUD and GC it triggers) | 0.61 | 0.45 | 1.37 | 3.11 | 41.1 |

* **Budgets for the overhaul** (plan section 3.10):
  * live-tick mean ≤ 1.3 × 3.06 = **3.97 ms**;
  * p95 ≤ 1.3 × 5.11 = **6.64 ms**;
  * no tick with more than 4 ms of AI decisions.
* **Today:** 282 ticks over 4 ms of AI decisions (35 per round) and
  306 ticks over 4 ms of AI including side effects.

Mean ms per live tick by subsystem (detailed run):

| subsystem | mean | p99 |
|---|---|---|
| bullet | 0.818 | 1.72 |
| brains | 0.645 | 3.15 |
| animation | 0.623 | 1.55 |
| movement | 0.620 | 1.38 |
| tactical | 0.242 | 0.57 |
| perception | 0.188 | 1.11 |
| gadget_ai | 0.006 | 0.16 |
| destruction | 0.006 | 0.01 |
| team | 0.005 | 0.12 |

Expensive calls (detailed run). `q:` rows are inside the subsystems above. "Side effect" marks work
the AI triggers but does not decide with:

| call | calls | mean ms | p99 ms | max ms | calls over 4 ms |
|---|---|---|---|---|---|
| deploy_gadget (side effect) | 27 | 67.048 | 303.69 | 322.8 | 27 |
| set_weapon_model (side effect) | 188 | 53.889 | 156.61 | 168.3 | 114 |
| fire (not AI) | 607 | 9.487 | 115.61 | 141.8 | 54 |
| throw | 37 | 3.681 | 84.59 | 127.7 | 2 |
| throw_drone (side effect) | 31 | 28.524 | 35.31 | 37.4 | 31 |
| vision | 80,046 | 0.085 | 0.27 | 28.5 | 2 |
| lane_path | 551 | 2.101 | 12.70 | 16.1 | 81 |
| find_path | 2,311 | 1.306 | 7.37 | 16.1 | 101 |
| preaim | 62,840 | 0.033 | 1.22 | 8.1 | 28 |
| flee | 3 | 2.545 | 3.59 | 3.6 | 0 |
| lean | 387,996 | 0.003 | 0.04 | 2.5 | 0 |
| hearing | 480,176 | 0.003 | 0.04 | 2.3 | 0 |
| defend_setup | 8 | 1.210 | 1.31 | 1.3 | 0 |
| think | 42,622 | 0.007 | 0.03 | 1.1 | 0 |
| find_cover | 131 | 0.212 | 0.49 | 0.6 | 0 |
| breach | 1,375 | 0.004 | 0.01 | 0.4 | 0 |
| post_plant_spots | 3 | 0.202 | 0.26 | 0.3 | 0 |
| bomb_plant (side effect) | 3 | 0.098 | 0.10 | 0.1 | 0 |
| radio_hud (side effect) | 635 | 0.012 | 0.04 | 0.1 | 0 |
| use_gadget (side effect) | 12 | 0.037 | 0.07 | 0.1 | 0 |

* **Spikes come from building meshes on demand**, mostly in the AI's call path:
  * `Tactical.deploy` builds a gadget model (mean 67 ms, up to 323 ms);
  * `CharacterBody.set_weapon` rebuilds and flattens the third-person weapon model on every
    weapon switch (mean 54 ms, up to 168 ms). Bots switch to plant, throw and draw the pistol.
  * drone throws build a drone (about 28 ms).
* **Firing is not AI, but expensive**: a bot's shot costs 9.5 ms on average. A profile of
  `BotWeapons._fire` shows the cost is in the killing shots: the victim drops its gun, and
  `PickupManager.spawn` builds the dropped weapon model (about 135 ms, `add_chamfer_box`). The
  impact effects, tracer and muzzle flash cost under 2 ms.
  * One fix serves all of these: build each model once and instance it.
* **Pure decision spikes are path queries.** `NavMesh.find_path` (mean 1.3 ms, p99 7.4 ms, max
  16 ms), mostly through `_lane_path` on long lanes and `_preaim`. With them the AI decisions
  exceed 4 ms in 282 ticks in 8 rounds.
* **Garbage-collection pauses**: 256 collections, p99 0.9 ms, max 59 ms. Rare and not caused
  by the AI.
* The ROADMAP's "about 5-6 ms per tick" is the debug overlay's per-frame logic time. That includes
  the renderer-side updates and several ticks per frame, so it is not comparable with this per-tick
  figure.
* Absolute numbers depend on this machine. The overhaul is compared with the same tool, seeds and
  machine.

## Determinism

`--seed` reproduces rounds:
* **Two runs of seed 5** (3 rounds each) printed identical match logs: every kill with its time to
  0.1 s, killer, victim, weapon, distance and modes; every plant, defuse and round result; total
  shots and hits; every bot's K/D/A and money; gadget use.
* **The seed 3 timing and detailed runs** produced the same match (5 : 3, 49 kills, 3 plants), even
  with the extra instrumentation.

Caveats:
* This holds with the demos' fixed frame time.
* In interactive play, the bodies' render interpolation is what Bullet syncs the hitboxes from at
  the next physics step. Hit registration can therefore depend on the frame rate (plan, section 2.2).
* Global `random` is only used by debris, decals and particles, which do not touch gameplay
  collision masks.

## Soldiers

![Soldiers before](../images/soldiers_before.jpg)

| | Geoms (= draw calls per pass) | triangles | vertices |
|---|---|---|---|
| body (6 materials: uniform, glove/balaclava, helmet, goggles, vest, boots) | 6 | 1,760 | 2,424 |
| held rifle (flattened, 4 materials) | 4 | 884 | 7,120 |

* **Draw calls.** A soldier is drawn in the main pass, the depth pre-pass and every shadow cascade
  it overlaps (2-4 by preset), so 10 calls per pass.
* **Skinning.** Rigid, one bone per vertex, 24 × `mat4` palette =
  384 vertex-uniform components.
* **Animation CPU.** `CharacterBody.animate` costs 69 µs per
  running soldier, so 0.69 ms per tick for 10 moving
  soldiers. Standing ones are skipped by the pose cache.

**Hitbox exposed areas in cm².** Each hit group is seen from the front and the side, standing and
crouched, holding a rifle, with a 5 mm ray grid. Workstream B must keep every value within ±10 %:

| view | head | chest | stomach | arm | leg | total |
|---|---|---|---|---|---|---|
| stand front | 383 | 950 | 1317 | 1070 | 2570 | 6291 |
| stand side | 377 | 440 | 700 | 1107 | 1450 | 4074 |
| crouch front | 383 | 1009 | 786 | 1138 | 1658 | 4973 |
| crouch side | 376 | 448 | 687 | 1106 | 1316 | 3934 |

## Files

* `legacy_normal_seed{1,2}.json`: 24-round behaviour runs. Per-death records, stacking incidents,
  plans, rounds, plus timing measured under the load of two parallel runs.
* `legacy_normal_seed3_timing.json`: solo run, whole ticks only. This is the performance reference.
* `legacy_normal_seed3_detail.json`: solo run with the subsystem breakdown, the expensive calls and
  the worst 25 AI spikes with their causes.
* `soldier_stats.json`: draw calls, triangles, skinning palette, `animate` cost and hitbox areas.
