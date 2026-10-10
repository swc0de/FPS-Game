# Milestone 8 results

Measured on the v2 brain (`ai/v2/`) against the Phase 0 baseline in
[`docs/baseline/`](../baseline/README.md) and the acceptance criteria in
[OVERHAUL_PLAN.md](../OVERHAUL_PLAN.md) section 7. Same kind of cloud container, CPU model and
software OpenGL as the baseline. The cloud VM's speed drifts between sessions, so every timing
comes with a legacy control run from the same session.

The first part is the final state: the follow-up to the merge of Milestones 7-9 into `main`,
then the stacking fix. The last part is Milestone 8 as it was merged; the numbers from before
its performance pass are in [`before_perf/`](before_perf/).

## Final summary

"In main before" is the code merged into `main`: Milestone 8 with Milestone 9's fitted hit boxes
(v2 against v2 from [docs/results_m9/](../results_m9/README.md); its head-to-head was measured on
the Milestone 8 hit boxes). "Follow-up" is `1ccd8f8` (merged as swc0de/FPS-Game#4), "final" is
`5ecbe8a`, after the stacking fix. Legacy is the Phase 0 baseline.

| criterion | target | legacy | in main before | follow-up | **final** | met |
|---|---|---|---|---|---|---|
| v2 beats legacy (same aim and reaction numbers) | ≥ 70 % of rounds, ≥ 60 rounds, sides swapped | | 42 % (32/77) | 46 % (39/85, CI 36-56) | **43 %** (31 of 72, Wilson 95 % CI 32-55): attack 21/33 (64 %), defence 10/39 (26 %) | **no**: kept as is, your decision |
| deaths while reloading | lower | 20.3 % | 12.7 % | 6.5 % | **3.4 %** | yes |
| unseen deaths (killer not seen in the last 5 s) | lower | 8.4 % | 5.0 % | 5.2 % | **6.2 %** | yes |
| deaths traded within 3 s | higher | 13.6 % | 15.9 % | 17.7 % | **15.0 %** | yes |
| stacking (teammates within 0.8 m for 1 s) | ≤ 0.1 per round | 3.38 | 1.79 | 0.15 | **0.04** (2 in 48 rounds; seeds 5-6 also 0.04) | **yes** |
| stuck bots (5 s without progress) | 0 | 2 + 0 | 0 + 0 | 0 + 0 | **0 + 0** (seeds 5-6 too) | yes |
| no attack plan above 40 % of rounds | ≤ 40 % | 50 % | 29 % (as merged) | 33 % | **33 %** (execute) | yes |
| fairness audit, v2 | 0 violations | | 0 | 0 | **0** in every run (72 head-to-head rounds, 96 v2-vs-v2 rounds) | yes |
| live tick mean | ≤ 1.3 × legacy | | 1.40 × (Milestone 8 as merged) | 1.25 × (a slow session) | **1.29 ×** (4.01 against 3.12 ms, same session, no drawing); the detail pairs 1.28 × and 1.33 × | yes, at the edge |
| live tick p95 | ≤ 1.3 × legacy | | 1.32 × | 1.15 × | **1.21 ×** (6.35 against 5.26 ms) | yes |
| AI decision spikes | ≤ 4 ms at p99 (Milestone 8); re-based after the follow-up to "at most legacy's in the same session" | 2.37 / 2.38 ms (two runs) | 3.05 ms (follow-up code, this session) | 5.57 against legacy's 5.97 ms (the slow session) | **3.02 / 3.24 ms** (two runs) | 4 ms: **yes**; at most legacy's: **no** in this session (decision in the ROADMAP) |
| tests | utility, belief, comms, tactical map, roles, audit | | 253 | 254 | **257**, all green (3 skipped); new or rewritten: no aim or reaction error beyond the profile, a hole blocked by a crate, a way apart from off the mesh, regroup spots apart, the bunker room on the mesh | yes |

Behaviour rows: means of the seed 1 and seed 2 matches (24 rounds each, Normal, v2 against v2
for the whole match, `--audit`), legacy from the baseline runs with the same seeds and
definitions.

## The stacking fix

After the follow-up, stacking was 0.15 incidents per round against the 0.1 target. Each
incident was replayed from its seed (a seeded match repeats exactly) with a probe on both bots.
Two more seeds (5 and 6, never used for tuning) showed it was worse than seeds 1-2 suggested:
0.31 per round. Five causes, all fixed (`ai/v2/brain.py`, `ai/v2/strategy.py`,
`ai/v2/controllers.py`, `maps/prefabs_military.py`):

* **Flashed bots** (3 of the 7 on seeds 1-2): a blinded bot returned before the step apart, so
  two flashed bots backed off side by side or stood blind together. The step apart now applies
  while flashed too.
* **Off the walkable mesh** (3 of 7): bots pressed into a wall's margin (beside a narrow door at
  Barracks, at a wall edge on B Long, on a 0.45 m ledge at CT A) stand on cells that are not
  walkable, where no walkable line starts, so every way apart looked blocked. The ways apart are
  now judged from the nearest walkable cell, or the step goes onto it. The CT A pair had also
  been given the same rotation spot: bots rotating or regrouping together now get spots 1.6 m
  apart.
