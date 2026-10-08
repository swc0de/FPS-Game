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
| v2 beats legacy (same aim and reaction numbers) | ≥ 70 % of rounds, ≥ 60 rounds, sides swapped | **42 %** (32 of 77, Wilson 95 % CI 31-53): attack 21/36 (58 %), defence 11/41 (27 %). Before the performance pass: 43 % (38/88, CI 33-54) | **no** |
| deaths while reloading | lower | 20.3 % → **8.4 %** | yes |
| unseen deaths (killer not seen in the last 5 s) | lower | 8.4 % → **9.2 %** (seed 1: 13.9 → 6.7; seed 2: 3.0 → 11.6). Before the pass: 8.0 % | **no** |
| deaths traded within 3 s | higher | 13.6 % → **11.9 %** (of tradeable deaths 22.3 → 22.8 %). Before the pass: 16.1 % | **no** |
| stacking (teammates within 0.8 m for 1 s) | ≤ 0.1 per round | 3.38 → **1.38** per round (before the pass 1.08) | **no** |
| stuck bots (5 s without progress) | 0 | 2 + 0 → **0 + 1** | **no** |
| no attack plan above 40 % of rounds | ≤ 40 % | largest share **29 %** | yes |
| fairness audit, v2 | 0 violations | **0** in every run (77 head-to-head rounds, 48 v2-vs-v2 rounds) | yes |
| live tick mean | ≤ 3.97 ms (1.3 × 3.06) | **4.42 ms** on the final code, legacy control in the same session 3.15 ms (1.40 ×); an earlier session: 3.95 ms against 2.91 (1.36 ×) | **no** (met in one session out of two; over 1.3 × the same-session legacy in both) |
| live tick p95 | ≤ 6.64 ms (1.3 × 5.11) | **7.31 ms**, legacy control 5.55 ms (1.32 ×); earlier session 6.52 against 4.95 (1.32 ×) | **no** (as above) |
| AI decision spikes (rule as chosen: AI p99 ≤ 4 ms) | p99 ≤ 4 ms | **3.90 ms** on the final code (3.51 ms in the earlier session; 4.21 before the pass; legacy 3.11) | yes, by a small margin |
| new tests | utility, belief, comms, tactical map, roles, audit | 4 new modules (`test_ai_infra`, `test_knowledge`, `test_tactical_map`, `test_v2_brain`); 210 tests in total, all green | yes |

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
| 201 | 13 / 19 | 10 / 12 | 3 / 7 |
| 202 | 6 / 19 | 2 / 7 | 4 / 12 |
| 203 | 9 / 22 | 7 / 12 | 2 / 10 |
| 204 | 4 / 17 | 2 / 5 | 2 / 12 |
| **all** | **32 / 77 (42 %, CI 31-53)** | 21 / 36 | 11 / 41 |
| before the performance pass | 38 / 88 (43 %, CI 33-54) | 25 / 48 | 13 / 40 |

What it shows:

* **The performance pass at first cost real behaviour**, then the cause was found and fixed. The
  first measurement after the pass gave 37 % (30/82) and v2 against v2 stacking of 2.98 per round
  (1.08 before): personal space was checked every fourth tick, but the step apart it sets lasts
  one tick, so stacked bots moved apart a quarter as much. The step is now held between checks.
  On the fixed code the head-to-head is back at 42 %, the same as before the pass within its
  interval, and stacking at 1.38 per round.
* **Two behaviour rows got worse than before the pass** (single 24-round matches are noisy, so
  this may be partly chance): trades within 3 s 16.1 % → 11.9 % and unseen deaths 8.0 % → 9.2 %,
  now both on the wrong side of legacy. The pass halved the path search budget (260 → 140 node
  expansions per team per tick), so a route to a teammate's killer arrives later; that is the
  most likely cause. Going back to 260 is a decision in ROADMAP "Milestone 8 decisions to
  confirm", with its measured cost below.
