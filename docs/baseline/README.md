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

BEHAVIOUR_TABLE

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
* **Utility is almost absent.** One grenade (a frag) was thrown in 48 rounds. Execute flashes and
  smokes are ordered at the site centre and the defenders' rotation point. Both sites are under a
  roof, so `solve_throw` finds no clear arc and the order is dropped. Defenders never throw on
  purpose.
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

TIMING_TABLE

TIMING_NOTES

## Determinism

DETERMINISM

## Soldiers

![Soldiers before](../images/soldiers_before.jpg)

SOLDIER_STATS

## Files

* `legacy_normal_seed{1,2}.json`: 24-round behaviour runs. Per-death records, stacking incidents,
  plans, rounds, plus timing measured under the load of two parallel runs.
* `legacy_normal_seed3_timing.json`: solo run, whole ticks only. This is the performance reference.
* `legacy_normal_seed3_detail.json`: solo run with the subsystem breakdown, the expensive calls and
  the worst 25 AI spikes with their causes.
* `soldier_stats.json`: draw calls, triangles, skinning palette, `animate` cost and hitbox areas.