* **A bent step too weak** (1 of 7): two bots crossing at spawn each slowed to 0.35 for the
  other one ahead, and the step apart bent their way at that strength, which ground friction
  ate. A bent step is now at least a whole step.
* **The bunker room was not on the navmesh** (all 10 on seeds 5-6, 8 of them in one round:
  attackers queued shoulder to shoulder underground). A building's floor slab stops at the
  walls' inner faces; on the ground the ground floor covers the strip under a doorway, 3.2 m
  down nothing did. The bunker room had no floor under its two doors, was cut off from the ramp
  and the tunnel, and was dropped as unreachable (about 1,900 cells); bots of both brains walked
  in anyway, with no mesh to follow. An off-ground building now gets a threshold under every door
  (a shared level fix: legacy bots have the room on their mesh too).
* **A stuck bot** that this showed up on seed 2: a defender put its own shield across the gap
  between a gabion and the yard and stood in the corner with its path through the shield. It
  jumped at the shield, the jump counted as progress, and the escape never started. Progress is
  now measured over the ground, and the escape is judged from the nearest walkable cell.

Tests: a way apart from off the mesh, regroup spots kept apart, the bunker room on the mesh
with the route from the ramp to the tunnel through it (each fails on the old code).

Stacking incidents per round and stuck bots, v2 against v2, 24 rounds each:

| code | seed 1 | seed 2 | seed 5 | seed 6 | stuck |
|---|---|---|---|---|---|
| follow-up (`1ccd8f8`) | 0.08 | 0.21 | 0.38 | 0.25 | 0 |
| + flashed, off-mesh, regroup spots, whole step (`bfa255e`) | 0.00 | 0.12 | 0.33 | 0.08 | 0 |
| + bunker on the navmesh (`1c8523e`) | 0.08 | 0.00 | 0.00 | 0.04 | 1 (seed 2) |
| **final**, + unstick (`5ecbe8a`) | **0.00** | **0.08** | **0.00** | **0.08** | **0** |

### Head-to-head, final code

| seed | v2 won | on attack | on defence |
|---|---|---|---|
| 201 | 13 / 15 | 12 / 12 | 1 / 3 |
| 202 | 1 / 14 | 0 / 2 | 1 / 12 |
| 203 | 11 / 24 | 8 / 12 | 3 / 12 |
| 204 | 6 / 19 | 1 / 7 | 5 / 12 |
| **all** | **31 / 72 (43 %, CI 32-55)** | 21 / 33 (64 %) | 10 / 39 (26 %) |

* **The same as before within the interval** (46 % on the follow-up code, 46 % with the first
  spacing fixes). Single matches swing widely: 201 ended 13-2 for v2, 202 13-1 for legacy (v2
  started on defence and won 1 of 12). Fewer rounds than before because two matches ended early.
* **Attack**: rushes won 4 of 4, contact 5 of 7, executes 6 of 9, defaults 3 of 6, splits 2 of 6.
  **Defence**: retake won 4 of 13, 2-1-2 3 of 13, aggressive 2 of 4, stack 1 of 9; legacy got the
  first kill in 24 of the 39 rounds v2 defended.