* **Fights**: v2 kills 240, deaths 293. It dies less while reloading (22 against legacy's 41) and
  less often to an enemy it never saw (19 / 32), and trades a little less (35 / 41).
* **Attack is ahead** (58 %): contact won 4 of 5, rushes 3 of 3, defaults 5 of 8, executes and
  splits 4 of 8 each, fakes 1 of 4.
* **Defence is the gap** (27 %). The 2-1-2 setup won 4 of 15, retake 3 of 13, stack 2 of 9,
  aggressive 2 of 4. Of legacy's attack wins, 28 of 30 were eliminations.
* **Utility**: v2 threw 113 flashes (44 % blinded an enemy), 9 smokes and 21 frags. The legacy
  brain throws almost none.
* **Fairness**: legacy reads hidden state 41,323 times in the four matches (the leaks listed in
  the baseline); v2 0.

## Behaviour: v2 against v2

| metric | legacy seed 1 | v2 seed 1 | legacy seed 2 | v2 seed 2 |
|---|---|---|---|---|
| attack round wins % | 62 | 54 | 62 | 67 |
| deaths while reloading % | 22.0 | 9.7 | 18.6 | 7.0 |
| unseen deaths % | 13.9 | 6.7 | 3.0 | 11.6 |
| killer never seen that round % | 12.1 | 3.0 | 1.8 | 6.4 |
| deaths traded within 3 s % | 12.1 | 13.3 | 15.0 | 10.5 |
| traded, of tradeable deaths % | 19.3 | 23.8 | 25.3 | 21.7 |
| killer's sight of victim before the kill s | 3.15 | 2.15 | 3.45 | 1.79 |
| live time seen by an enemy % | 29.7 | 22.8 | 27.0 | 20.5 |
| stacking incidents per round | 3.58 | 1.25 | 3.17 | 1.50 |
| clumps (3+ within 2.5 m for 2 s) | 22 | 29 | 23 | 33 |
| stuck bots | 2 | 0 | 0 | 1 |
| path-follower micro-stucks | 83 | 291 | 80 | 346 |
| bullet hits / shots % | 38 | 50 | 41 | 50 |
| flashes that blinded an enemy % | 0 | 34 | 0 | 40 |
| smokes that blocked an enemy sighting % | 0 | 45 | 0 | 62 |
| frags that hurt an enemy % | 0 | 26 | 0 | 23 |
| largest attack plan share % | 50 (execute) | 25 | 42 (execute) | 29 |

These matches ran without drawing (`BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64`, the same game logic);
their timing rows are not used.

## Performance

Solo runs of seed 3, 8 rounds, Normal, rendering on (the baseline's method), each with a legacy
control run right before it in the same session: the VM's speed drifts by up to about 10 %
between sessions, so the ratio is the fair number.

| | legacy control | v2 | ratio | budget |
|---|---|---|---|---|
| live tick mean ms, final code | 3.15 | **4.42** | 1.40 × | ≤ 3.97 (1.3 × Phase 0) |
| live tick p95 ms, final code | 5.55 | **7.31** | 1.32 × | ≤ 6.64 |
| live tick mean ms, earlier session (before the personal-space fix) | 2.91 | 3.95 | 1.36 × | |
| live tick p95 ms, earlier session | 4.95 | 6.52 | 1.32 × | |
| before the performance pass | 3.14 / 5.71 | 4.21 / 7.29 | 1.34 × / 1.28 × | |

Detailed runs (per-subsystem timers; their overhead raises the totals a little):

| | legacy (Phase 0) | v2 final | v2 before the pass |
|---|---|---|---|
| AI decisions, mean ms per tick | 0.61 | **1.42** | 1.51 |
| AI decisions, p99 ms | 3.11 | **3.90** | 4.21 |
| ticks with AI decisions over 4 ms | 282 | 412 | 542 |
| worst AI tick ms | 41.1 | 62.9 (a 60 ms garbage-collection pause inside a re-think); 15.9 without it | 14.6 |
| path search budget 260 instead of 140 (decision 2) | | AI mean 1.46 ms, **p99 4.15 ms** (over the rule), 488 ticks over 4 ms | |

* **The spike rule you chose (AI p99 ≤ 4 ms) is met**, by a small margin (3.51 and 3.90 ms in two
  sessions). The AI's own cost fell 6-13 %.
* **The whole tick is still about 1.32-1.40 × legacy's** (budget 1.3 ×); against the Phase 0
  numbers it was within budget in one session (3.95 ms) and over in the other (4.42 ms).
* **The cuts**, in `ai/v2/`:
  * exact, the match unchanged tick for tick (a seeded 4-round match logs the same state for
    every bot on every tick): the possibility field relaxes into preallocated buffers with
    precomputed travel times; the team's view is tested only on points in line of sight; small
    nearby-point queries use 4 m buckets; line-of-sight rows are cached; the roster is built once
    per tick;
  * cadence, behaviour nearly the same: at most three re-thinks per team per tick (the others
    wait one tick, inside the 0.11-0.16 s re-think jitter); the team samples a view every third
    tick and steps the field every fourth, the two teams staggered; walking re-decided every
    0.1 s; spacing every other tick, personal space every fourth (the step apart held in
    between); the path search budget 140 node expansions per tick (was 260).
* **Model builds inside the tick** (shared code, both brains): the first deploy of a gadget kind
  cost 264 ms in one tick, the first gun of a kind 72 ms. All 19 gun, grenade, gadget and charge
  models are now built at match load (about 1 s). Decision-neutral: the seeded match logs the
  same state with and without it.

## Files

* `h2h/s201.json` ... `h2h/s204.json`: the four head-to-head matches.
* `v2_normal_seed1.json`, `v2_normal_seed2.json`: v2 against v2, 24 rounds, Normal, `--audit`.
* `v2_normal_seed3_timing.json`, `legacy_normal_seed3_timing_control.json`: the final timing pair;
  `*_earlier.json`: the pair from the earlier session.
* `v2_normal_seed3_detail.json`: solo run with the per-subsystem breakdown;
  `v2_normal_seed3_detail_lazy_models.json`: before models were built at load (the 264 ms tick).
* `before_perf/`: the same files from before the performance pass.
