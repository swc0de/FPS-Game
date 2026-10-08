# Milestone 8 results

Measured on the finished v2 brain (`ai/v2/`) after the performance pass, against the Phase 0
baseline in [`docs/baseline/`](../baseline/README.md) and the acceptance criteria in
[OVERHAUL_PLAN.md](../OVERHAUL_PLAN.md) section 7. Same kind of cloud container, CPU model and
software OpenGL as the baseline. The cloud VM's speed drifts between sessions, so every timing
below comes with a legacy control run from the same session.

The numbers from before the performance pass are kept in [`before_perf/`](before_perf/).

## Summary

| criterion | target | result | met |
|---|---|---|---|
| v2 beats legacy (same aim and reaction numbers) | ≥ 70 % of rounds, ≥ 60 rounds, sides swapped | **37 %** (30 of 82, Wilson 95 % CI 27-47): attack 16/38 (42 %), defence 14/44 (32 %). Before the performance pass: 43 % (38/88, CI 33-54) | **no** |
| deaths while reloading | lower | re-measuring on the final code; before the pass: 20.3 % → 10.1 % | yes (before the pass) |
| unseen deaths (killer not seen in the last 5 s) | lower | re-measuring on the final code; before the pass: 8.4 % → 8.0 % | barely (before the pass) |
| deaths traded within 3 s | higher | re-measuring on the final code; before the pass: 13.6 % → 16.1 % | yes (before the pass) |
| stacking (teammates within 0.8 m for 1 s) | ≤ 0.1 per round | re-measuring on the final code; before the pass: 3.38 → 1.08 per round | **no** |
| stuck bots (5 s without progress) | 0 | re-measuring on the final code; before the pass: 2 + 0 → 0 + 1 | **no** (before the pass) |
| no attack plan above 40 % of rounds | ≤ 40 % | re-measuring on the final code; before the pass: largest share 25 % | yes (before the pass) |
| fairness audit, v2 | 0 violations | **0** in every run (82 head-to-head rounds, 48 v2-vs-v2 rounds) | yes |
| live tick mean | ≤ 3.97 ms (1.3 × 3.06) | **3.95 ms**; legacy control in the same session 2.91 ms (v2 = 1.36 ×) | yes, against the Phase 0 budget; not against the same-session legacy |
| live tick p95 | ≤ 6.64 ms (1.3 × 5.11) | **6.52 ms**; legacy control 4.95 ms (1.32 ×) | yes, as above |
| AI decision spikes (rule as chosen: AI p99 ≤ 4 ms) | p99 ≤ 4 ms | **3.51 ms** (was 4.21; legacy 3.11) | yes |
| new tests | utility, belief, comms, tactical map, roles, audit | 4 new modules (`test_ai_infra`, `test_knowledge`, `test_tactical_map`, `test_v2_brain`); 209+ tests in total, all green | yes |

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
frame; the game logic is the same fixed 64 Hz tick. A seeded match now repeats exactly (the
debris of breakable walls drew from the unseeded global generator; fixed). The JSON of each
match is in [`h2h/`](h2h/).

| seed | v2 won | on attack | on defence |
|---|---|---|---|
| 201 | 9 / 22 | 4 / 12 | 5 / 10 |
| 202 | 3 / 16 | 0 / 4 | 3 / 12 |
| 203 | 9 / 22 | 8 / 12 | 1 / 10 |
| 204 | 9 / 22 | 4 / 10 | 5 / 12 |
| **all** | **30 / 82 (37 %, CI 27-47)** | 16 / 38 | 14 / 44 |
| before the performance pass | 38 / 88 (43 %, CI 33-54) | 25 / 48 | 13 / 40 |

What it shows:

* **The performance pass may have cost a few points; the data cannot tell.** The two intervals
  overlap widely, and any change re-rolls every seeded match (seed 202 went from 13/24 to 3/16).
  The pass halved the path search budget (260 → 140 node expansions per team per tick), so a
  new route takes about twice as long to arrive; that is the change most likely to matter. It
  is listed as an option in ROADMAP "Milestone 8 decisions to confirm".