* **Fights**: v2 kills 229, deaths 278; deaths while reloading 15 against legacy's 41, to an
  enemy never seen 10 against 29; traded 44 of v2's deaths (15.8 %) against 34 of legacy's
  (14.8 %).
* **Utility**: 111 flashes (37 % blinded an enemy), 7 smokes, 16 frags. **Fairness**: legacy read
  hidden state 34,340 times; v2 0.

### Behaviour, final code: v2 against v2

| metric | legacy | in main before | follow-up | final, seed 1 | final, seed 2 | final, mean |
|---|---|---|---|---|---|---|
| attack round wins % | 62 | 64.5 | 79 | 71 | 71 | **71** |
| deaths while reloading % | 20.3 | 12.7 | 6.5 | 4.3 | 2.4 | **3.4** |
| unseen deaths % (killer not seen in the last 5 s) | 8.4 | 5.0 | 5.2 | 8.1 | 4.3 | **6.2** |
| killer never seen that round % | 7.0 | 3.5 | 3.4 | 5.0 | 2.4 | **3.7** |
| deaths traded within 3 s % | 13.6 | 15.9 | 17.7 | 13.6 | 16.5 | **15.0** |
| traded, of tradeable deaths % | 22.3 | 26.7 | 28.7 | 26.2 | 29.9 | **28.0** |
| killer's sight of victim before the kill s | 3.30 | 2.06 | 1.95 | 2.11 | 2.14 | **2.12** |
| live time seen by an enemy % | 28.3 | 20.5 | 18.5 | 18.1 | 15.4 | **16.8** |
| stacking incidents per round | 3.38 | 1.79 | 0.15 | 0.00 | 0.08 | **0.04** |
| clumps (3+ within 2.5 m for 2 s) | 22.5 | 34.5 | 35 | 44 | 41 | **42.5** |
| stuck bots | 2 + 0 | 0 + 0 | 0 + 0 | 0 | 0 | **0 + 0** |
| path-follower micro-stucks | 82 | 407 | 216 | 276 | 199 | **238** |
| bullet hits / shots % | 39.5 | 44 | 45 | 44 | 49 | **47** |
| flashes that blinded an enemy % | 0 | 40 | 37 | 33 | 37 | **35** |
| largest attack plan share % | 50 / 42 (execute) | 29 (as merged) | 33 | 33 (execute) | 29 (execute) | **33** |

Seeds 5 and 6 on the final code: stacking 0.00 and 0.08, stuck 0, deaths while reloading 4.5 and
6.1 %, unseen deaths 9.1 and 9.4 %, trades 10.4 and 12.7 %, largest plan 33 %, audit 0. Trades on
single matches range from about 10 to 23 % on the same code, as found before.

### Performance, final code

Seed 3, 8 rounds, Normal, no drawing (`BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64`, the same 64 Hz
game logic), alone on the machine, one run after the other in one session. This session (after a
container restart) runs about twice as fast as the one that measured the follow-up.

| | legacy control | v2 | ratio | budget |
|---|---|---|---|---|
| live tick mean ms | 3.12 | **4.01** | **1.29 ×** | ≤ 1.3 × |
| live tick p95 ms | 5.26 | **6.35** | **1.21 ×** | ≤ 1.3 × |
| live tick p99 ms | 7.41 | 8.41 | 1.13 × | |
| with the per-subsystem timers, two pairs: mean ms | 3.32, 3.39 | 4.25, 4.52 | 1.28 ×, 1.33 × | |

Same session, with the per-subsystem timers:

| code | brain | AI decisions mean / p95 / p99 ms | ticks with AI over 4 ms | worst AI tick ms |
|---|---|---|---|---|
| final (`5ecbe8a`) | legacy | 0.57 / 1.28 / **2.37**; repeat 0.59 / 1.31 / **2.38** | 147; 157 | 28.3 (a path search); 110.5 |
| final | v2 | 1.12 / 2.25 / **3.02**; repeat 1.21 / 2.41 / **3.24** | 74; 99 | 8.2; 10.5 |
| follow-up (`1ccd8f8`) | v2 | 1.20 / 2.31 / 3.05 | 66 | 8.1 |
| Milestone 8 as merged (`5877274`, mannequins) | v2 | 1.22 / 2.35 / 3.18 | 97 | 7.0 |

