# Milestone 8 results

Measured on the finished v2 brain (`ai/v2/`), against the Phase 0 baseline in
[`docs/baseline/`](../baseline/README.md) and the acceptance criteria in
[OVERHAUL_PLAN.md](../OVERHAUL_PLAN.md) section 7. Same container, same CPU, same
software OpenGL as the baseline.

## Summary

| criterion | target | result | met |
|---|---|---|---|
| v2 beats legacy (same aim and reaction numbers) | ≥ 70 % of rounds, ≥ 60 rounds, sides swapped | **43 %** (38 of 88, Wilson 95 % CI 33-54): attack 25/48 (52 %), defence 13/40 (33 %) | **no** |
| deaths while reloading | lower | 20.3 % → **10.1 %** | yes |
| unseen deaths (killer not seen in the last 5 s) | lower | 8.4 % → **8.0 %** (seed 1: 13.9 → 8.6; seed 2: 3.0 → 7.5) | barely |
| deaths traded within 3 s | higher | 13.6 % → **16.1 %** (of tradeable deaths 22.3 → 27.6 %) | yes |
| stacking (teammates within 0.8 m for 1 s) | ≤ 0.1 per round | 3.38 → **1.08** per round | **no** |
| stuck bots (5 s without progress) | 0 | 2 + 0 → **0 + 1** | **no** |
| no attack plan above 40 % of rounds | ≤ 40 % | largest share **25 %** | yes |
| fairness audit, v2 | 0 violations | **0** in every run (88 head-to-head rounds, 48 v2-vs-v2 rounds) | yes |
| live tick mean | ≤ 3.97 ms (1.3 × 3.06) | **4.21 ms** (1.38 × the baseline; 1.34 × legacy re-run on this machine) | **no** |
| live tick p95 | ≤ 6.64 ms (1.3 × 5.11) | **7.29 ms** (1.43 × the baseline; 1.28 × legacy re-run) | **no** |
| AI decision spikes | no tick over 4 ms | **542** ticks over 4 ms, worst 14.6 ms (legacy: 282, worst 41.1 ms) | **no** (legacy neither) |
| new tests | utility, belief, comms, tactical map, roles, audit | 4 new modules (`test_ai_infra`, `test_knowledge`, `test_tactical_map`, `test_v2_brain`), 209 tests in total, all green | yes |

Behaviour rows: averages of the seed 1 and seed 2 matches (24 rounds each, Normal, the whole
match v2 against v2), legacy numbers from the baseline runs with the same seeds and definitions
(`python tools/bot_metrics.py --compare docs/baseline/legacy_normal_seed1.json
docs/results/v2_normal_seed1.json`).

## Head-to-head: v2 against legacy

Four full matches on the final code, Normal, both brains with the same perception, aim
controller, profile numbers and reaction sampler. v2 started on defence in seeds 201 and 203 and
on attack in 202 and 204; sides swap at halftime in every match.

```
# seeds 201 and 203 with --ai team0=v2,team1=legacy; 202 and 204 with --ai team0=legacy,team1=v2
BOT_DEMO_TRACE=1 BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64 BOT_DEMO_ROUNDS=30 BOT_DEMO_JSON=s201.json \
  xvfb-run -a python main.py --demo bots --seed 201 --res 320x180 --preset low --windowed \
  --ai team0=v2,team1=legacy --audit
```

`BOT_DEMO_NORENDER` and `BOT_DEMO_TICKS=64` only skip drawing and run a second of game time per
frame; the game logic is the same fixed 64 Hz tick. The JSON of each match is in [`h2h/`](h2h/).

| | rounds | v2 won | on attack | on defence |
|---|---|---|---|---|
| seeds 201-204 (final code) | 88 | 38 (43 %, CI 33-54) | 25 / 48 | 13 / 40 |

What it shows:

* **Fights**: v2 kills 283, deaths 336. It dies far less while reloading (34 against legacy's 53),
  is unseen when killed about as often (20 / 22), and trades more (45 / 40).
* **Attack is even** (52 %). Splits won 7 of 10 and executes 8 of 12; fakes 0 of 5 and defaults
  1 of 7.
* **Defence is the gap** (33 %). The 2-1-2 setup won 7 of 14, the retake setup 3 of 13 and the
  aggressive setup 0 of 4. Of legacy's attack wins, 25 were eliminations.
* **Seed variance is large, mostly on defence.** Earlier batches on earlier commits: 55 % over 65
  rounds (defence 17/31), 28 % over 36 rounds (defence 1/20). Single matches snowball through the
  economy, so a batch of 60-90 rounds still has a ±10-point interval.
* **Utility**: v2 threw 121 flashes (35 % blinded an enemy), 13 smokes and 26 frags. The legacy
  brain throws none in these matches.
* **Fairness**: legacy reads hidden state 56,406 times in the four matches (the leaks listed in the
  baseline); v2 0. An experiment that closed legacy's leaks barely changed its own results (attack
  65 % against 62 %), so the leaks are not where legacy's edge comes from.