* **Fights**: v2 kills 236, deaths 307. It dies less while reloading (33 against legacy's 51) and
  less often to an enemy it never saw (14 / 29), and trades about as often (39 / 40).
* **Attack**: splits won 4 of 7, executes 4 of 10, rushes 3 of 5; contact and default 2 each.
* **Defence is the gap.** The 2-1-2 setup won 7 of 15, retake 4 of 15, stack 3 of 7, the
  aggressive setup 0 of 7. Of legacy's attack wins, 27 of 30 were eliminations.
* **Utility**: v2 threw 122 flashes (32 % blinded an enemy), 10 smokes and 26 frags. The legacy
  brain throws almost none.
* **Fairness**: legacy reads hidden state 51,048 times in the four matches (the leaks listed in
  the baseline); v2 0.

## Behaviour: v2 against v2

Being re-run on the final code (seeds 1 and 2, 24 rounds each). The table from before the
performance pass is in [`before_perf/`](before_perf/) and in git history.

## Performance

Solo runs of seed 3, 8 rounds, Normal, rendering on (the baseline's method). The legacy control
ran right before v2 in the same session, so the ratio is the fair number.

| | legacy control | v2 | v2 before the pass | budget |
|---|---|---|---|---|
| live tick mean ms | 2.91 | **3.95** (1.36 ×) | 4.21 (1.34 × its control, 3.14) | ≤ 3.97 |
| live tick p95 ms | 4.95 | **6.52** (1.32 ×) | 7.29 (1.28 × its control, 5.71) | ≤ 6.64 |
| AI decisions, mean ms per tick (detailed run) | 0.61 (Phase 0) | **1.32** | 1.51 | |
| AI decisions, p99 ms (detailed run) | 3.11 (Phase 0) | **3.51** | 4.21 | ≤ 4 (rule as chosen) |
| ticks with AI decisions over 4 ms (detailed run) | 282 (Phase 0) | (running) | 542 | |
| worst AI tick ms (detailed run) | 41.1 (Phase 0) | (running) | 14.6 | |

* **The AI's own cost fell 13 %** (1.51 → 1.32 ms per tick) and its p99 to 3.51 ms, under the 4 ms
  rule. The cuts, in `ai/v2/`:
  * exact, the match unchanged tick for tick (a seeded 4-round match logs the same state for
    every bot on every tick): the possibility field relaxes into preallocated buffers with
    precomputed travel times; the team's view is tested only on points in line of sight; small
    nearby-point queries use 4 m buckets; line-of-sight rows are cached; the roster is built once
    per tick;
  * cadence, behaviour nearly the same: at most three re-thinks per team per tick (the others
    wait one tick, inside the 0.11-0.16 s re-think jitter); the team samples a view every third
    tick and steps the field every fourth, the two teams staggered; walking re-decided every
    0.1 s; spacing every other tick, personal space every fourth; the path search budget 140
    node expansions per tick (was 260).
* **The whole tick meets the Phase 0 budget, but v2 is still about 1.32-1.36 × the legacy tick
  measured in the same session** (budget 1.3 ×). This session's machine was about 7 % faster
  than the last one (legacy 3.14 → 2.91 ms), which accounts for most of the absolute gain.
* **Model builds inside the tick** (shared code, both brains): the first deploy of a gadget kind
  cost 264 ms in one tick, the first gun of a kind 72 ms. All 19 gun, grenade, gadget and charge
  models are now built at match load (about 1 s). Decision-neutral: the seeded match logs the
  same state with and without it.
* This cloud VM varies by about ±10 % between sessions; every subsystem, physics included, moves
  together.

## Files

* `h2h/s201.json` ... `h2h/s204.json`: the four head-to-head matches.
* `v2_normal_seed1.json`, `v2_normal_seed2.json`: v2 against v2, 24 rounds, Normal, `--audit`.
* `v2_normal_seed3_timing.json`: solo run, whole ticks only (the performance reference);
  `legacy_normal_seed3_timing_control.json`: the legacy control run just before it.
* `v2_normal_seed3_detail.json`: solo run with the per-subsystem breakdown, models built at load;
  `v2_normal_seed3_detail_lazy_models.json`: the same before that fix (where the 264 ms tick is).
* `before_perf/`: the same files from before the performance pass.