* **The tick budget is met, at its edge**: 1.29 × in the plain pair, 1.28 × and 1.33 × in two
  detail pairs (single runs vary by a few per cent). The slow session measured 1.25 ×.
* **The stacking fix costs nothing measurable**: v2's AI is the same as on the follow-up code
  and on the Milestone 8 code in this session (p99 3.0-3.2 ms).
* **The AI spike rule** depends on which version you keep:
  * "AI p99 ≤ 4 ms" (your Milestone 8 choice): **met**, 3.02-3.24 ms;
  * "at most legacy's in the same session" (your re-based choice after the follow-up): **not
    met** here, legacy 2.37-2.38 ms. It was met in the slow session (5.57 against 5.97 ms), where
    a bot's shot cost 11-13 ms at p99 and dominated legacy's spikes; in this session a shot costs
    5 ms and legacy's ordinary decisions are cheaper than v2's. v2 has fewer ticks over 4 ms than
    legacy here (74-99 against 147-157) and a much lower worst tick (8-10 against 28-110 ms).
  ROADMAP, "Decision to confirm".
* The longest ticks (55-92 ms) are garbage-collection pauses (51-86 ms), except one 116 ms legacy
  tick with a 108 ms perception call.

### Files

* `final/h2h/s201.json` ... `s204.json`: the four head-to-head matches on the final code.
* `final/v2_normal_seed{1,2,5,6}.json`: v2 against v2, 24 rounds, Normal, `--audit`.
* `final/timing/`: the timing pair, two detail pairs, and the same-session controls
  (`control_followup_v2_*`, `control_m8_v2_*`).
* `follow_up/`: the same for the follow-up code (`1ccd8f8`), with its same-session controls.

## The follow-up

What changed, in order, with the head-to-head after each step (four full matches, seeds 201-204,
sides swapped, as below):

| step | commit | v2 won | on attack | on defence |
|---|---|---|---|---|
| spread out: separation while moving and fighting, a goal a teammate stands on counts as reached | `67825fe` | 26 / 75 (35 %, CI 25-46) | 17 / 39 | 9 / 36 |
| a whole step apart (a 0.6 step was eaten by friction); defenders keep the angle they hold | `3d1dd4c` | 34 / 83 (41 %, CI 31-52) | 22 / 35 | 12 / 48 |
| stuck fixes (holes blocked by an obstacle, razor wire); equal mechanics (your choice) | `b443e11`, `b633099` | 33 / 85 (39 %, CI 29-49) | 27 / 39 | 6 / 46 |
| lighter mistakes (your choice: Normal at the old Expert rates) | `0f500d5` | 43 / 89 (48 %, CI 38-59) | 29 / 44 | 14 / 45 |
| retake: in together, defuse only when safe or forced | `4d1a5af` | 41 / 90 (46 %, CI 36-56) | 29 / 43 | 12 / 47 |
| **follow-up**: holds that watch one entry, no gadget placing once the round is live | `1ccd8f8` | **39 / 85 (46 %, CI 36-56)** | 29 / 42 | 10 / 43 |

Two diagnostics on `3d1dd4c` (not committed) decided the human-error questions you answered:

* **legacy with its information leaks closed** (exact attacker and killer positions, team
  reports): 34 / 85 (40 %, CI 30-51), the same as against the leaking legacy. The leaks are not
  what v2 loses to.
* **v2 without any human error** (no mistakes, no stress, no second-enemy reaction penalty, no
  flick error): 49 / 89 (55 %, CI 45-65). The errors cost about 14 points. You chose equal
  mechanics (the reaction penalty and the flick error gone, so aim and reaction are exactly the
  Milestone 5 profile) and lighter mistakes.

Every interval overlaps the next: single steps are within the noise of four matches, the whole
follow-up is +4 points over the merged 42 % (+11 over the first step).

### The changes