## Behaviour: v2 against v2

| metric | legacy seed 1 | v2 seed 1 | legacy seed 2 | v2 seed 2 |
|---|---|---|---|---|
| attack round wins % | 62 | 54 | 62 | 67 |
| deaths while reloading % | 22.0 | 9.7 | 18.6 | 10.5 |
| unseen deaths % | 13.9 | 8.6 | 3.0 | 7.5 |
| killer never seen that round % | 12.1 | 2.9 | 1.8 | 3.7 |
| deaths traded within 3 s % | 12.1 | 14.3 | 15.0 | 17.9 |
| traded, of tradeable deaths % | 19.3 | 22.8 | 25.3 | 32.5 |
| killer's sight of victim before the kill s | 3.15 | 2.03 | 3.45 | 2.09 |
| live time seen by an enemy % | 29.7 | 19.9 | 27.0 | 20.2 |
| stacking incidents per round | 3.58 | 1.08 | 3.17 | 1.08 |
| clumps (3+ within 2.5 m for 2 s) | 22 | 38 | 23 | 37 |
| stuck bots | 2 | 0 | 0 | 1 |
| path-follower micro-stucks | 83 | 314 | 80 | 314 |
| bullet hits / shots % | 38 | 49 | 41 | 49 |
| flashes that blinded an enemy % | 0 | 46 | 0 | 41 |
| smokes that blocked an enemy sighting % | 0 | 57 | 0 | 60 |
| frags that hurt an enemy % | 0 | 20 | 0 | 29 |
| largest attack plan share % | 50 (execute) | 25 | 42 (execute) | 25 |

The v2 runs' own timing rows are not used: they ran in parallel with the head-to-head.

## Performance

Solo runs of seed 3, 8 rounds, Normal, rendering on (the baseline's method):

| | legacy (Phase 0) | legacy, re-run later on this machine | v2 | budget |
|---|---|---|---|---|
| live tick mean ms | 3.06 | 3.14 | **4.21** | ≤ 3.97 |
| live tick p95 ms | 5.11 | 5.71 | **7.29** | ≤ 6.64 |
| AI decisions, mean ms per tick (detailed run) | 0.61 | | 1.51 | |
| ticks with AI decisions over 4 ms (detailed run) | 282 | | 542 | 0 |
| worst AI tick ms (detailed run) | 41.1 | | 14.6 | |
| animation, mean ms per tick (detailed run) | 0.62 | | 0.86 | |

* **v2 costs about 1.35-1.4 × legacy's tick**, over the 1.3 × budget.
  * Most of the difference is the AI itself, +0.9 ms a tick:
    * team upkeep (the possibility field, sampling the team's view, the budgeted path search);
    * the per-bot controllers (spacing, walking near danger, crosshair placement).
  * Animation adds +0.24 ms. v2 bots move and aim more, so the pose cache skips less often.
  * Busier rounds add a little physics.
* **The 4 ms spike rule** is not met by either brain on this CPU. v2's worst tick is lower than
  legacy's.
* **This cloud VM varies by about ±10 % between runs.** Legacy re-run later in the session measured
  3.14 / 5.71 ms, and every subsystem, physics included, moved together.
* **A round of cuts made no measurable difference** when measured back to back with legacy: a cap
  on thinks per tick, a smaller path budget, slower team upkeep, a cached roster, spacing and
  walking decided less often, and skinning only bodies in view. They are not in this PR: they
  also change behaviour slightly and would have invalidated the head-to-head. The options are in
  ROADMAP "Milestone 8 decisions to confirm".

## Files

* `v2_normal_seed1.json`, `v2_normal_seed2.json`: v2 against v2, 24 rounds, Normal, `--audit`.
* `v2_normal_seed3_timing.json`: solo run, whole ticks only (the performance reference).
* `v2_normal_seed3_detail.json`: solo run with the per-subsystem breakdown.
* `legacy_normal_seed3_timing_rerun.json`: the legacy timing run repeated on this machine later in
  the session, for the run-to-run variation.
* `h2h/s201.json` ... `h2h/s204.json`: the four head-to-head matches.