* **Stacking.** 60 incidents traced: two bots sent to one goal point (staging, holds, posts),
  bots running one lane side by side, and bots fighting from one spot where the counter-strafe
  cancelled the one-tick step apart. Now the step apart from teammates closer than 1.1 m bends
  the way of a moving or fighting bot, a whole step at a time, sideways when straight apart is
  blocked; a goal a teammate already stands on counts as reached 1.8 m short
  (not the charge or a pickup). `ai/v2/brain.py`, `ai/v2/controllers.py`.
* **Stuck bots.** Three stuck reports replayed from their seeds (a seeded match repeats exactly)
  and dumped at the moment:
  * a breach with a crate against its far side: the link through the hole found floor round the
    crate, so routes went into it. A Milestone 5 bot and a v2 planter both pushed there for the
    rest of the round. A hole now links only with floor right at it on both sides and nothing in
    the way at knee and chest height (`ai/navlinks.py`, shared by both brains: a bug fix that
    changes legacy's routes through such holes too; test);
  * two v2 defenders in their own razor wire: wire slows to 0.32 × and, walking, friction ate the
    acceleration (0.05 m/s). A v2 bot in wire runs.
* **Equal mechanics** (your choice). The humaniser no longer delays the reaction to a second enemy
  while busy with one, and no longer picks the side of the first-shot error from the flick.
  Test: v2 adds no aim or reaction error beyond the profile.
* **Lighter mistakes** (your choice). Normal has the old Expert mistake rates (over-peek 3 %,
  reload in the open 2 %, late trade 5 %, ...), Hard and Expert half and a third of that, no
  missed calls from Normal up; Easy unchanged.
* **Defence**, from the opening deaths of v2 defenders in the head-to-head (67 traced):
  * defenders hold the angle instead of hiding and re-peeking at range, and fall back from two or
    more visible enemies (outnumbered) only when they are close;
  * retake: gather until three are ready (or the clock presses, or 12 s), go in together, defuse
    only with no enemy alive, when the clock forces it, or after 4 s without contact with two
    teammates near; otherwise cover the defuser;
  * hold spots scored by their best entry plus 0.3 × the second, minus 0.5 for each more, and
    exposure to the attackers' side counts twice as much: the old sum over every entry put the
    top spots at A where they saw, and were seen from, all three entries (34 of the 67 opening
    deaths were on hold spots, 29 at A). Tactical map cache version 2 (rebuilt once);
  * 3 s into the live phase a defender drops gadget placements still to do and goes to its hold
    (13 of the 67 were still placing wire or a sensor in an entry 20 s in).
* **Metrics**: the v2 defence setup per round (`setups_detail`).

### Head-to-head, follow-up code (`1ccd8f8`)

```
# seeds 201 and 203 with --ai team0=v2,team1=legacy; 202 and 204 with --ai team0=legacy,team1=v2
BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64 BOT_DEMO_ROUNDS=30 BOT_DEMO_JSON=s201.json \
  xvfb-run -a python main.py --demo bots --seed 201 --res 320x180 --preset low --windowed \
  --ai team0=v2,team1=legacy --audit
```

| seed | v2 won | on attack | on defence |
|---|---|---|---|
| 201 | 13 / 20 | 11 / 12 | 2 / 8 |
| 202 | 6 / 19 | 4 / 7 | 2 / 12 |
| 203 | 10 / 23 | 7 / 12 | 3 / 11 |
| 204 | 10 / 23 | 7 / 11 | 3 / 12 |
| **all** | **39 / 85 (46 %, CI 36-56)** | 29 / 42 (69 %) | 10 / 43 (23 %) |

* **Attack is at 69 %**: executes won 11 of 12, contact 6 of 8, rushes 3 of 4, fakes 3 of 5,
  defaults 3 of 7, splits 3 of 6.
* **Defence is still the gap** (23 %): 2-1-2 won 3 of 14, retake 2 of 14, stack 2 of 9,
  aggressive 3 of 6. Legacy got the first kill in 32 of the 43 rounds v2 defended and won 26 of
  those; all 33 of legacy's attack wins were eliminations. v2's 10 defence wins: 7 defuses, 2
  eliminations, 1 on time.
* **Fights**: v2 kills 273, deaths 319. It dies far less while reloading (20 against legacy's
  48), less often to an enemy it never saw (19 / 25), and is traded less (35 / 47).
* **Utility**: v2 threw 153 flashes (36 % blinded an enemy), 12 smokes and 36 frags; legacy
  threw one frag.
* **Fairness**: legacy read hidden state 36,856 times in the four matches; v2 0.

### Behaviour, follow-up code: v2 against v2

| metric | legacy | in main before | seed 1 | seed 2 | mean |
|---|---|---|---|---|---|
| attack round wins % | 62 | 64.5 | 83 | 75 | **79** |
| deaths while reloading % | 20.3 | 12.7 | 4.6 | 8.5 | **6.5** |
| unseen deaths % (killer not seen in the last 5 s) | 8.4 | 5.0 | 4.0 | 6.3 | **5.2** |
| killer never seen that round % | 7.0 | 3.5 | 2.9 | 4.0 | **3.4** |
| deaths traded within 3 s % | 13.6 | 15.9 | 20.6 | 14.8 | **17.7** |
| traded, of tradeable deaths % | 22.3 | 26.7 | 35.2 | 22.2 | **28.7** |
| killer's sight of victim before the kill s | 3.30 | 2.06 | 1.93 | 1.97 | **1.95** |
| live time seen by an enemy % | 28.3 | 20.5 | 20.4 | 16.7 | **18.5** |
| stacking incidents per round | 3.38 | 1.79 | 0.08 | 0.21 | **0.15** |
| clumps (3+ within 2.5 m for 2 s) | 22.5 | 34.5 | 33 | 37 | **35** |
| stuck bots | 2 + 0 | 0 + 0 | 0 | 0 | **0 + 0** |
| path-follower micro-stucks | 82 | 407 | 179 | 254 | **216** |
| bullet hits / shots % | 39.5 | 44 | 43 | 46 | **45** |
| headshot kills % | 35 | 31.5 | 31 | 24 | **28** |
| flashes that blinded an enemy % | 0 | 40 | 35 | 40 | **37** |
| frags that hurt an enemy % | 0 | 24 | 20 | 38 | **29** |
| largest attack plan share % | 50 / 42 (execute) | 29 (as merged) | 33 (execute) | 33 (execute) | **33** |

`python tools/bot_metrics.py --compare docs/baseline/legacy_normal_seed1.json
docs/results/follow_up/v2_normal_seed1.json` prints the rows for one seed.

* **Stacking: 0.15 per round**, 7 incidents in 48 rounds (target 0.1, i.e. at most 4.8), from
  1.79 in `main` and 3.38 for legacy. Seed 1 is under the target (0.08), seed 2 over (0.21). The
  same two matches measured 0 incidents at the equal-mechanics step (`b633099`), with the same
  spacing code: the changes since (lighter mistakes, defence) play the matches differently, and
  a count this small moves between 0 and 7 with them. Four of the 7 were two bots on their team
  task (holds, staging), two alert, one in a fight; all at different places.
* **Stuck: 0 + 0**, path-follower micro-stucks halved (407 → 216 a match; they include the
  intentional waits at corners and doors).
* **Fights**: deaths while reloading halved again (12.7 → 6.5 %, legacy 20.3 %), more deaths
  traded (17.7 %, legacy 13.6 %), unseen deaths as before (5.2 %, legacy 8.4 %), seen less (18.5 %
  of live time, legacy 28.3 %).
* **Attack wins 79 % of v2-against-v2 rounds** (64.5 % in `main`): v2's attack is stronger than
  its defence, as in the head-to-head.
* **Audit**: 0 violations in both matches.

### Performance, follow-up code

Seed 3, 8 rounds, Normal, the same method for every row: `--full-match`, no drawing
(`BOT_DEMO_NORENDER=1 BOT_DEMO_TICKS=64`, the same 64 Hz game logic), alone on the machine, one
run after the other in one session. Rendering on, as for the Milestone 8 numbers, took about 4
minutes a round with the soldiers on this software-GL VM.

| | legacy control | v2 | ratio | budget |
|---|---|---|---|---|
| live tick mean ms | 6.59 | **8.21** | **1.25 ×** | ≤ 1.3 × |
| live tick p95 ms | 11.41 | **13.17** | **1.15 ×** | ≤ 1.3 × |
| live tick p99 ms | 15.31 | 16.27 | 1.06 × | |
| with the per-subsystem timers: mean / p95 ms | 7.03 / 12.32 | 8.42 / 13.27 | 1.20 × / 1.08 × | |

| AI per tick (detail runs) | legacy | v2 |
|---|---|---|
| AI decisions mean ms | 1.20 | 2.13 |
| **AI decisions p99 ms** (rule: v2 at most legacy's) | **5.97** | **5.57** |
| ticks with AI decisions over 4 ms | 950 | 2,156 |
| worst AI tick ms | 59.7 (a path search) | 32.6 (one re-think) |
| a bot's shot (`fire`: the bullet, hits, deaths, ragdolls, effects), p99 / max ms | 11.3 / 23.8 | 13.2 / 24.2 |
| shared: animation / physics step / movement, mean ms | 1.87 / 2.12 / 1.15 | 2.22 / 2.17 / 1.20 |

Controls, the same session and method, with the per-subsystem timers (so the tick is a little
higher than in the plain runs above):

| code | brain | tick mean / p95 ms | ratio to legacy (same code) | AI decisions mean / p99 ms | ticks with AI over 4 ms |
|---|---|---|---|---|---|
| Milestone 8 as merged (mannequins, `5877274`) | legacy | 5.34 / 10.14 | | 0.94 / 4.18 | 401 |
| | v2 | 7.80 / 14.10 | 1.46 × / 1.39 × | 2.08 / **5.63** | 2,092 |
| `main` before the follow-up (`ecd07bd`) | v2 | 9.24 / 14.59 | 1.31 × / 1.18 × against the follow-up's legacy | 2.37 / **6.21** | 3,411 |
| follow-up (`1ccd8f8`) | legacy | 7.03 / 12.32 | | 1.20 / 5.97 | 950 |
| | v2 | 8.42 / 13.27 | 1.20 × / 1.08 × | 2.13 / **5.57** | 2,156 |

There is no legacy run on `main`: its legacy differs from the follow-up's only by the navmesh fix,
so the follow-up's legacy run stands in.

* **The tick budget is met**: v2's tick is 1.25 × legacy's at the mean and 1.15 × at p95.
  Milestone 8 as merged was 1.40 × / 1.32 × in its own measurement and 1.46 × / 1.39 × in this
  session. Part of the gain is a larger denominator (Milestone 9's soldiers made every tick
  dearer for both brains), part is v2 itself: its extra cost over legacy is 1.4-1.6 ms a tick,
  2.5 ms for the Milestone 8 code in this session.
* **The AI spike rule as you re-based it is met**: v2's AI p99 at most the same session's legacy
  (5.57 against 5.97 ms). As the absolute 4 ms it was before, it is met by neither brain in this
  measurement. This session and method measure higher than the ones before: the same
  Milestone 8 code that met the rule at 3.90 ms (its own session, rendering on) measures 5.63 ms
  here, and its legacy 4.18 ms (3.11 at Phase 0). Against the same-session controls the follow-up v2
  is at the Milestone 8 level (5.57 against 5.63), below `main` before the follow-up (6.21) and
  below legacy (5.97): the follow-up added no AI cost (mean 2.37 → 2.13 ms). Milestone 9 made a
  bot's shot dearer for both brains (`fire` p99 3.9-4.1 → 11-13 ms; it covers the hit tests and
  what a kill sets off, both changed by Milestone 9), and that counts as AI time because bots
  shoot from their brain update. Scaled by the Milestone 8 control, the follow-up v2 would be about
  3.9 ms in the session where the 4 ms rule was set.
* The longest ticks of the detail runs (135 ms legacy, 142 ms v2) are garbage-collection pauses
  (130 and 131 ms).

### Files

* `follow_up/h2h/s201.json` ... `s204.json`: the four head-to-head matches on the follow-up code.
* `follow_up/v2_normal_seed1.json`, `follow_up/v2_normal_seed2.json`: v2 against v2, 24 rounds,
  Normal, `--audit`.
* `follow_up/timing/`: the timing pair (`*_timing*.json`, whole ticks only), the detail pair
  (`*_detail*.json`, per subsystem) and the three same-session controls (`control_*.json`:
  `main` before the follow-up, Milestone 8 v2 and legacy).

## Milestone 8 as merged

Measured on the Milestone 8 code after its performance pass, before Milestone 9's hit boxes.

### Summary

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
| live tick mean | ≤ 3.97 ms (1.3 × 3.06) | **4.42 ms** on the Milestone 8 code, legacy control in the same session 3.15 ms (1.40 ×); an earlier session: 3.95 ms against 2.91 (1.36 ×) | **no** (met in one session out of two; over 1.3 × the same-session legacy in both) |
| live tick p95 | ≤ 6.64 ms (1.3 × 5.11) | **7.31 ms**, legacy control 5.55 ms (1.32 ×); earlier session 6.52 against 4.95 (1.32 ×) | **no** (as above) |
| AI decision spikes (rule as chosen: AI p99 ≤ 4 ms) | p99 ≤ 4 ms | **3.90 ms** on the Milestone 8 code (3.51 ms in the earlier session; 4.21 before the pass; legacy 3.11) | yes, by a small margin |
| new tests | utility, belief, comms, tactical map, roles, audit | 4 new modules (`test_ai_infra`, `test_knowledge`, `test_tactical_map`, `test_v2_brain`); 210 tests in total, all green | yes |

Behaviour rows: averages of the seed 1 and seed 2 matches (24 rounds each, Normal, the whole
match v2 against v2), legacy numbers from the baseline runs with the same seeds and definitions
(`python tools/bot_metrics.py --compare docs/baseline/legacy_normal_seed1.json
docs/results/v2_normal_seed1.json`).

### Head-to-head: v2 against legacy

Four full matches on the Milestone 8 code, Normal, both brains with the same perception, aim
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
* **Two behaviour rows are worse than before the pass**: trades within 3 s 16.1 % → 11.9 % and
  unseen deaths 8.0 % → 9.2 %, both on the wrong side of legacy. The pass halved the path search
  budget (260 → 140 node expansions per team per tick), the suspected cause, so the same two
  matches were re-run at 260: trades 15.2 %, unseen deaths 8.4 % (JSON in
  [`path260/`](path260/)). But the Milestone 9 branch, still at 140 and differing only in the
  hit boxes, gave 15.0 % and 8.1 % on the same seeds. Single 24-round matches vary by about as
  much as the gap, so the budget is not shown to cause it; 260 also breaks the spike rule
  (below). ROADMAP "Milestone 8 decisions to confirm", decision 2.
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

### Behaviour: v2 against v2

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

### Performance

Solo runs of seed 3, 8 rounds, Normal, rendering on (the baseline's method), each with a legacy
control run right before it in the same session: the VM's speed drifts by up to about 10 %
between sessions, so the ratio is the fair number.

| | legacy control | v2 | ratio | budget |
|---|---|---|---|---|
| live tick mean ms, Milestone 8 code | 3.15 | **4.42** | 1.40 × | ≤ 3.97 (1.3 × Phase 0) |
| live tick p95 ms, Milestone 8 code | 5.55 | **7.31** | 1.32 × | ≤ 6.64 |
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
| path search budget 260 instead of 140 (decision 2) | | AI mean 1.46 ms, **p99 4.15 ms** (over the rule), 488 ticks over 4 ms; v2 against v2 seeds 1-2: trades 15.2 %, unseen deaths 8.4 %, stuck 2 + 0 | |

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

### Files

* `h2h/s201.json` ... `h2h/s204.json`: the four head-to-head matches.
* `v2_normal_seed1.json`, `v2_normal_seed2.json`: v2 against v2, 24 rounds, Normal, `--audit`.
* `v2_normal_seed3_timing.json`, `legacy_normal_seed3_timing_control.json`: the final timing pair;
  `*_earlier.json`: the pair from the earlier session.
* `v2_normal_seed3_detail.json`: solo run with the per-subsystem breakdown;
  `v2_normal_seed3_detail_lazy_models.json`: before models were built at load (the 264 ms tick).
* `path260/`: v2 against v2, seeds 1 and 2, with the path search budget at 260 (decision 2).
* `before_perf/`: the same files from before the performance pass.
